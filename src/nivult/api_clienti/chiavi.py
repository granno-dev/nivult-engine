"""Chiavi API e crediti del portale B2B.

La chiave in chiaro non si conserva mai: in tabella va lo sha256 esadecimale.
Per gli account del portale quota e consumo sono account-level: il volume e'
il massimo di TUTTE le chiavi, revocate comprese, e il consumo del mese e' la
somma di TUTTE le chiavi. Ogni mutazione passa dal lock della riga ``users``,
cosi' creazione, revoca, reveal, export e chiamate API non possono scavallarsi.
Si spende prima la quota mensile e poi ``users.crediti_extra``; addebito
e registro ``portale_uso`` committano insieme. Il reset mensile vale solo
per le subscription Creem attive, anche annuali; il trial non si rinnova.
Le chiavi interne ``NULL`` conservano quota e lock sulla singola chiave.

    python -m nivult.api_clienti.chiavi nuova --etichetta "cliente X" --crediti 10000
    python -m nivult.api_clienti.chiavi lista
    python -m nivult.api_clienti.chiavi revoca --id <uuid>
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import secrets
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import psycopg
import psycopg_pool

log = logging.getLogger("nivult.api_clienti.chiavi")


class ChiaveInvalida(Exception):
    """Chiave assente, revocata o account sospeso: dall'esterno e' un 401."""


class CreditiEsauriti(Exception):
    """Franchigia e ricarica finite; porta i numeri per la risposta 429."""

    def __init__(self, crediti_mensili: int, mese_uso, crediti_extra: int = 0) -> None:
        super().__init__("crediti mensili ed extra esauriti")
        self.crediti_mensili = crediti_mensili
        self.mese_uso = mese_uso
        self.crediti_extra = crediti_extra


class ContatoreNonDisponibile(Exception):
    """Il contatore non ha risposto: il chiamante deve fallire chiuso con 503."""


_URL: str | None = None
_pool: psycopg_pool.ConnectionPool | None = None
_cache: dict[str, tuple[float, dict]] = {}


def configura_pool(pool: psycopg_pool.ConnectionPool | None) -> None:
    global _pool
    _pool = pool


@contextmanager
def connessione():
    if _pool is None:
        with psycopg.connect(_url()) as conn:
            yield conn
        return
    with _pool.connection() as conn, conn.transaction():
        yield conn


def _url() -> str:
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
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _mese() -> date:
    return date.today().replace(day=1)


def _reset_abbonamento(status: str, subscription_id, period_end) -> bool:
    if not subscription_id or status not in ("active", "scheduled_cancel"):
        return False
    if status == "scheduled_cancel" and period_end:
        if isinstance(period_end, str):
            period_end = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
        if period_end.tzinfo is None:
            period_end = period_end.replace(tzinfo=UTC)
        return period_end > datetime.now(UTC)
    return True


def _leggi(key_hash: str) -> dict | None:
    with connessione() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT k.id::text, k.label, k.crediti_mensili, k.usati_mese, "
            "       k.mese_uso, k.user_id::text, "
            "       (SELECT max(k2.crediti_mensili) FROM api_chiavi k2 "
            "        WHERE k2.user_id = k.user_id) AS monthly_tier, "
            "       u.subscription_status, u.subscription_plan, u.subscription_interval, "
            "       u.current_period_end, u.creem_customer_id, u.creem_subscription_id "
            "FROM api_chiavi k LEFT JOIN users u ON u.id = k.user_id "
            "WHERE k.key_hash = %s AND k.revoked_at IS NULL "
            "  AND (k.user_id IS NULL OR u.status = 'active')",
            (key_hash,))
        r = cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "label": r[1], "crediti_mensili": r[2],
            "usati_mese": r[3], "mese_uso": r[4], "user_id": r[5],
            "monthly_tier": int(r[6]) if r[6] is not None else int(r[2]),
            "subscription_status": r[7], "subscription_plan": r[8],
            "subscription_interval": r[9], "current_period_end": r[10],
            "creem_customer_id": r[11], "creem_subscription_id": r[12]}


def verifica(chiave: str) -> dict:
    """Autentica senza spendere; l'addebito resta un passaggio separato."""
    try:
        rec = _leggi(_hash(chiave))
    except (psycopg.errors.LockNotAvailable, psycopg_pool.PoolTimeout) as exc:
        raise ContatoreNonDisponibile("lettura chiave non disponibile") from exc
    except Exception as exc:  # noqa: BLE001
        raise ContatoreNonDisponibile("lettura chiave non disponibile") from exc
    if rec is None:
        raise ChiaveInvalida()
    return rec


