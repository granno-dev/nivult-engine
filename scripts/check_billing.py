#!/usr/bin/env python3
"""Test economici su PostgreSQL reale, senza servizi o pagamenti esterni.

Crea e alla fine ELIMINA esclusivamente ``nivult_fix_billing_test``. Il DSN
amministrativo predefinito e' la Postgres locale; si puo' impostare
BILLING_TEST_ADMIN_URL senza indicare un database diverso da ``postgres``.

    .venv/bin/python scripts/check_billing.py
"""
from __future__ import annotations

import gzip
import hashlib
import os
import sys
import tempfile
import traceback
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
import psycopg  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402

from nivult.api_clienti import billing, chiavi, dati  # noqa: E402

DB_NAME = "nivult_fix_billing_test"
ADMIN_DSN = os.environ.get("BILLING_TEST_ADMIN_URL",
                           "postgresql://127.0.0.1:5432/postgres")
TEST_DSN = make_conninfo(ADMIN_DSN, dbname=DB_NAME)
PASSED = []
FAILED = []
SEQUENZA = 0


def check(label, got, expected=True):
    if got == expected:
        PASSED.append(label)
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label} — atteso {expected!r}, ottenuto {got!r}")


def section(title):
    print(f"\n— {title} —")


def _drop_database():
    info = psycopg.conninfo.conninfo_to_dict(ADMIN_DSN)
    host = str(info.get("host") or "")
    if not (host in ("", "localhost", "127.0.0.1", "::1") or host.startswith("/")):
        raise RuntimeError("il banco billing gira solo su PostgreSQL locale")
    if psycopg.conninfo.conninfo_to_dict(TEST_DSN).get("dbname") != DB_NAME:
        raise RuntimeError("DSN di test non sicuro")
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                     .format(sql.Identifier(DB_NAME)))


def _crea_database():
    if psycopg.conninfo.conninfo_to_dict(TEST_DSN).get("dbname") != DB_NAME:
        raise RuntimeError("DSN di test non sicuro")
    _drop_database()
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_NAME)))
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("""
            CREATE TABLE users (
              id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              email text NOT NULL UNIQUE,
              status text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'paused', 'deleted')),
              crediti_extra integer NOT NULL DEFAULT 0 CHECK (crediti_extra >= 0)
            );
            CREATE TABLE api_chiavi (
              id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
              key_hash text UNIQUE NOT NULL CHECK (key_hash ~ '^[0-9a-f]{64}$'),
              label text NOT NULL,
              created_at timestamptz NOT NULL DEFAULT now(),
              revoked_at timestamptz,
              crediti_mensili integer NOT NULL DEFAULT 10000
                CHECK (crediti_mensili >= 0),
              usati_mese integer NOT NULL DEFAULT 0 CHECK (usati_mese >= 0),
              mese_uso date NOT NULL DEFAULT CURRENT_DATE,
              user_id uuid REFERENCES users(id) ON DELETE SET NULL,
              key_tail text,
              last_used_at timestamptz
            );
            CREATE TABLE portale_uso (
              id bigserial PRIMARY KEY,
              user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              at timestamptz NOT NULL DEFAULT now(),
              via text NOT NULL,
              azione text NOT NULL,
              dettaglio text,
              crediti integer NOT NULL DEFAULT 1
            );
            CREATE TABLE portale_rivelazioni (
              user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              tipo text NOT NULL CHECK (tipo IN ('job', 'azienda')),
              riferimento text NOT NULL,
              revealed_at timestamptz NOT NULL DEFAULT now(),
              PRIMARY KEY (user_id, tipo, riferimento)
            );
            CREATE TABLE creem_eventi (
              id text PRIMARY KEY,
              ricevuto_at timestamptz NOT NULL DEFAULT now()
            );
            CREATE TABLE portale_acquisti (
              id bigserial PRIMARY KEY,
              user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              at timestamptz NOT NULL DEFAULT now(),
              crediti integer NOT NULL,
              importo_cent integer,
              valuta text,
              evento_id text NOT NULL UNIQUE
            );
            """)


def _uno(query, params=()):
    with psycopg.connect(TEST_DSN) as conn:
        return conn.execute(query, params).fetchone()[0]


