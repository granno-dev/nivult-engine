"""Archivio dello storico: le offerte scadute da piu' di 30 giorni
lasciano Postgres e vanno sull'N5, in parquet mensili zstd.

Perche': Hetzner ha 75 GB e il costo non deve salire (decisione del
03/10/2026). Lo storico resta TUTTO — descrizioni incluse — ma compresso
sull'array da 6 TB di casa, leggibile via DuckDB/httpfs quando serve.

Ordine non negoziabile, per mese:
  1. si SCRIVONO i parquet (ats_jobs + le tre tabelle figlie:
     job_classifications, offerte_dettagli, etichette_tec)
  2. si caricano sull'N5 e si verifica la dimensione remota
  3. SOLO ALLORA si cancellano da Postgres, figlie prima dei genitori,
     a lotti da 2.000 — e si misura che i conti tornino.

Se l'N5 non risponde o un conto non torna: niente cancellazioni, il
mese si rifa' domani. --senza-cancellazione scrive e carica soltanto.

Uso:
  python -m nivult.ats.archivio_storico [--senza-cancellazione] [--solo-stima]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import time

import duckdb
import psycopg

log = logging.getLogger("nivult.ats.archivio_storico")

ATS_DSN = os.environ.get(
    "ATS_DATABASE_URL",
    "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")

SOGLIA_GIORNI = 30
# la lettura va a lotti PICCOLI: i raw di settembre portano immagini
# base64 da megabyte a riga, e 2.000 per volta hanno fatto OOM-kill sul
# server (03/10/2026). La cancellazione resta a lotti grandi.
LOTTO_LETTURA = 200
LOTTO = 2000
STAGING = "/mnt/HC_Volume_106941692/storico-staging"
# Sull'N5 (modalita' --su-n5) il parquet si scrive qui: e' il path del
# container mappato su /mnt/cache/appdata/nivult-operaio; un cron dell'host
# lo sposta sull'array (/mnt/user/nivult-archivio/storico). La verifica
# prima della cancellazione e' la RILETTURA del parquet, non l'scp.
STAGING_N5 = "/opt/nivult/storico"
N5 = "root@100.119.200.7"
N5_KEY = "/root/.ssh/id_ed25519_n5"
N5_DIR = "/mnt/user/nivult-archivio/storico"

FIGLIE = ("job_classifications", "offerte_dettagli", "etichette_tec")

# tipi Postgres OID -> DuckDB: l'archivio conserva i tipi, non solo testo
_TIPI = {16: "BOOLEAN", 23: "INTEGER", 20: "BIGINT", 25: "VARCHAR",
         1043: "VARCHAR", 19: "VARCHAR", 700: "FLOAT", 701: "DOUBLE",
         1082: "DATE", 1114: "TIMESTAMP", 1184: "TIMESTAMPTZ",
         2950: "VARCHAR", 114: "VARCHAR", 3802: "VARCHAR",
         1000: "VARCHAR", 1009: "VARCHAR"}


def _valore(v):
    """jsonb/liste/dict -> testo JSON; uuid -> str; il resto com'e'."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v) if v is not None and type(v).__name__ == "UUID" else v


def _mesi_da_archiviare(cur) -> list[tuple[dt.date, int]]:
    return cur.execute("""
        SELECT date_trunc('month', expired_at)::date AS m, count(*)
          FROM ats_jobs
         WHERE expired_at IS NOT NULL AND expired_at < %s
         GROUP BY 1 ORDER BY 1""", (_soglia(),)).fetchall()


def _soglia() -> dt.date:
    return dt.date.today() - dt.timedelta(days=SOGLIA_GIORNI)


