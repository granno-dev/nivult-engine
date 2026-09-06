"""Ritenzione del grezzo sulle offerte scadute: lo storico resta, il
peso no.

Deciso con Giuseppe il 2026-09-06: lo storico pulito e classificato ha un
valore (anche di rivendita) e NON si cancella. Quello che dopo 60 giorni
dalla scadenza non serve piu' e' il payload grezzo della piattaforma —
HTML, campi doppi, boilerplate — che e' il grosso degli 8 GB di
`ats_jobs` e dei 500 MB al giorno di crescita del backup.

Cosa resta di un'offerta potata: TUTTE le colonne estratte (titolo,
azienda, luogo, paese, date, seniority, contratto, remoto, competenze,
lingue, stipendio, famiglia in job_classifications) e, dentro `raw`,
solo la descrizione in testo — il campo che vale — piu' il marcatore
`potato_at`. Chi legge `raw->>'description'` non si accorge di niente.

Pagine a chiave e lotti ordinati: regola di casa. Idempotente: una riga
potata (raw ? 'potato_at') non si ripassa.
"""
from __future__ import annotations

import logging
import time

import psycopg

log = logging.getLogger("nivult.ats.potatura_raw")
GIORNI = 60


def pota(dsn: str, giorni: int = GIORNI, tetto: int = 200000) -> dict:
    st = {"potate": 0, "byte_prima": 0, "byte_dopo": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        ultimo = None
        while st["potate"] < tetto:
            righe = c.execute("""
                SELECT id, pg_column_size(raw)
                  FROM ats_jobs
                 WHERE expired_at < now() - make_interval(days => %s)
                   AND raw IS NOT NULL AND NOT (raw ? 'potato_at')
                   AND (%s::uuid IS NULL OR id > %s)
                 ORDER BY id LIMIT 300""", (giorni, ultimo, ultimo)).fetchall()
            if not righe:
                break
            ids = [r[0] for r in righe]
            st["byte_prima"] += sum(r[1] or 0 for r in righe)
            for _ in range(3):
                try:
                    c.execute("""
                        UPDATE ats_jobs
                           SET raw = jsonb_strip_nulls(jsonb_build_object(
                                   'description', coalesce(raw->>'description',
                                                           raw->>'descriptionPlain',
                                                           raw->>'descriptionHtml'),
                                   'potato_at', now()::text))
                         WHERE id = ANY(%s)""", (ids,))
                    break
                except psycopg.errors.DeadlockDetected:
                    time.sleep(1)
            st["byte_dopo"] += c.execute(
                "SELECT coalesce(sum(pg_column_size(raw)),0) FROM ats_jobs WHERE id = ANY(%s)",
                (ids,)).fetchone()[0]
            st["potate"] += len(ids)
            ultimo = ids[-1]
    log.info("potatura raw: %s", st)
    return st


def main() -> int:
    import argparse, json
    from .runner import ATS_DSN
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.potatura_raw")
    ap.add_argument("--giorni", type=int, default=GIORNI)
    ap.add_argument("--tetto", type=int, default=200000)
    ap.add_argument("--dry-run", action="store_true", help="conta soltanto")
    a = ap.parse_args()
    if a.dry_run:
        with psycopg.connect(ATS_DSN) as c:
            n, b = c.execute("""SELECT count(*), coalesce(sum(pg_column_size(raw)),0) FROM ats_jobs
                WHERE expired_at < now() - make_interval(days => %s) AND raw IS NOT NULL
                  AND NOT (raw ? 'potato_at')""", (a.giorni,)).fetchone()
            print(json.dumps({"da_potare": n, "byte_grezzo": int(b)}))
        return 0
    st = pota(ATS_DSN, a.giorni, a.tetto)
    print(json.dumps(st))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
