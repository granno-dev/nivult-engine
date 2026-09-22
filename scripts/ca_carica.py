"""Corporations Canada (registro federale) -> `ca_imprese` (22/09/2026).

Open Government Licence, aggiornato ogni giorno, nessun account: due CSV
(societa' attive CBCA ~100 MB, altre attive ~9 MB) con nome, legge
costitutiva (= forma giuridica), stato, data anniversario (l'incorporazione),
sede con provincia e CAP. Niente NACE e niente dipendenti. Le societa'
provinciali (Ontario, Québec...) NON ci sono: copre solo chi e' federale.

    python scripts/ca_carica.py --scarica      # sul server: scarica, carica, cancella
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

URL = ("https://d4bf66bykfyaf.cloudfront.net/corporations-active-cbca-en.csv",
       "https://d4bf66bykfyaf.cloudfront.net/corporations-active-non-cbca-en.csv")
COLONNE = ("numero", "bn", "nome", "nome2", "legge", "stato", "anniversario", "via", "citta", "provincia", "paese", "cap", "nome_norm")
DDL = """
CREATE TABLE IF NOT EXISTS ca_imprese (
  numero text PRIMARY KEY, bn text, nome text, nome2 text, legge text, stato text, anniversario date,
  via text, citta text, provincia text, paese text, cap text, nome_norm text);
CREATE INDEX IF NOT EXISTS ca_imprese_nome_norm_idx ON ca_imprese (nome_norm);
CREATE INDEX IF NOT EXISTS ca_imprese_nome2_norm_idx ON ca_imprese (nome2) WHERE nome2 IS NOT NULL;
"""


def _norm(s: str) -> str:
    s = re.sub(r"\b(inc|incorporated|ltd|limited|ltee|ltée|corp|corporation|co|company|llc|llp|"
               r"group|groupe|holding|holdings|canada)\b\.?", " ", s.lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _data(d: str) -> str | None:
    return d if re.match(r"^\d{4}-\d{2}-\d{2}$", d or "") else None


def carica(dsn: str) -> int:
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(DDL)
        c.execute("TRUNCATE ca_imprese")
        n = 0
        for url in URL:
            with urllib.request.urlopen(url, timeout=120) as resp:
                testo = io.TextIOWrapper(resp, encoding="utf-8-sig", newline="")
                lettore = csv.DictReader(testo)
                buf = io.StringIO()
                w = csv.writer(buf, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_NONE, escapechar="\\")
                with c.cursor() as cur, cur.copy("COPY ca_imprese (" + ", ".join(COLONNE) + ") FROM STDIN (FORMAT text, NULL '')") as cp:
                    for r in lettore:
                        nome = (r.get("Corporate name - form 1") or "").strip()
                        if not nome:
                            continue
                        w.writerow([r.get("Corporation number"), r.get("Business number (BN)") or "", nome,
                                    (r.get("Corporate name - form 2") or "").strip(), r.get("Governing legislation") or "",
                                    r.get("Status") or "", _data(r.get("Anniversary date")) or "",
                                    " ".join(x for x in (r.get("Street"), r.get("Street 2")) if x).strip(),
                                    r.get("City/town") or "", r.get("Province/territory") or "", r.get("Country") or "",
                                    r.get("Postal code") or "", _norm(nome)])
                        n += 1
                        if buf.tell() > 1 << 20:
                            cp.write(buf.getvalue()); buf.seek(0); buf.truncate()
                    cp.write(buf.getvalue())
        c.execute("ANALYZE ca_imprese")
        return c.execute("SELECT count(*) FROM ca_imprese").fetchone()[0]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scarica", action="store_true")
    a = ap.parse_args(argv)
    dsn = os.environ.get("ATS_DATABASE_URL")
    if not dsn:
        for f in ("/opt/nivult/.env", "/opt/nivult/engine/.env"):
            try:
                m = re.search(r"^POSTGRES_PASSWORD=(.*)$", open(f).read(), re.M)
                if m:
                    dsn = "postgresql://nivult:" + m.group(1).strip() + "@127.0.0.1:5432/nivult_ats"
                    break
            except OSError:
                pass
    print("ca_imprese:", carica(dsn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
