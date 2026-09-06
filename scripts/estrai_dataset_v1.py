"""Dataset di addestramento per nivult-v1 — curato secondo la rubrica
(docs/rubrica-classificazione.md), non semplicemente scaricato.

Cosa cambia rispetto a v0 (che aveva il 63% di righe SENZA testo e
imparava dai titoli, da maestri incoerenti sulle coppie ambigue):

1. **Solo etichette GLM con rubrica** (sprint dal 06/09 08:30 UTC). Le
   etichette a dizionario (livello1/2/3, dizionario*) NON entrano: sono
   coerenti con se stesse, non con la rubrica.
2. **Il testo e' la regola, non l'eccezione.** Le righe senza descrizione
   entrano solo se GLM ha comunque dato una famiglia (= titolo che la
   rubrica considera inequivocabile) e al massimo per il 20% di ogni
   famiglia: il modello deve saper leggere anche un titolo nudo, ma non
   deve imparare a indovinare da li'.
3. **Teste nuove:** oltre a famiglia e seniority, contratto, remoto e
   lingue richieste (queste ultime a regole, `lingue_richieste.py`).
   `null` = nessuna etichetta = la loss ignora quella testa per quella
   riga (il notebook usa ignore_index).
4. **Bilanciamento** a tetto per famiglia (20k), e le famiglie sotto il
   pavimento (3k) si integrano con etichette GLM pre-rubrica — solo con
   testo, e solo su famiglie fuori dalle coppie ambigue.
5. **Il set d'esame e' fuori per id**: il golden v1 (giudicato a mano)
   piu' 12 righe casuali per famiglia come termometro generale.

Uso (sul server, nel venv):
    ATS_DATABASE_URL=... python estrai_dataset_v1.py [--dopo 2026-09-06T08:30+00] [--golden-mano campione_esame_v1_giudicato.json]
"""
import os, sys, json, gzip, random, re, argparse
from collections import Counter, defaultdict
import psycopg

random.seed(42)
TETTO_FAM = 20000
PAVIMENTO_FAM = 3000
QUOTA_SENZA_TESTO = 0.20
# famiglie delle coppie ambigue: qui entrano SOLO etichette con rubrica
AMBIGUE = {"Trades", "Construction", "Retail", "Sales", "Finance & Accounting",
           "Consulting", "Food & Beverage", "Hospitality", "Transportation",
           "Logistics", "Software", "Technology", "Management & Leadership",
           "Education", "Sports & Recreation", "Customer Service & Support",
           "Administrative"}
_TAG = re.compile(r"<[^>]+>")


def pulisci(t: str | None, n: int = 1000) -> str:
    t = _TAG.sub(" ", t or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dopo", default="2026-09-06T08:30:00+00:00", help="inizio dello sprint con rubrica")
    ap.add_argument("--golden-mano", default=None, help="json giudicato a mano: [{id, family, seniority?, ...}]")
    ap.add_argument("--out", default="/opt/nivult")
    a = ap.parse_args()
    c = psycopg.connect(os.environ["ATS_DATABASE_URL"])

    SEL = """SELECT j.id::text, j.title, coalesce(j.location, j.city, ''), j.country,
                    coalesce(j.raw->>'description', j.raw->>'descriptionPlain', j.raw->>'descriptionHtml', ''),
                    x.family, j.seniority, j.employment_type, j.remote, j.languages_required, j.lang
               FROM ats_jobs j JOIN job_classifications x ON x.job_id = j.id
              WHERE j.title IS NOT NULL AND length(j.title) > 2 AND x.model = 'glm-5.3-flash'"""
    print("estrazione rubrica...", flush=True)
    rubrica = c.execute(SEL + " AND j.sprint_at >= %s", (a.dopo,)).fetchall()
    print(f"  etichette con rubrica: {len(rubrica)}", flush=True)
    print("estrazione pre-rubrica (solo per integrare le famiglie rare)...", flush=True)
    pre = c.execute(SEL.replace("x.model = 'glm-5.3-flash'", "x.model IN ('glm-5.3-flash','glm-5.2')")
                    + " AND j.sprint_at < %s AND length(coalesce(j.raw->>'description','')) > 80", (a.dopo,)).fetchall()
    print(f"  etichette GLM pre-rubrica con testo: {len(pre)}", flush=True)

    def riga(r):
        jid, tit, loc, ctry, desc, fam, sen, et, rem, lingue, lang = r
        return {"id": jid, "title": tit, "location": loc, "country": ctry,
                "text": pulisci(desc), "family": fam, "seniority": sen,
                "employment_type": et, "remote": rem,
                "languages_required": list(lingue) if lingue else None, "lang": lang}

    # --- set d'esame: a mano (autorita') + termometro casuale
    escludi: set[str] = set()
    golden: list[dict] = []
    if a.golden_mano:
        for g in json.load(open(a.golden_mano)):
            g = dict(g); g["fonte"] = "mano"
            golden.append(g); escludi.add(g["id"])
        print(f"  golden a mano: {len(golden)}")

    per_fam: dict[str, list] = defaultdict(list)
    for r in rubrica:
        if r[0] not in escludi:
            per_fam[r[5]].append(riga(r))
    for fam, lst in per_fam.items():
        random.shuffle(lst)
        con = [x for x in lst if len(x["text"]) > 80]
        for x in con[:12]:
            x = dict(x); x["fonte"] = "casuale"; golden.append(x); escludi.add(x["id"])

    # --- training: testo prima, senza testo con quota, tetto per famiglia
    train: list[dict] = []
    stat = {}
    for fam, lst in per_fam.items():
        con = [x for x in lst if len(x["text"]) > 80 and x["id"] not in escludi]
        senza = [x for x in lst if len(x["text"]) <= 80 and x["id"] not in escludi]
        presi = con[:TETTO_FAM]
        q = min(len(senza), int(len(presi) * QUOTA_SENZA_TESTO / (1 - QUOTA_SENZA_TESTO)) if presi else 0,
                TETTO_FAM - len(presi))
        presi += senza[:q]
        stat[fam] = [len(presi), len(con), len(senza), 0]
        train += presi
    # --- pavimento: famiglie rare integrate dal pre-rubrica (mai le ambigue)
    pre_fam: dict[str, list] = defaultdict(list)
    for r in pre:
        if r[0] not in escludi:
            pre_fam[r[5]].append(riga(r))
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

    print("\nfamiglia: nel training | con testo disponibili | senza testo disponibili | integrate pre-rubrica")
    for fam, (n, con, senza, integ) in sorted(stat.items(), key=lambda kv: -kv[1][0]):
        print(f"  {fam:32s} {n:6d} | {con:6d} | {senza:6d} | {integ:5d}")
    print(f"\ntraining: {len(train)} righe ({sum(1 for x in train if len(x['text']) > 80)} con testo) | golden: {len(golden)}")
    for testa in ("seniority", "employment_type", "remote", "languages_required"):
        print(f"  testa {testa}: etichettate {sum(1 for x in train if x.get(testa))}")
    print("  lingue del testo:", Counter(x["lang"] for x in train).most_common(8))

    def scrivi(nome, dati):
        with gzip.open(nome, "wt") as f:
            for x in dati:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")
    scrivi(f"{a.out}/dataset-train-v1.jsonl.gz", train)
    scrivi(f"{a.out}/dataset-golden-v1.jsonl.gz", golden)
    print("scritti", f"{a.out}/dataset-train-v1.jsonl.gz", f"{a.out}/dataset-golden-v1.jsonl.gz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