def _registra_uso_cur(cur, user_id: str, via: str, azione: str,
                      dettaglio: str | None, crediti: int) -> None:
    cur.execute(
        "INSERT INTO portale_uso (user_id, via, azione, dettaglio, crediti) "
        "VALUES (%s, %s, %s, %s, %s)",
        (user_id, via, azione, (dettaglio or "")[:300], crediti))


def _riepilogo_da_righe(righe: list[dict], extra: int,
                        resetta_mensile: bool = False) -> dict:
    mese = _mese()
    quota = max((int(r["crediti_mensili"]) for r in righe), default=0)
    usati = sum(int(r["usati_mese"]) for r in righe
                if resetta_mensile is False or _come_data(r["mese_uso"]) >= mese)
    mensili = max(0, quota - usati)
    return {"crediti_mensili": quota, "usati": usati,
            "crediti_extra": extra, "residui_mensili": mensili,
            "saldo": mensili + extra, "mese_uso": mese}


def _spendi_account_cur(cur, user_id: str, n: int, via: str, azione: str,
                        dettaglio: str | None, chiave_id: str | None = None,
                        extra: int | None = None,
                        resetta_mensile: bool = False) -> dict | None:
    """Il chiamante tiene gia' il lock FOR UPDATE sulla riga users."""
    cur.execute(
        "SELECT id::text, label, crediti_mensili, usati_mese, mese_uso, revoked_at "
        "FROM api_chiavi WHERE user_id = %s ORDER BY created_at, id FOR UPDATE",
        (user_id,))
    righe = [{"id": r[0], "label": r[1], "crediti_mensili": r[2],
              "usati_mese": r[3], "mese_uso": r[4], "revoked_at": r[5]}
             for r in cur.fetchall()]
    target = None
    if chiave_id is not None:
        target = next((r for r in righe if r["id"] == str(chiave_id)
                       and r["revoked_at"] is None), None)
        if target is None:
            raise ChiaveInvalida()
    else:
        target = next((r for r in righe if r["revoked_at"] is None), None)
        if target is None:
            return None

    if extra is None:
        cur.execute("SELECT crediti_extra FROM users WHERE id = %s", (user_id,))
        ru = cur.fetchone()
        if ru is None:
            return None
        extra = int(ru[0])
    prima = _riepilogo_da_righe(righe, extra, resetta_mensile)
    dal_mese = min(n, prima["residui_mensili"])
    dall_extra = n - dal_mese
    if dall_extra > extra:
        if chiave_id is not None:
            raise CreditiEsauriti(prima["crediti_mensili"], prima["mese_uso"], extra)
        return None

    mese = prima["mese_uso"]
    cur.execute(
        "UPDATE api_chiavi SET "
        "  usati_mese = CASE WHEN mese_uso < %s AND %s::boolean "
        "                 THEN %s ELSE usati_mese + %s END, "
        "  mese_uso = %s, last_used_at = now() "
        "WHERE id = %s AND revoked_at IS NULL",
        (mese, resetta_mensile, dal_mese, dal_mese, mese, target["id"]))
    if cur.rowcount != 1:
        raise ChiaveInvalida()
    if dall_extra:
        cur.execute("UPDATE users SET crediti_extra = crediti_extra - %s "
                    "WHERE id = %s AND crediti_extra >= %s",
                    (dall_extra, user_id, dall_extra))
        if cur.rowcount != 1:
            raise ContatoreNonDisponibile("saldo extra cambiato durante l'addebito")
    _registra_uso_cur(cur, user_id, via, azione, dettaglio, n)

    usati_chiave = (dal_mese if resetta_mensile and _come_data(target["mese_uso"]) < mese
                    else int(target["usati_mese"]) + dal_mese)
    return {"id": target["id"], "label": target["label"], "user_id": user_id,
            "crediti_mensili": prima["crediti_mensili"],
            "usati_mese": prima["usati"] + dal_mese,
            "usati": prima["usati"] + dal_mese,
            "usati_chiave": usati_chiave, "mese_uso": mese,
            "crediti_extra": extra - dall_extra,
            "residui_mensili": max(0, prima["crediti_mensili"]
                                   - prima["usati"] - dal_mese),
            "saldo": max(0, prima["crediti_mensili"] - prima["usati"] - dal_mese)
                     + extra - dall_extra}


