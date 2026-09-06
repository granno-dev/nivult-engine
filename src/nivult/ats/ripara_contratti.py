"""Riparazione una tantum del contratto: le offerte attive con
`employment_type` NULL che pero' lo dicono nel grezzo.

Misurato il 2026-09-06 durante l'audit: ~140.000 offerte attive con
«Full time», «FullTime», «Full-time», «Contract», «Part time»,
«permanent_full_time», «fixed_term_contract», «intern»... nel grezzo e
NULL in colonna, perche' la normalizzazione le aveva viste con una
versione che non conosceva quei formati e non le ripassava piu'
(normalized_at pieno). Per l'AF svedese il campo e' un dict con
`label` (Heltid/Deltid/Tidsbegränsad).

Usa contratto.da_raw, la stessa regola che da oggi usa la normalizzazione:
niente doppia verita'. Pagine a chiave, lotti ordinati. Idempotente.
Sull'operaio N5: legge il grezzo, un core, minuti.
"""
from __future__ import annotations

import logging
import time
from collections import Counter

import psycopg

from nivult.ats.contratto import da_raw

log = logging.getLogger("nivult.ats.ripara_contratti")


def ripara(dsn: str, tetto: int = 500000) -> dict:
    st: dict = {"viste": 0, "riempite": 0}
    per_valore: Counter = Counter()
    with psycopg.connect(dsn, autocommit=True) as c:
        ultimo = None
        while st["viste"] < tetto:
            righe = c.execute("""
                SELECT id, raw FROM ats_jobs
                 WHERE expired_at IS NULL AND employment_type IS NULL
                   AND (raw ?| ARRAY['employmentType','employment_type','typeOfEmployment',
                                     'jobType','job_type','timeType','employmentStatusLabel',
                                     'contractType','ContractType','commitment',
                                     'working_hours_type','workingHours'])
                   AND (%s::uuid IS NULL OR id > %s)
                 ORDER BY id LIMIT 500""", (ultimo, ultimo)).fetchall()
            if not righe:
                break
            agg = []
            for jid, raw in righe:
                st["viste"] += 1
                v = da_raw(raw)
                if v:
                    agg.append((v, jid))
                    per_valore[v] += 1
            for k in range(0, len(agg), 300):
                for _ in range(3):
                    try:
                        with c.cursor() as cc:
                            cc.executemany("UPDATE ats_jobs SET employment_type=%s "
                                           "WHERE id=%s AND employment_type IS NULL", agg[k:k+300])
                        break
                    except psycopg.errors.DeadlockDetected:
                        time.sleep(1)
            st["riempite"] += len(agg)
            ultimo = righe[-1][0]
            if st["viste"] % 50000 < 500:
                log.info("ripara contratti: %s", st)
    st["per_valore"] = dict(per_valore)
    log.info("ripara contratti FINE: %s", st)
    return st


def main() -> int:
    import argparse, json
    from .runner import ATS_DSN
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.ripara_contratti")
    ap.add_argument("--tetto", type=int, default=500000)
    a = ap.parse_args()
    print(json.dumps(ripara(ATS_DSN, a.tetto)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