def _righe(query, params=()):
    with psycopg.connect(TEST_DSN) as conn:
        return conn.execute(query, params).fetchall()


def _esegui(query, params=()):
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(query, params)


def _utente(extra=0, quota=None):
    global SEQUENZA
    SEQUENZA += 1
    with psycopg.connect(TEST_DSN) as conn:
        uid = conn.execute(
            "INSERT INTO users (email, crediti_extra) VALUES (%s, %s) RETURNING id::text",
            (f"billing-{SEQUENZA}@esempio.test", extra)).fetchone()[0]
        if quota is None:
            return uid, None, None
        chiaro = f"nv-test-{SEQUENZA}-{hashlib.sha256(str(SEQUENZA).encode()).hexdigest()[:12]}"
        kid = conn.execute(
            "INSERT INTO api_chiavi (key_hash, label, crediti_mensili, user_id, key_tail) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id::text",
            (hashlib.sha256(chiaro.encode()).hexdigest(), "test", quota, uid,
             chiaro[-6:])).fetchone()[0]
        return uid, chiaro, kid


def _somma_usati(uid):
    return int(_uno("SELECT coalesce(sum(usati_mese), 0) FROM api_chiavi WHERE user_id = %s",
                    (uid,)))


def _extra(uid):
    return int(_uno("SELECT crediti_extra FROM users WHERE id = %s", (uid,)))


def _uso(uid, via=None):
    if via:
        return int(_uno("SELECT count(*) FROM portale_uso WHERE user_id = %s AND via = %s",
                        (uid, via)))
    return int(_uno("SELECT count(*) FROM portale_uso WHERE user_id = %s", (uid,)))


def _paralleli(n, funzione):
    barriera = Barrier(n)

    def uno(i):
        try:
            barriera.wait(timeout=20)
            return funzione(i)
        except Exception as exc:  # noqa: BLE001
            return exc

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(uno, range(n)))


def _evento(eid, tipo, uid=None, volume=100, checkout_id=None, metadata=False):
    checkout_id = checkout_id or "ch_" + eid
    checkout = {"id": checkout_id, "object": "checkout",
                "request_id": "req_" + checkout_id}
    if metadata:
        checkout["metadata"] = {"user_id": uid, "volume": volume}
    if tipo == "checkout.completed":
        checkout["status"] = "completed"
        checkout["metadata"] = {"user_id": uid, "volume": volume}
        checkout["order"] = {"id": "ord_" + checkout_id, "amount": 4900,
                             "currency": "EUR", "status": "paid",
                             "transaction": "tran_" + checkout_id}
        return {"id": eid, "eventType": tipo, "object": checkout}
    if tipo == "refund.created":
        return {"id": eid, "eventType": tipo,
                "object": {"id": "ref_" + eid, "object": "refund",
                           "status": "succeeded", "refund_amount": 4900,
                           "refund_currency": "EUR", "checkout": checkout,
                           "order": {"id": "ord_" + checkout_id},
                           "transaction": {"id": "tran_" + checkout_id,
                                           "order": "ord_" + checkout_id}}}
    return {"id": eid, "eventType": tipo,
            "object": {"id": "disp_" + eid, "object": "dispute",
                       "amount": 4900, "currency": "EUR", "checkout": checkout,
                       "order": {"id": "ord_" + checkout_id},
                       "transaction": {"id": "tran_" + checkout_id,
                                       "order": "ord_" + checkout_id}}}