def _addebita_chiave_db(chiave_id: str, via: str, azione: str,
                        dettaglio: str | None, crediti: int) -> dict:
    if isinstance(crediti, bool) or not isinstance(crediti, int) or crediti < 0:
        raise ValueError("numero crediti non valido")
    with connessione() as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '2s'")
        cur.execute(
            "SELECT k.user_id::text, u.status, u.crediti_extra, "
            "       u.subscription_status, u.creem_subscription_id, u.current_period_end "
            "FROM api_chiavi k JOIN users u ON u.id = k.user_id "
            "WHERE k.id = %s FOR UPDATE OF u", (chiave_id,))
        r = cur.fetchone()
        if r is not None:
            user_id, status, extra, subscription_status, subscription_id, period_end = r
            if status != "active":
                raise ChiaveInvalida()
            resetta = _reset_abbonamento(subscription_status, subscription_id, period_end)
            rec = _spendi_account_cur(
                cur, user_id, crediti, via, azione, dettaglio, chiave_id,
                extra=extra, resetta_mensile=resetta)
            if rec is None:
                raise CreditiEsauriti(0, _mese())
            return rec

        cur.execute(
            "SELECT id::text, label, crediti_mensili, usati_mese, mese_uso "
            "FROM api_chiavi WHERE id = %s AND user_id IS NULL "
            "  AND revoked_at IS NULL FOR UPDATE", (chiave_id,))
        r = cur.fetchone()
        if r is None:
            raise ChiaveInvalida()
        mese = _mese()
        usati = 0 if _come_data(r[4]) < mese else int(r[3])
        if usati + crediti > int(r[2]):
            raise CreditiEsauriti(int(r[2]), mese)
        cur.execute(
            "UPDATE api_chiavi SET usati_mese = %s, mese_uso = %s, last_used_at = now() "
            "WHERE id = %s RETURNING usati_mese", (usati + crediti, mese, chiave_id))
        dopo = cur.fetchone()[0]
        return {"id": r[0], "label": r[1], "user_id": None,
                "crediti_mensili": int(r[2]), "usati_mese": dopo, "usati": dopo,
                "usati_chiave": dopo, "mese_uso": mese, "crediti_extra": 0,
                "residui_mensili": max(0, int(r[2]) - dopo),
                "saldo": max(0, int(r[2]) - dopo)}


def addebita_chiave(chiave_id: str, via: str = "api", azione: str = "api call",
                    dettaglio: str | None = None, crediti: int = 1) -> dict:
    """Spende N crediti e scrive il registro nello stesso commit."""
    try:
        return _addebita_chiave_db(chiave_id, via, azione, dettaglio, crediti)
    except (ChiaveInvalida, CreditiEsauriti, ContatoreNonDisponibile):
        raise
    except (psycopg.errors.LockNotAvailable, psycopg_pool.PoolTimeout) as exc:
        raise ContatoreNonDisponibile("addebito non disponibile") from exc
    except Exception as exc:  # noqa: BLE001
        raise ContatoreNonDisponibile("addebito non disponibile") from exc


def autentica(chiave: str) -> dict:
    """Compatibilita': autenticazione e un addebito atomico in successione."""
    rec = verifica(chiave)
    return addebita_chiave(str(rec["id"]))


def _consuma(chiave_id: str) -> int | None:
    try:
        return addebita_chiave(chiave_id)["usati_chiave"]
    except (ChiaveInvalida, CreditiEsauriti):
        return None


