"""Prepara il banco di prova per la GPU a noleggio.

Si prendono offerte che il 2B ha GIA' lavorato: cosi' il confronto e' appaiato —
stesse offerte, stesso prompt, due modelli — invece di due medie su campioni
diversi, che e' l'errore che ho gia' fatto il 18/09.

Si escludono le righe scritte dal buttafuori e le gemelle: quelle il modello non
le ha davvero lette, e falserebbero sia la velocita' sia la qualita'.
"""
from __future__ import annotations
import gzip
import html as _html
import json
import os
import re

import psycopg

CAMPI = ("description", "content", "descriptionHtml", "descriptionPlain", "externalDescription",
         "jobDescription", "job_description", "body", "content_html", "description_html", "text")
TAG = re.compile(r"<[^>]+>")
N = int(os.environ.get("N", "2000"))


def pulito(t: str | None) -> str:
    t = _html.unescape(_html.unescape(t or ""))
    return re.sub(r"\s+", " ", TAG.sub(" ", t).replace("\xa0", " ")).strip()


campi_sql = ", ".join(f"j.raw->>'{c}'" for c in CAMPI)
sql = f"""
SELECT j.id, j.title, coalesce(j.location, j.city, ''), j.country, j.lang,
       coalesce((SELECT v FROM unnest(ARRAY[{campi_sql}]) v WHERE length(v) >= 300 LIMIT 1), ''),
       e.tecnologie, e.sintesi, m.sintesi
  FROM ats_jobs j
  JOIN estrazioni_v2b e ON e.job_id = j.id
  LEFT JOIN sintesi_mt5 m ON m.job_id = j.id
 WHERE j.expired_at IS NULL
   AND e.modello = 'nivult-2b'          -- letta davvero dal modello, non gemella
   AND e.tecnologie IS NOT NULL
 ORDER BY random() LIMIT %s
"""

with psycopg.connect(os.environ["ATS_DATABASE_URL"]) as c:
    righe = c.execute(sql, (N,)).fetchall()

fuori = []
for jid, tit, sede, paese, lang, grezzo, tec2b, sin2b, sinmt5 in righe:
    t = pulito(grezzo)
    if len(t) < 300:
        continue
    fuori.append({"id": str(jid), "title": tit, "location": sede, "country": paese,
                  "lang": lang, "text": t[:6000],
                  "tecnologie_2b": tec2b, "sintesi_2b": sin2b, "sintesi_mt5": sinmt5})

perc = "/opt/nivult/banco-gpu.jsonl.gz"
with gzip.open(perc, "wt") as f:
    for r in fuori:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

car = sum(len(r["text"]) for r in fuori)
print(f"scritte {len(fuori):,} offerte in {perc}")
print(f"  caratteri di testo: {car:,}  (~{car/3.6/1e6:.1f} M token da leggere)")
print(f"  con la sintesi del 2B: {sum(1 for r in fuori if r['sintesi_2b']):,}")
print(f"  con la sintesi di mT5: {sum(1 for r in fuori if r['sintesi_mt5']):,}")
print(f"  peso del file: {os.path.getsize(perc)/1e6:.1f} MB")