def test_concorrenza():
    section("lock account, revoche e chiavi interne")
    uid, k1, _ = _utente(quota=12)
    k2, _ = chiavi.nuova("seconda", 999, uid)
    risultati = _paralleli(24, lambda i: chiavi.autentica(k1 if i % 2 else k2))
    check("12 addebiti ammessi fra due chiavi",
          sum(not isinstance(r, Exception) for r in risultati), 12)
    check("12 rifiuti a franchigia finita",
          sum(isinstance(r, chiavi.CreditiEsauriti) for r in risultati), 12)
    check("consumo account esatto", _somma_usati(uid), 12)
    check("una registrazione per addebito", _uso(uid), 12)
    check("saldo mai negativo", chiavi.saldo_per_utente(uid), 0)
    check("la seconda chiave eredita il volume account",
          chiavi.volume_per_utente(uid), 12)
    _esegui("UPDATE users SET crediti_extra = 3 WHERE id = %s", (uid,))
    extra = _paralleli(4, lambda _: chiavi.spendi_per_utente(uid))
    check("franchigia poi extra, senza overspend", sum(r is True for r in extra), 3)
    check("extra consumati fino a zero", _extra(uid), 0)
    check("consumo mensile non include l'extra", _somma_usati(uid), 12)

    uid2, ka, _ = _utente(quota=20)
    kb, _ = chiavi.nuova("b", 20, uid2)
    for _ in range(5):
        chiavi.autentica(ka)
    id_a = _uno("SELECT id::text FROM api_chiavi WHERE key_hash = %s",
                (hashlib.sha256(ka.encode()).hexdigest(),))
    check("revoca effettiva", chiavi.revoca_per_utente(id_a, uid2), True)
    kc, _ = chiavi.nuova("c", 20, uid2)
    check("revoca+ricreazione conserva uso e quota",
          (chiavi.volume_per_utente(uid2), _somma_usati(uid2)), (20, 5))
    dopo = _paralleli(16, lambda i: chiavi.autentica(kb if i % 2 else kc))
    check("solo i 15 crediti rimasti passano",
          sum(not isinstance(r, Exception) for r in dopo), 15)
    check("nessun reset dopo revoca", _somma_usati(uid2), 20)
    _esegui("UPDATE api_chiavi SET crediti_mensili = 5 WHERE user_id = %s", (uid2,))
    _esegui("UPDATE users SET crediti_extra = 2 WHERE id = %s", (uid2,))
    check("downgrade non crea saldo mensile negativo", chiavi.saldo_per_utente(uid2), 2)
    check("dopo downgrade si spende solo extra", chiavi.spendi_per_utente(uid2), True)
    check("extra e saldo restano coerenti", (_extra(uid2), chiavi.saldo_per_utente(uid2)), (1, 1))

    interna, kid = chiavi.nuova("interna", 2)
    check("chiave interna resta NULL",
          _uno("SELECT user_id IS NULL FROM api_chiavi WHERE id = %s", (kid,)), True)
    interni = _paralleli(4, lambda _: chiavi.autentica(interna))
    check("chiave NULL conserva tetto proprio",
          sum(not isinstance(r, Exception) for r in interni), 2)
    check("chiave NULL non scrive uso portale",
          _uno("SELECT count(*) FROM portale_uso WHERE via = 'api' AND user_id IS NULL"), 0)