def registra_uso(user_id: str | None, via: str, azione: str,
                 dettaglio: str | None = None, crediti: int = 1) -> None:
    """Compatibilita' coi vecchi chiamanti: arricchisce, non duplica, l'addebito."""
    if not user_id:
        return
    try:
        with connessione() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM portale_uso WHERE user_id = %s AND via = %s "
                "  AND azione = %s AND coalesce(dettaglio, '') = %s "
                "  AND crediti = %s AND at > now() - interval '10 seconds' "
                "ORDER BY at DESC LIMIT 1",
                (user_id, via, azione, dettaglio or "", crediti))
            if cur.fetchone():
                return
            cur.execute(
                "SELECT id, azione, coalesce(dettaglio, '') FROM portale_uso "
                "WHERE user_id = %s AND via = %s AND crediti = %s "
                "  AND at > now() - interval '10 seconds' ORDER BY id",
                (user_id, via, crediti))
            righe = cur.fetchall()
            pendente = next((r for r in righe if r[1] == "filtered export"
                             or (r[1] == azione and not r[2])), None)
            if pendente:
                cur.execute("UPDATE portale_uso SET azione = %s, dettaglio = %s "
                            "WHERE id = %s",
                            (azione, (dettaglio or "")[:300], pendente[0]))
            elif not righe:
                _registra_uso_cur(cur, user_id, via, azione, dettaglio, crediti)
    except Exception as exc:  # noqa: BLE001
        log.warning("registro uso non aggiornato (%s/%s): %s", via, azione, exc)


def invalida(chiave_id: str) -> None:
    for h, (_, rec) in list(_cache.items()):
        if str(rec.get("id")) == str(chiave_id):
            del _cache[h]


def svuota_cache() -> None:
    _cache.clear()


def _riga_chiave(r, account: dict | None = None) -> dict:
    rec = {"id": r[0], "etichetta": r[1], "creata_il": r[2].isoformat(),
           "revocata_il": r[3].isoformat() if r[3] else None,
           "monthly_credits": r[4], "used": r[5],
           "usage_month": r[6].isoformat(), "coda": r[7],
           "ultimo_uso": r[8].isoformat() if len(r) > 8 and r[8] else None}
    if account is not None:
        rec.update({"account_quota": account["crediti_mensili"],
                    "account_used": account["usati"],
                    "account_balance": account["saldo"],
                    "account_purchased_credits": account["crediti_extra"]})
    return rec


def _riepilogo_cur(cur, user_id: str) -> dict:
    cur.execute("SELECT crediti_extra, subscription_status, creem_subscription_id, "
                "       current_period_end FROM users WHERE id = %s", (user_id,))
    ru = cur.fetchone()
    extra = int(ru[0]) if ru else 0
    resetta = _reset_abbonamento(ru[1], ru[2], ru[3]) if ru else False
    cur.execute(
        "SELECT id::text, label, crediti_mensili, usati_mese, mese_uso, revoked_at "
        "FROM api_chiavi WHERE user_id = %s", (user_id,))
    righe = [{"id": r[0], "label": r[1], "crediti_mensili": r[2],
              "usati_mese": r[3], "mese_uso": r[4], "revoked_at": r[5]}
             for r in cur.fetchall()]
    return _riepilogo_da_righe(righe, extra, resetta)


def riepilogo_per_utente(user_id: str) -> dict:
    with connessione() as conn, conn.cursor() as cur:
        return _riepilogo_cur(cur, user_id)


def _servizio_chiave_db(chiave_id: str) -> dict:
    with connessione() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT k.id::text, k.label, k.crediti_mensili, k.usati_mese, "
            "       k.mese_uso, k.user_id::text, u.status "
            "FROM api_chiavi k LEFT JOIN users u ON u.id = k.user_id "
            "WHERE k.id = %s AND k.revoked_at IS NULL FOR UPDATE OF k",
            (chiave_id,))
        r = cur.fetchone()
        if r is None or (r[5] and r[6] != "active"):
            raise ChiaveInvalida()
        cur.execute(
            "UPDATE api_chiavi SET last_used_at = now() "
            "WHERE id = %s AND revoked_at IS NULL", (chiave_id,))
        if cur.rowcount != 1:
            raise ChiaveInvalida()
        usati_chiave = 0 if _come_data(r[4]) < _mese() else int(r[3])
        if r[5]:
            rec = _riepilogo_cur(cur, r[5])
        else:
            quota = int(r[2])
            rec = {"crediti_mensili": quota, "usati": usati_chiave,
                   "crediti_extra": 0,
                   "residui_mensili": max(0, quota - usati_chiave),
                   "saldo": max(0, quota - usati_chiave), "mese_uso": _mese()}
        return {**rec, "id": r[0], "label": r[1], "user_id": r[5],
                "usati_chiave": usati_chiave}


