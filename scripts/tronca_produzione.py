"""In produzione, quanta parte dell'annuncio leggono davvero i modelli?

Il golden e' tagliato a 4.000 caratteri per costruzione, quindi non dice
niente su questo. Qui si prendono annunci VERI dalla coda di produzione, con
la stessa estrazione del demone, e si guarda quanti superano la finestra.
"""
import json, os, subprocess, sys
from transformers import AutoTokenizer

MOD = os.environ.get("MODELLO_TEC", "/opt/nivult/gpu/tec-v1-ck00500")
tok = AutoTokenizer.from_pretrained(MOD)
CAMPI = ("description","content","descriptionHtml","descriptionPlain","externalDescription",
         "jobDescription","job_description","Job_Description","body","content_html",
         "description_html","descriptionBody","text","ShortDescriptionStr")
campi = ", ".join(f"j.raw->>'{c}'" for c in CAMPI)
sql = f"""
SET max_parallel_workers_per_gather = 0;
WITH s AS MATERIALIZED (SELECT id, title FROM ats_jobs WHERE expired_at IS NULL
   ORDER BY md5(id::text) LIMIT 1200)
SELECT row_to_json(t) FROM (
 SELECT s.title, coalesce((SELECT v FROM unnest(ARRAY[{campi}]) v WHERE length(v)>=300 LIMIT 1),'') AS testo
   FROM s JOIN ats_jobs j ON j.id=s.id) t;
"""
# separatore di record ESPLICITO: il testo degli annunci contiene ritorni a
# capo che spezzano una riga JSON a meta', e il parsing riga-per-riga muore con
# «Unterminated string». Con -R il record finisce dove diciamo noi.
SEP = "\u00a7\u00a7\u00a7"
p = subprocess.run(["docker","exec","-i","nivult-db-1","psql","-U","nivult","-d","nivult_ats",
                    "-tA","-R",SEP,"-v","ON_ERROR_STOP=1"],
                   input=sql, capture_output=True, text=True)
if p.returncode: sys.exit(f"psql: {p.stderr[:300]}")
righe = []
for r in p.stdout.split(SEP):
    r = r.strip()
    if r.startswith("{"):
        try: righe.append(json.loads(r))
        except Exception: pass

import re, html as _h
def pulito(t):
    t = re.sub(r"<[^>]+>", " ", t or "")
    return re.sub(r"\s+", " ", _h.unescape(t)).strip()

L = []
for d in righe:
    testo = f"{d.get('title') or ''}\n{pulito(d.get('testo'))}"
    L.append(len(tok(testo, add_special_tokens=False)["input_ids"]))
L.sort()
n = len(L) or 1
print(f"=== {n} annunci veri dalla produzione")
print(f"  mediana {L[n//2]} token   90 pc {L[int(n*0.9)]}   99 pc {L[int(n*0.99)]}   max {L[-1]}")
print()
for f in (1024, 1536, 2048, 3072, 4096):
    t = sum(1 for x in L if x > f)
    # quanta parte del testo si perde, in media, su quelli troncati
    persa = sum((x - f) / x for x in L if x > f) / max(t, 1) * 100
    print(f"  finestra {f:>5}: troncati {t*100/n:>5.1f}%   "
          f"e su quelli si perde in media il {persa:.0f}% del testo")
