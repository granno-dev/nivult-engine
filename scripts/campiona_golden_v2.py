"""Il campione per il golden v2: mille righe, stratificate, etichettate a mano.

Il golden misura tutto cio' che i tre modelli fanno — famiglia, seniority,
contratto, remoto, tecnologie, sintesi — sulle STESSE righe, cosi' un annuncio
letto una volta vale per tutti gli esami. Stratificato per famiglia (la
famiglia scritta oggi, che sia di v1 o di GLM: serve solo a distribuire, chi
etichetta non la vede) e per piattaforma, con testo intero. Gli id finiscono
in `golden_v2_ids`, che i costruttori dei dataset devono escludere: un golden
che entra nell'addestramento misura la memoria, non la capacita'.

  python campiona_golden_v2.py --lotto 1 --righe 20 > lotto-01.jsonl

Ogni riga porta anche cio' che i modelli hanno scritto OGGI (famiglia, campi,
tecnologie, sintesi), in una chiave a parte: chi etichetta legge prima
l'annuncio e decide, e solo dopo confronta — altrimenti l'etichetta copia.
"""
from __future__ import annotations
import argparse
import html as _html
import json
import os
import re
import sys
import uuid

import psycopg

CAMPI = ("description", "content", "descriptionHtml", "descriptionPlain", "externalDescription",
         "jobDescription", "job_description", "Job_Description", "body", "content_html",
         "description_html", "descriptionBody", "text", "ShortDescriptionStr")
_TAG = re.compile(r"<[^>]+>")
DDL = "CREATE TABLE IF NOT EXISTS golden_v2_ids (job_id uuid PRIMARY KEY, lotto int NOT NULL, creato_at timestamptz DEFAULT now())"


def pulisci(t):
    t = _html.unescape(_html.unescape(t or ""))
    return re.sub(r"\s+", " ", _TAG.sub(" ", t).replace("\xa0", " ")).strip()


def dsn():
    if os.environ.get("ATS_DATABASE_URL"):
        return os.environ["ATS_DATABASE_URL"]
    for f in ("/opt/nivult/.env", "/opt/nivult/engine/.env"):
        try:
            m = re.search(r"^POSTGRES_PASSWORD=(.*)$", open(f).read(), re.M)
            if m:
                return "postgresql://nivult:" + m.group(1).strip() + "@127.0.0.1:5432/nivult_ats"
        except OSError:
            pass
    raise SystemExit("ATS_DATABASE_URL assente")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lotto", type=int, required=True)
    ap.add_argument("--righe", type=int, default=20)
    a = ap.parse_args()
    campi = ", ".join(f"j.raw->>'{c}'" for c in CAMPI)
    with psycopg.connect(dsn(), autocommit=True) as c:
        c.execute(DDL)
        c.execute("SET statement_timeout = '10min'")
        famiglie = [r[0] for r in c.execute(
            "SELECT coalesce(family, v1_family) FROM job_classifications WHERE coalesce(family, v1_family) IS NOT NULL "
            "GROUP BY 1 ORDER BY count(*) DESC")]
        # a rotazione: il lotto k prende le famiglie k, k+n, k+2n... cosi' in
        # cinquanta lotti ogni famiglia compare piu' volte, le grandi di piu'
        scelte = [famiglie[(a.lotto - 1 + i * 7) % len(famiglie)] for i in range(a.righe)]
        prese, out = set(), []
        for fam in scelte:
            r = c.execute(f"""
                SELECT j.id, j.title, coalesce(j.location, j.city, ''), j.country, j.lang, j.platform_id,
                       coalesce(x.family, x.v1_family), x.model, j.seniority, j.employment_type, j.remote,
                       coalesce((SELECT v FROM unnest(ARRAY[{campi}]) v WHERE length(v) >= 300 LIMIT 1), ''),
                       (SELECT tecnologie FROM tecnologie_v1 t WHERE t.job_id = j.id),
                       (SELECT sintesi FROM sintesi_finali s WHERE s.job_id = j.id LIMIT 1)
                  FROM job_classifications x JOIN ats_jobs j ON j.id = x.job_id
                 WHERE x.job_id >= %s::uuid AND coalesce(x.family, x.v1_family) = %s AND j.expired_at IS NULL
                   AND EXISTS (SELECT 1 FROM unnest(ARRAY[{campi}]) v WHERE length(v) >= 300)
                   AND NOT EXISTS (SELECT 1 FROM golden_v2_ids g WHERE g.job_id = j.id)
                 ORDER BY x.job_id LIMIT 1""", (str(uuid.uuid4()), fam)).fetchone()
            if not r or r[0] in prese:
                continue
            prese.add(r[0])
            testo = pulisci(r[11])
            out.append({"id": str(r[0]), "titolo": r[1], "sede": r[2], "paese": r[3], "lingua": r[4],
                        "piattaforma": r[5], "testo": testo,
                        "oggi": {"famiglia": r[6], "famiglia_da": r[7], "seniority": r[8], "contratto": r[9],
                                 "remoto": r[10], "tecnologie": r[12], "sintesi": r[13]}})
        for o in out:
            c.execute("INSERT INTO golden_v2_ids (job_id, lotto) VALUES (%s, %s) ON CONFLICT DO NOTHING", (o["id"], a.lotto))
    for o in out:
        print(json.dumps(o, ensure_ascii=False))
    print(f"lotto {a.lotto}: {len(out)} righe", file=sys.stderr)


if __name__ == "__main__":
    main()
