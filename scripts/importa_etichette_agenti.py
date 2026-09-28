"""Importa in etichette_tec le etichette della campagna-agenti (maestro: noi).

Il campionato del 25/09 ha deciso il maestro della v4: gli agenti Kimi a
F1 87,0% sulle 104 golden indipendenti, contro 74,0% di gpt-oss-120b e
~65% di DeepSeek. Le etichette viaggiano come file di lotti (50 offerte
per file) e qui prendono il nome modello «kimi-agenti», cosi' il
costruttore del dataset le preferisce a quelle deepseek quando ci sono
tutt'e due (stesso annuncio: vince il maestro migliore).

Uso: ATS_DATABASE_URL=... python scripts/importa_etichette_agenti.py <dir1> [dir2 ...]
"""
import glob
import json
import os
import sys

import psycopg

MODELLO = "kimi-agenti"


def main() -> None:
    dsn = os.environ["ATS_DATABASE_URL"]
    tot = saltate = orfani = 0
    with psycopg.connect(dsn, autocommit=True) as c:
        for d in sys.argv[1:]:
            for f in sorted(glob.glob(os.path.join(d, "lotto-*.json"))):
                offerte = json.load(open(f, encoding="utf-8"))["offerte"]
                # qualche id del lotto non e' piu' in ats_jobs (28/09: i
                # lotti fotografano un corpus che respira): si importano
                # quelli che ci sono, gli altri si contano e basta
                vivi = {r[0] for r in c.execute(
                    "SELECT id::text FROM ats_jobs WHERE id = ANY(%s::uuid[])",
                    ([o["id"] for o in offerte],)).fetchall()}
                for o in offerte:
                    if o["id"] not in vivi:
                        orfani += 1
                        continue
                    # i lotti piu' recenti scrivono tecnologie come lista
                    # di stringhe, i primi come {nome, ruolo}: entrambi
                    tec = [(t["nome"] if isinstance(t, dict) else t)
                           for t in o.get("tecnologie", [])]
                    tolte = o.get("escluse", [])
                    n = c.execute(
                        "INSERT INTO etichette_tec (job_id, modello, tecnologie, tolte, grezze) "
                        "VALUES (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb) "
                        "ON CONFLICT (job_id, modello) DO NOTHING",
                        (o["id"], MODELLO, json.dumps(tec, ensure_ascii=False),
                         json.dumps(tolte, ensure_ascii=False),
                         json.dumps(o.get("tecnologie", []), ensure_ascii=False))).rowcount
                    tot += n
                    saltate += (n == 0)
    print(f"inserite {tot} etichette ({MODELLO}), gia' presenti {saltate}, "
          f"orfani (id non piu' in ats_jobs) {orfani}")


if __name__ == "__main__":
    main()