def test_reveal_export():
    section("reveal ed export atomici")
    uid, _, _ = _utente(quota=10)
    rivelazioni = _paralleli(16, lambda _: chiavi.rivela_per_utente(uid, "job", "job-1"))
    check("N reveal concorrenti: un solo addebito", rivelazioni.count("ok"), 1)
    check("gli altri reveal sono gratuiti", rivelazioni.count("gratis"), 15)
    check("una rivelazione e un uso", (_uno(
        "SELECT count(*) FROM portale_rivelazioni WHERE user_id = %s", (uid,)),
        _uso(uid)), (1, 1))
    chiavi.registra_uso(uid, "portale", "posting unlock", "job-1")
    check("il vecchio chiamante non duplica il registro reveal", _uso(uid), 1)

    uid_sospeso, _, _ = _utente(quota=10)
    _esegui("UPDATE users SET status = 'paused' WHERE id = %s", (uid_sospeso,))
    check("account sospeso non viene confuso con crediti esauriti",
          chiavi.rivela_per_utente(uid_sospeso, "azienda", "paused-company"),
          "account_sospeso")
    check("account sospeso non spende né crea reveal",
          (_somma_usati(uid_sospeso), _uso(uid_sospeso), _uno(
              "SELECT count(*) FROM portale_rivelazioni WHERE user_id = %s", (uid_sospeso,))),
          (0, 0, 0))

    uid, _, _ = _utente(extra=4, quota=10)
    export = _paralleli(4, lambda i: chiavi.spendi_n_per_utente(
        uid, 4, dettaglio=f"17 righe reali · export {i}"))
    check("export concorrenti tutto-o-niente", export.count(True), 3)
    check("un export rifiutato senza addebito", export.count(False), 1)
    check("export consuma franchigia ed extra", (_somma_usati(uid), _extra(uid)), (10, 2))
    check("registro export uguale agli addebiti",
          _uno("SELECT sum(crediti) FROM portale_uso WHERE user_id = %s", (uid,)), 12)

    uid, _, _ = _utente(quota=20)
    stima, righe_reali = 95, 17
    costo_stimato = -(-stima // 10)
    costo_reale = -(-righe_reali // 10)
    check("la stima non e' il costo addebitato", (costo_stimato, costo_reale), (10, 2))
    check("addebito sulle righe effettive",
          chiavi.spendi_n_per_utente(uid, costo_reale, azione="filtered export (postings)",
                                     dettaglio=f"{righe_reali} rows"), True)
    check("contatore e registro usano il costo reale",
          (_somma_usati(uid), _uno("SELECT crediti FROM portale_uso WHERE user_id = %s", (uid,))),
          (2, 2))
    check("zero righe non addebita", chiavi.spendi_n_per_utente(uid, 0), True)
    check("zero righe non aggiunge registro", _uso(uid), 1)


def test_webhook():
    section("webhook, rimborsi e dispute documentate")
    uid, _, _ = _utente(extra=20)
    evento = _evento("evt-concorrente", "checkout.completed", uid)
    risultati = _paralleli(12, lambda _: billing.applica_pagamento(TEST_DSN, evento))
    check("un solo accredito per evento", risultati.count("accreditato (1 utente)"), 1)
    check("le altre consegne sono duplicati", risultati.count("duplicato"), 11)
    check("saldo accreditato una volta", _extra(uid), 120)
    check("evento, acquisto e audit unici", (
        _uno("SELECT count(*) FROM creem_eventi WHERE id = %s", (evento["id"],)),
        _uno("SELECT count(*) FROM portale_acquisti WHERE evento_id = %s", (evento["id"],)),
        _uso(uid, "billing")), (1, 1, 1))

    uid_assente = "00000000-0000-0000-0000-000000000001"
    try:
        billing.applica_pagamento(TEST_DSN, _evento(
            "evt-assente", "checkout.completed", uid_assente))
    except HTTPException as exc:
        check("utente assente -> 422, mai accreditato zero", exc.status_code, 422)
    check("evento non applicabile annullato",
          _uno("SELECT count(*) FROM creem_eventi WHERE id = 'evt-assente'"), 0)
    malformato = _evento("evt-volume", "checkout.completed", uid)
    malformato["object"]["metadata"]["volume"] = "non-un-numero"
    try:
        billing.applica_pagamento(TEST_DSN, malformato)
    except HTTPException as exc:
        check("volume malformato -> 422", exc.status_code, 422)

    uid, _, _ = _utente(quota=0)
    pagamento = _evento("evt-rimborso-base", "checkout.completed", uid)
    billing.applica_pagamento(TEST_DSN, pagamento)
    chiavi.spendi_n_per_utente(uid, 30)
    rimborso = _evento("evt-rimborso", "refund.created", uid,
                       checkout_id=pagamento["object"]["id"])
    esito = billing.applica_pagamento(TEST_DSN, rimborso)
    check("refund.created revoca solo i 70 non spesi",
          (esito, _extra(uid)), ("rimborso applicato (70 crediti revocati)", 0))
    check("rimborso non cancella lo storico",
          _righe("SELECT crediti FROM portale_acquisti WHERE user_id = %s ORDER BY id", (uid,)),
          [(100,), (-70,)])
    check("retry rimborso e' duplicato",
          billing.applica_pagamento(TEST_DSN, rimborso), "duplicato")
    altro = dict(rimborso)
    altro["id"] = "evt-rimborso-successivo"
    billing.applica_pagamento(TEST_DSN, altro)
    check("altro storno sullo stesso acquisto non rende negativo il saldo", _extra(uid), 0)

    uid, chiave, _ = _utente(quota=0)
    pagamento = _evento("evt-disputa-base", "checkout.completed", uid)
    billing.applica_pagamento(TEST_DSN, pagamento)
    chiavi.spendi_n_per_utente(uid, 40)
    disputa = _evento("evt-disputa", "dispute.created", uid,
                      checkout_id=pagamento["object"]["id"])
    esito = billing.applica_pagamento(TEST_DSN, disputa)
    check("dispute.created senza metadata usa il request/audit originale",
          esito, "disputa applicata (60 crediti revocati; account sospeso)")
    check("disputa sospende l'account e non va sotto zero",
          (_extra(uid), _uno("SELECT status FROM users WHERE id = %s", (uid,))), (0, "paused"))
    try:
        chiavi.autentica(chiave)
    except chiavi.ChiaveInvalida:
        check("API sospesa durante la disputa", True)
    else:
        check("API sospesa durante la disputa", False)
    ignoto = _evento("evt-disputa-ignota", "dispute.created", uid,
                     checkout_id="ch-mai-visto")
    try:
        billing.applica_pagamento(TEST_DSN, ignoto)
    except HTTPException as exc:
        check("disputa non correlabile -> 422 per riconciliazione", exc.status_code, 422)


def test_checkout_senza_rete():
    section("checkout normalizzato, HTTP interamente finto")
    os.environ["CREEM_API_KEY"] = "test-finto"
    os.environ["CREEM_PRODOTTO"] = "prod_finto"
    try:
        billing.checkout("uid", "a@esempio.test", "volume", "https://ok", "https://no")
    except HTTPException as exc:
        check("volume checkout invalido -> 400", exc.status_code, 400)
    originale = billing.httpx.post

    def irraggiungibile(*args, **kwargs):
        raise httpx.ConnectError("rete finta spenta")

    billing.httpx.post = irraggiungibile
    try:
        billing.checkout("uid", "a@esempio.test", 100, "https://ok", "https://no")
    except billing.BillingNonPronto:
        check("malfunzionamento rete -> BillingNonPronto/503 al router", True)
    else:
        check("malfunzionamento rete -> BillingNonPronto/503 al router", False)

    corpo = {}

    class Risposta:
        status_code = 200
        text = "{}"

        def json(self):
            return {"checkout_url": "https://checkout.esempio.test/sessione"}

    def finto(url, **kwargs):
        corpo.update(kwargs["json"])
        return Risposta()

    billing.httpx.post = finto
    try:
        url = billing.checkout("uid", "a@esempio.test", 100,
                               "https://ok", "https://annulla")
        check("checkout finto riuscito senza chiamate reali",
              url, "https://checkout.esempio.test/sessione")
        check("request_id e success_url documentati",
              (corpo["request_id"].startswith("nivult-crediti-"),
               corpo["success_url"], corpo["metadata"]["volume"],
               "cancel_url" in corpo),
              (True, "https://ok", 100, False))
    finally:
        billing.httpx.post = originale


def test_v1_reale():
    section("soglie HTTP, 404 e RapidAPI sul contatore reale")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from nivult.api_clienti import v1

    with tempfile.TemporaryDirectory(prefix="nivult-billing-http-") as tmp:
        percorso = Path(tmp) / "offerte.jsonl.gz"
        with gzip.open(percorso, "wt") as f:
            f.write('{"id":"uno"}\n')
        finto = types.SimpleNamespace(
            cambiamenti=lambda since, cursor, limit: ([{"id": "delta"}], None),
            stato_export=lambda: {"data": "2026-10-24", "righe": {"offerte": 1},
                                  "file": {"offerte": str(percorso),
                                           "aziende": str(Path(tmp) / "manca.gz")}},
            copertura=lambda: {"ok": True},
            stream_export_senza_pii=dati.stream_export_senza_pii)
        v1.dati = finto
        v1.vivo = types.SimpleNamespace(offerte=lambda filtri, cursor, limit: ([], None))
        app = FastAPI()
        app.include_router(v1.router)
        client = TestClient(app)
        for quota, atteso in ((499_999, 403), (500_000, 200)):
            uid, chiave, _ = _utente(extra=1_000_000, quota=quota)
            r = client.get("/v1/exports/latest/offerte", headers={"X-Api-Key": chiave})
            check(f"bulk {quota}: HTTP {atteso}", r.status_code, atteso)
            check(f"bulk {quota}: addebiti solo se ammesso", _somma_usati(uid),
                  1 if atteso == 200 else 0)
            check(f"bulk {quota}: extra non alza il piano e resta invariato", _extra(uid),
                  1_000_000)
        for quota, atteso in ((199_999, 403), (200_000, 200)):
            uid, chiave, _ = _utente(extra=1_000_000, quota=quota)
            r = client.get("/v1/changes", params={"since": "2026-10-01T00:00:00Z"},
                           headers={"X-Api-Key": chiave})
            check(f"delta {quota}: HTTP {atteso}", r.status_code, atteso)
            check(f"delta {quota}: addebiti solo se ammesso", _somma_usati(uid),
                  1 if atteso == 200 else 0)
        uid, chiave, _ = _utente(quota=500_000)
        r = client.get("/v1/exports/latest/aziende", headers={"X-Api-Key": chiave})
        check("file mancante -> 404 senza consumo reale",
              (r.status_code, _somma_usati(uid)), (404, 0))
        originale = chiavi._addebita_chiave_db

        def guasto(*args):
            raise psycopg.OperationalError("fault contatore")

        chiavi._addebita_chiave_db = guasto
        try:
            r = client.get("/v1/coverage", headers={"X-Api-Key": chiave})
        finally:
            chiavi._addebita_chiave_db = originale
        check("errore contatore -> 503 prima dei dati",
              (r.status_code, _somma_usati(uid)), (503, 0))
        os.environ["RAPIDAPI_PROXY_SECRET"] = "rapid-finto"
        headers = {"X-RapidAPI-Proxy-Secret": "rapid-finto"}
        prima = _uno("SELECT coalesce(sum(usati_mese), 0) FROM api_chiavi")
        r = client.get("/v1/changes", params={"since": "2026-10-01T00:00:00Z"}, headers=headers)
        check("RapidAPI delta invariato", r.status_code, 200)
        r = client.get("/v1/exports/latest/offerte", headers=headers)
        check("RapidAPI bulk ancora 403", r.status_code, 403)
        check("RapidAPI non tocca i contatori",
              _uno("SELECT coalesce(sum(usati_mese), 0) FROM api_chiavi"), prima)


class _Iniettore:
    def __init__(self, target=None):
        self.target = target
        self.count = 0
        self.statements = []

    def dopo(self, query):
        self.count += 1
        self.statements.append(" ".join(str(query).split())[:180])
        if self.count == self.target:
            raise psycopg.OperationalError(f"fault dopo statement {self.count}")


class _CursorFinto:
    def __init__(self, cursor, iniettore):
        self._cursor = cursor
        self._iniettore = iniettore

    def execute(self, query, params=None):
        risultato = self._cursor.execute(query, params)
        self._iniettore.dopo(query)
        return risultato

    def __getattr__(self, nome):
        return getattr(self._cursor, nome)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *args):
        return self._cursor.__exit__(*args)


class _ConnFinta:
    def __init__(self, conn, iniettore):
        self._conn = conn
        self._iniettore = iniettore

    def cursor(self, *args, **kwargs):
        return _CursorFinto(self._conn.cursor(*args, **kwargs), self._iniettore)

    def __getattr__(self, nome):
        return getattr(self._conn, nome)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *args):
        return self._conn.__exit__(*args)


