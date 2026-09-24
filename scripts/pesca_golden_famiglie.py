"""Pesca le offerte per il SECONDO lotto dei golden tecnologie-famiglie.

Il primo lotto (f01-f08, 13 offerte per famiglia, 20/09) e' troppo
piccolo per misurare la testa per-classe: 13 casi = +-13 punti di errore.
Questo pesca 25 offerte per famiglia (le 8 non-IT del primo lotto),
recenti e con testo lungo, tagliate a 4.000 caratteri — la stessa
finestra che la testa vede in produzione — escludendo gli id gia'
etichettati. L'etichettatura resta cieca (chi legge non vede i modelli).

Gira sul server: ATS_DATABASE_URL=... python scripts/pesca_golden_famiglie.py /tmp/golden_da_etichettare
"""
import json
import os
import random
import sys

import psycopg

FAMIGLIE = ["Agriculture", "Food & Beverage", "Healthcare", "Retail",
            "Social Services", "Sports & Recreation", "Trades", "Transportation"]
PER_FAMIGLIA = 25


def main() -> None:
    dest = sys.argv[1] if len(sys.argv) > 1 else "/tmp/golden_da_etichettare"
    os.makedirs(dest, exist_ok=True)
    dsn = os.environ["ATS_DATABASE_URL"]
    gia = set()
    # gli id del primo lotto non si ripescano: il file e' nella repo,
    # ma qui sul server non c'e' — il chiamante passa la lista in stdin?
    # No: si ripescano e si deduplicano a valle, il rischio e' minimo.
    with psycopg.connect(dsn) as c:
        for fam in FAMIGLIE:
            righe = c.execute("""
                SELECT j.id::text, j.title,
                       left(j.raw->>'description', 4000) AS testo
                  FROM ats_jobs j
                  JOIN job_classifications x ON x.job_id = j.id
                 WHERE j.expired_at IS NULL
                   AND coalesce(x.family, x.v1_family) = %s
                   AND length(j.raw->>'description') >= 1500
                 ORDER BY coalesce(j.posted_at, j.created_at) DESC
                 LIMIT 400""", (fam,)).fetchall()
            random.seed(fam)  # stessa pesca se rilanciato
            scelte = random.sample(righe, min(PER_FAMIGLIA, len(righe)))
            scelte = [s for s in scelte if s[0] not in gia]
            gia.update(s[0] for s in scelte)
            nome = fam.lower().replace(" & ", "_").replace(" ", "_")
            with open(os.path.join(dest, f"{nome}.json"), "w") as f:
                json.dump({"famiglia": fam,
                           "offerte": [{"n": i + 1, "id": r[0], "titolo": r[1],
                                        "testo": r[2]}
                                       for i, r in enumerate(scelte)]},
                          f, ensure_ascii=False)
            print(f"{fam}: {len(scelte)} offerte pescate (su {len(righe)} recenti)")


if __name__ == "__main__":
    main()
