"""Le chiavi dell'API clienti /v1: creazione, verifica e crediti.

La chiave in chiaro non si conserva mai: in tabella va lo sha256
esadecimale, come per login_tokens (migrazione 0062). Si stampa UNA VOLTA
SOLA, qui:

    python -m nivult.api_clienti.chiavi nuova --etichetta "cliente X" --crediti 10000
    python -m nivult.api_clienti.chiavi lista
    python -m nivult.api_clienti.chiavi revoca --id <uuid>

DATABASE_URL viene dall'ambiente o da /opt/nivult/.env, come negli altri
moduli (vedi ats/dettagli.py).

Il giro a ogni richiesta (autentica): hash della chiave -> chiave esistente
e non revocata -> crediti del MESE CORRENTE residui -> consumo di un
credito. 401 se manca o non vale, 429 se i crediti sono finiti.

La cache in memoria (TTL 60s) esiste perche' le chiavi sono poche e ogni
richiesta le riverifica: un minuto di ritardo sulla revoca e' il prezzo
accettato per non pagare una query a chiamata. La revoca da QUESTO processo
invalida subito; da un altro processo vale il TTL.

Se l'UPDATE del contatore fallisce la risposta NON si blocca: si logga e
basta. Il dato serve al cliente prima della contabilita' — ma una chiave
senza crediti non passa comunque, perche' il controllo e' sulla lettura.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import secrets
import sys
import time
from datetime import date

import psycopg

log = logging.getLogger("nivult.api_clienti.chiavi")

TTL_CACHE = 60  # secondi: vedi il docstring del modulo


class ChiaveInvalida(Exception):
    """Chiave assente, sconosciuta o revocata: dall'esterno e' sempre un 401."""


class CreditiEsauriti(Exception):
    """Il mese e' finito per questa chiave. Porta i numeri per il 429."""

    def __init__(self, crediti_mensili: int, mese_uso) -> None:
        super().__init__("crediti mensili esauriti")
        self.crediti_mensili = crediti_mensili
        self.mese_uso = mese_uso


# hash -> (scadenza monotonic, record). Record: id, label, crediti_mensili,
# usati_mese, mese_uso. La copia in cache e' quella che autentica incrementa:
# fra un TTL e l'altro e' lei a far scattare il 429, non il database.
_cache: dict[str, tuple[float, dict]] = {}

_URL: str | None = None


def _url() -> str:
    """DATABASE_URL dall'ambiente, o dai .env del server come dettagli.py."""
    global _URL
    if _URL:
        return _URL
    url = os.environ.get("DATABASE_URL")
    if not url:
        for f in ("/opt/nivult/.env", "/opt/nivult/engine/.env"):
            try:
                m = re.search(r"^DATABASE_URL=(.*)$", open(f).read(), re.M)
            except OSError:
                continue
            if m:
                url = m.group(1).strip().strip("'\"")
                break
    if not url:
        raise SystemExit(
            "DATABASE_URL non impostata.\n"
            "  Locale : copia .env.example in .env e compilala\n"
            "  Server : la stringa di connessione sta in /opt/nivult/.env")
    _URL = url
    return url


def _hash(chiave: str) -> str:
    return hashlib.sha256(chiave.encode()).hexdigest()


def _come_data(v) -> date:
    """mese_uso e' una date dal database; dal finto del banco puo' essere str."""
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _leggi(key_hash: str) -> dict | None:
    """La riga della chiave, se esiste ed e' attiva. Unica parte che legge il DB.

    Le revocate non escono proprio: per il chiamante sono indistinguibili
    dalle mai esistite, e dall'esterno non si scopre quali chiavi ci sono.
    """
    with psycopg.connect(_url()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, label, crediti_mensili, usati_mese, mese_uso "
            "FROM api_chiavi WHERE key_hash = %s AND revoked_at IS NULL",
            (key_hash,))
        r = cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "label": r[1], "crediti_mensili": r[2],
            "usati_mese": r[3], "mese_uso": r[4]}