def _in_parquet(dsn: str, tabella: str, dove_sql: str, params: tuple,
                percorso: str, su_n5: bool = False) -> int:
    """Una query Postgres -> un parquet zstd, in streaming a lotti."""
    con = duckdb.connect()
    con.execute("SET memory_limit='700MB'")
    tmp = "/opt/nivult/tmp-duckdb" if su_n5 \
        else "/mnt/HC_Volume_106941692/tmp-duckdb"
    os.makedirs(tmp, exist_ok=True)
    con.execute(f"SET temp_directory='{tmp}'")
    scritte = 0
    try:
        with psycopg.connect(dsn) as pg, pg.cursor() as cur:
            cur.execute(f"SELECT * FROM {tabella} WHERE {dove_sql}", params)
            colonne = [d.name for d in cur.description]
            tipi = [_TIPI.get(d.type_code, "VARCHAR") for d in cur.description]
            schema = ", ".join(f'"{c}" {t}' for c, t in zip(colonne, tipi))
            con.execute(f'CREATE TABLE t ({schema})')
            while True:
                lotto = cur.fetchmany(LOTTO_LETTURA)
                if not lotto:
                    break
                con.executemany(
                    f"INSERT INTO t VALUES ({','.join('?' * len(colonne))})",
                    [tuple(_valore(v) for v in r) for r in lotto])
                scritte += len(lotto)
        con.execute(f"COPY t TO '{percorso}' (FORMAT PARQUET, "
                    "COMPRESSION ZSTD)")
        return scritte
    finally:
        con.close()


def _carica_su_n5(percorso: str) -> int:
    """scp sul N5; ritorna la dimensione remota verificata, o 0."""
    nome = os.path.basename(percorso)
    ssh = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30",
           "-o", "StrictHostKeyChecking=no", "-i", N5_KEY, N5]
    subprocess.run(ssh + [f"mkdir -p {N5_DIR}"], check=True)
    subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-i", N5_KEY,
                    "-o", "StrictHostKeyChecking=no", percorso,
                    f"{N5}:{N5_DIR}/{nome}"], check=True)
    out = subprocess.run(ssh + [f"stat -c %s {N5_DIR}/{nome}"],
                         capture_output=True, text=True, check=True)
    return int(out.stdout.strip())


def _verifica_rilettura(percorso: str, attese: int) -> bool:
    """La prova che il parquet e' sano: rileggerlo e contare le righe.
    Sulla modalita' N5 e' LEI il lasciapassare della cancellazione —
    il file sta gia' a casa, non c'e' scp da verificare."""
    con = duckdb.connect()
    try:
        n = con.execute(f"SELECT count(*) FROM '{percorso}'").fetchone()[0]
        return n == attese
    except Exception:                            # noqa: BLE001
        return False
    finally:
        con.close()


def _cancella_mese(conn, mese: dt.date, cancella: bool) -> dict:
    """Figlie prima dei genitori, a lotti: mai un lock lungo."""
    # il mese di cerniera si taglia alla soglia dei 30 giorni: senza, il
    # giro archiviava TUTTE le scadute del mese, anche le fresche
    # (03/10/2026: 1.946 attese, ~1M lette, ore di spill)
    fine = min((mese.replace(day=28) + dt.timedelta(days=4)).replace(day=1),
               _soglia())
    stats = {}
    with conn.cursor() as cur:
        # NIENTE ON COMMIT DROP: si committa a ogni lotto (lock corti) e
        # la tabella deve sopravvivere fino all'ultimo
        cur.execute("""CREATE TEMP TABLE _arch_ids AS
                       SELECT id FROM ats_jobs
                        WHERE expired_at >= %s AND expired_at < %s""",
                    (mese, fine))
        cur.execute("CREATE INDEX ON _arch_ids(id)")
        conn.commit()
        n_genitori = cur.execute("SELECT count(*) FROM _arch_ids"
                                 ).fetchone()[0]
        for tabella in (*FIGLIE, "ats_jobs"):
            if not cancella:
                stats[tabella] = 0
                continue
            tot = 0
            while True:
                if tabella == "ats_jobs":
                    cur.execute("""DELETE FROM ats_jobs WHERE id IN (
                                     SELECT id FROM _arch_ids LIMIT %s)""",
                                (LOTTO,))
                else:
                    cur.execute(f"""DELETE FROM {tabella} WHERE job_id IN (
                                     SELECT id FROM _arch_ids LIMIT %s)""",
                                (LOTTO,))
                n = cur.rowcount
                tot += n
                conn.commit()
                if n < LOTTO:
                    break
            stats[tabella] = tot
        stats["genitori_attesi"] = n_genitori
    return stats


