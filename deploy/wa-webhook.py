#!/usr/bin/env python3
"""Il ricevitore dei webhook WhatsApp (wuzapi) — gira in un containerino sul
N5 accanto a wuzapi, e scrive ogni messaggio in arrivo in nivult_ats.wa_inbox.

Perche' una tabella e non una chiamata all'API: il digest legge l'inbox in
PULL (cerca_collegamenti, ha_chiesto_stop), Postgres e' gia' la coda di casa,
e un webhook perso perche' l'API era giu' non esiste: wuzapi ritenta, la
tabella non dimentica.

Sicurezza: il token dell'istanza wuzapi viaggia nel corpo (`token` in json
mode) e si confronta: chi non lo sa non puo' scrivere nell'inbox.
"""
from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

TOKEN = os.environ.get("WA_WEBHOOK_TOKEN", "")
DSN = os.environ.get("ATS_DATABASE_URL", "")
PORTA = int(os.environ.get("WA_WEBHOOK_PORTA", "8079"))

_JID = re.compile(r"^(\d{6,15})@s\.whatsapp\.net$")


def _telefono(jid: str) -> str | None:
    m = _JID.fullmatch(jid or "")
    return f"+{m.group(1)}" if m else None


def _testo(msg: dict) -> str:
    v = (msg or {}).get("conversation")
    if isinstance(v, str):
        return v
    v = ((msg or {}).get("extendedTextMessage") or {}).get("text")
    return v if isinstance(v, str) else ""


def _tratta(body: dict) -> None:
    # La forma vera sul filo (misurata il 04/10/2026): wuzapi spedisce un
    # involucro {instanceName, jsonData: "<stringa>", userID} — l'evento e'
    # DENTRO jsonData. Il token, se arriva, deve combaciare; il receiver non
    # e' pubblicato fuori dalla rete docker, quindi l'assenza non apre nulla.
    if isinstance(body.get("jsonData"), str):
        try:
            body = json.loads(body["jsonData"])
        except ValueError:
            return
    if TOKEN and body.get("token") and body.get("token") != TOKEN:
        return
    if body.get("type") != "Message":
        return
    ev = body.get("event") or {}
    info = ev.get("Info") or {}
    # Da quando WhatsApp passa ai LID, Chat puo' essere «…@lid»: il numero
    # vero sta in SenderAlt. Mai fidarsi di un solo campo.
    telefono = None
    for jid in (info.get("Chat"), info.get("SenderAlt"), info.get("Sender")):
        telefono = _telefono(jid or "")
        if telefono:
            break
    testo = _testo(ev.get("Message") or {})
    if not telefono:
        return  # gruppi, broadcast, status: non sono l'inbox dei digest
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO wa_inbox (telefono, testo, da_noi) "
            "VALUES (%s, %s, %s)",
            (telefono, testo, bool(info.get("IsFromMe"))))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404); self.end_headers(); return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            _tratta(body)
            self.send_response(200)
        except Exception:                            # noqa: BLE001
            self.send_response(200)  # mai 5xx: wuzapi ritenterebbe all'infinito
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):                       # niente log per richiesta
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORTA), Handler).serve_forever()
