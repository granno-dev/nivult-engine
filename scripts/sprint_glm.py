"""Sprint una-tantum: arricchisce l'arretrato con GLM-5.3-Flash (promo).
Una chiamata per offerta riempie seniority, employment_type, remote,
country E la famiglia — tutto insieme. reasoning_effort=low (il modello
ragiona sempre, non si spegne; low tiene l'output a ~44 token).

Protezioni: tetto di spesa HARD (si ferma a $35), marcatore sprint_at
(non ripaga mai una riga), ripartibile, scritture a lotti ordinati con
retry sul deadlock. Non sovrascrive valori esistenti (coalesce).
Ordina per posted_at DESC: le offerte recenti (quelle utili ai digest)
per prime."""
import os, json, re, time, psycopg
import httpx
from concurrent.futures import ThreadPoolExecutor

DSN = os.environ["ATS_DATABASE_URL"]
GLM = os.environ["GLM_API_KEY"]
TETTO = float(os.environ.get("TETTO_SPESA", "35.0"))   # dollari, cap duro
# chiamate in parallelo: la latenza di GLM-5.3-Flash e' ~8 s a chiamata
# (misurato 06/09), quindi il ritmo e' PAR/8 offerte al secondo. Si alza
# finche' le «fallite (429?)» nel log restano sotto il 2% di una pagina.
PAR = int(os.environ.get("SPRINT_PAR", "40"))
PREZZO_IN, PREZZO_OUT = 0.075/1e6, 0.25/1e6            # promo
PREZZO_CACHE = 0.015/1e6   # il prompt di sistema (la rubrica) e' in cache: misurato 512/572 token

# autocommit e chiusura: senza, questa connessione restava «idle in
# transaction» per tutta la durata dello sprint (giorni) e VACUUM non
# poteva pulire niente di quello che lo sprint stesso riscriveva
with psycopg.connect(DSN, autocommit=True) as c0:
    FAM = [r[0] for r in c0.execute("SELECT DISTINCT family FROM job_classifications ORDER BY 1").fetchall()]
VAL_SEN = {"intern","junior","mid","senior","lead","head"}
VAL_ET = {"full_time","part_time","contract","temporary","internship","apprenticeship"}
VAL_REM = {"remote","hybrid","onsite"}
# La RUBRICA (docs/rubrica-classificazione.md) nel prompt: le coppie ambigue
# decise una volta, e niente indovinelli senza testo. Nata dall'audit del
# 2026-09-06: gli errori di GLM erano tutti titoli ambigui senza descrizione.
REGOLE = (
 "RULES for family (the person's JOB, not the company's sector): "
 "skilled manual work (carpenter, electrician, plumber, painter, welder, mechanic, installer, cleaner) -> Trades; "
 "building-site roles (site manager, construction PM, mason) -> Construction; "
 "in-store selling (cashier, clerk, sales associate, store staff) -> Retail; "
 "selling to clients/companies (account manager, negotiator, business developer, real-estate agent) -> Sales; "
 "tax/accounting/audit/credit/banking/insurance/treasury -> Finance & Accounting (never Consulting); "
 "process/strategy/IT advisory for external clients -> Consulting; "
 "kitchen, bar, bakery, restaurant, barback -> Food & Beverage; reception, events, hotel front-office, host -> Hospitality; "
 "drivers, trucking, delivery -> Transportation; warehouse/supply chain -> Logistics; "
 "software development/QA/product owner -> Software; IT infrastructure/support/networks/AV-IT installs -> Technology; "
 "call center/help desk -> Customer Service & Support; "
 "VP/director/head with P&L or people leadership as the core -> Management & Leadership, but 'Senior Manager Sales' -> Sales; "
 "tutor/educator/mentoring/activity leader -> Education; sports/recreation instructor -> Sports & Recreation; "
 "veterinary -> Healthcare; secretary/office facility supervisor -> Administrative. "
 # aggiunte dal set d'esame a mano (280 casi, 2026-09-06)
 "Restaurant/bar/kitchen service EVEN INSIDE A HOTEL -> Food & Beverage; hotel front office/housekeeping/rooms -> Hospitality; "
 "office or clinic receptionist -> Administrative; patient access/registration/medical billing -> Administrative (not Healthcare); "
 "welder/pipefitter/electrician even on a construction site -> Trades; laborer/mason/drywall/site foreman -> Construction; "
 "'Consultant' doing accounting/tax -> Finance & Accounting; 'Consultant' on SAP/ERP/process for clients -> Consulting; "
 "Engineer/Architect/Developer titles -> Software or Technology even if the employer is a consultancy; "
 "internal IT help desk -> Technology; a company's customer care -> Customer Service & Support; grocery/store floor staff -> Retail; "
 "corporate learning & development -> Human Resources; teachers/tutors/instructors (even cooking) -> Education; "
 "job coach/support worker/disability care -> Social Services; regulatory affairs/compliance -> Legal; "
 "mortgage/banking-product advisor or seller -> Sales; credit analyst/accountant/treasury -> Finance & Accounting; "
 "product manager of physical products -> Marketing; data product owner -> Data & Analytics; "
 "CNC programmer/machinist/line operator -> Manufacturing; repair technician (devices, vehicles, machinery) -> Trades; QA/QC engineer -> Engineering. "
 "If title and text describe different jobs, the TEXT wins. Unsolicited application/'not hiring'/'Test' -> unknown. "
 "If the TEXT is empty and the title is ambiguous, family MUST be unknown: never guess. "
 "Seniority: INFER from responsibilities, years, autonomy, scope; unknown only with no signal. "
 "employment_type: only if stated or strongly implied (per diem/CDD/befristet -> temporary; Ausbildung -> apprenticeship); never assume full_time. "
 "remote: infer from arrangement. country: ISO2 from location, XX if none/Remote/Europe.")
