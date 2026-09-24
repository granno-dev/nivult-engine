"""Banco di precisione per _boilerplate_azienda (24/09/2026).

Prende N tenant reali dalla coda del passo aziende_dettagli (stessa
SQL_TENANT, quindi stesso mix di piattaforme e di paesi), applica la
funzione e stampa nome + estratto per la classificazione a mano.
Non scrive niente: lettura sola.

Uso: ATS_DATABASE_URL=... python scripts/banco_boilerplate.py [N]
"""
import os
import sys

import psycopg

# il codice testato e' quello vero del modulo, non una copia
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from nivult.ats.aziende_dettagli import _boilerplate_azienda  # noqa: E402


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    dsn = os.environ["ATS_DATABASE_URL"]
    with psycopg.connect(dsn) as c:
        # le aziende che nel flusso reale arriverebbero al boilerplate:
        # offerte attive e nessuna descrizione da jsonld ne' dal sito.
        # random() per un mix vero di dimensioni e piattaforme
        tenants = c.execute("""
            SELECT c.id, c.platform_id, c.slug, c.company_name
              FROM ats_companies c
              LEFT JOIN aziende_dettagli d ON d.company_id = c.id
             WHERE c.job_count > 0
               AND (d.description IS NULL OR d.description_da = 'annunci (boilerplate)')
             ORDER BY random() LIMIT %s""", (n,)).fetchall()
        visti = trovati = 0
        for cid, pid, slug, nome in tenants:
            visti += 1
            estratto = _boilerplate_azienda(c, pid, slug, nome)
            if estratto:
                trovati += 1
                print(f"=== {nome} [{pid}/{slug}]")
                print(estratto[:400].replace("\n", " "))
                print()
        print(f"--- visti {visti}, con boilerplate {trovati}", file=sys.stderr)


if __name__ == "__main__":
    main()