def registra_servizio_chiave(chiave_id: str) -> dict:
    """Aggiorna solo last_used e legge il riepilogo, senza addebiti o ledger."""
    try:
        return _servizio_chiave_db(chiave_id)
    except (ChiaveInvalida, ContatoreNonDisponibile):
        raise
    except (psycopg.errors.LockNotAvailable, psycopg_pool.PoolTimeout) as exc:
        raise ContatoreNonDisponibile("lettura servizio non disponibile") from exc
    except Exception as exc:  # noqa: BLE001
        raise ContatoreNonDisponibile("lettura servizio non disponibile") from exc


def volume_per_utente(user_id: str) -> int:
    """Volume mensile dell'account; le ricariche una tantum non lo aumentano."""
    return int(riepilogo_per_utente(user_id)["crediti_mensili"])


def trial_per_utente(user_id: str) -> tuple[list[dict], str | None]:
    with connessione() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (user_id,))
        if cur.fetchone() is None:
            return [], None
        cur.execute(
            "SELECT id::text, label, created_at, revoked_at, crediti_mensili, "
            "       usati_mese, mese_uso, key_tail, last_used_at FROM api_chiavi "
            "WHERE user_id = %s ORDER BY created_at", (user_id,))
        righe = cur.fetchall()
        account = _riepilogo_cur(cur, user_id)
        if righe:
            return [_riga_chiave(r, account) for r in righe], None
        chiave = "nv_" + secrets.token_urlsafe(32)
        cur.execute(
            "INSERT INTO api_chiavi (key_hash, label, crediti_mensili, user_id, "
            "  key_tail) VALUES (%s, %s, %s, %s, %s)",
            (_hash(chiave), "Main key (trial)", 500, user_id, chiave[-6:]))
        return [], chiave


def nuova(etichetta: str, crediti: int, user_id: str | None = None) -> tuple[str, str]:
    if crediti < 0:
        raise ValueError("crediti negativi")
    chiave = "nv_" + secrets.token_urlsafe(32)
    with connessione() as conn, conn.cursor() as cur:
        if user_id:
            cur.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (user_id,))
            if cur.fetchone() is None:
                raise ValueError("utente inesistente")
            cur.execute("SELECT coalesce(max(crediti_mensili), 0) FROM api_chiavi "
                        "WHERE user_id = %s", (user_id,))
            esistente = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM api_chiavi WHERE user_id = %s", (user_id,))
            if cur.fetchone()[0]:
                crediti = int(esistente)
        cur.execute(
            "INSERT INTO api_chiavi (key_hash, label, crediti_mensili, user_id, "
            "  key_tail) VALUES (%s, %s, %s, %s, %s) RETURNING id::text",
            (_hash(chiave), etichetta, crediti, user_id, chiave[-6:]))
        chiave_id = cur.fetchone()[0]
    return chiave, chiave_id


def lista_per_utente(user_id: str) -> list[dict]:
    with connessione() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, label, created_at, revoked_at, crediti_mensili, "
            "       usati_mese, mese_uso, key_tail, last_used_at FROM api_chiavi "
            "WHERE user_id = %s ORDER BY created_at", (user_id,))
        righe = cur.fetchall()
        account = _riepilogo_cur(cur, user_id)
        return [_riga_chiave(r, account) for r in righe]