def _consuma(chiave_id: str) -> int | None:
    """Un credito in meno, in un'unica istruzione: la transazione e' la riga.

    Il reset mensile e' dentro lo stesso UPDATE: se mese_uso e' di un mese
    vecchio il contatore riparte da 1, altrimenti sale di 1. E il TETTO e'
    nello stesso UPDATE (26/09/2026): prima si leggeva il contatore dalla
    cache del processo e con N worker il cliente riceveva N x crediti.

    28/09/2026: il tetto per le chiavi del PORTALE e' dell'ACCOUNT, non
    della chiave: la somma degli usati delle chiavi attive dell'utente
    contro il volume massimo. Senza, N chiavi nuove erano N volte i
    crediti gratis del mese. Le chiavi interne (user_id NULL) restano col
    tetto proprio, com'e' sempre stato.
    Torna il conteggio dopo la scrittura, o None se il tetto e' pieno
    (o la chiave revocata — il chiamante distingue).
    """
    with psycopg.connect(_url()) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE api_chiavi k SET "
            "  usati_mese = CASE WHEN k.mese_uso < date_trunc('month', CURRENT_DATE)::date "
            "                    THEN 1 ELSE k.usati_mese + 1 END, "
            "  mese_uso   = date_trunc('month', CURRENT_DATE)::date "
            "WHERE k.id = %s AND k.revoked_at IS NULL "
            "  AND ( "
            "    (k.user_id IS NULL AND (k.mese_uso < date_trunc('month', CURRENT_DATE)::date "
            "                            OR k.usati_mese < k.crediti_mensili)) "
            "    OR (k.user_id IS NOT NULL AND "
            "        (SELECT coalesce(sum(CASE WHEN o.mese_uso < date_trunc('month', CURRENT_DATE)::date "
            "                                  THEN 0 ELSE o.usati_mese END), 0) "
            "           FROM api_chiavi o "
            "          WHERE o.user_id = k.user_id AND o.revoked_at IS NULL) "
            "        < (SELECT max(m.crediti_mensili) FROM api_chiavi m "
            "            WHERE m.user_id = k.user_id AND m.revoked_at IS NULL)) "
            "  ) "
            "RETURNING usati_mese",
            (chiave_id,))
        r = cur.fetchone()
        if r is not None:
            conn.commit()
            return r[0]
        # Tetto mensile pieno: per le chiavi del PORTALE si attinge alla
        # ricarica (users.crediti_extra, 28/09/2026 — i crediti comprati
        # non scadono e si spendono solo a franchigia esaurita). Le
        # chiavi interne (user_id NULL) non entrano nella FROM: per loro
        # None, come e' sempre stato.
        cur.execute(
            "UPDATE users u SET crediti_extra = u.crediti_extra - 1 "
            "FROM api_chiavi k "
            "WHERE k.id = %s AND k.user_id = u.id "
            "  AND k.revoked_at IS NULL AND u.crediti_extra > 0 "
            "RETURNING u.crediti_extra", (chiave_id,))
        r2 = cur.fetchone()
        if r2 is None:
            conn.commit()
            return None
        cur.execute("SELECT usati_mese FROM api_chiavi WHERE id = %s",
                    (chiave_id,))
        dopo = cur.fetchone()
        conn.commit()
        return dopo[0] if dopo else 0


def autentica(chiave: str) -> dict:
    """Verifica la chiave e consuma un credito. -> il record della chiave.

    Alza ChiaveInvalida (401) o CreditiEsauriti (429): la traduzione in HTTP
    sta nel router, questo modulo non sa cosa sia FastAPI.
    """
    h = _hash(chiave)
    voce = _cache.get(h)
    if voce and voce[0] > time.monotonic():
        rec = voce[1]
    else:
        trovato = _leggi(h)
        if trovato is None:
            # I fallimenti non si mettono in cache: una chiave appena creata
            # deve funzionare subito, non fra un TTL.
            raise ChiaveInvalida()
        rec = dict(trovato)
        _cache[h] = (time.monotonic() + TTL_CACHE, rec)

    mese = date.today().replace(day=1)
    if _come_data(rec["mese_uso"]) < mese:
        # Reset mensile: il conteggio letto e' di un mese vecchio. Qui si
        # aggiorna la vista; sul database lo scrive _consuma con lo stesso
        # criterio, cosi' le due non possono divergere.
        rec["usati_mese"] = 0
        rec["mese_uso"] = mese
    try:
        dopo = _consuma(rec["id"])
    except Exception as exc:  # noqa: BLE001
        # La risposta parte lo stesso: il dato viene prima della contabilita'.
        log.warning("conteggio credito fallito per %s: %s", rec.get("id"), exc)
        dopo = rec["usati_mese"] + 1
    if dopo is None:
        # il tetto l'ha detto il DB, atomico: con N worker la risposta e'
        # la stessa per tutti (26/09/2026)
        raise CreditiEsauriti(rec["crediti_mensili"], rec["mese_uso"])
    rec["usati_mese"] = dopo
    return rec


