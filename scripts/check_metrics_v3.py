#!/usr/bin/env python3
"""Metriche di comportamento reale su _test/_dev, senza rete."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg

from check_modules import seed, wipe
from nivult.config import database_name, database_url
from nivult.matching.metrics import metriche_reali

PASSATI = 0
FALLITI = 0


def check(nome, condizione):
    global PASSATI, FALLITI
    if condizione:
        PASSATI += 1
        print("  ok   ", nome)
    else:
        FALLITI += 1
        print("  FAIL ", nome)


def main() -> int:
    if not database_name(database_url()).endswith(("_test", "_dev")):
        raise SystemExit("serve un database _test/_dev")
    with psycopg.connect(database_url()) as conn:
        wipe(conn)
        seed(conn)
        m = metriche_reali(conn, 120)
        check("consegne reali", m["consegnate"] == 1)
        check("candidature reali", m["candidature"] == 1)
        check("salvataggio reale", m["salvate"] == 1)
        check("nessuno scarto inventato", m["scartate"] == 0)
        check("tasso calcolato sulle consegne", m["tasso_candidature"] == 1.0)
        check("provider reale", m["provider"][0]["model"] == "glm-5.2")
        check("nessun evento apertura inventato", "aperture" not in m)
        wipe(conn)
        m = metriche_reali(conn)
        check("zero consegne non produce divisioni per zero", m["tasso_candidature"] is None)
        check("nessuna riga utente", m["valutate"] == 0)
        try:
            metriche_reali(conn, 0)
        except ValueError:
            check("periodo non valido rifiutato", True)
        else:
            check("periodo non valido rifiutato", False)
        wipe(conn)
    print(f"\n{PASSATI} ok, {FALLITI} falliti")
    return 1 if FALLITI else 0


if __name__ == "__main__":
    raise SystemExit(main())
