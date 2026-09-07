"""Dataset di addestramento per nivult-v1 — curato secondo la rubrica
(docs/rubrica-classificazione.md), con le garanzie promesse a Giuseppe il
2026-09-06 («assicuriamoci che questa volta il database di training sia
fatto bene»):

1. SOLO etichette GLM con rubrica (sprint dal 06/09 08:30 UTC). Niente
   dizionario, niente v0, niente ESCO.
2. IL TESTO E' LA REGOLA: righe senza descrizione ammesse solo se GLM ha
   dato una famiglia (titolo inequivocabile) e mai oltre il 20% di una
   famiglia. v0 era al 63% senza testo: e' il motivo per cui confondeva.
3. DIVISIONE PER AZIENDA, non per riga: gli annunci-fotocopia della stessa
   azienda stanno tutti da una parte sola, cosi' l'esame misura il capire,
   non il ricordare.
4. DEDUPLICA di titolo+azienda+citta' prima di contare.
5. BILANCIAMENTO: tetto per famiglia, pavimento per le rare integrato solo
   da etichette GLM pre-rubrica con testo e fuori dalle coppie ambigue.
6. ESAME separato: i 280 casi a mano (autorita') + 12 casuali per famiglia
   (termometro), tutti fuori dall'addestramento per id E per azienda.
7. RAPPORTO: distribuzioni, quote senza testo, lingue, e 100 righe a caso
   in un file leggibile (controllo a occhio prima del via: >5 sbagliate
   su 100 = non si addestra).
8. Teste: famiglia, seniority, contratto, remoto, lingue richieste.
   null = la loss ignora quella testa per quella riga.

Uso (sul server o sul N5, nel venv):
  ATS_DATABASE_URL=... python scripts/estrai_dataset_v1.py \
      --golden-mano docs/esame-v1-giudicato.json --out /opt/nivult/v1
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict

import psycopg

random.seed(42)
TETTO_FAM = 20000
PAVIMENTO_FAM = 3000
QUOTA_SENZA_TESTO = 0.20
QUOTA_ESAME_AZIENDE = 0.05        # 5% delle aziende va all'esame/validazione
AZIENDA_GRANDE = 300              # sopra questi annunci un'azienda dell'esame a mano NON viene riservata per intero
AMBIGUE = {"Trades", "Construction", "Retail", "Sales", "Finance & Accounting",
           "Consulting", "Food & Beverage", "Hospitality", "Transportation",
           "Logistics", "Software", "Technology", "Management & Leadership",
           "Education", "Sports & Recreation", "Customer Service & Support",
           "Administrative"}
_TAG = re.compile(r"<[^>]+>")


def pulisci(t: str | None, n: int = 1000) -> str:
    """Prima le entita' (due volte: phenom e freshteam codificano l'HTML
    intero come &lt;p&gt;), poi i tag, poi gli spazi. Misurato il 06/09
    sulla prima versione: il 54% delle righe portava &nbsp;/&lt; nel testo."""
    import html
    t = html.unescape(html.unescape(t or ""))
    t = _TAG.sub(" ", t).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()[:n]


def chiave_dup(titolo: str, azienda: str, luogo: str) -> str:
    base = f"{(titolo or '').lower().strip()}|{azienda}|{(luogo or '').lower().strip()}"
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def chiave_testo(titolo: str, testo: str) -> str:
    """Annunci-fotocopia di catene e agenzie: stesso titolo e stesso testo in
    citta' diverse (5.430 nella prima versione). Uno basta."""
    base = f"{(titolo or '').lower().strip()}|{(testo or '')[:300].lower()}"
    return hashlib.sha1(base.encode()).hexdigest()[:16]


