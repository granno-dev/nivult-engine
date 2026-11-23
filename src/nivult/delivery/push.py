"""Notifiche push native via Expo Push API.

L'app (Mavult, Expo) registra un token per dispositivo con
`POST /me/push/token`; `invia_notifica` e' il sender generico, mentre
`invia_digest_pronto` costruisce il contenuto del digest giornaliero.

La push e' un AVVISO, non la consegna: il digest e' gia' registrato e parte
soltanto se il canale app e' attivo. Nessun guasto di questo modulo puo'
risalire: ogni errore si annota nel log e basta, e un token che Expo
dichiara morto (`DeviceNotRegistered`) viene cancellato subito — tenerlo
significherebbe pagare una chiamata a giro per un dispositivo che non
rispondera' mai piu'.

Nessuna libreria dedicata: l'API di Expo e' una POST JSON, e httpx e'
gia' una dipendenza del motore.

Configurazione: nessuna. L'endpoint pubblico di Expo non chiede credenziali
per i token ExponentPushToken (l'access token Expo serve solo per le ricevute
di consegna, che qui non si consultano: ci basta il ticket immediato).
"""

from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger("nivult.delivery.push")

# Come in telegram.py: httpx a INFO stampa ogni URL, e qui il corpo della
# richiesta contiene i token dei dispositivi. Nei log non devono finire.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

EXPO_SEND = "https://exp.host/--/api/v2/push/send"
TIMEOUT = 10.0  # secondi: la push non deve mai tenere fermo il worker
CHUNK_SIZE = 100
TOKEN_EXPO = re.compile(r"^(?:ExponentPushToken|ExpoPushToken)\[[A-Za-z0-9_\-]+\]$")

# Tre lingue, non le nove del sito: l'app nativa parla it/en/es. Il testo
# sta qui e non in delivery/testi.py perche' non e' il digest — e' il
# cartello «il digest e' pronto», e tradurlo nelle nove lingue per un
# prodotto che ne parla tre sarebbe peso morto.
TESTI = {
    "it": ("Il tuo digest è pronto", "{n} ruoli selezionati per te oggi"),
    "en": ("Your digest is ready", "{n} roles picked for you today"),
    "es": ("Tu digest está listo", "{n} roles seleccionados para ti hoy"),
}


def _token_corto(token: str) -> str:
    """Il token NON si logga intero: e' un dato quasi-personale."""
    return token[:12] + "…"


def invia_notifica(conn, user_id: str, *, titolo: str, corpo: str,
                   dati: dict, canale: str = "reminders", badge: int | None = None) -> int:
    """Invia una notifica Expo arbitraria; ritorna i dispositivi raggiunti."""
    inviati = 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, delivery_channels FROM users WHERE id = %s",
                        (user_id,))
            r = cur.fetchone()
            if not r or r[0] != "active" or "app" not in (r[1] or []):
                return 0
            cur.execute("SELECT token, platform FROM user_push_tokens "
                        "WHERE user_id = %s ORDER BY created_at, token", (user_id,))
            dispositivi = [(token, platform) for token, platform in cur.fetchall()
                           if len(token) <= 200 and TOKEN_EXPO.fullmatch(token)]
            if not dispositivi:
                return 0

        for inizio in range(0, len(dispositivi), CHUNK_SIZE):
            chunk = dispositivi[inizio:inizio + CHUNK_SIZE]
            messaggi = []
            for token, _platform in chunk:
                messaggio = {
                    "to": token,
                    "title": titolo,
                    "body": corpo,
                    "sound": "default",
                    "channelId": canale,
                    "data": dati,
                }
                if badge is not None:
                    messaggio["badge"] = badge
                messaggi.append(messaggio)
            try:
                risposta = httpx.post(
                    EXPO_SEND, json=messaggi, timeout=TIMEOUT,
                    headers={"Accept": "application/json"})
                risposta.raise_for_status()
                inviati += len(chunk)
                tickets = (risposta.json() or {}).get("data") or []
            except Exception as exc:  # noqa: BLE001
                log.warning("push: chunk %d per l'utente %s fallito: %s",
                            inizio // CHUNK_SIZE + 1, user_id, exc)
                continue

            morti = []
            for (token, _platform), ticket in zip(chunk, tickets):
                if (isinstance(ticket, dict) and ticket.get("status") == "error"
                        and (ticket.get("details") or {}).get("error")
                        == "DeviceNotRegistered"):
                    morti.append(token)
            if morti:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM user_push_tokens "
                                "WHERE user_id = %s AND token = ANY(%s)",
                                (user_id, morti))
                conn.commit()
                log.info("push: %d token morti cancellati per l'utente %s "
                         "(es. %s)", len(morti), user_id, _token_corto(morti[0]))
        log.info("push %s: %d dispositivi per l'utente %s", canale, inviati, user_id)
        return inviati
    except Exception as exc:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.warning("push %s per l'utente %s non partita: %s", canale, user_id, exc)
        return inviati


def invia_digest_pronto(conn, user_id: str) -> int:
    """Invia l'avviso Expo in chunk da 100; non propaga mai errori al digest."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT locale FROM users WHERE id = %s", (user_id,))
            riga = cur.fetchone()
            if not riga:
                return 0
            cur.execute(
                "SELECT count(*) FROM digest_items di "
                "WHERE di.user_id = %s AND di.digest_id = ("
                "  SELECT d.id FROM digests d "
                "  WHERE d.user_id = %s AND d.status = 'sent' "
                "  ORDER BY d.sent_at DESC LIMIT 1)",
                (user_id, user_id))
            n_match = cur.fetchone()[0]
        titolo, corpo = TESTI.get(riga[0], TESTI["en"])
        return invia_notifica(
            conn, user_id, titolo=titolo, corpo=corpo.format(n=n_match),
            dati={"schermata": "digest"}, canale="digest", badge=n_match)
    except Exception as exc:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        log.warning("push digest per l'utente %s non partita: %s", user_id, exc)
        return 0