def archivia(dsn: str, cancella: bool, su_n5: bool = False) -> dict:
    esito = {"mesi": [], "saltati": []}
    staging = STAGING_N5 if su_n5 else STAGING
    os.makedirs(staging, exist_ok=True)
    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            mesi = _mesi_da_archiviare(cur)
    if not mesi:
        log.info("niente da archiviare (scadute oltre %d giorni: zero)",
                 SOGLIA_GIORNI)
        return esito
    for mese, attese in mesi:
        t0 = time.time()
        etichetta = mese.strftime("%Y-%m")
        # il mese di cerniera si taglia alla soglia dei 30 giorni: senza,
        # il giro archiviava TUTTE le scadute del mese, anche le fresche
        # (03/10/2026: 1.946 attese, ~1M lette, ore di spill)
        fine = min((mese.replace(day=28) + dt.timedelta(days=4)).replace(day=1),
                   _soglia())
        log.info("── %s: %d righe attese", etichetta, attese)
        file_mese = {}
        conteggi = {}
        try:
            # 1. i parquet: genitori e figlie (per job_id del mese)
            dove_g = "expired_at >= %s AND expired_at < %s"
            p_g = os.path.join(staging, f"ats_jobs-{etichetta}.parquet")
            conteggi["ats_jobs"] = _in_parquet(dsn, "ats_jobs", dove_g,
                                               (mese, fine), p_g, su_n5)
            file_mese["ats_jobs"] = p_g
            for tabella in FIGLIE:
                p_f = os.path.join(staging, f"{tabella}-{etichetta}.parquet")
                conteggi[tabella] = _in_parquet(
                    dsn, tabella,
                    "job_id IN (SELECT id FROM ats_jobs WHERE expired_at >= %s"
                    " AND expired_at < %s)", (mese, fine), p_f, su_n5)
                file_mese[tabella] = p_f
            if conteggi["ats_jobs"] != attese:
                raise ValueError(f"conteggio genitori diverso: "
                                 f"{conteggi['ats_jobs']} != {attese}")
            # 2. la verifica prima di toccare Postgres: sull'N5 e' la
            # rilettura dei parquet; da Hetzner e' l'scp verificato
            for tabella, percorso in file_mese.items():
                if su_n5:
                    if not _verifica_rilettura(percorso, conteggi[tabella]):
                        raise ValueError(f"rilettura fallita: {percorso}")
                else:
                    remota = _carica_su_n5(percorso)
                    locale = os.path.getsize(percorso)
                    if remota != locale:
                        raise ValueError(f"upload {tabella}: remota {remota} "
                                         f"!= locale {locale}")
            # 3. le cancellazioni, contate
            with psycopg.connect(dsn, autocommit=False) as conn:
                stats = _cancella_mese(conn, mese, cancella)
            if cancella and stats.get("ats_jobs") != stats["genitori_attesi"]:
                raise ValueError("cancellate meno righe del previsto: "
                                 f"{stats}")
            # lo staging si pulisce solo nella modalita' Hetzner: in
            # modalita' N5 il parquet E' l'archivio (lo sposta l'host)
            if not su_n5:
                for p in file_mese.values():
                    os.remove(p)
            esito["mesi"].append({"mese": etichetta,
                                  "righe": conteggi["ats_jobs"],
                                  "figlie": {t: conteggi[t] for t in FIGLIE},
                                  "cancellate": stats.get("ats_jobs", 0),
                                  "secondi": int(time.time() - t0)})
            log.info("── %s: archiviato%s (%ds)", etichetta,
                     " e cancellato" if cancella else " (senza cancellare)",
                     time.time() - t0)
        except Exception as e:                      # noqa: BLE001
            log.error("%s: FALLO (%s) — nessuna cancellazione per questo "
                      "mese", etichetta, e)
            esito["saltati"].append(etichetta)
    return esito


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.archivio_storico")
    ap.add_argument("--senza-cancellazione", action="store_true",
                    help="scrive e carica i parquet ma NON cancella da "
                         "Postgres (prova sul campo)")
    ap.add_argument("--su-n5", action="store_true",
                    help="gira nel container dell'N5: scrive in "
                         "/opt/nivult/storico, niente scp, la verifica "
                         "e' la rilettura del parquet")
    ap.add_argument("--solo-stima", action="store_true")
    a = ap.parse_args()
    if a.solo_stima:
        with psycopg.connect(ATS_DSN) as conn, conn.cursor() as cur:
            print(_mesi_da_archiviare(cur))
        return 0
    print(archivia(ATS_DSN, cancella=not a.senza_cancellazione,
                   su_n5=a.su_n5))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
