"""Il campione per estendere il golden alle otto famiglie del buttafuori.

Il golden di settembre e' fatto di annunci che il buttafuori lasciava passare.
Ma la testa di marcatura non ha piu' un buttafuori — vede tutto il mercato — e
proprio sulle otto famiglie escluse (mestieri, trasporti, ristorazione, sanita',
commercio, agricoltura, servizi sociali, sport) non esiste nessun metro.

Serve saperlo perche' le rese misurate li' sono bassissime: Retail 0,09 nomi per
annuncio, Transportation 0,06, contro 8,35 di Software. O quegli annunci non
contengono tecnologie — e allora va bene cosi' — o la testa non le riconosce, e
allora stiamo perdendo proprio la coda lunga che dicevamo di voler vendere
(Tigsvetsning, ORSY, Pick-by-Voice non stanno negli annunci di Software).

DUE REGOLE, o il campione non vale:

  1. SI ETICHETTA ALLA CIECA. Qui non si tira fuori niente da tecnologie_v1: chi
     legge non deve sapere cosa ha risposto il modello, o finisce per dargli
     ragione. E' la stessa regola dei primi dieci lotti.
  2. Gli annunci gia' etichettati si escludono per id, e il dataset di
     addestramento pure: un esame su righe viste in addestramento misura la
     memoria, non la capacita'.

Esce un banco (jsonl.gz, la forma che esame_golden.py legge) e un dump
leggibile, con il testo tagliato alla stessa finestra che la testa vede in
produzione: 1024 token, cioe' circa 4.000 caratteri. Piu' in la' il modello non
guarda, quindi segnare li' una tecnologia sarebbe contarla come persa quando non
l'ha mai vista.
"""
from __future__ import annotations
import glob
import gzip
import json
import os
import subprocess
import sys

FAMIGLIE = ("Agriculture", "Social Services", "Retail", "Sports & Recreation",
            "Food & Beverage", "Transportation", "Trades", "Healthcare")
PER_FAMIGLIA = int(os.environ.get("PER_FAMIGLIA", "13"))
FINESTRA = int(os.environ.get("FINESTRA", "4000"))
GOLDEN = os.environ.get("GOLDEN_TEC", "/opt/nivult/golden-tec")
# ON_ERROR_STOP non e' un di piu': senza, psql esce con 0 anche quando la query
# fallisce, e lo script scrive tranquillamente un campione vuoto (20/09/2026).
PSQL = ["docker", "exec", "-i", "nivult-db-1", "psql", "-U", "nivult", "-d", "nivult_ats",
        "-tA", "-v", "ON_ERROR_STOP=1"]

CAMPI = ("description", "content", "descriptionHtml", "descriptionPlain", "externalDescription",
         "jobDescription", "job_description", "Job_Description", "body", "content_html",
         "description_html", "descriptionBody", "text", "ShortDescriptionStr")


def gia_etichettati() -> list[str]:
    ids = []
    for p in glob.glob(os.path.join(GOLDEN, "*.json")):
        try:
            d = json.load(open(p))
        except Exception:                                          # noqa: BLE001
            continue
        for o in (d.get("offerte") if isinstance(d, dict) else d) or []:
            if isinstance(o, dict) and o.get("id"):
                ids.append(o["id"])
    return sorted(set(ids))


def main() -> None:
    esclusi = gia_etichettati()
    print(f"gia' etichettati, esclusi: {len(esclusi)}", file=sys.stderr)
    lista = ",".join(f"'{x}'" for x in esclusi) or "'00000000-0000-0000-0000-000000000000'"
    campi = ", ".join(f"j.raw->>'{c}'" for c in CAMPI)
    fam = ", ".join(f"'{f}'" for f in FAMIGLIE)

    # PRIMA si sorteggia, POI si legge il testo. Scritta al contrario — con il
    # testo estratto dentro la CTE e il filtro sulla lunghezza sopra — questa
    # query tira fuori il raw di ogni offerta delle otto famiglie (centinaia di
    # migliaia) per poi tenerne 104: oltre tre minuti senza finire. Qui il giro
    # largo tocca solo colonne indicizzate, e il raw si apre 104 volte.
    #
    # Il prezzo: si sorteggia piu' del necessario (il triplo) perche' qualcuno
    # non avra' un testo abbastanza lungo, e si taglia dopo. Bilanciato lo stesso,
    # perche' il sorteggio e' per famiglia e l'ordine e' stabile (md5 dell'id).
    # Niente worker paralleli: il container del database ha i 64 MB di /dev/shm
    # che Docker da' per default, e un hash join parallelo su queste dimensioni
    # muore con «could not resize shared memory segment». Il limite vero va
    # alzato ricreando il container (--shm-size=1g); qui basta non usarli.
    sql = f"""
SET max_parallel_workers_per_gather = 0;
WITH scelte AS MATERIALIZED (
  SELECT j.id, j.title, coalesce(j.lang,'') AS lang, c.family,
         row_number() OVER (PARTITION BY c.family ORDER BY md5(j.id::text)) AS n
    FROM ats_jobs j
    JOIN job_classifications c ON c.job_id = j.id
   WHERE c.family IN ({fam})
     AND j.expired_at IS NULL
     AND j.id NOT IN ({lista})),
larghe AS MATERIALIZED (
  SELECT s.id, s.title, s.lang, s.family, s.n,
         coalesce((SELECT v FROM unnest(ARRAY[{campi}]) v WHERE length(v) >= 400 LIMIT 1), '') AS testo
    FROM scelte s JOIN ats_jobs j ON j.id = s.id
   WHERE s.n <= {PER_FAMIGLIA * 3}),
tenute AS (
  SELECT *, row_number() OVER (PARTITION BY family ORDER BY n) AS k
    FROM larghe WHERE length(testo) >= 400)
SELECT row_to_json(t) FROM (
  SELECT id::text, title, lang, family, left(testo, {FINESTRA}) AS testo
    FROM tenute WHERE k <= {PER_FAMIGLIA}
   ORDER BY family, k) t;
"""
    p = subprocess.run(PSQL, input=sql, capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"psql: {p.stderr[:400]}")
    # si tengono solo le righe JSON: il SET iniziale stampa «SET» su stdout
    righe = [json.loads(r) for r in p.stdout.splitlines() if r.startswith("{")]
    if not righe:
        raise SystemExit("nessuna riga: non si sovrascrive il campione con il vuoto")
    print(f"campionate: {len(righe)}", file=sys.stderr)

    with gzip.open("/opt/nivult/banco-famiglie.jsonl.gz", "wt") as f:
        for i, r in enumerate(righe, 1):
            f.write(json.dumps({"id": r["id"], "title": r["title"], "text": r["testo"]},
                               ensure_ascii=False) + "\n")

    with open("/opt/nivult/golden-tec/indice-famiglie.json", "w", encoding="utf-8") as f:
        json.dump([{"n": i, "id": r["id"], "title": r["title"], "lang": r["lang"],
                    "family": r["family"]} for i, r in enumerate(righe, 1)],
                  f, ensure_ascii=False, indent=1)

    with open("/opt/nivult/famiglie-da-leggere.txt", "w", encoding="utf-8") as f:
        for i, r in enumerate(righe, 1):
            f.write(f"\n{'='*78}\n#{i}  [{r['family']}]  {r['title']}  ({r['lang']})\n"
                    f"id: {r['id']}\n{'-'*78}\n{r['testo']}\n")
    print("scritti: banco-famiglie.jsonl.gz, indice-famiglie.json, famiglie-da-leggere.txt",
          file=sys.stderr)


if __name__ == "__main__":
    main()