def _revoca(chiave_id: str, user_id: str | None = None) -> bool:
    try:
        with connessione() as conn, conn.cursor() as cur:
            if user_id:
                cur.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (user_id,))
                if cur.fetchone() is None:
                    return False
                cur.execute("UPDATE api_chiavi SET revoked_at = now() "
                            "WHERE id = %s AND user_id = %s AND revoked_at IS NULL",
                            (chiave_id, user_id))
            else:
                cur.execute("SELECT user_id::text FROM api_chiavi WHERE id = %s", (chiave_id,))
                r = cur.fetchone()
                if r is None:
                    return False
                if r[0]:
                    cur.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (r[0],))
                    if cur.fetchone() is None:
                        return False
                cur.execute("UPDATE api_chiavi SET revoked_at = now() "
                            "WHERE id = %s AND revoked_at IS NULL", (chiave_id,))
            fatta = cur.rowcount > 0
    except psycopg.errors.InvalidTextRepresentation:
        return False
    if fatta:
        invalida(chiave_id)
    return fatta


def revoca_per_utente(chiave_id: str, user_id: str) -> bool:
    return _revoca(chiave_id, user_id)


def spendi_per_utente(user_id: str, via: str = "export",
                      azione: str = "daily export",
                      dettaglio: str | None = None) -> bool:
    return spendi_n_per_utente(user_id, 1, via=via, azione=azione, dettaglio=dettaglio)


COSTO_COMPANY = 3


class QuotaExplorer(Exception):
    def __init__(self, retry_after: int, quota: str) -> None:
        super().__init__(f"Explorer {quota} quota exhausted")
        self.retry_after = max(1, int(retry_after))
        self.quota = quota