def lato_azienda(azienda: str) -> str:
    """Deterministico: la stessa azienda finisce sempre dallo stesso lato."""
    h = int(hashlib.sha1(azienda.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "esame" if h < QUOTA_ESAME_AZIENDE else "train"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dopo", default="2026-09-06T08:30:00+00:00")
    ap.add_argument("--escludi-piattaforme", default="",
                    help="piattaforme da tenere FUORI (virgole): per quelle il cui testo non e' affidabile")
    ap.add_argument("--escludi-chimere", default="icims,workday,cornerstone,eploy,traffit,pinpoint,vincere",
                    help="piattaforme con id PER TENANT: una riga il cui URL non contiene lo slug e' una chimera "
                         "(titolo/URL di un tenant, testo di un altro: chiave (piattaforma, external_id) in collisione)")
    ap.add_argument("--golden-mano", default=None)
    ap.add_argument("--out", default="/opt/nivult/v1")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    c = psycopg.connect(os.environ["ATS_DATABASE_URL"])

    SEL = """SELECT j.id::text, j.title, coalesce(j.location, j.city, ''), j.country,
                    coalesce(j.raw->>'description', j.raw->>'descriptionPlain', j.raw->>'descriptionHtml', ''),
                    x.family, j.seniority, j.employment_type, j.remote, j.languages_required, j.lang,
                    j.platform_id || '/' || j.slug
               FROM ats_jobs j JOIN job_classifications x ON x.job_id = j.id
              WHERE j.title IS NOT NULL AND length(j.title) > 2 AND {modello}"""
    # Piattaforme escluse per intero: se il testo e' di un altro annuncio
    # (iCIMS, 07/09/2026: 0 su 25 testi salvati stavano nella pagina viva),
    # anche l'etichetta GLM e' presa da quel testo, e non si salva niente
    # tenendo il solo titolo.
    escluse = [p.strip() for p in a.escludi_piattaforme.split(",") if p.strip()]
    if escluse:
        # ARRAY[...] e non '{...}': SEL passa da .format(), e le graffe lo rompono
        SEL += " AND NOT (j.platform_id = ANY(ARRAY[" + ",".join("'%s'" % p.replace("'", "") for p in escluse) + "]))"
        print(f"  piattaforme escluse: {escluse}", flush=True)
    # Le CHIMERE (07/09/2026): su iCIMS, Workday e simili l'id dell'annuncio
    # e' unico per TENANT, ma la chiave dell'archivio e' (piattaforma, id):
    # il job 14145 di un tenant sovrascrive titolo e URL del 14145 di un
    # altro e il testo resta quello vecchio. Misurato: iCIMS 33%, Workday
    # 7% delle attive con URL di un altro tenant. Fuori dal dataset finche'
    # la chiave non diventa (piattaforma, tenant, id).
    chimere = [p.strip() for p in a.escludi_chimere.split(",") if p.strip() and p.strip() not in escluse]
    if chimere:
        SEL += (" AND NOT (j.platform_id = ANY(ARRAY[" + ",".join("'%s'" % p.replace("'", "") for p in chimere)
                + "]) AND j.url NOT ILIKE '%%' || j.slug || '%%')")
        print(f"  chimere escluse (URL senza slug) su: {chimere}", flush=True)
    print("estrazione rubrica...", flush=True)
    rubrica = c.execute(SEL.format(modello="x.model = 'glm-5.3-flash'") + " AND j.sprint_at >= %s",
                        (a.dopo,)).fetchall()
    print(f"  etichette con rubrica: {len(rubrica)}", flush=True)
    pre = c.execute(SEL.format(modello="x.model IN ('glm-5.3-flash','glm-5.2')")
                    + " AND j.sprint_at < %s AND length(coalesce(j.raw->>'description','')) > 80",
                    (a.dopo,)).fetchall()
    print(f"  etichette GLM pre-rubrica con testo: {len(pre)}", flush=True)

    def riga(r):
        jid, tit, loc, ctry, desc, fam, sen, et, rem, lingue, lang, az = r
        return {"id": jid, "title": tit, "location": loc, "country": ctry,
                "text": pulisci(desc), "family": fam, "seniority": sen,
                "employment_type": et, "remote": rem,
                "languages_required": list(lingue) if lingue else None,
                "lang": lang, "azienda": az}

    # --- esame a mano: autorita'; le sue aziende vanno tutte al lato esame
    escludi_id: set[str] = set()
    aziende_esame: set[str] = set()
    golden: list[dict] = []
    if a.golden_mano:
        for g in json.load(open(a.golden_mano)):
            g = dict(g); g["fonte"] = "mano"; g["text"] = pulisci(g.get("text"))
            golden.append(g); escludi_id.add(g["id"])
        az_mano = {r[0]: r[1] for r in c.execute(
            "SELECT id::text, platform_id || '/' || slug FROM ats_jobs WHERE id = ANY(%s::uuid[])",
            ([g["id"] for g in golden],)).fetchall()}
        for g in golden:
            g["azienda"] = az_mano.get(g["id"])
        # Le aziende dell'esame a mano vanno tutte al lato esame — ma solo
        # se PICCOLE. Le agenzie per il lavoro italiane (Gi Group, Manpower,
        # Umana…) hanno migliaia di annunci ciascuna: riservarle all'esame
        # per 3-4 casi giudicati a mano toglieva dal training 25.000 delle
        # 34.000 righe italiane (07/09/2026: l'Italia era all'1%). Per le
        # grandi bastano le difese di riga: id esclusi e testo-fotocopia.
        conte = {r[0]: r[1] for r in c.execute(
            "SELECT platform_id || '/' || slug, count(*) FROM ats_jobs WHERE platform_id || '/' || slug = ANY(%s) "
            "GROUP BY 1", (list(set(az_mano.values())),)).fetchall()}
        grandi = {az for az, n in conte.items() if n >= AZIENDA_GRANDE}
        aziende_esame |= set(az_mano.values()) - grandi
        print(f"  golden a mano: {len(golden)} ({len(aziende_esame)} aziende riservate all'esame; "
              f"{len(grandi)} grandi lasciate al training: {sorted(grandi)[:6]}…)")

    # --- dedup + divisione per azienda
    visti_dup: set[str] = set()
    visti_testo: set[str] = set()
    dup = 0
    train_pool: dict[str, list] = defaultdict(list)
    esame_pool: dict[str, list] = defaultdict(list)
    # i testi dell'esame a mano sono "visti": una fotocopia loro non entra nel training
    for g in golden:
        visti_testo.add(chiave_testo(g.get("title"), pulisci(g.get("text"))))
    for r in rubrica:
        x = riga(r)
        if x["id"] in escludi_id:
            continue
        k = chiave_dup(x["title"], x["azienda"], x["location"])
        kt = chiave_testo(x["title"], x["text"]) if len(x["text"]) > 80 else None
        if k in visti_dup or (kt and kt in visti_testo):
            dup += 1; continue
        visti_dup.add(k)
        if kt:
            visti_testo.add(kt)
        lato = "esame" if x["azienda"] in aziende_esame else lato_azienda(x["azienda"])
        (esame_pool if lato == "esame" else train_pool)[x["family"]].append(x)
    print(f"  duplicati titolo+azienda+citta' scartati: {dup}")

    # --- esame casuale: 12 per famiglia dal lato esame, con testo
    for fam, lst in esame_pool.items():
        random.shuffle(lst)
        for x in [y for y in lst if len(y["text"]) > 80][:12]:
            x = dict(x); x["fonte"] = "casuale"; golden.append(x); escludi_id.add(x["id"])

    # --- training: testo prima, senza testo con quota, tetto per famiglia
    train: list[dict] = []
    stat: dict[str, list] = {}
    for fam, lst in train_pool.items():
        random.shuffle(lst)
        con = [x for x in lst if len(x["text"]) > 80]
        senza = [x for x in lst if len(x["text"]) <= 80]
        presi = con[:TETTO_FAM]
        q = min(len(senza), int(len(presi) * QUOTA_SENZA_TESTO / (1 - QUOTA_SENZA_TESTO)) if presi else 0,
                TETTO_FAM - len(presi))
        presi += senza[:q]
        stat[fam] = [len(presi), len(con), len(senza), 0]
        train += presi
    # --- pavimento dal pre-rubrica: solo con testo, mai le ambigue, mai aziende dell'esame
    pre_fam: dict[str, list] = defaultdict(list)
    for r in pre:
        x = riga(r)
        if x["id"] in escludi_id or x["azienda"] in aziende_esame or lato_azienda(x["azienda"]) == "esame":
            continue
        k = chiave_dup(x["title"], x["azienda"], x["location"])
        kt = chiave_testo(x["title"], x["text"])
        if k in visti_dup or kt in visti_testo:
            continue
        visti_dup.add(k); visti_testo.add(kt)
        pre_fam[x["family"]].append(x)
    for fam, lst in pre_fam.items():
        if fam in AMBIGUE:
            continue
        have = stat.get(fam, [0, 0, 0, 0])[0]
        if have < PAVIMENTO_FAM:
            random.shuffle(lst)
            agg = lst[:PAVIMENTO_FAM - have]
            train += agg
            stat.setdefault(fam, [0, 0, 0, 0])
            stat[fam][0] += len(agg); stat[fam][3] = len(agg)
    random.shuffle(train)

    # --- controlli duri: niente fughe fra train ed esame
    id_train = {x["id"] for x in train}; az_train = {x["azienda"] for x in train}
    fuga_id = sum(1 for g in golden if g["id"] in id_train)
    az_gold = set(a_ for a_ in (az_mano.values() if a.golden_mano else []))
    az_gold |= {g["azienda"] for g in golden if g.get("azienda")}
    fuga_az = len(az_gold & az_train)
    assert fuga_id == 0, f"FUGA: {fuga_id} id dell'esame nel training"
    assert fuga_az == 0, f"FUGA: {fuga_az} aziende dell'esame nel training"

    # --- rapporto
    rap = []
    rap.append("famiglia | training | con testo disp. | senza testo disp. | dal pre-rubrica")
    for fam, (n, con, senza, integ) in sorted(stat.items(), key=lambda kv: -kv[1][0]):
        rap.append(f"  {fam:32s} {n:6d} | {con:6d} | {senza:6d} | {integ:5d}")
    con_testo = sum(1 for x in train if len(x["text"]) > 80)
    rap.append(f"\ntraining: {len(train)} righe, {con_testo} con testo ({100*con_testo//max(1,len(train))}%) | "
               f"esame: {len(golden)} ({sum(1 for g in golden if g.get('fonte')=='mano')} a mano) | aziende nel training: {len(az_train)}")
    for testa in ("seniority", "employment_type", "remote", "languages_required"):
        rap.append(f"  testa {testa}: etichettate {sum(1 for x in train if x.get(testa))}")
    rap.append("  lingue del testo: " + str(Counter(x["lang"] for x in train).most_common(8)))
    rap.append(f"  fughe train/esame: id={fuga_id} aziende={fuga_az} (devono essere 0)")
    testo_rap = "\n".join(rap); print(testo_rap)
    open(f"{a.out}/rapporto-v1.txt", "w").write(testo_rap + "\n")

    # --- 100 righe a caso per il controllo a occhio
    campione = random.sample(train, min(100, len(train)))
    with open(f"{a.out}/controllo-100.txt", "w") as f:
        for i, x in enumerate(campione):
            f.write(f"#{i} [{x['family']} | sen={x['seniority']} et={x['employment_type']} rem={x['remote']} "
                    f"lr={x['languages_required']}] {x['title']} | {x['location']} | {x['azienda']}\n"
                    f"   {x['text'][:400]}\n")

    def scrivi(nome, dati):
        with gzip.open(nome, "wt") as f:
            for x in dati:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")
    scrivi(f"{a.out}/dataset-train-v1.jsonl.gz", train)
    scrivi(f"{a.out}/dataset-golden-v1.jsonl.gz", golden)
    print("scritti:", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
