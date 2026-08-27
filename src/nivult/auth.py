"""Autenticazione senza password: magic link via email, sessioni con token.

Non esiste una colonna password da nessuna parte, e questo modulo è il motivo:
il possesso dell'indirizzo email È l'autenticazione. Il token del link vale
15 minuti e una volta sola; la sessione che ne nasce vale 30 giorni.

Di entrambi si conserva SOLO lo sha256: se il database trapela, quello che si
trova non permette di entrare. Il token in chiaro non entra mai nel database,
nei log, né negli errori.

    python -m nivult.auth richiedi --email utente@esempio.it
    python -m nivult.auth consuma --token <token>
    python -m nivult.auth verifica --sessione <token>
"""

from __future__ import annotations

import argparse
import hashlib
import secrets
import sys
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from nivult.config import database_url, load_dotenv, safe_dsn

# Finestra di vita del magic link. Corta di proposito: è un rinvio da email,
# non un oggetto che viaggia in tasca per giorni.
LINK_VALIDITA = timedelta(minutes=15)
SESSIONE_VALIDITA = timedelta(days=30)
# Quanti link si possono chiedere per utente nella finestra. Serve a frenare
# l'uso del nostro SMTP come martello verso un indirizzo (il count sta in
# login_tokens, index dedicato).
MAX_LINK_NELLA_FINESTRA = 3
FINESTRA_RATE = timedelta(minutes=15)


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _utente_o_nuovo(cur, email: str, locale: str | None = None) -> str:
    """L'utente con quell'email, creato al volo se non esiste.

    Chiedere un link su un indirizzo nuovo È la registrazione: senza password
    non c'è altro passo. I default sono i più innocui (basic, settimanale,
    UTC): tutto si corregge in onboarding prima che l'utente paghi.
    """
    cur.execute("SELECT id::text FROM users WHERE email = %s", (email,))
    r = cur.fetchone()
    if r:
        return r[0]
    # La lingua della pagina da cui e' arrivata la richiesta e' il miglior
    # segnale che abbiamo alla nascita dell'account. Solo alla nascita: un
    # utente esistente ha gia' la sua, e una visita non e' una scelta.
    cur.execute(
        "INSERT INTO users (email, plan, subscription_status, delivery_channels, "
        "  frequency, send_weekday, timezone, locale) VALUES "
        "(%s, 'basic', 'trialing', '{email}', 'weekly', 1, 'UTC', %s) "
        "RETURNING id::text",
        (email, locale or "en"))
    return cur.fetchone()[0]