def invalida(chiave_id: str) -> None:
    """Toglie dalla cache la chiave revocata (stesso processo)."""
    for h, (_, rec) in list(_cache.items()):
        if str(rec.get("id")) == str(chiave_id):
            del _cache[h]


def svuota_cache() -> None:
    """Per il banco e per le prove: la cache e' un'ottimizzazione, non stato."""
    _cache.clear()


def trial_per_utente(user_id: str) -> tuple[list[dict], str | None]:
    """Le chiavi dell'utente, e se non ne ha nessuna la trial appena
    creata in chiaro (si vede ORA, mai piu').

    La creazione e' serializzata per utente (28/09/2026): due tab
    aperte sulla stessa registrazione chiedevano /me/chiavi insieme,
    vedevano entrambe l'elenco vuoto e creavano DUE trial — il
    banner mostrava una, l'elenco l'altra. Il lock advisory vale per
    la transazione: la seconda richiesta aspetta la prima e trova la
    chiave gia' fatta."""
    with psycopg.connect(_url()) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"trial-chiave:{user_id}",))
        cur.execute(
            "SELECT id::text, label, created_at, revoked_at, crediti_mensili, "
            "       usati_mese, mese_uso FROM api_chiavi "
            "WHERE user_id = %s ORDER BY created_at", (user_id,))
        righe = cur.fetchall()
        if righe:
            conn.commit()
            return ([{"id": r[0], "etichetta": r[1],
                      "creata_il": r[2].isoformat(),
                      "revocata_il": r[3].isoformat() if r[3] else None,
                      "crediti_mensili": r[4], "usati_mese": r[5],
                      "mese_uso": r[6].isoformat()} for r in righe], None)
        chiave = "nv_" + secrets.token_urlsafe(32)
        cur.execute(
            "INSERT INTO api_chiavi (key_hash, label, crediti_mensili, user_id) "
            "VALUES (%s, %s, %s, %s)",
            (_hash(chiave), "principale (trial)", 1000, user_id))
        conn.commit()
        return ([], chiave)


def nuova(etichetta: str, crediti: int, user_id: str | None = None) -> tuple[str, str]:
    """Crea la chiave e ritorna (chiave in chiaro, id). Si vede ORA, mai piu'.

    Per un utente del portale il volume e' DELL'ACCOUNT: la chiave nuova
    eredita il volume massimo delle chiavi esistenti (28/09/2026 — prima
    ogni chiave nasceva con 1.000 crediti suoi: N chiavi = N volte i
    crediti gratis. Chiuso il giorno in cui l'abbiamo visto)."""
    if user_id:
        esistenti = lista_per_utente(user_id)
        volumi = [r["crediti_mensili"] for r in esistenti if not r["revocata_il"]]
        crediti = max(volumi) if volumi else crediti
    chiave = "nv_" + secrets.token_urlsafe(32)
    with psycopg.connect(_url()) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_chiavi (key_hash, label, crediti_mensili, user_id) "
            "VALUES (%s, %s, %s, %s) RETURNING id::text",
            (_hash(chiave), etichetta, crediti, user_id))
        chiave_id = cur.fetchone()[0]
        conn.commit()
    return chiave, chiave_id


def lista_per_utente(user_id: str) -> list[dict]:
    """Le chiavi del cliente, per la dashboard del portale. Mai la chiave."""
    with psycopg.connect(_url()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, label, created_at, revoked_at, crediti_mensili, "
            "       usati_mese, mese_uso FROM api_chiavi "
            "WHERE user_id = %s ORDER BY created_at", (user_id,))
        return [{"id": r[0], "etichetta": r[1],
                 "creata_il": r[2].isoformat(),
                 "revocata_il": r[3].isoformat() if r[3] else None,
                 "crediti_mensili": r[4], "usati_mese": r[5],
                 "mese_uso": r[6].isoformat()} for r in cur.fetchall()]


def revoca_per_utente(chiave_id: str, user_id: str) -> bool:
    """Revoca logica, solo se la chiave e' di chi chiede."""
    with psycopg.connect(_url()) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "UPDATE api_chiavi SET revoked_at = now() "
                "WHERE id = %s AND user_id = %s AND revoked_at IS NULL",
                (chiave_id, user_id))
        except psycopg.errors.InvalidTextRepresentation:
            return False
        fatta = cur.rowcount > 0
        conn.commit()
    invalida(chiave_id)
    return fatta


