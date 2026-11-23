#!/usr/bin/env python3
"""Test pooling API su PostgreSQL locale isolato; nessun servizio esterno."""
from __future__ import annotations

import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import check_billing as banco

TEST_DSN = banco.TEST_DSN
os.environ["DATABASE_URL"] = TEST_DSN
os.environ["API_LETTURA_DSN"] = TEST_DSN
os.environ["API_OPENSEARCH_JOBS"] = "1"

import psycopg
import psycopg_pool
from fastapi.testclient import TestClient

from nivult.api.app import create_app
from nivult.api_clienti import chiavi, opensearch, v1, vivo


def test_recovery() -> None:
    uid, chiave, kid = banco._utente(quota=5)
    pool = psycopg_pool.ConnectionPool(
        TEST_DSN, open=False, min_size=1, max_size=2,
        kwargs={"connect_timeout": 2,
                "options": "-c statement_timeout=5000 "
                           "-c idle_in_transaction_session_timeout=10000"},
        timeout=2, max_lifetime=1800, max_idle=300,
        check=psycopg_pool.ConnectionPool.check_connection)
    try:
        pool.open(wait=True, timeout=5)
        chiavi.configura_pool(pool)
        banco.check("prima lettura sul pool", chiavi.verifica(chiave)["id"], kid)
        with pool.connection() as conn:
            pid = conn.info.backend_pid
        with psycopg.connect(TEST_DSN, autocommit=True) as admin:
            terminato = admin.execute(
                "SELECT pg_terminate_backend(%s)", (pid,)).fetchone()[0]
        banco.check("backend terminato dal test", terminato, True)
        time.sleep(0.2)
        banco.check("recovery dopo terminazione", chiavi.verifica(chiave)["id"], kid)
        with pool.connection() as conn:
            banco.check("il recovery usa un backend nuovo", conn.info.backend_pid != pid,
                        True)
        banco.check("account invariato dopo recovery", chiavi.verifica(chiave)["user_id"],
                    uid)
    finally:
        chiavi.configura_pool(None)
        pool.close()


def test_lifespan_e_http() -> None:
    app = create_app()
    with TestClient(app) as client:
        account_pool = app.state.account_pool
        lettura_pool = app.state.lettura_pool
        http_client = app.state.opensearch_client
        banco.check("lifespan apre account pool", account_pool.closed, False)
        banco.check("lifespan apre lettura pool", lettura_pool.closed, False)
        banco.check("lifespan apre client HTTP", http_client.is_closed, False)
        banco.check("root durante lifespan", client.get("/").status_code, 200)
        with opensearch._http_client() as primo, opensearch._http_client() as secondo:
            banco.check("client OpenSearch condiviso", primo is secondo, True)
            banco.check("client OpenSearch e' quello del lifespan", primo is http_client,
                        True)

        uid, chiave, _ = banco._utente(quota=15)
        originale = v1.opensearch.offerte
        disponibili = []

        def finto_opensearch(filtri, cursore, limite):
            disponibili.append(account_pool.get_stats()["pool_available"])
            return [{"id": "job-test"}], None

        v1.opensearch.offerte = finto_opensearch
        try:
            r = client.get("/v1/jobs?limit=1", headers={"X-Api-Key": chiave})
            banco.check("jobs con pool attivo", r.status_code, 200)
            banco.check("auth rilasciata prima di OpenSearch",
                        disponibili and disponibili[0] >= 2, True)

            risultati = banco._paralleli(
                24, lambda i: chiavi.autentica(chiave))
            ammessi = sum(not isinstance(x, Exception) for x in risultati)
            esauriti = sum(isinstance(x, chiavi.CreditiEsauriti) for x in risultati)
            banco.check("14 crediti residui ammessi sotto concorrenza", ammessi, 14)
            banco.check("10 richieste oltre quota rifiutate", esauriti, 10)
            banco.check("nessun overspend con pool", banco._somma_usati(uid), 15)
            banco.check("un evento per addebito con pool", banco._uso(uid), 15)

            uid_lock, chiave_lock, _ = banco._utente(quota=5)
            with psycopg.connect(TEST_DSN) as blocco:
                blocco.execute("SELECT id FROM users WHERE id = %s FOR UPDATE",
                               (uid_lock,))
                r = client.get("/v1/jobs?limit=1",
                               headers={"X-Api-Key": chiave_lock})
                blocco.rollback()
            banco.check("LockNotAvailable diventa HTTP 503", r.status_code, 503)
            banco.check("lock timeout non addebita", banco._somma_usati(uid_lock), 0)

            with ExitStack() as stack:
                for _ in range(8):
                    conn = stack.enter_context(account_pool.connection())
                    stack.enter_context(conn.transaction())
                r = client.get("/v1/jobs?limit=1",
                               headers={"X-Api-Key": chiave_lock})
            banco.check("PoolTimeout diventa HTTP 503", r.status_code, 503)
            banco.check("pool timeout non addebita", banco._somma_usati(uid_lock), 0)
        finally:
            v1.opensearch.offerte = originale

    banco.check("shutdown chiude account pool", account_pool.closed, True)
    banco.check("shutdown chiude lettura pool", lettura_pool.closed, True)
    banco.check("shutdown chiude client HTTP", http_client.is_closed, True)
    banco.check("shutdown scollega account pool", chiavi._pool is None, True)
    banco.check("shutdown scollega lettura pool", vivo._pool is None, True)
    banco.check("shutdown scollega client OpenSearch", opensearch._client is None, True)
    banco.check("fallback diretto dopo lifespan", chiavi.verifica(chiave)["user_id"], uid)
    with opensearch._http_client() as temporaneo:
        banco.check("fallback HTTP fuori lifespan", temporaneo is not http_client, True)
    banco.check("fallback HTTP chiuso dopo uso", temporaneo.is_closed, True)


def main() -> int:
    print(f"verifica pooling su database isolato {banco.DB_NAME}; nessuna chiamata esterna")
    chiavi._URL = TEST_DSN
    try:
        banco._crea_database()
        test_recovery()
        test_lifespan_e_http()
    except Exception as exc:  # noqa: BLE001
        banco.FAILED.append(f"eccezione non intercettata: {exc!r}")
        import traceback
        traceback.print_exc()
    finally:
        chiavi.configura_pool(None)
        vivo.configura_pool(None)
        opensearch.configura_client(None)
        try:
            banco._drop_database()
            print(f"database {banco.DB_NAME} eliminato")
        except Exception as exc:  # noqa: BLE001
            banco.FAILED.append(f"cleanup database: {exc}")
    print(f"{len(banco.PASSED)} controlli passati, {len(banco.FAILED)} falliti")
    return 1 if banco.FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
