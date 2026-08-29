#!/usr/bin/env python3
"""Travasa nel funnel le offerte ATS pronte a entrare.

    python scripts/ponte_ats.py --dry-run    # cosa entrerebbe, senza scrivere
    python scripts/ponte_ats.py              # travasa
    python scripts/ponte_ats.py --giorni 60  # allarga la finestra

Il perché di ogni scelta sta in `nivult/ponte_ats.py`. Qui c'è solo
l'involucro da riga di comando: come ogni pezzo del motore, si prova da
solo senza dover far girare il resto.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from nivult.config import database_url, safe_dsn
from nivult.ponte_ats import GIORNI_FRESCHEZZA, ats_database_url, importa


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="mostra cosa entrerebbe, senza scrivere niente")
    p.add_argument("--giorni", type=int, default=GIORNI_FRESCHEZZA,
                   help=f"finestra di freschezza (default: {GIORNI_FRESCHEZZA})")
    a = p.parse_args()

    motore, ats = database_url(), ats_database_url()
    print(f"motore : {safe_dsn(motore)}")
    print(f"ats    : {safe_dsn(ats)}")
    if a.dry_run:
        print("DRY RUN — non verrà scritto nulla")

    with psycopg.connect(motore) as conn, psycopg.connect(ats) as conn_ats:
        r = importa(conn, conn_ats, giorni=a.giorni, dry_run=a.dry_run)

    print()
    print(f"  idonee all'ingresso : {r.esaminate}")
    print(f"  nuove               : {r.importate}")
    print(f"  aggiornate          : {r.aggiornate}")
    print(f"  scadute             : {r.scadute}")
    if r.scartate:
        print("  scartate:")
        for motivo, n in sorted(r.scartate.items(), key=lambda x: -x[1]):
            print(f"    {n:>5}  {motivo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
