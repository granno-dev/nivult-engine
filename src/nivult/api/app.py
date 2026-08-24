"""L'API del motore, per il sito pubblico.

Il sito sta su Cloudflare Pages come export statico: non può fare da
backend-for-frontend, quindi l'API è questa, esposta su api.nivult.com, e il
sito la chiama dal browser con CORS.

Autenticazione senza password: il browser chiede un magic link per un
indirizzo, il link arriva per email, il sito lo riscambia qui contro un token
di sessione da tenere e presentare come Bearer. Nessuna password, da nessuna
parte, per costruzione dello schema.

    uvicorn nivult.api.app:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import ipaddress
import os

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

from nivult import auth
from nivult.config import database_url, load_dotenv

load_dotenv()


def _ip(request: Request) -> str | None:
    """L'indirizzo del chiamante, se è davvero un indirizzo.

    Dietro il tunnel di Cloudflare l'host è quello locale del connettore: il
    vero IP sta negli header. Ma qui si difende solo da input non-IP —
    un valore sporco non deve far 500 una richiesta di login.
    """
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


class RichiestaLink(BaseModel):
    email: EmailStr


class ConsumoLink(BaseModel):
    token: str


def create_app() -> FastAPI:
    app = FastAPI(title="Nivult", version="0.1.0", docs_url=None, redoc_url=None)
    # Il sito sta su un altro origine per costruzione (Pages statico): senza
    # CORS non parla con noi. Gli origine ammessi stanno in ambiente.
    origini = [o.strip() for o in
               os.environ.get("CORS_ORIGINS", "https://nivult.com").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware, allow_origins=origini, allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"])

    def connessione():
        """Una connessione per richiesta: il pattern di tutto il motore."""
        with psycopg.connect(database_url()) as conn:
            yield conn

    def utente(request: Request, conn=Depends(connessione)) -> str:
        """Dipendenza di autenticazione: Bearer <token di sessione> -> user_id."""
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(401, "sessione richiesta")
        uid = auth.verifica_sessione(conn, header[7:].strip())
        if not uid:
            raise HTTPException(401, "sessione non valida")
        return uid

    @app.get("/")
    def radice():
        # Il controllo che un umano può fare col browser: l'API è viva?
        return {"servizio": "nivult-api", "stato": "ok"}

    @app.post("/auth/magic-link", status_code=202)
    def magic_link(corpo: RichiestaLink, request: Request,
                   conn=Depends(connessione)):
        # La risposta è sempre la stessa, anche a fine rate limit: dall'esterno
        # non si scopre né se l'indirizzo esiste né quanto è vicino al muro.
        auth.richiedi_magic_link(
            conn, str(corpo.email),
            ip=_ip(request),
            ua=request.headers.get("User-Agent"))
        return {"esito": "se l'indirizzo è valido, ricevi un link"}

    @app.post("/auth/consuma")
    def consuma(corpo: ConsumoLink, request: Request,
                conn=Depends(connessione)):
        r = auth.consuma(conn, corpo.token.strip(),
                         ip=_ip(request),
                         ua=request.headers.get("User-Agent"))
        if not r:
            raise HTTPException(401, "link scaduto, già usato o inesistente")
        sessione, uid = r
        return {"sessione": sessione, "utente": {"id": uid}}

    @app.post("/auth/logout")
    def logout(request: Request, uid: str = Depends(utente),
               conn=Depends(connessione)):
        header = request.headers.get("Authorization", "")
        auth.revoca_sessione(conn, header[7:].strip())
        return {"esito": "sessione revocata"}

    @app.get("/me")
    def me(uid: str = Depends(utente), conn=Depends(connessione)):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT email::text, plan, subscription_status, delivery_channel, "
                "frequency, timezone, email_verified_at IS NOT NULL, status "
                "FROM users WHERE id = %s", (uid,))
            r = cur.fetchone()
        if not r:
            raise HTTPException(404, "utente inesistente")
        return {"id": uid, "email": r[0], "piano": r[1], "abbonamento": r[2],
                "canale": r[3], "frequenza": r[4], "fuso": r[5],
                "email_verificata": r[6], "stato": r[7]}

    return app


app = create_app()