def _installa(modulo, iniettore):
    originale = modulo.psycopg

    def connect(*args, **kwargs):
        return _ConnFinta(psycopg.connect(*args, **kwargs), iniettore)

    modulo.psycopg = types.SimpleNamespace(
        connect=connect, Error=psycopg.Error, errors=psycopg.errors)
    return originale


def _fault_suite(label, modulo, setup, operazione, rollback_ok, commit_ok):
    contesto = setup()
    iniettore = _Iniettore()
    originale = _installa(modulo, iniettore)
    try:
        operazione(contesto)
    finally:
        modulo.psycopg = originale
    check(f"{label}: baseline committata", commit_ok(contesto), True)
    totale = iniettore.count
    check(f"{label}: trovati statement SQL da iniettare", totale > 0, True)
    for target in range(1, totale + 1):
        contesto = setup()
        iniettore = _Iniettore(target)
        originale = _installa(modulo, iniettore)
        try:
            try:
                operazione(contesto)
            except HTTPException as exc:
                riuscito = exc.status_code == 503
            except chiavi.ContatoreNonDisponibile:
                riuscito = True
            else:
                riuscito = False
        finally:
            modulo.psycopg = originale
        rollback = rollback_ok(contesto)
        iniettore = _Iniettore()
        originale = _installa(modulo, iniettore)
        try:
            operazione(contesto)
        finally:
            modulo.psycopg = originale
        check(f"{label}: fault {target}/{totale}, rollback + un retry",
              (riuscito, rollback, commit_ok(contesto)), (True, True, True))