SYS = ("You label job postings. Reply ONLY compact JSON, no prose. Fields: "
       "seniority(intern|junior|mid|senior|lead|head|unknown), "
       "employment_type(full_time|part_time|contract|temporary|internship|apprenticeship|unknown), "
       "remote(remote|hybrid|onsite|unknown), country(ISO2 or XX), "
       f"family(exactly one of: {', '.join(FAM)}, or unknown), "
       # aggiunti il 06/09 su osservazione di Giuseppe: l'ingresso e' gia'
       # pagato, una riga in piu' di uscita costa +$0.012/1000 offerte
       "skills(list of up to 8 short skill/tool names as written in the text, [] if none), "
       "salary_min, salary_max (numbers ONLY if a salary is explicitly stated, else null; never estimate), "
       "salary_currency(ISO 4217 or null), salary_period(year|month|day|hour|null). " + REGOLE)

cli = httpx.Client(timeout=45)

def _str(v):
    """GLM a volte torna un campo come lista (es. country ['FR','BE']):
    prendo il primo elemento; None/altro -> None. Cosi' nessun campo
    fa crashare il parsing."""
    if isinstance(v, list):
        v = v[0] if v else None
    return v if isinstance(v, str) else None

def _colonna(c):
    if c.execute("SELECT 1 FROM information_schema.columns WHERE table_name='ats_jobs' AND column_name='sprint_at'").fetchone() is None:
        c.execute("ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS sprint_at timestamptz")

def label(jid, tit, luo, desc):
    p = {"model":"glm-5.3-flash","temperature":0,"max_tokens":260,   # 120 -> 260: entrano le competenze
         "reasoning_effort":"low",
         "messages":[{"role":"system","content":SYS},
             {"role":"user","content":f"TITLE: {tit}\nLOCATION: {luo}\nTEXT: {desc}"}]}
    try:
        r = cli.post("https://api.z.ai/api/paas/v4/chat/completions",
                     headers={"Authorization":f"Bearer {GLM}"}, json=p)
        d = r.json()
        if "choices" not in d:          # 429 o errore API: non marco, si riprova
            return jid, None, 0, 0, 0
        u = d.get("usage",{})
        g = json.loads(re.search(r"\{.*\}", d["choices"][0]["message"]["content"], re.S).group(0))
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return jid, g, u.get("prompt_tokens",0), u.get("completion_tokens",0), cached
    except Exception:
        return jid, None, 0, 0, 0

