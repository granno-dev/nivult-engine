"""RIPARARE GLI STAGE INVENTATI (22/09/2026).

Misurato il 21/09 sulle offerte attive: 72k con contratto «internship», 30k
delle quali senza NESSUNA parola da stage nel titolo («Senior Alliance
Manager», «Head of Global Product Quality», «Psychiater»); piu' 97k con
seniority «intern». La piattaforma non lo dichiarava: era il modello (v1 a
soglia 0,5, o GLM prima di lui). Un annuncio di stage lo dice, in qualunque
lingua: dove non lo dice, l'etichetta si toglie e l'offerta torna in coda a
v1, che ora (prova lessicale nel demone) scrive la sua seconda scelta.

Per ogni offerta attiva con employment_type in (internship, apprenticeship)
o seniority = intern:
  - se la piattaforma DICHIARA lo stage (contratto.da_raw sul grezzo): resta;
  - se titolo o testo lo nominano (testo.evidenza_stage): resta;
  - altrimenti: il campo torna NULL, l'offerta entra in ripasso_v1_dal_titolo
    (fase 7) con locale_v1_at azzerato.

Solo CPU su Hetzner: legge il grezzo di ~138k righe a lotti di 500.
Idempotente: ripete solo cio' che e' ancora etichettato cosi'.

    cd /opt/nivult/engine && .venv/bin/python scripts/migrazioni/2026-09-22/ripara_stage.py [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time

import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))
from nivult.ats.contratto import da_raw          # noqa: E402
from nivult.ats.testo import descrizione, evidenza_stage, pulito   # noqa: E402

log = logging.getLogger("ripara_stage")
STAGE = ("internship", "apprenticeship")

SQL_CODA = """
SELECT j.id, j.title, j.raw, j.lang, j.employment_type, j.seniority
  FROM ats_jobs j
 WHERE j.expired_at IS NULL
   AND (j.employment_type IN ('internship', 'apprenticeship') OR j.seniority = 'intern')
   AND j.id > %s
 ORDER BY j.id
 LIMIT %s
"""


def ripara(dsn: str, lotto: int = 500, dry: bool = False) -> dict:
    st = {"viste": 0, "dichiarate": 0, "con_prova": 0, "contratto_tolto": 0, "seniority_tolta": 0, "in_coda": 0}
    ultimo = "00000000-0000-0000-0000-000000000000"
    t0 = time.time()
    with psycopg.connect(dsn, autocommit=True) as c:
        while True:
            righe = c.execute(SQL_CODA, (ultimo, lotto)).fetchall()
            if not righe:
                break
            ultimo = righe[-1][0]
            togli_con, togli_sen, coda = [], [], []
            for jid, titolo, raw, lang, con, sen in righe:
                st["viste"] += 1
                dichiarato = da_raw(raw) if isinstance(raw, dict) else None
                if dichiarato in STAGE:
                    st["dichiarate"] += 1
                    continue
                if evidenza_stage(titolo, pulito(descrizione(raw, massimo=None) or ""), lang):
                    st["con_prova"] += 1
                    continue
                if con in STAGE:
                    togli_con.append(jid)
                if sen == "intern":
                    togli_sen.append(jid)
                coda.append(jid)
            if dry or not coda:
                st["contratto_tolto"] += len(togli_con); st["seniority_tolta"] += len(togli_sen); st["in_coda"] += len(coda)
                continue
            for tentativo in range(10):
                try:
                    with c.transaction():
                        if togli_con:
                            c.execute("UPDATE ats_jobs SET employment_type = NULL WHERE id = ANY(%s::uuid[]) "
                                      "AND employment_type IN ('internship', 'apprenticeship')", (sorted(togli_con),))
                        if togli_sen:
                            c.execute("UPDATE ats_jobs SET seniority = NULL WHERE id = ANY(%s::uuid[]) AND seniority = 'intern'",
                                      (sorted(togli_sen),))
                        c.execute("UPDATE ats_jobs SET locale_v1_at = NULL WHERE id = ANY(%s::uuid[])", (sorted(coda),))
                        c.execute("INSERT INTO ripasso_v1_dal_titolo (job_id, fase) SELECT unnest(%s::uuid[]), 7 "
                                  "ON CONFLICT (job_id) DO UPDATE SET fase = 7", (sorted(coda),))
                    break
                except psycopg.errors.DeadlockDetected:
                    log.warning("deadlock, tentativo %d", tentativo + 1)
                    time.sleep(2 + 3 * tentativo)
            else:
                raise RuntimeError("deadlock persistente")
            st["contratto_tolto"] += len(togli_con); st["seniority_tolta"] += len(togli_sen); st["in_coda"] += len(coda)
            if st["viste"] % 5000 < lotto:
                log.info("ripara_stage: %s in %.0f s", st, time.time() - t0)
    log.info("ripara_stage FINE: %s in %.0f s", st, time.time() - t0)
    return st


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--lotto", type=int, default=500)
    ap.add_argument("--dry-run", action="store_true")
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
    print(ripara(dsn, a.lotto, a.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
