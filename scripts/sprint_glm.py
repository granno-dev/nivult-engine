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
PAR = 20
PREZZO_IN, PREZZO_OUT = 0.075/1e6, 0.25/1e6            # promo
PREZZO_CACHE = 0.015/1e6   # il prompt di sistema (la rubrica) e' in cache: misurato 512/572 token

c0 = psycopg.connect(DSN)
FAM = [r[0] for r in c0.execute("SELECT DISTINCT family FROM job_classifications ORDER BY 1").fetchall()]
VAL_SEN = {"intern","junior","mid","senior","lead","head"}
VAL_ET = {"full_time","part_time","contract","temporary","internship"}
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
 "If the TEXT is empty and the title is ambiguous, family MUST be unknown: never guess. "
 "Seniority: INFER from responsibilities, years, autonomy, scope; unknown only with no signal. "
 "employment_type: only if stated or strongly implied (per diem/CDD/befristet -> temporary; Ausbildung -> apprenticeship); never assume full_time. "
 "remote: infer from arrangement. country: ISO2 from location, XX if none/Remote/Europe.")
SYS = ("You label job postings. Reply ONLY compact JSON, no prose. Fields: "
       "seniority(intern|junior|mid|senior|lead|head|unknown), "
       "employment_type(full_time|part_time|contract|temporary|internship|apprenticeship|unknown), "
       "remote(remote|hybrid|onsite|unknown), country(ISO2 or XX), "
       f"family(exactly one of: {', '.join(FAM)}, or unknown). " + REGOLE)

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
    p = {"model":"glm-5.3-flash","temperature":0,"max_tokens":120,
         "reasoning_effort":"low",
         "messages":[{"role":"system","content":SYS},
             {"role":"user","content":f"TITLE: {tit}\nLOCATION: {luo}\nTEXT: {desc}"}]}
    try:
        r = cli.post("https://api.z.ai/api/paas/v4/chat/completions",
                     headers={"Authorization":f"Bearer {GLM}"}, json=p)
        d = r.json()
        if "choices" not in d:
            return jid, None, 0, 0
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
        while speso < TETTO:
            righe = c.execute("""
                SELECT j.id, j.title, coalesce(j.location,j.city,''),
                       left(coalesce(j.raw->>'description',j.raw->>'descriptionPlain',''),700)
                  FROM ats_jobs j
                 WHERE j.expired_at IS NULL AND j.sprint_at IS NULL
                   AND (j.seniority IS NULL OR j.employment_type IS NULL
                        OR NOT EXISTS (SELECT 1 FROM job_classifications x WHERE x.job_id=j.id))
                 ORDER BY j.posted_at DESC NULLS LAST
                 LIMIT 600""").fetchall()
            if not righe:
                print("FINITO: niente piu' da fare"); break
            agg_job = []; agg_fam = []
            with ThreadPoolExecutor(max_workers=PAR) as ex:
                for jid, g, ti, to, cached in ex.map(lambda r: label(*r), righe):
                    speso += (ti-cached)*PREZZO_IN + cached*PREZZO_CACHE + to*PREZZO_OUT
                    if g is None:
                        continue   # chiamata fallita: NON marco, si riprova
                    sv = _str(g.get("seniority")); ev = _str(g.get("employment_type"))
                    rv = _str(g.get("remote")); cv = _str(g.get("country")); fv = _str(g.get("family"))
                    sen = sv if sv in VAL_SEN else None
                    et = ev if ev in VAL_ET else None
                    rem = rv if rv in VAL_REM else None
                    ctry = cv if cv and re.match(r"^[A-Z]{2}$", cv) and cv != "XX" else None
                    fam = fv if fv in FAM else None
                    agg_job.append((sen, et, rem, ctry, jid))
                    if fam:
                        agg_fam.append((jid, fam))
            # scritture a lotti ordinati, retry deadlock
            for lotto, sql in ((sorted(agg_job, key=lambda x:x[4]),
                    "UPDATE ats_jobs SET seniority=coalesce(seniority,%s), "
                    "employment_type=coalesce(employment_type,%s), "
                    "remote=coalesce(remote,%s), country=coalesce(country,%s), "
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
                                "VALUES (%s,%s,0.8,'glm-5.3-flash',now()) ON CONFLICT (job_id) DO NOTHING", parte)
                        break
                    except psycopg.errors.DeadlockDetected: time.sleep(1)
            fatte += len(agg_job); fam_scritte += len(fam_ord)
            print(f"{fatte} offerte, {fam_scritte} famiglie, spesi ${speso:.2f}/{TETTO}", flush=True)
    print(f"FINE: {fatte} offerte arricchite, {fam_scritte} classificate, spesa totale ${speso:.2f}")

if __name__ == "__main__":
    main()