def consuma_quota_explorer(user_id: str, ip: str | None) -> None:
    now = datetime.now(UTC)
    minute = now.replace(second=0, microsecond=0)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        with connessione() as conn, conn.cursor() as cur:
            cur.execute("SELECT subscription_status FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if row is None:
                raise ChiaveInvalida()
            active = row[0] in ("active", "scheduled_cancel")
            buckets = [
                ("user_minute", user_id, minute, 20, minute + timedelta(minutes=1)),
                ("user_day", user_id, day, 1000 if active else 150,
                 day + timedelta(days=1)),
            ]
            if ip:
                buckets.insert(1, ("ip_minute", "ip:" + ip, minute, 20,
                                   minute + timedelta(minutes=1)))
            for scope, subject, window_start, limit, window_end in buckets:
                cur.execute(
                    "INSERT INTO explorer_rate_limits "
                    "(scope, subject, window_start, hits) VALUES (%s, %s, %s, 1) "
                    "ON CONFLICT (scope, subject, window_start) DO UPDATE "
                    "SET hits = explorer_rate_limits.hits + 1 RETURNING hits",
                    (scope, subject, window_start))
                if int(cur.fetchone()[0]) > limit:
                    raise QuotaExplorer((window_end - now).total_seconds(), scope)
    except QuotaExplorer:
        raise
    except ChiaveInvalida:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ContatoreNonDisponibile("quota Explorer non disponibile") from exc


def rivela_per_utente(user_id: str, tipo: str, riferimento: str) -> str:
    if tipo == "job":
        return "non_disponibile"
    if tipo != "azienda":
        raise ValueError("tipo reveal non valido")
    azione = "company unlock"
    costo = COSTO_COMPANY
    try:
        with connessione() as conn, conn.cursor() as cur:
            cur.execute("SELECT status, subscription_status, creem_subscription_id, current_period_end "
                "FROM users WHERE id = %s FOR UPDATE", (user_id,))
            ru = cur.fetchone()
            if ru is None:
                return "senza_crediti"
            if ru[0] != "active":
                return "account_sospeso"
            cur.execute(
                "SELECT 1 FROM portale_rivelazioni "
                "WHERE user_id = %s AND tipo = %s AND riferimento = %s",
                (user_id, tipo, riferimento))
            if cur.fetchone():
                return "gratis"
            rec = _spendi_account_cur(
                cur, user_id, costo, "portale", azione, riferimento,
                resetta_mensile=_reset_abbonamento(ru[1], ru[2], ru[3]))
            if rec is None:
                return "senza_crediti"
            cur.execute(
                "INSERT INTO portale_rivelazioni (user_id, tipo, riferimento) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (user_id, tipo, riferimento))
            if cur.rowcount != 1:
                raise ContatoreNonDisponibile("reveal concorrente non coerente")
            return "ok"
    except (ChiaveInvalida, CreditiEsauriti):
        return "senza_crediti"
    except Exception as exc:  # noqa: BLE001
        raise ContatoreNonDisponibile("reveal non disponibile") from exc


def e_rivelata(user_id: str, tipo: str, riferimento: str) -> bool:
    with connessione() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM portale_rivelazioni "
                    "WHERE user_id = %s AND tipo = %s AND riferimento = %s",
                    (user_id, tipo, riferimento))
        return cur.fetchone() is not None


def conta_rivelazioni(user_id: str) -> int:
    with connessione() as conn:
        return conn.execute("SELECT count(*) FROM portale_rivelazioni WHERE user_id = %s",
                            (user_id,)).fetchone()[0]


def rivelate_per_utente(user_id: str, tipo: str, riferimenti: list[str]) -> set:
    if not riferimenti:
        return set()
    with connessione() as conn, conn.cursor() as cur:
        cur.execute("SELECT riferimento FROM portale_rivelazioni "
                    "WHERE user_id = %s AND tipo = %s AND riferimento = ANY(%s)",
                    (user_id, tipo, riferimenti))
        return {r[0] for r in cur.fetchall()}


def spendi_n_per_utente(user_id: str, n: int, via: str = "export",
                        azione: str = "filtered export",
                        dettaglio: str | None = None) -> bool:
    """Addebito atomico di N crediti, con registro nel medesimo commit."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 0 or n > 2_000_000_000:
        raise ValueError("numero crediti non valido")
    if n == 0:
        return True
    try:
        with connessione() as conn, conn.cursor() as cur:
            cur.execute("SELECT status, subscription_status, creem_subscription_id, current_period_end "
                        "FROM users WHERE id = %s FOR UPDATE", (user_id,))
            ru = cur.fetchone()
            if ru is None or ru[0] != "active":
                return False
            return _spendi_account_cur(
                cur, user_id, n, via, azione, dettaglio,
                resetta_mensile=_reset_abbonamento(ru[1], ru[2], ru[3])
            ) is not None
    except ChiaveInvalida:
        return False
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, ContatoreNonDisponibile):
            raise
        raise ContatoreNonDisponibile("addebito account non disponibile") from exc


def saldo_per_utente(user_id: str) -> int:
    return int(riepilogo_per_utente(user_id)["saldo"])


def saldo_per_chiave(chiave_id: str) -> int:
    with connessione() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id::text, crediti_mensili, usati_mese, mese_uso "
                    "FROM api_chiavi WHERE id = %s AND revoked_at IS NULL", (chiave_id,))
        riga = cur.fetchone()
        if riga is None:
            raise ChiaveInvalida()
        if riga[0]:
            return int(_riepilogo_cur(cur, riga[0])["saldo"])
        mese = _mese()
        usati = 0 if _come_data(riga[3]) < mese else int(riga[2])
        return max(0, int(riga[1]) - usati)


def abbonamento_per_utente(user_id: str) -> dict:
    with connessione() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT subscription_plan, subscription_interval, subscription_status, "
            "       current_period_end, creem_customer_id, creem_subscription_id "
            "FROM users WHERE id = %s", (user_id,))
        r = cur.fetchone()
    if r is None:
        raise ChiaveInvalida()
    return {"subscription_plan": r[0], "subscription_interval": r[1],
            "subscription_status": r[2],
            "current_period_end": r[3].isoformat() if r[3] else None,
            "creem_customer_id": r[4], "creem_subscription_id": r[5]}


def extra_per_utente(user_id: str) -> int:
    with connessione() as conn, conn.cursor() as cur:
        cur.execute("SELECT crediti_extra FROM users WHERE id = %s", (user_id,))
        r = cur.fetchone()
        return int(r[0]) if r else 0


def lista() -> list[dict]:
    with connessione() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, label, created_at, revoked_at, crediti_mensili, "
            "       usati_mese, mese_uso FROM api_chiavi ORDER BY created_at")
        return [{"id": r[0], "etichetta": r[1], "creata_il": r[2].isoformat(),
                 "revocata_il": r[3].isoformat() if r[3] else None,
                 "crediti_mensili": r[4], "usati_mese": r[5],
                 "mese_uso": r[6].isoformat()} for r in cur.fetchall()]


def revoca(chiave_id: str) -> bool:
    return _revoca(chiave_id)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.api_clienti.chiavi", description=__doc__)
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