def main():
    speso = 0.0; fatte = 0; fam_scritte = 0
    with psycopg.connect(DSN, autocommit=True) as c:
        _colonna(c)
        # LA CODA. Scegliere la pagina ordinando ogni volta le 900k righe
        # residue costava 143-190 s a pagina (misurato 06/09: seq scan +
        # sort sul grezzo), contro ~1 minuto di GLM. La coda si calcola
        # UNA volta (stesso ordine: prima chi ha la descrizione, poi le
        # piu' recenti) e ogni pagina e' una lettura per chiave. Le righe
        # servite si tolgono; quando e' vuota si ricostruisce (entrano le
        # nuove arrivate), e se resta vuota lo sprint ha finito.
        def costruisci_coda():
            c.execute("DROP TABLE IF EXISTS sprint_coda")
            c.execute("""
                CREATE TABLE sprint_coda AS
                SELECT j.id, row_number() OVER (
                         ORDER BY (length(coalesce(j.raw->>'description','')) > 80) DESC,
                                  j.posted_at DESC NULLS LAST) AS ord
                  FROM ats_jobs j
                 WHERE j.expired_at IS NULL AND j.sprint_at IS NULL
                   AND (j.seniority IS NULL OR j.employment_type IS NULL
                        OR NOT EXISTS (SELECT 1 FROM job_classifications x
                                        WHERE x.job_id=j.id AND x.model IN ('glm-5.3-flash','glm-5.2')))""")
            c.execute("ALTER TABLE sprint_coda ADD PRIMARY KEY (ord)")
            n = c.execute("SELECT count(*) FROM sprint_coda").fetchone()[0]
            print(f"coda costruita: {n} offerte [{time.strftime('%H:%M')}]", flush=True)
            return n
        if c.execute("SELECT to_regclass('sprint_coda')").fetchone()[0] is None \
                or c.execute("SELECT count(*) FROM sprint_coda").fetchone()[0] == 0:
            costruisci_coda()
        ricostruita = False
        while speso < TETTO:
            righe = c.execute("""
                SELECT j.id, j.title, coalesce(j.location,j.city,''),
                       left(coalesce(j.raw->>'description',j.raw->>'descriptionPlain',''),700), q.ord
                  FROM sprint_coda q JOIN ats_jobs j ON j.id = q.id
                 WHERE j.sprint_at IS NULL AND j.expired_at IS NULL
                 ORDER BY q.ord LIMIT 600""").fetchall()
            if not righe:
                if ricostruita or costruisci_coda() == 0:
                    print("FINITO: niente piu' da fare"); break
                ricostruita = True
                continue
            ricostruita = False
            ord_max = righe[-1][4]
            righe = [r[:4] for r in righe]
            agg_job = []; agg_fam = []; fallite = 0
            with ThreadPoolExecutor(max_workers=PAR) as ex:
                for jid, g, ti, to, cached in ex.map(lambda r: label(*r), righe):
                    speso += (ti-cached)*PREZZO_IN + cached*PREZZO_CACHE + to*PREZZO_OUT
                    if g is None:
                        fallite += 1
                        continue   # chiamata fallita (429/rete): NON marco, si riprova
                    sv = _str(g.get("seniority")); ev = _str(g.get("employment_type"))
                    rv = _str(g.get("remote")); cv = _str(g.get("country")); fv = _str(g.get("family"))
                    sen = sv if sv in VAL_SEN else None
                    et = ev if ev in VAL_ET else None
                    rem = rv if rv in VAL_REM else None
                    ctry = cv if cv and re.match(r"^[A-Z]{2}$", cv) and cv != "XX" else None
                    fam = fv if fv in FAM else None
                    # competenze: liste corte, minuscole, senza doppi; niente frasi
                    sk = g.get("skills") if isinstance(g.get("skills"), list) else []
                    visti = set(); skills = []
                    for s in sk:
                        if isinstance(s, str):
                            s = re.sub(r"\s+", " ", s).strip().strip(".,;:").lower()[:40]
                            if 1 < len(s) and s not in visti:
                                visti.add(s); skills.append(s)
                    skills = skills[:8] or None
                    # stipendio: solo numeri veri, solo se dichiarato (min<=max, tetto anti-delirio)
                    def _num(x):
                        try:
                            v = float(x); return v if 0 < v < 10_000_000 else None
                        except (TypeError, ValueError): return None
                    smin, smax = _num(g.get("salary_min")), _num(g.get("salary_max"))
                    if smin and smax and smin > smax: smin, smax = smax, smin
                    scur = _str(g.get("salary_currency")); scur = scur.upper() if scur and re.match(r"^[A-Za-z]{3}$", scur) else None
                    sper = _str(g.get("salary_period")); sper = sper if sper in ("year","month","day","hour") else None
                    if not (smin or smax): scur = sper = None
                    agg_job.append((sen, et, rem, ctry, skills, smin, smax, scur, sper, jid))
                    if fam:
                        agg_fam.append((jid, fam))
            # scritture a lotti ordinati, retry deadlock
            for lotto, sql in ((sorted(agg_job, key=lambda x:x[-1]),
                    "UPDATE ats_jobs SET seniority=coalesce(seniority,%s), "
                    "employment_type=coalesce(employment_type,%s), "
                    "remote=coalesce(remote,%s), country=coalesce(country,%s), "
                    # le competenze di GLM VINCONO su quelle gia' scritte: il matcher
                    # ESCO di profilo.py metteva «compile airport certification
                    # manuals» su 15.344 offerte (cassieri, ecografisti, carpentieri).
                    # Se GLM non ne trova, resta cio' che c'era.
                    "skills=coalesce(%s, skills), "
                    "salary_min=coalesce(salary_min,%s), salary_max=coalesce(salary_max,%s), "
                    "salary_currency=coalesce(salary_currency,%s), salary_period=coalesce(salary_period,%s), "
                    "sprint_at=now() WHERE id=%s"),):
                for i in range(0,len(lotto),300):
                    parte=lotto[i:i+300]
                    for _ in range(3):
                        try:
                            with c.cursor() as cc: cc.executemany(sql, parte)
                            break
                        except psycopg.errors.DeadlockDetected: time.sleep(1)
            fam_ord = sorted(agg_fam, key=lambda x:x[0])
            for i in range(0,len(fam_ord),300):
                parte=fam_ord[i:i+300]
                for _ in range(3):
                    try:
                        with c.cursor() as cc:
                            cc.executemany("INSERT INTO job_classifications (job_id,family,confidence,model,classified_at) "
                                "VALUES (%s,%s,0.8,'glm-5.3-flash',now()) "
                                # GLM con rubrica vince sul dizionario (audit a mano: ~95%);
                                # 'unknown' non arriva qui (fam e' None), quindi non cancella mai
                                "ON CONFLICT (job_id) DO UPDATE SET family=EXCLUDED.family, "
                                "confidence=EXCLUDED.confidence, model=EXCLUDED.model, "
                                "classified_at=EXCLUDED.classified_at", parte)
                        break
                    except psycopg.errors.DeadlockDetected: time.sleep(1)
            c.execute("DELETE FROM sprint_coda WHERE ord <= %s", (ord_max,))
            fatte += len(agg_job); fam_scritte += len(fam_ord)
            print(f"{fatte} offerte, {fam_scritte} famiglie, spesi ${speso:.2f}/{TETTO}"
                  + (f", {fallite} fallite (429?)" if fallite else "") + f" [{time.strftime('%H:%M')}]", flush=True)
            if fallite > len(righe) // 2:
                time.sleep(60)     # GLM sta rifiutando: aspettare costa meno che martellare
    print(f"FINE: {fatte} offerte arricchite, {fam_scritte} classificate, spesa totale ${speso:.2f}", flush=True)
    # un milione di righe riscritte lasciano spazio morto e statistiche
    # vecchie: si pulisce subito, non quando qualcuno se ne accorge
    with psycopg.connect(DSN, autocommit=True) as cv:
        for t in ("ats_jobs", "job_classifications"):
            cv.execute(f"VACUUM (ANALYZE) {t}")
            print(f"VACUUM ANALYZE {t}: fatto", flush=True)

if __name__ == "__main__":
    main()
