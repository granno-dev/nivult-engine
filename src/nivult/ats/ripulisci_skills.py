"""Pulizia una tantum delle competenze sporcate dal matcher ESCO.

Misurato il 2026-09-06: «compile airport certification manuals» su 15.344
offerte attive, «dental» la competenza piu' frequente del corpus,
«Georgian» e «Microsoft Access» su un paramedico. Il matcher a
sottostringa su 13.485 etichette ESCO (profilo.deterministico) e' stato
spento; qui si ricalcolano le competenze con il SOLO dizionario di
profilo (_SKILL_RX, preciso e corto) su ogni offerta che le aveva,
ESCLUSE quelle che lo sprint GLM ha gia' riscritto dopo le 13:00 UTC del
06/09 (quelle vengono dal testo e sono migliori).

Da eseguire sull'operaio N5: legge il grezzo di ~390k righe, un core per
qualche decina di minuti. Pagine a chiave, lotti ordinati. Idempotente.
"""
from __future__ import annotations

import logging
import time

import psycopg

from nivult.ats.profilo import deterministico

log = logging.getLogger("nivult.ats.ripulisci_skills")
DA_QUANDO_GLM = "2026-09-06 13:00+00"


def ripulisci(dsn: str, tetto: int = 500000) -> dict:
    st = {"viste": 0, "svuotate": 0, "ridotte": 0, "uguali": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        ultimo = None
        while st["viste"] < tetto:
            righe = c.execute("""
                SELECT id, title, coalesce(location, city, ''), raw, skills
                  FROM ats_jobs
                 WHERE cardinality(skills) > 0
                   AND (sprint_at IS NULL OR sprint_at < %s)
                   AND (%s::uuid IS NULL OR id > %s)
                 ORDER BY id LIMIT 500""", (DA_QUANDO_GLM, ultimo, ultimo)).fetchall()
            if not righe:
                break
            agg = []
            for jid, tit, luo, raw, vecchie in righe:
                st["viste"] += 1
                nuove = deterministico(tit, luo, raw or {})[2] or None
                if nuove == list(vecchie):
                    st["uguali"] += 1
                    continue
                st["svuotate" if not nuove else "ridotte"] += 1
                agg.append((nuove, jid))
            agg.sort(key=lambda r: r[1])
            for k in range(0, len(agg), 300):
                for _ in range(3):
                    try:
                        with c.cursor() as cc:
                            cc.executemany("UPDATE ats_jobs SET skills=%s WHERE id=%s", agg[k:k+300])
                        break
                    except psycopg.errors.DeadlockDetected:
                        time.sleep(1)
            ultimo = righe[-1][0]
            if st["viste"] % 50000 < 500:
                log.info("ripulisci skills: %s", st)
    log.info("ripulisci skills FINE: %s", st)
    return st


def main() -> int:
    import argparse, json
    from .runner import ATS_DSN
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.ripulisci_skills")
    ap.add_argument("--tetto", type=int, default=500000)
    a = ap.parse_args()
    print(json.dumps(ripulisci(ATS_DSN, a.tetto)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