def test_fault_injection():
    section("fault injection dopo ogni statement SQL")

    def setup_spend():
        uid, _, _ = _utente(extra=5, quota=4)
        return {"uid": uid}

    _fault_suite(
        "export", chiavi, setup_spend,
        lambda c: chiavi.spendi_n_per_utente(c["uid"], 6, dettaglio="60 righe reali"),
        lambda c: (_somma_usati(c["uid"]), _extra(c["uid"]), _uso(c["uid"])) == (0, 5, 0),
        lambda c: (_somma_usati(c["uid"]), _extra(c["uid"]), _uso(c["uid"])) == (4, 3, 1))

    def setup_reveal():
        uid, _, _ = _utente(quota=4)
        return {"uid": uid}

    _fault_suite(
        "reveal", chiavi, setup_reveal,
        lambda c: chiavi.rivela_per_utente(c["uid"], "job", "fault-job"),
        lambda c: (_somma_usati(c["uid"]), _uso(c["uid"]), _uno(
            "SELECT count(*) FROM portale_rivelazioni WHERE user_id = %s", (c["uid"],)))
        == (0, 0, 0),
        lambda c: (_somma_usati(c["uid"]), _uso(c["uid"]), _uno(
            "SELECT count(*) FROM portale_rivelazioni WHERE user_id = %s", (c["uid"],)))
        == (1, 1, 1))

    def setup_api():
        uid, _, kid = _utente(quota=4)
        return {"uid": uid, "kid": kid}

    _fault_suite(
        "addebito API", chiavi, setup_api,
        lambda c: chiavi.addebita_chiave(c["kid"], azione="/v1/jobs"),
        lambda c: (_somma_usati(c["uid"]), _uso(c["uid"])) == (0, 0),
        lambda c: (_somma_usati(c["uid"]), _uso(c["uid"])) == (1, 1))

    def setup_webhook():
        global SEQUENZA
        SEQUENZA += 1
        uid, _, _ = _utente(extra=7)
        return {"uid": uid, "evento": _evento(
            f"evt-fault-{SEQUENZA}", "checkout.completed", uid)}

    _fault_suite(
        "webhook", billing, setup_webhook,
        lambda c: billing.applica_pagamento(TEST_DSN, c["evento"]),
        lambda c: (_extra(c["uid"]), _uso(c["uid"], "billing"), _uno(
            "SELECT count(*) FROM creem_eventi WHERE id = %s", (c["evento"]["id"],)),
            _uno("SELECT count(*) FROM portale_acquisti WHERE user_id = %s", (c["uid"],)))
        == (7, 0, 0, 0),
        lambda c: (_extra(c["uid"]), _uso(c["uid"], "billing"), _uno(
            "SELECT count(*) FROM creem_eventi WHERE id = %s", (c["evento"]["id"],)),
            _uno("SELECT count(*) FROM portale_acquisti WHERE user_id = %s", (c["uid"],)))
        == (107, 1, 1, 1))


def main():
    print(f"verifica billing su database isolato {DB_NAME}; nessuna chiamata esterna")
    os.environ["DATABASE_URL"] = TEST_DSN
    chiavi._URL = TEST_DSN
    try:
        _crea_database()
        test_concorrenza()
        test_reveal_export()
        test_webhook()
        test_checkout_senza_rete()
        test_v1_reale()
        test_fault_injection()
    except Exception:  # noqa: BLE001
        FAILED.append("eccezione non intercettata")
        traceback.print_exc()
    finally:
        try:
            _drop_database()
            print(f"\ndatabase {DB_NAME} eliminato")
        except Exception as exc:  # noqa: BLE001
            FAILED.append("cleanup database")
            print(f"FAIL cleanup database: {exc}")
    print(f"\n{len(PASSED)} controlli passati, {len(FAILED)} falliti")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