def richiedi_magic_link(conn: psycopg.Connection, email: str, *, ip=None, ua=None,
                        invia=None, locale: str | None = None) -> str | None:
    """Genera un magic link per l'indirizzo e lo affida all'email.

    Ritorna il token in chiaro (al chiamante, che lo mette nell'email e lo
    dimentica), oppure None se la finestra di rate limit è piena. `invia` è
    iniettabile per i test; il default spedisce davvero via SMTP.
    """
    email = email.strip().lower()
    with conn.cursor() as cur:
        # Il rate limit guarda TUTTI i link della finestra, consumati o no:
        # chi martella deve trovare il muro subito, non dopo tre consumi.
        cur.execute("SELECT count(*) FROM login_tokens lt JOIN users u ON u.id = lt.user_id "
                    "WHERE u.email = %s AND lt.created_at > now() - make_interval(mins => %s)",
                    (email, FINESTRA_RATE.seconds // 60))
        if cur.fetchone()[0] >= MAX_LINK_NELLA_FINESTRA:
            return None
        uid = _utente_o_nuovo(cur, email, locale)
        token = secrets.token_urlsafe(32)
        cur.execute(
            "INSERT INTO login_tokens (user_id, token_hash, expires_at, requested_ip, "
            "  requested_ua) VALUES (%s, %s, now() + make_interval(mins => %s), %s, %s)",
            (uid, _sha256(token), LINK_VALIDITA.seconds // 60, ip, ua))
    conn.commit()

    # La lingua dell'email: quella della pagina da cui l'utente sta
    # chiedendo il link — sta leggendo in quella — con la lingua salvata
    # sull'account come ripiego per i client che non la mandano.
    if not locale:
        with conn.cursor() as cur:
            cur.execute("SELECT locale FROM users WHERE email = %s", (email,))
            r = cur.fetchone()
        locale = (r and r[0]) or "en"

    from nivult.delivery.testi import t
    x = t(locale)
    link = f"{_base_url()}/verify?token={token}"
    if invia is None:
        from nivult.delivery.email import invia_generica
        invia = lambda a, oggetto, testo, html: invia_generica(a, oggetto, testo, html)  # noqa: E731
    invia(email, x["ml_oggetto"],
          f"{x['ml_corpo']}\n\n{link}\n\n{x['ml_ignora']}\n",
          f'<div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;'
          f'padding:32px 24px;color:#111;"><p style="margin:0 0 4px 0;font-size:13px;'
          f'letter-spacing:0.12em;color:#888;">N I V U L T</p>'
          f'<p style="margin:24px 0;font-size:17px;line-height:1.5;">'
          f'<a href="{link}" style="color:#0a5a3c;">{x["ml_entra"]}</a></p>'
          f'<p style="margin:0;font-size:13px;color:#888;">{x["ml_ignora"]}</p></div>')
    return token


def consuma(conn: psycopg.Connection, token: str, *, ip=None, ua=None
            ) -> tuple[str, str] | None:
    """Scambia il magic link con una sessione. -> (token_sessione, user_id).

    Il consumo è atomico nel UPDATE condizionato: due richieste concorrenti
    con lo stesso link, una sola vince.
    """
    if not token:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE login_tokens SET consumed_at = now() "
            "WHERE token_hash = %s AND consumed_at IS NULL AND expires_at > now() "
            "RETURNING user_id::text, origin", (_sha256(token),))
        r = cur.fetchone()
        if not r:
            conn.rollback()
            return None
        uid, origine = r
        sessione = secrets.token_urlsafe(32)
        # L'origine viaggia col token, non la sceglie chi consuma: un ritorno
        # OAuth passa di qui, e una sessione marcata 'magic_link' mentirebbe
        # proprio dove serve la verità, cioè indagando su un accesso sospetto.
        cur.execute(
            "INSERT INTO sessions (user_id, token_hash, expires_at, origin, ip, user_agent) "
            "VALUES (%s, %s, now() + make_interval(days => %s), %s, %s, %s)",
            (uid, _sha256(sessione), SESSIONE_VALIDITA.days, origine, ip, ua))
        # Il link consumato prova il possesso dell'indirizzo — ma solo il link.
        # Un gettone nato da OAuth non prova niente sull'email: là la decisione
        # è già stata presa da nivult.oauth, che sa di quale provider fidarsi.
        if origine == "magic_link":
            cur.execute("UPDATE users SET email_verified_at = COALESCE(email_verified_at, now()) "
                        "WHERE id = %s", (uid,))
    conn.commit()
    return sessione, uid


def verifica_sessione(conn: psycopg.Connection, token: str) -> str | None:
    """Il token di sessione è valido? -> user_id, altrimenti None.

    Aggiorna last_seen_at: costa un UPDATE ma dice quando una sessione è
    stata usata davvero, non solo quando è nata.
    """
    if not token:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sessions SET last_seen_at = now() "
            "WHERE token_hash = %s AND revoked_at IS NULL AND expires_at > now() "
            "RETURNING user_id::text", (_sha256(token),))
        r = cur.fetchone()
    conn.commit()
    return r[0] if r else None


def revoca_sessione(conn: psycopg.Connection, token: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("UPDATE sessions SET revoked_at = now() "
                    "WHERE token_hash = %s AND revoked_at IS NULL", (_sha256(token),))
        n = cur.rowcount
    conn.commit()
    return n > 0


def _base_url() -> str:
    import os
    return os.environ.get("SITE_URL", "https://nivult.com").rstrip("/")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.auth", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("richiedi", help="genera e invia un magic link")
    p1.add_argument("--email", required=True)
    p2 = sub.add_parser("consuma", help="scambia il link con una sessione")
    p2.add_argument("--token", required=True)
    p3 = sub.add_parser("verifica", help="verifica un token di sessione")
    p3.add_argument("--sessione", required=True)

    args = ap.parse_args(argv)
    load_dotenv()
    dsn = database_url()
    print(f"database: {safe_dsn(dsn)}")

    with psycopg.connect(dsn) as conn:
        if args.cmd == "richiedi":
            # In CLI il token si stampa: è l'unico modo di provarlo a mano.
            token_chiaro = richiedi_magic_link(conn, args.email, invia=lambda *a: None)
            print("token:", token_chiaro or "RATE LIMIT — troppi link richiesti")
            return 0 if token_chiaro else 1
        if args.cmd == "consuma":
            r = consuma(conn, args.token)
            if not r:
                print("token rifiutato: scaduto, già usato o inesistente"); return 1
            sessione, uid = r
            print("sessione:", sessione)
            print("utente: ", uid)
            return 0
        if args.cmd == "verifica":
            uid = verifica_sessione(conn, args.sessione)
            print("utente:", uid or "sessione non valida")
            return 0 if uid else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