def spendi_per_utente(user_id: str) -> bool:
    """Un credito dalla prima chiave attiva dell'utente: il reveal del
    portale paga dallo stesso contatore dell'API (27/09/2026).
    False se non c'e' una chiave attiva o se sono finiti sia la
    franchigia del mese sia la ricarica (il tuffo nell'extra lo fa
    _consuma, 28/09/2026)."""
    attive = [r for r in lista_per_utente(user_id) if not r["revocata_il"]]
    if not attive:
        return False
    return _consuma(attive[0]["id"]) is not None


def rivela_per_utente(user_id: str, tipo: str, riferimento: str) -> str:
    """Il reveal che si ricorda: gia' rivelato -> gratis; nuovo -> un
    credito e la riga che lo ricorda. Torna 'ok', 'gratis' (era gia'
    suo) o 'senza_crediti'."""
    with psycopg.connect(_url()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM portale_rivelazioni "
            "WHERE user_id = %s AND tipo = %s AND riferimento = %s",
            (user_id, tipo, riferimento))
        if cur.fetchone():
            return "gratis"
    if not spendi_per_utente(user_id):
        return "senza_crediti"
    with psycopg.connect(_url()) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO portale_rivelazioni (user_id, tipo, riferimento) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (user_id, tipo, riferimento))
        conn.commit()
    return "ok"


def e_rivelata(user_id: str, tipo: str, riferimento: str) -> bool:
    """Il dettaglio completo lo vede solo chi ha rivelato."""
    with psycopg.connect(_url()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM portale_rivelazioni "
            "WHERE user_id = %s AND tipo = %s AND riferimento = %s",
            (user_id, tipo, riferimento))
        return cur.fetchone() is not None


def conta_rivelazioni(user_id: str) -> int:
    """Quante rivelazioni ha in cassaforte l'utente: il cruscotto lo
    mostra («hai rivelato N aziende») — sono crediti spesi che TORNANO,
    perche' una rivelata non si ripaga mai due volte (28/09/2026)."""
    with psycopg.connect(_url()) as conn:
        return conn.execute(
            "SELECT count(*) FROM portale_rivelazioni WHERE user_id = %s",
            (user_id,)).fetchone()[0]


def rivelate_per_utente(user_id: str, tipo: str,
                        riferimenti: list[str]) -> set:
    """Quali di questi riferimenti sono gia' dell'utente (una query sola:
     il portale la chiama a ogni pagina di risultati)."""
    if not riferimenti:
        return set()
    with psycopg.connect(_url()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT riferimento FROM portale_rivelazioni "
            "WHERE user_id = %s AND tipo = %s AND riferimento = ANY(%s)",
            (user_id, tipo, riferimenti))
        return {r[0] for r in cur.fetchall()}


