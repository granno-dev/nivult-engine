#!/usr/bin/env python3
"""Completa la località dei CV attivi senza chiamare modelli esterni.

Senza argomenti esegue soltanto una verifica. Con --apply salva in un'unica
transazione; qualunque CV illeggibile annulla tutte le modifiche.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import psycopg

from nivult import crypto, cv, storage
from nivult.config import database_url, safe_dsn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    aggiornati = 0
    errori = 0
    print(f"database: {safe_dsn(database_url())}")
    with psycopg.connect(database_url()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, storage_key, original_filename, mime_type, encrypted_dek "
            "FROM user_cvs WHERE status = 'active' "
            "  AND location_city IS NULL AND location_country IS NULL "
            "  AND relocation_open IS NULL ORDER BY uploaded_at")
        righe = cur.fetchall()
        print(f"CV attivi da esaminare: {len(righe)}")
        for cv_id, chiave, nome, mime, encrypted_dek in righe:
            try:
                dati = crypto.decifra(storage.leggi(chiave), encrypted_dek)
                testo = cv.estrai_testo(nome or "", mime or "", dati)
                localita = cv.estrai_localita(testo)
            except Exception as exc:  # noqa: BLE001
                errori += 1
                print(f"CV {cv_id}: lettura impossibile ({type(exc).__name__})")
                continue
            if not any(localita.values()):
                continue
            if args.apply:
                cur.execute(
                    "UPDATE user_cvs SET location_city = %s, location_country = %s, "
                    "  relocation_open = %s WHERE id = %s "
                    "  AND location_city IS NULL AND location_country IS NULL "
                    "  AND relocation_open IS NULL",
                    (localita["location_city"], localita["location_country"],
                     localita["relocation_open"], cv_id))
                aggiornati += cur.rowcount
            else:
                aggiornati += 1
        if errori:
            conn.rollback()
            print(f"Errori: {errori}; nessuna modifica salvata")
            return 1
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    print(f"{'Aggiornati' if args.apply else 'Aggiornabili'}: {aggiornati}; errori: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