def spendi_n_per_utente(user_id: str, n: int) -> bool:
    """N crediti in una volta sola, atomici: l'export filtrato costa
    righe/10. Prima si consuma la franchigia del mese, poi la ricarica
    (users.crediti_extra, 28/09/2026). La riga utente presa FOR UPDATE
    e' il semaforo che tiene insieme i due serbatoi: tutto o niente."""
    attive = [r for r in lista_per_utente(user_id) if not r["revocata_il"]]
    if not attive:
        return False
    kid = attive[0]["id"]
    with psycopg.connect(_url()) as conn, conn.cursor() as cur:
        cur.execute("SELECT crediti_extra FROM users WHERE id = %s "
                    "FOR UPDATE", (user_id,))
        ru = cur.fetchone()
        if ru is None:
            conn.rollback()
            return False
        extra = ru[0]
        cur.execute(
            "SELECT coalesce(max(crediti_mensili), 0) FROM api_chiavi "
            "WHERE user_id = %s AND revoked_at IS NULL", (user_id,))
        cap = cur.fetchone()[0]
        cur.execute(
            "SELECT coalesce(sum(CASE WHEN mese_uso < date_trunc('month', CURRENT_DATE)::date "
            "                     THEN 0 ELSE usati_mese END), 0) "
            "FROM api_chiavi WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,))
        usati = cur.fetchone()[0]
        dal_mese = min(n, max(0, cap - usati))
        dall_extra = n - dal_mese
        if dall_extra > extra:
            conn.rollback()
            return False
        if dal_mese:
            cur.execute(
                "UPDATE api_chiavi SET "
                "  usati_mese = CASE WHEN mese_uso < date_trunc('month', CURRENT_DATE)::date "
                "                    THEN %s ELSE usati_mese + %s END, "
                "  mese_uso = date_trunc('month', CURRENT_DATE)::date "
                "WHERE id = %s AND revoked_at IS NULL",
                (dal_mese, dal_mese, kid))
        if dall_extra:
            cur.execute("UPDATE users SET crediti_extra = crediti_extra - %s "
                        "WHERE id = %s", (dall_extra, user_id))
        conn.commit()
        return True


def saldo_per_utente(user_id: str) -> int:
    """Quanti crediti restano all'ACCOUNT: la franchigia del mese non
    consumata PIU' la ricarica (users.crediti_extra, che non scade).
    Il modale dell'export lo mostra prima della spesa (28/09/2026)."""
    with psycopg.connect(_url()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(max(crediti_mensili), 0) "
            "       - coalesce(sum(CASE WHEN mese_uso < date_trunc('month', CURRENT_DATE)::date "
            "                           THEN 0 ELSE usati_mese END), 0) "
            "FROM api_chiavi WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,))
        mensile = max(0, cur.fetchone()[0])
        cur.execute("SELECT crediti_extra FROM users WHERE id = %s",
                    (user_id,))
        r = cur.fetchone()
        return mensile + (r[0] if r else 0)


def extra_per_utente(user_id: str) -> int:
    """La ricarica residua, per la card Credits del portale."""
    with psycopg.connect(_url()) as conn, conn.cursor() as cur:
        cur.execute("SELECT crediti_extra FROM users WHERE id = %s",
                    (user_id,))
        r = cur.fetchone()
        return r[0] if r else 0


def lista() -> list[dict]:
    """Le chiavi che esistono. MAI la chiave ne' il suo hash: non servono."""
    with psycopg.connect(_url()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, label, created_at, revoked_at, crediti_mensili, "
            "       usati_mese, mese_uso FROM api_chiavi ORDER BY created_at")
        return [{"id": r[0], "etichetta": r[1],
                 "creata_il": r[2].isoformat(),
                 "revocata_il": r[3].isoformat() if r[3] else None,
                 "crediti_mensili": r[4], "usati_mese": r[5],
                 "mese_uso": r[6].isoformat()} for r in cur.fetchall()]


def revoca(chiave_id: str) -> bool:
    """Revoca logica: la riga resta come traccia. -> True se esisteva ed era attiva."""
    with psycopg.connect(_url()) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "UPDATE api_chiavi SET revoked_at = now() "
                "WHERE id = %s AND revoked_at IS NULL", (chiave_id,))
        except psycopg.errors.InvalidTextRepresentation:
            # un id che non e' un uuid non esiste, non e' un errore del server
            return False
        fatta = cur.rowcount > 0
        conn.commit()
    invalida(chiave_id)
    return fatta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.api_clienti.chiavi",
                                 description=__doc__)
    sub = ap.add_subparsers(dest="comando", required=True)

    p_nuova = sub.add_parser("nuova", help="crea una chiave e la stampa UNA VOLTA")
    p_nuova.add_argument("--etichetta", required=True, help="per chi e'")
    p_nuova.add_argument("--crediti", type=int, default=10000,
                         help="crediti al mese (default 10000)")

    sub.add_parser("lista", help="le chiavi esistenti (mai le chiavi stesse)")

    p_revoca = sub.add_parser("revoca", help="revoca una chiave per id")
    p_revoca.add_argument("--id", required=True)

    a = ap.parse_args(argv)

    if a.comando == "nuova":
        if a.crediti < 0:
            ap.error("--crediti non puo' essere negativo")
        chiave, chiave_id = nuova(a.etichetta, a.crediti)
        print(f"Chiave creata per «{a.etichetta}» (id {chiave_id}, "
              f"{a.crediti} crediti/mese):\n\n  {chiave}\n\n"
              "Si mostra UNA VOLTA SOLA: in tabella resta solo l'hash sha256.")
        return 0

    if a.comando == "lista":
        righe = lista()
        if not righe:
            print("nessuna chiave.")
            return 0
        for r in righe:
            stato = f"REVOCATA {r['revocata_il']}" if r["revocata_il"] else "attiva"
            print(f"{r['id']}  {stato:<28}  {r['usati_mese']}/{r['crediti_mensili']}"
                  f" nel mese di {r['mese_uso']}  — {r['etichetta']}")
        return 0

    if a.comando == "revoca":
        if revoca(a.id):
            print(f"chiave {a.id} revocata.")
            return 0
        print(f"chiave {a.id} inesistente o gia' revocata.", file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
