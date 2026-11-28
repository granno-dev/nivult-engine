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

import hashlib
from contextlib import asynccontextmanager
import json
import logging
import ipaddress
import re
import os
import secrets
import shutil
import socket
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import psycopg
import psycopg_pool
from psycopg import Binary
from psycopg.rows import dict_row
from psycopg.types.json import Json
from fastapi import (BackgroundTasks, Depends, FastAPI, File, HTTPException,
                     Query, Request, Response, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, EmailStr, Field

from nivult import auth, oauth
from nivult import crypto, cv, gdpr, ponte_ats, storage
from nivult.api import cruscotto as cruscotto_mod
from nivult.api import booking as booking_mod
from nivult.api_clienti import v1 as api_clienti_v1
from nivult.api_clienti import chiavi as api_chiavi
from nivult.api_clienti import opensearch as api_opensearch
from nivult.api_clienti import vivo as api_vivo
from nivult.ponte_ats import ats_database_url
from nivult.config import database_url, load_dotenv
from nivult.delivery import telegram as telegram_mod
from nivult.delivery import whatsapp as whatsapp_mod
# Lo stipendio esce FORMATTATO dalla API: il JSONB di schema.org
# ({'@type', 'value', 'currency'}) e' un dettaglio di archivio — chi lo
# mostrava grezzo prendeva un oggetto React al posto di una riga di testo.
from nivult.delivery.email import stipendio as _fmt_stipendio
from nivult.matching import funnel as funnel_matching
from nivult.matching.llm import GLM
from nivult.matching.spiegazioni import evidenze
from nivult.matching.worker import calcola_slot

# I messaggi che il bot manda durante il collegamento. Non stanno in
# `delivery/testi.py` perche' non sono il digest: sono la conferma che il
# collegamento e' andato a buon fine, e l'utente li legge una volta sola.
# Il benvenuto e' tradotto perche' e' la prima frase che una persona riceve
# da noi su quel canale; gli errori restano in inglese, li vede quasi
# nessuno e tradurne nove copie non ripaga.
TG_SENZA_GETTONE = (
    "Hi! To receive your Nivult digest here, open the link from your "
    "Nivult setup page \u2014 this chat needs to be connected to your account first.")
TG_SCADUTO = (
    "That link has expired or was already used. Open your Nivult page again "
    "and generate a new one \u2014 they last ten minutes.")
TG_GIA_COLLEGATA = (
    "This Telegram account is already connected to another Nivult account. "
    "Disconnect it there first.")
# La conferma di collegamento: il PRIMO messaggio che il prodotto dice in
# chat, e un «Collegato.» secco era da citofono. Nome, benvenuto e la
# frequenza scelta — nella lingua dell'utente. Il segnaposto ~Nivult~
# diventa grassetto nel dialetto del canale (<b> su Telegram, *...* su
# WhatsApp); senza nome, il saluto sta in piedi da solo.
# Su WhatsApp viaggia nella finestra di servizio aperta dal messaggio
# dell'utente: gratuito, quindi il limite e' il gusto, non il costo.
BENVENUTO = {
    "en": "Hi{nome}! Welcome to ~Nivult~ \U0001F44B Your digest will arrive right here, {freq} \u2014 only the roles above your bar, scored, with the reason why.",
    "it": "Ciao{nome}! Benvenuto su ~Nivult~ \U0001F44B Il tuo digest arriver\u00e0 proprio qui, {freq} \u2014 solo le offerte sopra la tua soglia, con punteggio e motivo.",
    "fr": "Bonjour{nome} ! Bienvenue sur ~Nivult~ \U0001F44B Votre digest arrivera ici m\u00eame, {freq} \u2014 uniquement les offres au-dessus de votre seuil, avec score et raison.",
    "de": "Hallo{nome}! Willkommen bei ~Nivult~ \U0001F44B Dein Digest kommt ab jetzt genau hierher, {freq} \u2014 nur die Stellen \u00fcber deiner Schwelle, mit Score und Begr\u00fcndung.",
    "es": "\u00a1Hola{nome}! Bienvenido a ~Nivult~ \U0001F44B Tu resumen llegar\u00e1 aqu\u00ed mismo, {freq} \u2014 solo las ofertas por encima de tu umbral, con puntuaci\u00f3n y motivo.",
    "pt": "Ol\u00e1{nome}! Bem-vindo ao ~Nivult~ \U0001F44B O teu resumo vai chegar aqui mesmo, {freq} \u2014 s\u00f3 as ofertas acima do teu limiar, com pontua\u00e7\u00e3o e motivo.",
    "nl": "Hoi{nome}! Welkom bij ~Nivult~ \U0001F44B Je digest komt vanaf nu precies hier, {freq} \u2014 alleen de vacatures boven je lat, met score en reden.",
    "pl": "Cze\u015b\u0107{nome}! Witaj w ~Nivult~ \U0001F44B Tw\u00f3j digest b\u0119dzie przychodzi\u0107 dok\u0142adnie tutaj, {freq} \u2014 tylko oferty powy\u017cej Twojego progu, z punktacj\u0105 i uzasadnieniem.",
    "sv": "Hej{nome}! V\u00e4lkommen till ~Nivult~ \U0001F44B Din sammanfattning kommer h\u00e4danefter precis hit, {freq} \u2014 bara tj\u00e4nsterna \u00f6ver din ribba, med po\u00e4ng och motivering.",
}
FREQ_TESTO = {
    "en": {"daily": "every day", "weekly": "every week", "monthly": "once a month"},
    "it": {"daily": "ogni giorno", "weekly": "ogni settimana", "monthly": "una volta al mese"},
    "fr": {"daily": "chaque jour", "weekly": "chaque semaine", "monthly": "une fois par mois"},
    "de": {"daily": "jeden Tag", "weekly": "jede Woche", "monthly": "einmal im Monat"},
    "es": {"daily": "cada d\u00eda", "weekly": "cada semana", "monthly": "una vez al mes"},
    "pt": {"daily": "todos os dias", "weekly": "todas as semanas", "monthly": "uma vez por m\u00eas"},
    "nl": {"daily": "elke dag", "weekly": "elke week", "monthly": "\u00e9\u00e9n keer per maand"},
    "pl": {"daily": "codziennie", "weekly": "co tydzie\u0144", "monthly": "raz w miesi\u0105cu"},
    "sv": {"daily": "varje dag", "weekly": "varje vecka", "monthly": "en g\u00e5ng i m\u00e5naden"},
}


def _benvenuto(locale: str, nome: str | None, frequency: str,
               canale: str) -> str:
    """Il messaggio di benvenuto, montato per lingua e canale."""
    sagoma = BENVENUTO.get(locale, BENVENUTO["en"])
    freq = FREQ_TESTO.get(locale, FREQ_TESTO["en"]).get(frequency, "")
    battesimo = (nome or "").strip().split(" ")[0] if nome else ""
    testo = sagoma.replace("{nome}", f" {battesimo}" if battesimo else "")
    testo = testo.replace("{freq}", freq)
    prodotto = os.environ.get("PRODUCT_NAME", "Nivult")
    if canale == "telegram":
        return testo.replace("~Nivult~", f"<b>{prodotto}</b>")
    return testo.replace("~Nivult~", f"*{prodotto}*")


def _marchio(testo: str) -> str:
    return testo.replace("Nivult", os.environ.get("PRODUCT_NAME", "Nivult"))


WA_GIA_COLLEGATO = ("This WhatsApp number is already connected to another "
                    "Nivult account. Disconnect it there first.")


load_dotenv()
log = logging.getLogger("nivult.api")


def _ip(request: Request) -> str | None:
    """L'indirizzo del chiamante, se è davvero un indirizzo.

    Dietro il tunnel di Cloudflare l'host è quello locale del connettore: il
    vero IP sta in CF-Connecting-IP (lo scrive Cloudflare, il cliente non lo
    falsifica). Il 26/09/2026 il docstring lo prometteva e il codice non lo
    faceva: il rate limiting misurava il tunnel, non il visitatore. Qui si
    difende solo da input non-IP — un valore sporco non deve far 500 una
    richiesta di login.
    """
    host = (request.headers.get("cf-connecting-ip")
            or (request.client.host if request.client else None))
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


class RichiestaLink(BaseModel):
    email: EmailStr
    # La lingua della pagina da cui l'utente sta chiedendo il link: decide la
    # lingua dell'EMAIL, e alla prima richiesta diventa la lingua dell'account.
    locale: str | None = Field(default=None, pattern="^[a-z]{2}$")
    # Dove atterra il link: il portale B2B verifica da solo nella sua pagina
    # (27/09/2026). Solo le nostre origini passano, le altre si ignorano.
    redirect: str | None = Field(default=None, max_length=300)


class ConsumoLink(BaseModel):
    token: str
    native_verifier: str | None = None


class FiltriCluster(BaseModel):
    """I filtri deterministici del funnel, per coppia utente-cluster.

    Le liste vuote significano 'nessun vincolo': restringere è una scelta
    esplicita dell'utente, mai un effetto collaterale. I valori sono validati
    contro i vocabolari in tabella — un refuso filtrerebbe via tutto in
    silenzio, e sembrerebbe che per quell'utente il mercato sia vuoto.
    """
    languages: list[str] = []
    min_seniority: str | None = None
    max_seniority: str | None = None
    work_arrangements: list[str] = []
    employment_types: list[str] = []
    accepted_employer_kinds: list[str] = []
    needs_visa_sponsorship: bool = False
    min_headcount: int | None = Field(default=None, ge=0)
    max_headcount: int | None = Field(default=None, gt=0)
    # Il ruolo a cui punta ("HR Business Partner"). La famiglia decide dove
    # si legge; questo dice cosa si cerca, e va al modello come segnale di
    # punteggio — mai come chiave di ingestione (misurato: per titolo si
    # perde il 96% delle offerte) ne' come filtro secco.
    target_role: str | None = Field(default=None, max_length=120)
    # Settori accettati (vocabolario LinkedIn, dal corpus). Vuoto = tutti;
    # un'offerta senza il dato passa comunque, come per ogni filtro.
    industries: list[str] = []
    # Cosa cerca, con parole sue. Il tetto e' quello del vincolo in tabella:
    # questo testo entra nel prompt di OGNI offerta del cluster, quindi la
    # sua lunghezza si paga moltiplicata per il numero di offerte.
    wants: str | None = Field(default=None, max_length=1000)
    min_salary_annual: float | None = Field(default=None, gt=0)
    salary_currency: str | None = Field(default=None, pattern="^[A-Za-z]{3}$")
    desired_technologies: list[str] = []
    # La barra minima di score per QUESTA ricerca (slider 50-95 dell'app):
    # NULL = il default del motore (70). Il worker la scrive in
    # matches.threshold_used, da cui `passed` e' generata.
    min_score: int | None = Field(default=None, ge=50, le=95)
    frequency: Literal["daily", "weekly", "monthly"] | None = None
    send_weekday: int | None = Field(default=None, ge=1, le=7)
    send_monthday: int | None = Field(default=None, ge=1, le=28)
    send_hour_local: int | None = Field(default=None, ge=0, le=23)
    timezone: str | None = Field(default=None, max_length=100)

    model_config = {"extra": "forbid"}


class ProfiloCv(BaseModel):
    """Le correzioni al profilo letto dal CV.

    L'onboarding lo lasciava correggere e poi buttava via le modifiche:
    non esisteva una rotta per salvarle, e il testo che prometteva «ogni
    riga e' tua da correggere» era una promessa non mantenuta.
    """
    families: list[str] = []
    seniority: str | None = None
    skills: list[str] = []
    languages: list[str] = []
    years_experience: int | None = None


class NuovaRicerca(BaseModel):
    """Apri una ricerca: una famiglia professionale in un paese.

    Il cluster e' un meccanismo di CONDIVISIONE dell'ingestione — dieci
    utenti sullo stesso mercato lo scaricano una volta — non un catalogo di
    cio' che l'utente puo' chiedere. Se non esiste, si apre.
    """
    # Facoltativa: se manca si ricava dal ruolo. L'utente pensa per titoli;
    # lo scaffale da leggere e' un fatto nostro.
    family: str | None = Field(default=None, max_length=120)
    country: str = Field(pattern="^[A-Za-z]{2}$")
    filtri: FiltriCluster = Field(default_factory=FiltriCluster)


class PreferenzeUtente(BaseModel):
    """Le preferenze di consegna. I filtri di matching stanno per cluster."""
    # Modificabile: OAuth lo propone, ma il nome con cui uno vuole essere
    # chiamato e' suo, non del provider.
    display_name: str | None = Field(default=None, max_length=120)
    timezone: str | None = None
    frequency: str | None = None
    send_hour_local: int | None = None
    send_weekday: int | None = None
    send_monthday: int | None = None
    delivery_channels: list[str] | None = None
    delivery_email: EmailStr | None = None
    # telegram_chat_id e whatsapp_e164 NON stanno qui, deliberatamente: gli
    # indirizzi di consegna nascono SOLO dai flussi di collegamento, che
    # provano il possesso. Accettarli dal PUT permetterebbe di inserire la
    # chat o il numero di qualcun altro e dirottargli addosso i digest.
    # La lingua in cui l'utente legge: email, magic link, motivazioni GLM.
    locale: str | None = Field(default=None, pattern="^[a-z]{2}$")


PUSH_TOKEN_PATTERN = r"^(?:ExponentPushToken|ExpoPushToken)\[[A-Za-z0-9_\-]+\]$"
MAX_PUSH_TOKENS_PER_USER = 10


class PushTokenRegistrazione(BaseModel):
    """Il token push Expo di un dispositivo dell'app nativa."""
    token: str = Field(min_length=10, max_length=200, pattern=PUSH_TOKEN_PATTERN)
    platform: Literal["ios", "android"]


class PushTokenRimozione(BaseModel):
    token: str = Field(min_length=10, max_length=200, pattern=PUSH_TOKEN_PATTERN)


class StatoOfferta(BaseModel):
    base_version: int = Field(ge=0)
    salvata: bool
    scartata: bool
    motivo_scarto: Literal["company", "target", "salary", "location", "seniority",
                           "work_arrangement", "skills"] | None = None
    in_archivio: bool
    candidatura: Literal["not_started", "applied", "interview", "offer", "rejected"]
    salvata_il: datetime | None = None
    scartata_il: datetime | None = None
    archiviata_il: datetime | None = None
    applicata_il: datetime | None = None
    note: str | None = Field(default=None, max_length=10000)
    prossima_azione: str | None = Field(default=None, max_length=500)
    prossima_azione_il: datetime | None = None


class ColloquioOfferta(BaseModel):
    inizio: datetime
    note: str | None = Field(default=None, max_length=10000)
    promemoria_minuti: int | None = Field(default=None, ge=0, le=43200)


def _analizza_cv(conn, testo: str) -> dict:
    """Il profilo dal CV, con GLM, sui vocabolari veri del database.

    Sta a livello di modulo per poter essere sostituito nei test: l'API non
    chiama GLM direttamente.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT family FROM job_families ORDER BY sort_order")
        famiglie = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT code FROM experience_levels ORDER BY rank")
        seniority = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT api_value FROM filter_values WHERE parameter = 'ai_language'")
        lingue = [r[0] for r in cur.fetchall()]
    with GLM() as modello:
        return cv.estrai_profilo(modello, testo, famiglie=famiglie,
                                  seniority=seniority, lingue=lingue)


def _ip_pubblico(host: str) -> bool:
    """L'host risolve SOLO a indirizzi instradabili su internet?

    E' la difesa contro l'SSRF: la rotta dei loghi scarica un URL che viene
    dai dati delle offerte, cioe' da terzi, e senza questo controllo un URL
    costruito ad arte farebbe bussare il server a `169.254.169.254` (i
    metadati del cloud), a `127.0.0.1` o alla rete privata di Hetzner —
    posti che dall'esterno non si raggiungono e che rispondono solo perche'
    la richiesta parte da dentro.

    Si guardano TUTTI gli indirizzi a cui l'host risolve, non il primo: un
    host ostile puo' pubblicare un record pubblico e uno privato e sperare
    che il controllo veda quello buono e la connessione usi quello cattivo.
    Se anche uno solo e' privato, riservato, loopback, link-local o
    multicast, si rifiuta tutto.

    Resta una finestra teorica di DNS rebinding fra questo controllo e la
    connessione vera di httpx. E' stretta, e il fatto che la risposta torni
    al chiamante solo se e' un'immagine valida sotto i 512 KB la rende
    inservibile per esfiltrare: chi la volesse chiudere del tutto dovrebbe
    fissare la connessione all'IP gia' risolto, ma quello rompe TLS sui CDN
    che servono per SNI. La difesa qui e' proporzionata alla minaccia.
    """
    try:
        info = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    if not info:
        return False
    for famiglia, _, _, _, indirizzo in info:
        try:
            ip = ipaddress.ip_address(indirizzo[0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False
    return True


def _scarica_logo(url: str, *, max_byte: int = 512_000,
                  salti: int = 2) -> bytes | None:
    """Scarica un logo con i redirect gestiti a mano e ogni salto validato.

    `follow_redirects=True` era la seconda meta' del buco: un URL in un host
    innocuo poteva rimbalzare su uno interno, e il controllo sull'URL di
    partenza non l'avrebbe visto. Qui ogni tappa passa per `_ip_pubblico`
    prima di essere seguita, e si accettano solo http/https — mai `file://`
    o `gopher://`.
    """
    for _ in range(salti + 1):
        parti = httpx.URL(url)
        if parti.scheme not in ("http", "https") or not parti.host:
            return None
        if not _ip_pubblico(parti.host):
            return None
        try:
            with httpx.Client(timeout=8, follow_redirects=False) as c:
                risp = c.get(url)
        except httpx.HTTPError:
            return None
        if risp.is_redirect:
            prossimo = risp.headers.get("location")
            if not prossimo:
                return None
            url = str(httpx.URL(url).join(prossimo))
            continue
        if risp.status_code == 200 and len(risp.content) <= max_byte:
            return risp.content
        return None
    return None


def _tipo_immagine(dati: bytes) -> str | None:
    """Il MIME dai primi byte. None se non e' un'immagine che sappiamo servire.

    Serve anche da validazione: stiamo per conservare e ripubblicare un file
    scaricato da terzi, e l'unica prova che sia un'immagine sono i suoi byte.
    """
    if dati[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if dati[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if dati[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if dati[:4] == b"RIFF" and dati[8:12] == b"WEBP":
        return "image/webp"
    testa = dati[:200].lstrip()
    if testa[:5] == b"<?xml" or testa[:4] == b"<svg":
        return "image/svg+xml"
    return None


def _classifica_ruolo(famiglie: list[str], ruolo: str) -> str | None:
    """Da "HR Business Partner" alla famiglia della tassonomia, con GLM.

    A livello di modulo per la stessa ragione di _analizza_cv: i test lo
    sostituiscono e non parlano col modello. Ritorna None se la risposta non
    e' una famiglia vera: meglio chiedere di riprovare che archiviare una
    classificazione inventata.
    """
    from nivult.matching.llm import GLM, _estrai_json
    elenco = "\n".join(f"- {f}" for f in famiglie)
    with GLM() as m:
        risposta = m.chat([
            {"role": "system", "content":
                "Classifichi titoli di lavoro nella famiglia professionale "
                "giusta. Rispondi SOLO con JSON: {\"family\": \"...\"}, "
                "scegliendo ESATTAMENTE una voce dall'elenco."},
            {"role": "user", "content":
                f"FAMIGLIE:\n{elenco}\n\nTITOLO: {ruolo}"},
        ], max_tokens=60)
    try:
        family = str(_estrai_json(risposta).get("family", "")).strip()
    except Exception:
        return None
    return family if family in famiglie else None


def create_app() -> FastAPI:
    product_name = os.environ.get("PRODUCT_NAME", "Nivult")
    service_name = os.environ.get("SERVICE_NAME", "nivult-api")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        account_pool = psycopg_pool.ConnectionPool(
            database_url(), open=False, min_size=2, max_size=8,
            kwargs={"connect_timeout": 2,
                    "options": "-c statement_timeout=5000 "
                               "-c idle_in_transaction_session_timeout=10000"},
            timeout=2, max_lifetime=1800, max_idle=300,
            check=psycopg_pool.ConnectionPool.check_connection)
        lettura_pool = psycopg_pool.ConnectionPool(
            os.environ.get("API_LETTURA_DSN", ""), open=False,
            min_size=0, max_size=4, kwargs={"connect_timeout": 2},
            timeout=2, max_lifetime=1800, max_idle=300,
            check=psycopg_pool.ConnectionPool.check_connection)
        client = httpx.Client(
            base_url=api_opensearch.URL,
            timeout=httpx.Timeout(20.0, connect=2.0),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=16,
                                keepalive_expiry=30.0))
        app.state.account_pool = account_pool
        app.state.lettura_pool = lettura_pool
        app.state.opensearch_client = client
        try:
            account_pool.open(wait=True, timeout=5)
            lettura_pool.open(wait=True, timeout=5)
            api_chiavi.configura_pool(account_pool)
            api_vivo.configura_pool(lettura_pool)
            api_opensearch.configura_client(client)
            yield
        finally:
            api_chiavi.configura_pool(None)
            api_vivo.configura_pool(None)
            api_opensearch.configura_client(None)
            client.close()
            lettura_pool.close()
            account_pool.close()

    app = FastAPI(title=product_name, version="0.1.0", docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    b2b_attivo = os.environ.get(
        "B2B_ENABLED", "0" if product_name == "Mavult" else "1"
    ).strip().lower() in ("1", "true", "yes", "on")
    b2b_prefissi = ("/v1", "/portale", "/me/chiavi", "/me/volumi",
                    "/me/checkout", "/webhooks/creem", "/booking", "/cruscotto")
    match_attivo = funnel_matching.match_attivo_sql()
    # Il sito sta su un altro origine per costruzione (Pages statico): senza
    # CORS non parla con noi. Gli origine ammessi stanno in ambiente.
    origini = [o.strip() for o in
               os.environ.get("CORS_ORIGINS", "https://nivult.com").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware, allow_origins=origini, allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type", "X-Api-Key"],
        expose_headers=["Content-Disposition", "X-Credits-Charged",
                        "X-Export-Rows", "X-Download-Id"])

    @app.middleware("http")
    async def intestazioni_sicurezza(request: Request, call_next):
        """Header di sicurezza su ogni risposta.

        HSTS impedisce il declassamento a HTTP dopo la prima visita;
        nosniff toglie al browser la liberta' di indovinare il
        content-type di una risposta e trattarla per quello che non e'.
        Costano poco e chiudono due porte piccole. Cloudflare sta davanti
        e potrebbe aggiungerli, ma un default sano non va delegato a una
        configurazione che vive altrove.
        """
        if not b2b_attivo and request.url.path.startswith(b2b_prefissi):
            risposta = JSONResponse({"detail": "not found"}, status_code=404)
        else:
            risposta = await call_next(request)
        risposta.headers.setdefault("X-Content-Type-Options", "nosniff")
        if request.url.path.startswith((
                "/cruscotto", "/portale", "/me", "/auth/consuma", "/auth/logout")):
            # dati confidenziali: mai dentro un iframe altrui (niente
            # clickjacking), niente referrer in giro, niente cache
            risposta.headers.setdefault("X-Frame-Options", "DENY")
            risposta.headers.setdefault(
                "Content-Security-Policy", "frame-ancestors 'none'")
            risposta.headers.setdefault("Referrer-Policy", "no-referrer")
            risposta.headers.setdefault("Cache-Control", "no-store")
        risposta.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=63072000; includeSubDomains")
        return risposta

    app.middleware("http")(api_clienti_v1.billing_rapidapi)

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
        return {"servizio": service_name, "stato": "ok"}

    # Il cruscotto privato: pagina live del motore, dietro OAuth Microsoft
    # murato su UNA sola email (CRUSCOTTO_EMAIL). Niente token nell'URL.
    @app.get("/cruscotto", response_class=HTMLResponse)
    def cruscotto_home(request: Request):
        # ?anteprima: la porta d'ingresso anche da loggati (e' comunque
        # cio' che ogni non-loggato vede: niente da nascondere)
        # no-store: la pagina evolve a ogni deploy, e un browser che
        # serve la copia vecchia dalla cache e' un'ora persa a cercare
        # bug gia' corretti (successo davvero, due volte in una sera)
        intest = {"Cache-Control": "no-store"}
        if "anteprima" in request.query_params:
            return HTMLResponse(cruscotto_mod.LOGIN_PAGINA, headers=intest)
        if cruscotto_mod.sessione_valida(request.cookies.get("crus", "")):
            return HTMLResponse(cruscotto_mod.PAGINA, headers=intest)
        # non loggato: la porta d'ingresso di casa, non un redirect brusco
        return HTMLResponse(cruscotto_mod.LOGIN_PAGINA, headers=intest)

    @app.get("/cruscotto/entra")
    def cruscotto_entra():
        # state firmato messo anche in un cookie: al ritorno i due devono
        # combaciare (difesa CSRF), e nessuno stato vive nel database.
        state = cruscotto_mod.firma(secrets.token_urlsafe(18))
        resp = RedirectResponse(cruscotto_mod.url_login(state), status_code=302)
        resp.set_cookie("crus_state", state, max_age=600, httponly=True,
                        secure=True, samesite="lax", path="/cruscotto")
        return resp

    @app.get("/cruscotto/callback")
    def cruscotto_callback(request: Request, code: str = "", state: str = ""):
        atteso = request.cookies.get("crus_state", "")
        if (not state or state != atteso
                or cruscotto_mod._spacchetta(state) is None):
            raise HTTPException(status_code=404)
        email = cruscotto_mod.email_da_code(code)
        if email != cruscotto_mod.OPERATORE:
            # account Microsoft valido ma non autorizzato: rifiuto
            # cortese sulla porta d'ingresso, non un 403 crudo
            return RedirectResponse("/cruscotto?negato=1", status_code=302)
        resp = RedirectResponse("/cruscotto", status_code=302)
        resp.set_cookie("crus", cruscotto_mod.cookie_sessione(),
                        max_age=cruscotto_mod.DURATA, httponly=True,
                        secure=True, samesite="lax", path="/cruscotto")
        resp.delete_cookie("crus_state", path="/cruscotto")
        return resp

    @app.get("/cruscotto/esci")
    def cruscotto_esci():
        # via il cookie di sessione, si torna alla porta d'ingresso
        resp = RedirectResponse("/cruscotto", status_code=302)
        resp.delete_cookie("crus", path="/cruscotto")
        return resp

    @app.get("/cruscotto/dati")
    def cruscotto_dati(request: Request):
        if not cruscotto_mod.sessione_valida(request.cookies.get("crus", "")):
            raise HTTPException(status_code=404)
        # sessione a scorrimento: ogni aggiornamento della pagina sposta
        # avanti la scadenza. I venti minuti valgono da fermi, non da
        # aperti — vedi la nota su DURATA in cruscotto.py.
        resp = JSONResponse(cruscotto_mod.metriche(ats_database_url(), database_url()))
        resp.set_cookie("crus", cruscotto_mod.cookie_sessione(),
                        max_age=cruscotto_mod.DURATA, httponly=True,
                        secure=True, samesite="lax", path="/cruscotto")
        return resp

    @app.post("/auth/magic-link", status_code=202)
    def magic_link(corpo: RichiestaLink, request: Request,
                   conn=Depends(connessione)):
        # La risposta è sempre la stessa, anche a fine rate limit: dall'esterno
        # non si scopre né se l'indirizzo esiste né quanto è vicino al muro.
        base = percorso = None
        dest = (corpo.redirect or "").strip()
        if dest and dest != oauth.NATIVE_CALLBACK and oauth.destinazione_valida(dest):
            from urllib.parse import urlparse
            u = urlparse(dest)
            origine = f"{u.scheme}://{u.netloc}"
            base, percorso = origine, u.path or "/verify"
        auth.richiedi_magic_link(
            conn, str(corpo.email),
            ip=_ip(request),
            ua=request.headers.get("User-Agent"),
            locale=corpo.locale,
            base=base, percorso=percorso or "/verify")
        return {"esito": "se l'indirizzo è valido, ricevi un link"}

    @app.post("/auth/consuma")
    def consuma(corpo: ConsumoLink, request: Request,
                conn=Depends(connessione)):
        r = auth.consuma(conn, corpo.token.strip(),
                         ip=_ip(request),
                         ua=request.headers.get("User-Agent"),
                         native_verifier=corpo.native_verifier)
        if not r:
            raise HTTPException(401, "link scaduto, già usato o inesistente")
        sessione, uid = r
        # Email e nome subito nella sessione: senza, l'app non sa chi sia
        # l'utente fino alla prima /me — e un delivery_email vuoto faceva
        # fallire ogni salvataggio impostazioni con un 422 muto.
        with conn.cursor() as cur:
            cur.execute("SELECT email::text, display_name FROM users "
                        "WHERE id = %s", (uid,))
            row = cur.fetchone()
        return {"sessione": sessione,
                "utente": {"id": uid,
                           "email": row[0] if row else None,
                           "nome": row[1] if row else None}}

    @app.post("/auth/logout")
    def logout(request: Request, uid: str = Depends(utente),
               conn=Depends(connessione)):
        header = request.headers.get("Authorization", "")
        auth.revoca_sessione(conn, header[7:].strip())
        risposta = JSONResponse({"esito": "sessione revocata"},
                                headers={"Cache-Control": "no-store"})
        # L'account usa Bearer in sessionStorage. Scada anche eventuali cookie
        # residui di versioni precedenti, senza introdurre cookie di login.
        risposta.delete_cookie("nv_sessione", path="/", secure=True,
                               httponly=True, samesite="lax")
        return risposta

    # ── Accesso OAuth web e app nativa ──────────────────────────────────────
    #
    # Il giro web termina su /verify o su una pagina B2B dello stesso sito;
    # quello nativo sul solo deep link esatto. In URL viaggia sempre soltanto
    # un gettone monouso, mai il token di sessione.

    def _al_sito(percorso: str) -> RedirectResponse:
        return RedirectResponse(f"{oauth.site_url()}{percorso}", status_code=302)

    def _alla_destinazione(destinazione: str, **parametri: str) -> RedirectResponse:
        from urllib.parse import urlencode
        sep = "&" if "?" in destinazione else "?"
        return RedirectResponse(f"{destinazione}{sep}{urlencode(parametri)}",
                                status_code=302)

    def _errore_nativo(contesto, codice: str) -> RedirectResponse | None:
        if contesto and oauth.callback_nativa(contesto[0]):
            return _alla_destinazione(
                contesto[0], error=codice, native_state=contesto[1])
        return None

    @app.get("/auth/oauth/{provider}/start")
    def oauth_start(provider: str, request: Request, conn=Depends(connessione)):
        # Rotta non autenticata che scrive una riga: le righe sono minuscole,
        # vivono dieci minuti, e ogni passaggio pota le scadute. Se un giorno
        # servisse un freno, va messo qui.
        dest = request.query_params.get("dest") or ""
        native_state = request.query_params.get("native_state")
        native_challenge = request.query_params.get("native_challenge")
        destinazione = dest or None
        if dest and not oauth.destinazione_valida(dest):
            raise HTTPException(400, "destinazione OAuth non valida")
        if not oauth.parametri_nativi_validi(
                destinazione, native_state, native_challenge):
            raise HTTPException(400, "parametri OAuth nativi non validi")
        try:
            return RedirectResponse(
                oauth.inizia(conn, provider, destinazione=destinazione,
                             native_state=native_state,
                             native_challenge=native_challenge),
                status_code=302)
        except oauth.OAuthError as e:
            if oauth.callback_nativa(destinazione):
                return _alla_destinazione(
                    destinazione, error=e.codice, native_state=native_state)
            return _al_sito(f"/login?errore={e.codice}")

    @app.get("/auth/oauth/{provider}/callback")
    def oauth_callback(provider: str, code: str = "", state: str = "",
                       error: str = "", conn=Depends(connessione)):
        if error:
            # La destinazione arriva soltanto dal flusso validato allo start:
            # un eventuale `dest` sul callback viene ignorato.
            contesto = oauth.annulla(conn, provider, state)
            nativo = _errore_nativo(contesto, error[:128])
            if nativo:
                return nativo
            return _al_sito("/login?errore=accesso_annullato")

        contesto = oauth.contesto(conn, provider, state)
        try:
            gettone, destinazione = oauth.concludi(conn, provider, code, state)
        except oauth.OAuthError as e:
            # Il codice finisce nell'URL di ritorno, dove nessuno lo legge in
            # un guasto: anche a log, cosi' un login OAuth che fallisce si
            # diagnostica senza ricostruirlo dal browser.
            log.warning("oauth callback %s fallita: %s", provider, e.codice)
            nativo = _errore_nativo(contesto, e.codice)
            if nativo:
                return nativo
            return _al_sito(f"/login?errore={e.codice}")
        if gettone is None:
            if destinazione:
                return _alla_destinazione(destinazione, collegato="1")
            return _al_sito("/account?collegato=1")
        if oauth.callback_nativa(destinazione):
            if not contesto or contesto[0] != destinazione:
                log.error("contesto nativo assente dopo il consumo dello state")
                return _al_sito("/login?errore=contesto_nativo_assente")
            return _alla_destinazione(
                destinazione, token=gettone, native_state=contesto[1])
        if destinazione:
            return _alla_destinazione(destinazione, token=gettone)
        return _al_sito(f"/verify?token={gettone}")

    @app.post("/me/oauth/microsoft/collega")
    def collega_microsoft(uid: str = Depends(utente), conn=Depends(connessione)):
        try:
            url = oauth.inizia(
                conn, "microsoft",
                destinazione=f"{oauth.site_url()}/account",
                link_user_id=uid)
        except oauth.OAuthError as e:
            raise HTTPException(400, e.messaggio)
        return {"url": url}

    @app.get("/me/sicurezza")
    def sicurezza_utente(request: Request, uid: str = Depends(utente),
                         conn=Depends(connessione)):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider, link_method, last_login_at FROM oauth_identities "
                "WHERE user_id = %s AND revoked_at IS NULL ORDER BY provider", (uid,))
            provider = [{"provider": r[0], "metodo": r[1],
                         "ultimo_accesso": r[2].isoformat() if r[2] else None}
                        for r in cur.fetchall()]
            cur.execute(
                "SELECT id::text, created_at, last_seen_at, expires_at, origin, token_hash "
                "FROM sessions WHERE user_id = %s AND revoked_at IS NULL "
                "AND expires_at > now() ORDER BY created_at DESC", (uid,))
            corrente = auth._sha256(request.headers["Authorization"][7:].strip())
            sessioni = [{"id": r[0], "creata_il": r[1].isoformat(),
                         "ultimo_uso": (r[2] or r[1]).isoformat(),
                         "scade_il": r[3].isoformat(), "origine": r[4],
                         "corrente": r[5] == corrente}
                        for r in cur.fetchall()]
        return JSONResponse({"provider": provider, "sessioni": sessioni},
                            headers={"Cache-Control": "no-store"})

    @app.delete("/me/sessioni/{session_id}")
    def revoca_sessione_utente(session_id: str, uid: str = Depends(utente),
                               conn=Depends(connessione)):
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE sessions SET revoked_at = now() "
                            "WHERE id = %s AND user_id = %s AND revoked_at IS NULL",
                            (session_id, uid))
                if cur.rowcount != 1:
                    conn.rollback()
                    raise HTTPException(404, "sessione non trovata")
            conn.commit()
        except psycopg.errors.InvalidTextRepresentation:
            conn.rollback()
            raise HTTPException(404, "sessione non trovata") from None
        return {"revocata": True}

    @app.post("/me/sessioni/revoca-altre")
    def revoca_altre_sessioni(request: Request, uid: str = Depends(utente),
                              conn=Depends(connessione)):
        corrente = auth._sha256(request.headers["Authorization"][7:].strip())
        with conn.cursor() as cur:
            cur.execute("UPDATE sessions SET revoked_at = now() "
                        "WHERE user_id = %s AND revoked_at IS NULL "
                        "AND token_hash <> %s", (uid, corrente))
            revocate = cur.rowcount
        conn.commit()
        return {"revocate": revocate}

    @app.get("/me")
    def me(uid: str = Depends(utente), conn=Depends(connessione)):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT u.email::text, u.plan, u.subscription_status, "
                "u.delivery_channels, "
                "u.frequency, u.timezone, u.email_verified_at IS NOT NULL, u.status, "
                "u.next_digest_at, u.last_digest_at, u.send_hour_local, u.send_weekday, "
                "u.send_monthday, u.delivery_email::text, u.display_name, u.locale, "
                "u.telegram_chat_id IS NOT NULL, u.whatsapp_e164 IS NOT NULL, "
                # Quante ricerche include il piano, e quante ne ha aperte:
                # il pannello mostrava il piano senza dire cosa comprende,
                # e «2 di 2» e' l'unico modo per sapere se ne resta una.
                "q.max_searches, "
                "(SELECT count(*) FROM user_clusters uc WHERE uc.user_id = u.id), "
                "(SELECT min(uc.next_digest_at) FROM user_clusters uc "
                " JOIN clusters c ON c.id = uc.cluster_id "
                " WHERE uc.user_id = u.id AND uc.frequency IS NOT NULL "
                "   AND NOT uc.is_paused AND c.status = 'active'), "
                "EXISTS (SELECT 1 FROM user_clusters uc_e "
                "        JOIN clusters c_e ON c_e.id = uc_e.cluster_id "
                "        WHERE uc_e.user_id = u.id AND uc_e.frequency IS NULL "
                "          AND NOT uc_e.is_paused AND c_e.status = 'active'), "
                "(SELECT max(uc.last_digest_at) FROM user_clusters uc "
                " WHERE uc.user_id = u.id) "
                "FROM users u JOIN plan_quotas q ON q.plan = u.plan "
                "WHERE u.id = %s", (uid,))
            r = cur.fetchone()
        if not r:
            raise HTTPException(404, "utente inesistente")
        prossimo = min((d for d in (r[8] if r[21] or r[20] is None else None, r[20])
                       if d is not None), default=None)
        ultimo = max((d for d in (r[9], r[22]) if d is not None), default=None)
        return {"id": uid, "email": r[0], "piano": r[1], "abbonamento": r[2],
                "canali": r[3], "frequenza": r[4], "fuso": r[5],
                "email_verificata": r[6], "stato": r[7],
                # Il minimo fra lo slot ereditato e gli override, senza
                # riscrivere users.next_digest_at per abbellire la risposta.
                "prossimo_digest": prossimo.isoformat() if prossimo else None,
                "ultimo_digest": ultimo.isoformat() if ultimo else None,
                "ora_invio": r[10], "giorno_settimana": r[11],
                "giorno_mese": r[12], "email_consegna": r[13],
                "nome": r[14], "locale": r[15],
                # Non gli indirizzi: al sito serve sapere SE un canale si
                # puo' scegliere, non a quale chat o numero consegniamo.
                "telegram_collegato": r[16], "whatsapp_collegato": r[17],
                "max_ricerche": r[18], "ricerche_attive": r[19]}

    # --- vocabolari e cluster ----------------------------------------------

    @app.get("/vocabolari")
    def vocabolari(conn=Depends(connessione)):
        """I valori che il sito può offrire nei selettori, dai vocabolari
        in tabella: il sito non li hardcoda, così un valore nuovo o corretto
        arriva senza rilascio del sito.

        Ogni voce è una coppia {codice, etichetta}. Il codice è la chiave su
        cui si filtra e non cambia mai; l'etichetta è quello che l'utente
        legge. Prima uscivano solo i codici, e chi si iscriveva sceglieva fra
        `FULL_TIME` e `staffing_agency` — parole del database, non sue.
        """
        def coppie(righe):
            return [{"codice": c, "etichetta": e or c} for c, e in righe]

        with conn.cursor() as cur:
            cur.execute("SELECT code, label FROM experience_levels ORDER BY rank")
            livelli = coppie(cur.fetchall())
            cur.execute("SELECT parameter, api_value, label FROM filter_values "
                        "ORDER BY parameter, sort_order, api_value")
            per_parametro: dict[str, list[dict]] = {}
            for parametro, valore, etichetta in cur.fetchall():
                per_parametro.setdefault(parametro, []).append(
                    {"codice": valore, "etichetta": etichetta or valore})
            cur.execute("SELECT kind, label FROM employer_kinds ORDER BY rank")
            tipi = coppie(cur.fetchall())
            # Le famiglie professionali sono il catalogo di cio' che si puo'
            # CERCARE, non di cio' che stiamo gia' scaricando. Tenerle qui
                        # dentro voleva dire mostrare all'utente i tre cluster
            # attivi al posto del prodotto.
            cur.execute("SELECT family FROM job_families ORDER BY sort_order")
            famiglie = [f for (f,) in cur.fetchall()]

            # Quali lingue stanno DAVVERO arrivando. Non serve a limitare la
            # scelta — il vocabolario le ammette tutte — ma a dirlo: scegliere
            # una lingua che nessun mercato aperto pubblica darebbe un digest
            # vuoto, e un digest vuoto si legge come "non c'e' lavoro per me".
            # Meglio saperlo prima che dopo.
            cur.execute(
                "SELECT DISTINCT ai_job_language FROM jobs "
                "WHERE ai_job_language IS NOT NULL AND status = 'active'")
            presenti = {l for (l,) in cur.fetchall()}
            for v in per_parametro.get("ai_language", []):
                v["presente"] = v["codice"] in presenti

            cur.execute(
                "SELECT org_industry, count(*) FROM jobs "
                "WHERE org_industry IS NOT NULL AND status = 'active' "
                "GROUP BY 1 HAVING count(*) >= 5 ORDER BY count(*) DESC LIMIT 40")
            settori = [{"codice": i, "etichetta": i} for i, _ in cur.fetchall()]
            cur.execute(
                "SELECT skills.skill, count(*) FROM jobs j "
                "CROSS JOIN LATERAL unnest(j.ai_key_skills) AS skills(skill) "
                "WHERE j.status = 'active' "
                "GROUP BY skills.skill HAVING count(*) >= 3 "
                "ORDER BY count(*) DESC, skills.skill LIMIT 40")
            tecnologie = [{"codice": s, "etichetta": s, "presente": True}
                          for s, _ in cur.fetchall()]

        return {
            "livelli_esperienza": livelli,
            "lingue": per_parametro.get("ai_language", []),
            "modalita_lavoro": per_parametro.get("ai_work_arrangement", []),
            "tipi_contratto": per_parametro.get("ai_employment_type", []),
            "tipi_datore": tipi,
            # Promesso in CLAUDE.md fra i filtri con campo pieno al 100%, e
            # non era mai arrivato al sito.
            "sponsorship_visto": per_parametro.get("ai_visa_sponsorship", []),
            "famiglie": famiglie,
            # Dal corpus, non da una lista scritta: i settori che il filtro
            # puo' davvero incontrare. La soglia tiene fuori il rumore.
            "settori": settori,
            "tecnologie": tecnologie,
        }

    @app.get("/cluster")
    def cluster(conn=Depends(connessione)):
        """I cluster su cui ci si può iscrivere: famiglia × paese, mai un
        paese intero. Il volume serve al picker: un cluster morto è una
        promessa che il digest non può mantenere."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.id::text, c.family, c.country, "
                "       COALESCE(v.offerte_30g, 0) FROM clusters c "
                "LEFT JOIN cluster_volume_v v ON v.id = c.id "
                "WHERE c.status = 'active' ORDER BY c.family, c.country")
            return [{"id": r[0], "famiglia": r[1], "paese": r[2],
                     "offerte_30g": r[3]} for r in cur.fetchall()]

    @app.get("/copertura/paesi")
    def copertura_paesi(family: str, conn=Depends(connessione)):
        """Paesi realmente coperti per una famiglia, non un catalogo ISO."""
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM job_families WHERE family = %s", (family,))
            if not cur.fetchone():
                raise HTTPException(422, "famiglia professionale sconosciuta")
            cur.execute(
                "SELECT c.country, COALESCE(v.offerte_30g, 0) FROM clusters c "
                "LEFT JOIN cluster_volume_v v ON v.id = c.id "
                "WHERE c.family = %s AND c.status = 'active'", (family,))
            attivi = {r[0]: r[1] for r in cur.fetchall() if r[0]}

        from nivult.ingestion.runner import CLIENTS
        statiche: dict[str, list[str]] = {}
        for client in CLIENTS:
            for code in client.countries:
                statiche.setdefault(code, []).append(client.source)

        with psycopg.connect(ats_database_url()) as c_ats, c_ats.cursor() as cur:
            cur.execute(
                "SELECT j.country, count(*) FROM ats_jobs j "
                "JOIN job_classifications c ON c.job_id = j.id "
                "  AND coalesce(c.confidence, 0) >= 0.5 "
                "WHERE j.expired_at IS NULL "
                "  AND coalesce(j.posted_at, j.fetched_at) >= now() - interval '30 days' "
                "  AND c.family = %s GROUP BY j.country", (family,))
            eleggibili = {r[0]: r[1] for r in cur.fetchall() if r[0]}

        risultato = []
        for code in sorted(set(statiche) | set(eleggibili) | set(attivi)):
            fonti = statiche.get(code, [])
            if code in eleggibili:
                fonti = [*fonti, "ats"]
            risultato.append({
                "codice": code,
                "stato": "active" if code in attivi else "available",
                "fonti": fonti,
                "offerte_30g": attivi.get(code, eleggibili.get(code, 0)),
            })
        return risultato

    def _valida_filtri(cur, filtri: FiltriCluster) -> None:
        """I valori devono esistere nei vocabolari: un refuso filtrerebbe
        via tutto in silenzio, e il mercato sembrerebbe vuoto."""
        def vocabolario(parametro: str) -> set[str]:
            cur.execute("SELECT api_value FROM filter_values WHERE parameter = %s",
                        (parametro,))
            return {r[0] for r in cur.fetchall()}

        problemi: list[dict] = []

        def dentro(nome: str, valori: list[str], ammessi: set[str]):
            sconosciuti = [v for v in valori if v not in ammessi]
            if sconosciuti:
                problemi.append({"campo": nome, "valori": sconosciuti,
                                 "ammessi": sorted(ammessi)})

        dentro("languages", filtri.languages, vocabolario("ai_language"))
        if filtri.industries:
            cur.execute("SELECT DISTINCT org_industry FROM jobs "
                        "WHERE org_industry IS NOT NULL")
            noti = {r[0] for r in cur.fetchall()}
            # Con il corpus vuoto non c'e' nulla da confrontare: il vocabolario
            # non ha offerto niente, e rifiutare qui bloccherebbe i database
            # appena nati per un filtro che comunque non escluderebbe nulla.
            if noti:
                dentro("industries", filtri.industries, noti)
        dentro("work_arrangements", filtri.work_arrangements,
               vocabolario("ai_work_arrangement"))
        dentro("employment_types", filtri.employment_types,
               vocabolario("ai_employment_type"))
        if filtri.accepted_employer_kinds:
            cur.execute("SELECT kind FROM employer_kinds")
            dentro("accepted_employer_kinds", filtri.accepted_employer_kinds,
                   {r[0] for r in cur.fetchall()})
        cur.execute("SELECT code FROM experience_levels")
        livelli = {r[0] for r in cur.fetchall()}
        dentro("min_seniority", [f for f in (filtri.min_seniority,) if f], livelli)
        dentro("max_seniority", [f for f in (filtri.max_seniority,) if f], livelli)
        if (filtri.min_headcount is not None and filtri.max_headcount is not None
                and filtri.min_headcount > filtri.max_headcount):
            problemi.append({"campo": "min_headcount",
                             "valori": [filtri.min_headcount],
                             "ammessi": ["<= max_headcount"]})
        if (filtri.min_salary_annual is None) != (filtri.salary_currency is None):
            problemi.append({"campo": "min_salary_annual",
                             "valori": [filtri.min_salary_annual, filtri.salary_currency],
                             "ammessi": ["importo e valuta devono essere presenti insieme"]})
        if (len(filtri.desired_technologies) > 15
                or any(not item.strip() or len(item.strip()) > 80 for item in filtri.desired_technologies)):
            problemi.append({"campo": "desired_technologies",
                             "valori": filtri.desired_technologies,
                             "ammessi": ["fino a 15 etichette non vuote da 80 caratteri"]})
        if problemi:
            raise HTTPException(422, detail=problemi)

    def _famiglia_dal_ruolo(cur, ruolo: str) -> str:
        """La famiglia per un titolo digitato, dalla cache o da GLM.

        La cache e' mondiale, non per utente: "hr business partner" si
        classifica una volta sola. Il fallimento del modello e' un 502 che
        invita a riprovare, mai una famiglia inventata messa a catalogo.
        """
        norm = " ".join(ruolo.lower().split())
        cur.execute("SELECT family FROM role_family_cache WHERE role_norm = %s",
                    (norm,))
        r = cur.fetchone()
        if r:
            return r[0]
        cur.execute("SELECT family FROM job_families ORDER BY sort_order")
        famiglie = [f for (f,) in cur.fetchall()]
        try:
            family = _classifica_ruolo(famiglie, ruolo)
        except RuntimeError as exc:
            log.error("classificazione ruolo fallita: %s", exc)
            family = None
        if not family:
            raise HTTPException(
                502,
                "We could not work out the field for that role just now. "
                "Try again in a moment.")
        cur.execute(
            "INSERT INTO role_family_cache (role_norm, family) VALUES (%s, %s) "
            "ON CONFLICT (role_norm) DO NOTHING", (norm, family))
        return family

    def _ricerca_identificata(cur, uid: str, identificativo: str) -> tuple[str | None, str | None]:
        cur.execute(
            "SELECT id::text, cluster_id::text FROM user_clusters "
            "WHERE user_id = %s AND id::text = %s", (uid, identificativo))
        riga = cur.fetchone()
        if riga:
            return riga
        cur.execute(
            "SELECT id::text, cluster_id::text FROM user_clusters "
            "WHERE user_id = %s AND cluster_id::text = %s", (uid, identificativo))
        righe = cur.fetchall()
        if len(righe) > 1:
            raise HTTPException(409, "cluster_id is ambiguous: use the search UUID")
        return righe[0] if righe else (None, identificativo)

    _CAMPI_SCHEDULING = frozenset({
        "frequency", "send_weekday", "send_monthday", "send_hour_local", "timezone",
    })

    def _pianificazione_ricerca(cur, uid: str, filtri: FiltriCluster):
        presenti = _CAMPI_SCHEDULING & filtri.model_fields_set
        if not presenti:
            return None
        if "frequency" not in presenti:
            raise HTTPException(
                422, "frequency e' richiesta insieme agli altri campi di pianificazione")
        f = filtri.frequency
        giorni = (filtri.send_weekday, filtri.send_monthday)
        if f is None:
            if any(v is not None for v in (
                    *giorni, filtri.send_hour_local, filtri.timezone)):
                raise HTTPException(422, "frequency null ripristina l'eredità: "
                                    "gli altri valori devono essere null o assenti")
            return None, None, None, None, None, None
        if f == "daily" and any(v is not None for v in giorni):
            raise HTTPException(422, "daily non ammette send_weekday né send_monthday")
        if f == "weekly":
            if filtri.send_weekday is None:
                raise HTTPException(422, "weekly richiede send_weekday (1–7)")
            if filtri.send_monthday is not None:
                raise HTTPException(422, "weekly non ammette send_monthday")
        if f == "monthly":
            if filtri.send_monthday is None:
                raise HTTPException(422, "monthly richiede send_monthday (1–28)")
            if filtri.send_weekday is not None:
                raise HTTPException(422, "monthly non ammette send_weekday")
        if filtri.timezone is not None:
            try:
                ZoneInfo(filtri.timezone)
            except (ValueError, KeyError):
                raise HTTPException(422, "fuso orario non valido") from None

        cur.execute("SELECT send_hour_local, timezone FROM users WHERE id = %s", (uid,))
        ora_account, fuso_account = cur.fetchone()
        ora = filtri.send_hour_local if filtri.send_hour_local is not None else ora_account
        fuso = filtri.timezone or fuso_account
        try:
            slot = calcola_slot(f, ora, filtri.send_weekday, filtri.send_monthday,
                                fuso, datetime.now(timezone.utc))
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, f"orario non calcolabile: {exc}") from None
        return (f, filtri.send_weekday, filtri.send_monthday,
                filtri.send_hour_local, filtri.timezone, slot)

    def _scrivi_iscrizione(cur, uid: str, cluster_id: str,
                           filtri: FiltriCluster,
                           subscription_id: str | None = None) -> str | None:
        """Scrive filtri e, solo se presente, l'intera pianificazione nuova."""
        pianificazione = _pianificazione_ricerca(cur, uid, filtri)
        tipi = filtri.accepted_employer_kinds or \
            ["direct", "staffing_agency", "undisclosed"]
        tecnologie = list(dict.fromkeys(
            item.strip() for item in filtri.desired_technologies if item.strip()))
        valori = (uid, cluster_id, filtri.languages, filtri.min_seniority,
                  filtri.max_seniority, filtri.work_arrangements,
                  filtri.employment_types, filtri.needs_visa_sponsorship, tipi,
                  filtri.min_headcount, filtri.max_headcount,
                  (filtri.wants or "").strip() or None,
                  (filtri.target_role or "").strip() or None,
                  filtri.industries, filtri.min_salary_annual,
                  filtri.salary_currency.upper() if filtri.salary_currency else None,
                  tecnologie, filtri.min_score)
        if subscription_id is not None:
            sql = (
                "UPDATE user_clusters SET languages = %s, min_seniority = %s, "
                "  max_seniority = %s, work_arrangements = %s, employment_types = %s, "
                "  needs_visa_sponsorship = %s, accepted_employer_kinds = %s, "
                "  min_headcount = %s, max_headcount = %s, wants = %s, target_role = %s, "
                "  industries = %s, min_salary_annual = %s, salary_currency = %s, "
                "  desired_technologies = %s, min_score = %s, is_paused = false ")
            params = valori[2:]
            if pianificazione is not None:
                sql += (", frequency = %s, send_weekday = %s, send_monthday = %s, "
                        "send_hour_local = %s, timezone = %s, next_digest_at = %s ")
                params += pianificazione
            sql += "WHERE id = %s AND user_id = %s AND cluster_id = %s"
            cur.execute(sql, params + (subscription_id, uid, cluster_id))
            if cur.rowcount != 1:
                raise HTTPException(404, "ricerca inesistente")
            return subscription_id

        colonne = (
            "user_id, cluster_id, languages, min_seniority, max_seniority, "
            "work_arrangements, employment_types, needs_visa_sponsorship, "
            "accepted_employer_kinds, min_headcount, max_headcount, wants, "
            "target_role, industries, min_salary_annual, salary_currency, "
            "desired_technologies, min_score")
        conflitto = (
            "languages = EXCLUDED.languages, "
            "min_seniority = EXCLUDED.min_seniority, "
            "max_seniority = EXCLUDED.max_seniority, "
            "work_arrangements = EXCLUDED.work_arrangements, "
            "employment_types = EXCLUDED.employment_types, "
            "needs_visa_sponsorship = EXCLUDED.needs_visa_sponsorship, "
            "accepted_employer_kinds = EXCLUDED.accepted_employer_kinds, "
            "min_headcount = EXCLUDED.min_headcount, "
            "max_headcount = EXCLUDED.max_headcount, wants = EXCLUDED.wants, "
            "industries = EXCLUDED.industries, "
            "min_salary_annual = EXCLUDED.min_salary_annual, "
            "salary_currency = EXCLUDED.salary_currency, "
            "desired_technologies = EXCLUDED.desired_technologies, "
            "min_score = EXCLUDED.min_score, is_paused = false")
        if pianificazione is not None:
            colonne += (", frequency, send_weekday, send_monthday, "
                        "send_hour_local, timezone, next_digest_at")
            valori += pianificazione
            conflitto += (", frequency = EXCLUDED.frequency, "
                          "send_weekday = EXCLUDED.send_weekday, "
                          "send_monthday = EXCLUDED.send_monthday, "
                          "send_hour_local = EXCLUDED.send_hour_local, "
                          "timezone = EXCLUDED.timezone, "
                          "next_digest_at = EXCLUDED.next_digest_at")
        segnaposto = ",".join(["%s"] * len(valori))
        cur.execute(
            f"INSERT INTO user_clusters ({colonne}) VALUES ({segnaposto}) "
            "ON CONFLICT (user_id, cluster_id, target_role_key) DO UPDATE SET "
            + conflitto + " RETURNING id::text", valori)
        return cur.fetchone()[0]

    def _riempi_dal_archivio(cluster_id: str) -> None:
        """Travasa subito nel cluster appena aperto cio' che l'archivio ha.

        PERCHE' NON SI ASPETTA LA NOTTE. Il sito promette il primo digest
        entro 24 ore, e finora un mercato appena aperto restava vuoto fino
        all'ingestione dell'una: il digest partiva puntuale e non trovava
        niente da mandare.

        Non serve pero' chiamare una fonte a pagamento per riempirlo:
        l'archivio ATS ha gia' 31.380 offerte fresche e classificate,
        distribuite su 1.227 combinazioni mestiere x paese. Il ponte le
        travasa in secondi e non costa un centesimo — e' roba gia'
        scaricata, le manca solo di essere messa sullo scaffale giusto.

        In background perche' chi ha appena scelto un mercato non deve
        guardare una rotella: la risposta parte subito, il travaso finisce
        prima che il giro dei digest arrivi alle :10. Se fallisce non
        succede niente di grave — l'ingestione notturna e il ponte delle
        05:00 lo riprendono, e il primo digest riprova il giorno dopo.
        """
        try:
            with psycopg.connect(database_url()) as c, \
                 psycopg.connect(ats_database_url()) as c_ats:
                r = ponte_ats.importa(c, c_ats, solo_cluster=cluster_id)
            log.info("cluster %s riempito dall'archivio: %d nuove, %d aggiornate",
                     cluster_id, r.importate, r.aggiornate)
        except Exception as exc:  # noqa: BLE001
            log.warning("cluster %s: riempimento dall'archivio fallito: %s",
                        cluster_id, str(exc)[:200])

    def _anticipa_primo_digest(cur, uid: str, *, nuova_ricerca_id: str | None = None,
                               inizializza_override: bool = False) -> None:
        """Anticipa solo il primo digest, senza riscrivere ogni modifica."""
        cur.execute(
            "UPDATE users u SET next_digest_at = now() "
            "WHERE u.id = %s AND u.last_digest_at IS NULL "
            "  AND (u.next_digest_at IS NULL OR u.next_digest_at > now()) "
            "  AND NOT EXISTS (SELECT 1 FROM digests d WHERE d.user_id = u.id) "
            "  AND EXISTS (SELECT 1 FROM user_cvs cv "
            "              WHERE cv.user_id = u.id AND cv.status = 'active') "
            "  AND EXISTS (SELECT 1 FROM user_clusters uc "
            "              JOIN clusters c ON c.id = uc.cluster_id "
            "              WHERE uc.user_id = u.id AND NOT uc.is_paused "
            "                AND uc.frequency IS NULL AND c.status = 'active')", (uid,))
        if nuova_ricerca_id is None and not inizializza_override:
            return
        sql = (
            "UPDATE user_clusters uc SET next_digest_at = now() "
            "FROM users u, clusters c "
            "WHERE u.id = uc.user_id AND c.id = uc.cluster_id AND u.id = %s "
            "  AND uc.frequency IS NOT NULL AND uc.last_digest_at IS NULL "
            "  AND (uc.next_digest_at IS NULL OR uc.next_digest_at > now()) "
            "  AND NOT uc.is_paused AND c.status = 'active' "
            "  AND u.last_digest_at IS NULL "
            "  AND NOT EXISTS (SELECT 1 FROM digests d WHERE d.user_id = u.id) "
            "  AND EXISTS (SELECT 1 FROM user_cvs cv "
            "              WHERE cv.user_id = u.id AND cv.status = 'active')")
        params: list = [uid]
        if nuova_ricerca_id is not None:
            sql += " AND uc.id = %s"
            params.append(nuova_ricerca_id)
        cur.execute(sql, params)

    @app.post("/me/ricerca", status_code=201)
    def apri_ricerca(corpo: NuovaRicerca, sfondo: BackgroundTasks,
                     uid: str = Depends(utente),
                     conn=Depends(connessione)):
        """Apre una ricerca e ci iscrive l'utente, creando il cluster se non
        c'e' ancora.

        Il tetto del piano si verifica QUI e non solo nel sito: e' anche il
        freno sui crediti. Ogni ruolo seguito usa un cluster condiviso, che
        entra nell'ingestione notturna e consuma il tetto mensile della fonte
        finche' resta attivo, quindi il numero non e' una regola commerciale
        soltanto.
        """
        paese = corpo.country.upper()
        with conn.cursor() as cur:
            famiglia = corpo.family
            if not famiglia:
                ruolo = (corpo.filtri.target_role or "").strip()
                if not ruolo:
                    raise HTTPException(422, "serve la famiglia oppure il ruolo")
                famiglia = _famiglia_dal_ruolo(cur, ruolo)
            # Prima l'input, poi la quota. Al contrario, chiedere una famiglia
            # che non esiste da un piano pieno risponderebbe "compra un piano
            # piu' grande" a un refuso.
            cur.execute("SELECT 1 FROM job_families WHERE family = %s",
                        (famiglia,))
            if not cur.fetchone():
                raise HTTPException(422, "famiglia professionale sconosciuta")

            cur.execute(
                "SELECT q.max_searches, "
                "  (SELECT count(*) FROM user_clusters uc JOIN clusters c "
                "     ON c.id = uc.cluster_id "
                "   WHERE uc.user_id = %s AND c.status = 'active') "
                "FROM users u JOIN plan_quotas q ON q.plan = u.plan "
                "WHERE u.id = %s", (uid, uid))
            r = cur.fetchone()
            if not r:
                raise HTTPException(422, "piano sconosciuto")
            tetto, attuali = r

            ruolo_chiave = (corpo.filtri.target_role or "").strip().lower()
            cur.execute("SELECT 1 FROM user_clusters uc "
                        "JOIN clusters c ON c.id = uc.cluster_id "
                        "WHERE uc.user_id = %s AND c.family = %s AND c.country = %s "
                        "AND uc.target_role_key = %s",
                        (uid, famiglia, paese, ruolo_chiave))
            gia = cur.fetchone()
            esistente_generica = None
            if ruolo_chiave:
                cur.execute(
                    "SELECT uc.id::text FROM user_clusters uc "
                    "JOIN clusters c ON c.id = uc.cluster_id "
                    "WHERE uc.user_id = %s AND c.family = %s AND c.country = %s "
                    "AND uc.target_role_key = '' "
                    "AND NOT EXISTS (SELECT 1 FROM user_clusters alt "
                    "                WHERE alt.user_id = uc.user_id "
                    "                  AND alt.cluster_id = uc.cluster_id "
                    "                  AND alt.target_role_key <> '')",
                    (uid, famiglia, paese))
                esistente_generica = cur.fetchone()
            # Cambiare filtri o assegnare il primo ruolo non consuma una posizione.
            if not gia and not esistente_generica and attuali >= tetto:
                raise HTTPException(
                    409,
                    f"Your plan covers {tetto} searches, and you have {attuali}. "
                    f"Stop one from your panel, or move up a plan.")

            try:
                cur.execute("SELECT apri_cluster(%s, %s)::text", (famiglia, paese))
            except psycopg.errors.CheckViolation:
                conn.rollback()
                raise HTTPException(422, "famiglia professionale sconosciuta")
            cluster_id = cur.fetchone()[0]

            _valida_filtri(cur, corpo.filtri)
            try:
                ricerca_id = _scrivi_iscrizione(
                    cur, uid, cluster_id, corpo.filtri,
                    esistente_generica[0] if esistente_generica else None)
            except psycopg.errors.CheckViolation as exc:
                conn.rollback()
                raise HTTPException(422, "pianificazione o filtri incoerenti") from exc
            _anticipa_primo_digest(
                cur, uid,
                nuova_ricerca_id=(ricerca_id if not gia and not esistente_generica
                                  else None))
            cur.execute("SELECT status, last_successful_fetch_at IS NOT NULL "
                        "FROM clusters WHERE id = %s", (cluster_id,))
            _, gia_letto = cur.fetchone()
            cur.execute("SELECT next_digest_at, last_digest_at FROM user_clusters "
                        "WHERE id = %s AND user_id = %s", (ricerca_id, uid))
            prossimo, ultimo = cur.fetchone()
        conn.commit()
        # Il mercato si riempie SUBITO da cio' che abbiamo gia' in archivio,
        # senza chiamare nessuna fonte a pagamento e senza aspettare la notte.
        sfondo.add_task(_riempi_dal_archivio, cluster_id)
        # `nuovo` dice al sito se il primo digest deve aspettare la prima
        # ingestione notturna: un mercato appena aperto non ha ancora offerte,
        # e non dirlo farebbe sembrare guasto un prodotto che sta lavorando.
        return {"id": ricerca_id, "cluster_id": cluster_id,
                "nuovo": not gia_letto, "famiglia": famiglia,
                "next_digest_at": prossimo.isoformat() if prossimo else None,
                "last_digest_at": ultimo.isoformat() if ultimo else None}

    # Le traduzioni fra i campi ATS e i vocabolari del motore: le STESSE del
    # ponte (ponte_ats._come_rawjob), tenute a mano su due righe perche' li'
    # sono locali alla funzione. Se una cambia, cambi anche l'altra.
    _SEN_ATS = {"intern": "0-2", "junior": "0-2", "mid": "2-5",
                "senior": "5-10", "lead": "5-10", "head": "10+"}
    _REM_ATS = {"remote": "Remote Solely", "hybrid": "Hybrid",
                "onsite": "On-site"}

    @app.post("/me/ricerche/stima")
    def stima_ricerca(corpo: NuovaRicerca, uid: str = Depends(utente),
                      conn=Depends(connessione)):
        """Quante offerte degli ultimi 30 giorni passano queste regole.

        Il numero che la tab Regole mostra come «riceverai ~N ruoli a
        settimana»: prima era finto (contava match locali), ora e' il conto
        vero sull'archivio ATS — lo stesso corpus da cui il ponte riempie i
        cluster (`/copertura/paesi` legge con la stessa query di base).

        QUALI FILTRI ENTRANO NEL CONTO — solo quelli con un campo ATS
        affidabile, con la STESSA semantica del funnel (funnel.py: un campo
        NULL non esclude mai):

          - languages            -> ats_jobs.lang (il ponte la copia cosi')
          - work_arrangements    -> ats_jobs.remote, tradotta con _REM_ATS
          - min/max_seniority    -> ats_jobs.seniority, tradotta con _SEN_ATS
          - min_salary_annual    -> salary_min/currency/period/salary_da,
                                    clausola identica a quella del funnel

        QUALI RESTANO FUORI, e perche':

          - employment_types: ats_jobs.employment_type ha un vocabolario suo
            («FullTime», «Employee»…) che il ponte NON traduce apposta —
            filtrarlo qui escluderebbe offerte che il digest terrebbe;
          - accepted_employer_kinds: employer_kind nasce da un trigger sul
            database del motore, nell'archivio ATS non esiste;
          - industries, min/max_headcount: sono campi dell'organizzazione
            arricchita sul motore (org_industry/org_headcount), non di
            ats_jobs;
          - needs_visa_sponsorship, wants, target_role, desired_technologies:
            non sono filtri deterministici nemmeno nel funnel — sono segnali
            di punteggio per il modello, e una stima non li puo' contare.

        Il conto e' anche PRE-dedup: la stessa posizione pubblicata da due
        fonti conta due volte (il ponte la collassa, il funnel no finche'
        non entra nel motore). La stima e' quindi grossolana ma vera: mai
        piu' bassa del digest reale, piu' alta di poco.
        """
        paese = corpo.country.upper()
        filtri = corpo.filtri
        with conn.cursor() as cur:
            famiglia = corpo.family
            if not famiglia:
                ruolo = (filtri.target_role or "").strip()
                if not ruolo:
                    raise HTTPException(422, "serve la famiglia oppure il ruolo")
                famiglia = _famiglia_dal_ruolo(cur, ruolo)
            cur.execute("SELECT 1 FROM job_families WHERE family = %s",
                        (famiglia,))
            if not cur.fetchone():
                raise HTTPException(422, "famiglia professionale sconosciuta")
            # La stessa validazione della creazione: un refuso nel filtro
            # darebbe una stima a zero che sembra «mercato vuoto».
            _valida_filtri(cur, filtri)

            # La fascia di seniority in codici motore, poi rovesciata nei
            # valori ATS che vi si traducono (stessa query del funnel).
            cur.execute(
                "SELECT code FROM experience_levels WHERE rank BETWEEN "
                "COALESCE((SELECT rank FROM experience_levels WHERE code = %s), 0) "
                "AND COALESCE((SELECT rank FROM experience_levels WHERE code = %s), 5)",
                (filtri.min_seniority, filtri.max_seniority))
            livelli = {r[0] for r in cur.fetchall()}

        sql = ("SELECT count(*) FROM ats_jobs j "
               "JOIN job_classifications c ON c.job_id = j.id "
               "  AND coalesce(c.confidence, 0) >= 0.5 "
               "WHERE j.expired_at IS NULL "
               "  AND coalesce(j.posted_at, j.fetched_at) "
               "      >= now() - interval '30 days' "
               "  AND c.family = %s AND j.country = %s")
        params: list = [famiglia, paese]

        if filtri.languages:
            sql += " AND (j.lang IS NULL OR j.lang = ANY(%s))"
            params.append(filtri.languages)
        if filtri.work_arrangements:
            # NULL e valori che il ponte non sa tradurre non escludono:
            # nel motore arriverebbero come ai_work_arrangement NULL.
            ammessi = [k for k, v in _REM_ATS.items()
                       if v in filtri.work_arrangements]
            sql += (" AND (j.remote IS NULL OR NOT (j.remote = ANY(%s)) "
                    "OR j.remote = ANY(%s))")
            params += [list(_REM_ATS), ammessi]
        if filtri.min_seniority or filtri.max_seniority:
            ammessi = [k for k, v in _SEN_ATS.items() if v in livelli]
            sql += (" AND (j.seniority IS NULL OR NOT (j.seniority = ANY(%s)) "
                    "OR j.seniority = ANY(%s))")
            params += [list(_SEN_ATS), ammessi]
        if filtri.min_salary_annual is not None:
            # La clausola del funnel, parola per parola: il salario vincola
            # solo se dichiarato, annuale/mensile e nella stessa valuta.
            sql += (" AND (j.salary_min IS NULL OR j.salary_period IS NULL "
                    "OR j.salary_currency IS NULL OR j.salary_da IS NULL "
                    "OR j.salary_da <> 'dichiarato' "
                    "OR j.salary_currency <> %s "
                    "OR lower(j.salary_period) NOT IN ('year', 'month') "
                    "OR j.salary_min * CASE lower(j.salary_period) "
                    "  WHEN 'year' THEN 1 ELSE 12 END >= %s)")
            params += [filtri.salary_currency.upper(),
                       filtri.min_salary_annual]

        try:
            with psycopg.connect(ats_database_url()) as c_ats, \
                 c_ats.cursor() as cur:
                cur.execute(sql, params)
                n = cur.fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            # L'archivio ATS irraggiungibile non deve rompere la pagina delle
            # regole: 503 esplicito, il sito mostra «stima non disponibile».
            log.error("stima ricerca: archivio ATS non leggibile: %s",
                      str(exc)[:200])
            raise HTTPException(503, "stima non disponibile in questo momento")
        return {"offerte_30g": n, "stima_settimanale": round(n / 4.3)}

    def _svuota(request_id: str, email: str) -> None:
        """Lo svuotamento vero, in sottofondo e su una connessione sua.

        `execute_deletion` committa dopo OGNI lotto — e' tutto il suo punto:
        transazioni brevi invece di un lock lungo su tabelle calde. Con la
        connessione della richiesta HTTP, che una transazione ce l'ha gia'
        aperta, `conn.transaction()` aprirebbe un semplice SAVEPOINT e i lotti
        non verrebbero committati affatto. Il modulo lo rifiuta a voce alta
        (`_require_clean_connection`), ed e' giusto cosi'.

        Se si interrompe non si perde niente: la richiesta resta aperta e
        rilanciarla riprende da dove era. Per questo l'utente puo' andarsene
        subito — `request_deletion` ha gia' fatto la parte che lui vede.
        """
        try:
            with psycopg.connect(database_url()) as c:
                totali = gdpr.execute_deletion(
                    c, request_id, rimuovi_blob=storage.elimina)
            log.info("cancellazione %s completata: %s", request_id, totali)
        except Exception as exc:  # noqa: BLE001
            log.error("cancellazione %s fallita: %s", request_id, str(exc)[:300])

    @app.delete("/me", status_code=202)
    def cancella_account(sfondo: BackgroundTasks, uid: str = Depends(utente),
                         conn=Depends(connessione)):
        """Cancella l'account e tutto cio' che ne dipende. Irreversibile.

        202 e non 204, e la differenza e' onesta: quando questa risposta parte
        l'account E' cancellato — `request_deletion` ha gia' marcato l'utente,
        azzerato `next_digest_at` e depositato le chiavi dei file. Da quel
        momento non arriva piu' nessun digest e la sessione non vale piu'.
        Lo SVUOTAMENTO invece dura: righe a lotti e file su object storage, e
        va avanti in sottofondo. Dire 204 significherebbe «finito», e non lo e'
        ancora.

        Il diritto alla cancellazione e' del GDPR: non si chiede il motivo,
        non si offre uno sconto per restare, non si mette un modulo in mezzo.
        Un clic dal pannello, una conferma perche' e' irreversibile, e basta.
        """
        # La connessione della richiesta ha una transazione aperta: la chiudo
        # prima, perche' `request_deletion` pretende — a ragione — di poter
        # committare da sola.
        conn.commit()
        try:
            request_id = gdpr.request_deletion(conn, uid)
        except gdpr.DeletionError as exc:
            raise HTTPException(404, str(exc))
        sfondo.add_task(_svuota, request_id, uid)
        return {"esito": "cancellazione avviata", "richiesta": request_id}

    _OFFERTE_COLONNE = """
        m.id::text AS id, m.score, m.reason, m.evaluated_at,
        m.saved_at, m.dismissed_at, m.dismiss_reason, m.archived_at,
        m.application_status, m.applied_at, m.action_version, m.user_action_at,
        m.note, m.next_action, m.next_action_at,
        j.title, j.organization, j.url, j.link_kind, j.employer_kind,
        j.cities, j.salary,
        COALESCE(j.org_linkedin_slug, j.domain_derived) AS logo_slug,
        j.purged_at, j.source, j.date_posted, j.date_posted_declared,
        j.date_valid_through, j.first_seen_at, j.last_seen_alive_at,
        j.status AS offerta_status,
        ricerche.ricerca_ids, ricerche.ricerca_eleggibile_ids
    """
    _OFFERTE_FROM = """
        FROM matches m
        JOIN jobs j ON j.id = m.job_id
        LEFT JOIN LATERAL (
          SELECT
            COALESCE(array_agg(uc_r.id::text ORDER BY uc_r.created_at, uc_r.id), '{}')
              AS ricerca_ids,
            COALESCE(array_agg(uc_r.id::text ORDER BY uc_r.created_at, uc_r.id)
              FILTER (WHERE NOT uc_r.is_paused AND c_r.status = 'active'
                            AND m.score >= COALESCE(uc_r.min_score, 70)), '{}')
              AS ricerca_eleggibile_ids
          FROM job_clusters jc_r
          JOIN user_clusters uc_r ON uc_r.cluster_id = jc_r.cluster_id
                                 AND uc_r.user_id = m.user_id
          JOIN clusters c_r ON c_r.id = uc_r.cluster_id
          WHERE jc_r.job_id = m.job_id
        ) ricerche ON TRUE
    """

    def _iso(valore):
        return valore.isoformat() if valore is not None else None

    def _serializza_offerta(r: dict, lingua: str, *, dettaglio: bool = False) -> dict:
        analisi = r.get("analysis") or {}
        raw = r.get("raw") or {}
        offerta = {
            "punteggio": r["score"],
            "motivo": (analisi.get("perche") or r["reason"]) if dettaglio else r["reason"],
            "quando": _iso(r["evaluated_at"]),
            "titolo": r["title"], "azienda": r["organization"], "url": r["url"],
            "link_kind": r["link_kind"], "id": r["id"],
            "tipo_datore": r["employer_kind"],
            "citta": (r["cities"] or [None])[0],
            "stipendio": _fmt_stipendio(r["salary"], lingua or "en") or None,
            "logo": f"/logo/{r['logo_slug']}" if r["logo_slug"] else None,
            "archiviata": r["purged_at"] is not None,
            "salvata": r["saved_at"] is not None,
            "scartata": r["dismissed_at"] is not None,
            "motivo_scarto": r["dismiss_reason"],
            "in_archivio": r["archived_at"] is not None,
            "candidatura": r["application_status"],
            "applicata_il": _iso(r["applied_at"]),
            "action_version": r["action_version"],
            "azioni_aggiornate_il": _iso(r["user_action_at"]),
            "note": r["note"],
            "prossima_azione": r["next_action"],
            "prossima_azione_il": _iso(r["next_action_at"]),
            "fonte": r["source"],
            "pubblicata_il": (_iso(r["date_posted"])
                              if r["date_posted_declared"] is True else None),
            "valid_through": _iso(r["date_valid_through"]),
            "prima_rilevazione": _iso(r["first_seen_at"]),
            "ultima_verifica": _iso(r["last_seen_alive_at"]),
            "offerta_status": r["offerta_status"],
            "ricerca_ids": list(r["ricerca_ids"] or []),
            "ricerca_eleggibile_ids": list(r["ricerca_eleggibile_ids"] or []),
        }
        if dettaglio:
            offerta["annuncio"] = {
                "responsabilita": (analisi.get("responsabilita")
                                   or _campo_testo(raw.get("ai_core_responsibilities"))),
                "requisiti": (analisi.get("requisiti")
                              or _campo_testo(raw.get("ai_requirements_summary"))),
                "benefit": (analisi.get("benefit")
                            or _campo_testo(raw.get("ai_benefits"))),
                "orario": _campo_testo(raw.get("ai_working_hours")),
            }
            offerta["analisi"] = ({"pro": analisi.get("pros") or [],
                                   "attenzioni": analisi.get("cons") or []}
                                  if analisi else None)
            offerta["evidence"] = r.get("evidence")
            offerta["colloqui"] = list(r.get("colloqui") or [])
            offerta["documenti"] = list(r.get("documenti") or [])
        return offerta

    def _riga_offerta(cur, uid: str, match_id, *, completa: bool = False) -> dict:
        extra = ", m.analysis AS analysis, j.raw AS raw" if completa else ""
        cur.execute(
            f"SELECT {_OFFERTE_COLONNE} {extra} {_OFFERTE_FROM} "
            "WHERE m.id = %s AND m.user_id = %s",
            (str(match_id), uid))
        riga = cur.fetchone()
        if riga is None:
            raise HTTPException(404, "posting not found")
        return riga

    def _carica_collezioni(cur, uid: str, riga: dict) -> dict:
        cur.execute(
            "SELECT id::text AS id, starts_at, note, reminder_minutes "
            "FROM match_interviews WHERE match_id = %s AND user_id = %s "
            "ORDER BY starts_at, id", (riga["id"], uid))
        riga["colloqui"] = [{
            "id": r["id"], "inizio": _iso(r["starts_at"]), "note": r["note"],
            "promemoria_minuti": r["reminder_minutes"],
        } for r in cur.fetchall()]
        cur.execute(
            "SELECT id::text AS id, original_filename, mime_type, size_bytes, created_at "
            "FROM match_documents WHERE match_id = %s AND user_id = %s "
            "ORDER BY created_at, id", (riga["id"], uid))
        riga["documenti"] = [{
            "id": r["id"], "nome": r["original_filename"], "mime": r["mime_type"],
            "dimensione": r["size_bytes"], "creato_il": _iso(r["created_at"]),
        } for r in cur.fetchall()]
        return riga

    def _offerta_canonica(cur, uid: str, match_id) -> dict:
        riga = _riga_offerta(cur, uid, match_id, completa=True)
        return _serializza_offerta(_carica_collezioni(cur, uid, riga), "en", dettaglio=True)

    def _stato_attuale(r: dict) -> tuple:
        return (r["saved_at"] is not None, r["dismissed_at"] is not None,
                r["dismiss_reason"], r["archived_at"] is not None,
                r["application_status"], r["note"], r["next_action"],
                r["next_action_at"])

    def _conflitto_stato(r: dict) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": "stato cambiato: rileggi la versione corrente",
                     "offerta": _serializza_offerta(r, "en")})

    @app.get("/me/offerte")
    def mie_offerte(
            vista: Literal["match", "azioni", "salvate", "candidature",
                           "scartate", "archivio"] = Query("match"),
            ricerca_id: UUID | None = Query(None),
            uid: str = Depends(utente), conn=Depends(connessione),
            limite: int = Query(40, ge=1, le=100),
            offset: int = Query(0, ge=0), lingua: str = Query("en")):
        ricerca_uuid = None
        if ricerca_id is not None:
            with conn.cursor() as cur:
                ricerca_uuid, _ = _ricerca_identificata(cur, uid, str(ricerca_id))
            if ricerca_uuid is None:
                raise HTTPException(404, "ricerca inesistente")

        where = ["m.user_id = %s"]
        params: list = [uid]
        azione = (
            "m.saved_at IS NOT NULL OR m.dismissed_at IS NOT NULL "
            "OR m.archived_at IS NOT NULL OR m.application_status <> 'not_started' "
            "OR m.note IS NOT NULL OR m.next_action IS NOT NULL "
            "OR m.next_action_at IS NOT NULL "
            "OR EXISTS (SELECT 1 FROM match_interviews mi "
            "           WHERE mi.match_id = m.id AND mi.user_id = m.user_id) "
            "OR EXISTS (SELECT 1 FROM match_documents md "
            "           WHERE md.match_id = m.id AND md.user_id = m.user_id)")
        if vista == "match":
            if ricerca_uuid is None:
                where.append(match_attivo)
            else:
                where.append("""EXISTS (
                    SELECT 1 FROM job_clusters jc_f
                    JOIN user_clusters uc_f ON uc_f.cluster_id = jc_f.cluster_id
                    JOIN clusters c_f ON c_f.id = uc_f.cluster_id
                    WHERE jc_f.job_id = m.job_id AND uc_f.id = %s
                      AND uc_f.user_id = m.user_id AND NOT uc_f.is_paused
                      AND c_f.status = 'active'
                      AND m.score >= COALESCE(uc_f.min_score, 70))""")
                params.append(ricerca_uuid)
            where += ["j.status = 'active'", "m.dismissed_at IS NULL",
                      "m.archived_at IS NULL", "m.application_status = 'not_started'"]
        else:
            if vista == "azioni":
                where.append(f"({azione})")
            elif vista == "salvate":
                where.append("m.saved_at IS NOT NULL")
            elif vista == "candidature":
                where.append("m.application_status <> 'not_started'")
            elif vista == "scartate":
                where.append("m.dismissed_at IS NOT NULL")
            else:
                where.append("m.archived_at IS NOT NULL")
            if ricerca_uuid is not None:
                where.append("""EXISTS (
                    SELECT 1 FROM job_clusters jc_f
                    JOIN user_clusters uc_f ON uc_f.cluster_id = jc_f.cluster_id
                    WHERE jc_f.job_id = m.job_id AND uc_f.id = %s
                      AND uc_f.user_id = m.user_id)""")
                params.append(ricerca_uuid)

        condizione = " AND ".join(f"({parte})" for parte in where)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT count(*) AS totale {_OFFERTE_FROM} WHERE {condizione}",
                        params)
            totale = cur.fetchone()["totale"]
            cur.execute(
                f"SELECT {_OFFERTE_COLONNE} {_OFFERTE_FROM} WHERE {condizione} "
                "ORDER BY m.evaluated_at DESC, m.id DESC LIMIT %s OFFSET %s",
                params + [limite, offset])
            righe = cur.fetchall()
            cur.execute("SELECT count(*) AS valutate FROM matches WHERE user_id = %s",
                        (uid,))
            valutate = cur.fetchone()["valutate"]
        return {"valutate": valutate,
                "offerte": [_serializza_offerta(r, lingua) for r in righe],
                "totale": totale}

    @app.get("/me/digest")
    def storico_digest(uid: str = Depends(utente), conn=Depends(connessione)):
        """Le lettere realmente consegnate, con i match inclusi."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT d.id::text, d.sent_at, d.scheduled_for, d.status, "
                "       d.jobs_evaluated_count, d.jobs_sent_count, "
                "       COALESCE(array_agg(di.match_id::text) FILTER "
                "         (WHERE di.match_id IS NOT NULL), '{}') "
                "FROM digests d LEFT JOIN digest_items di ON di.digest_id = d.id "
                "WHERE d.user_id = %s AND d.status IN ('sent', 'skipped_empty') "
                "GROUP BY d.id ORDER BY d.scheduled_for DESC LIMIT 30", (uid,))
            return [{
                "id": r[0],
                "timestamp": (r[1] or r[2]).isoformat(),
                "kind": "silence" if r[3] == "skipped_empty" or r[5] == 0 else "digest",
                "readCount": r[4],
                "selectedCount": r[5],
                "read": True,
                "matchIds": r[6],
            } for r in cur.fetchall()]

    # ── il portale clienti B2B: le chiavi API dell'utente (27/09/2026) ──
    # Self-service: il cliente crea e revoca da solo. La chiave in chiaro
    # esce UNA volta, alla creazione; in tabella resta lo sha256. Al primo
    # accesso nasce la chiave trial da 500 crediti una tantum, senza rinnovo.
    from nivult.api_clienti import chiavi as _chiavi

    @app.exception_handler(_chiavi.ContatoreNonDisponibile)
    async def contatore_non_disponibile(request: Request, exc: Exception):
        return JSONResponse({"detail": "credit metering temporarily unavailable"},
                            status_code=503)

    @app.get("/me/chiavi")
    def mie_chiavi(uid: str = Depends(utente), conn=Depends(connessione)):
        extra = _chiavi.extra_per_utente(uid)
        lista, trial = _chiavi.trial_per_utente(uid)
        interno = _chiavi.riepilogo_per_utente(uid)
        saldo = {
            "monthly_credits": interno["crediti_mensili"],
            "used": interno["usati"],
            "monthly_remaining": interno["residui_mensili"],
            "purchased_credits": interno["crediti_extra"],
            "balance": interno["saldo"],
            "usage_month": interno["mese_uso"],
        }
        rivelazioni = _chiavi.conta_rivelazioni(uid)
        with conn.cursor() as cur:
            cur.execute("SELECT EXISTS(SELECT 1 FROM portale_uso "
                        "WHERE user_id = %s AND via = 'api')", (uid,))
            prima_chiamata = cur.fetchone()[0]
        base = {"extra": extra, "saldo": saldo, "rivelazioni": rivelazioni,
                "prima_chiamata_fatta": prima_chiamata,
                "abbonamento": _chiavi.abbonamento_per_utente(uid)}
        if lista:
            return {"chiavi": lista, **base}
        # primo accesso: la chiave trial SI MOSTRA in chiaro questa
        # volta sola — in tabella resta solo l'hash
        return {"chiavi": _chiavi.lista_per_utente(uid), **base,
                "chiave_nuova": trial,
                "nota": "la chiave si vede solo ora: conservala tu"}

    @app.post("/me/chiavi")
    async def nuova_chiave(request: Request, uid: str = Depends(utente)):
        # body opzionale: {"etichetta": "..."} — lunga al massimo 80 char
        try:
            corpo = await request.json()
        except Exception:                            # noqa: BLE001
            corpo = {}
        etichetta = str((corpo or {}).get("etichetta") or "").strip()[:80] \
            or "Portal key"
        chiave, chiave_id = _chiavi.nuova(etichetta, 500, user_id=uid)
        return {"chiave": chiave, "id": chiave_id,
                "nota": "la chiave si vede solo ora: conservala tu"}

    @app.delete("/me/chiavi/{chiave_id}")
    def revoca_mia_chiave(chiave_id: str, uid: str = Depends(utente)):
        if not _chiavi.revoca_per_utente(chiave_id, uid):
            raise HTTPException(404, "key not found")
        return {"revocata": True}

    # ── billing Creem (27/09/2026): checkout ospitato + webhook firmato ──
    from nivult.api_clienti import billing as _billing
    from nivult.api_clienti import dati as _dati
    from nivult.api_clienti import explorer as _explorer
    from nivult.api_clienti import vivo as _vivo

    def _abuso_explorer(request: Request, uid: str, motivo: str) -> None:
        log.warning(json.dumps({"event": "explorer_abuse", "reason": motivo,
                                "user_id": uid, "ip": _ip(request)}))

    def _freno_portale(request: Request, uid: str) -> None:
        try:
            _chiavi.consuma_quota_explorer(uid, _ip(request))
        except _chiavi.QuotaExplorer as exc:
            _abuso_explorer(request, uid, exc.quota)
            raise HTTPException(
                429, "Explorer rate limit exceeded; use the API or Filtered Export for volume",
                headers={"Retry-After": str(exc.retry_after)}) from exc
        except _chiavi.ChiaveInvalida as exc:
            raise HTTPException(401, "sessione non valida") from exc
        except _chiavi.ContatoreNonDisponibile as exc:
            raise HTTPException(503, "Explorer quota temporarily unavailable") from exc

    def _preview_error(request: Request, uid: str, message: str) -> HTTPException:
        _abuso_explorer(request, uid, "pagination_cap")
        return HTTPException(400, {"code": "preview_limit", "message": message,
                                   "alternatives": ["/v1/jobs", "/v1/companies",
                                                    "Filtered Export"]})

    def _intero(corpo: dict, chiave: str, default: int) -> int:
        valore = corpo.get(chiave, default)
        if isinstance(valore, bool):
            raise ValueError
        return int(valore)

    def _prepara_ricerca(corpo: dict, request: Request, uid: str,
                         kind: str) -> tuple[str | None, int, int]:
        try:
            limite = _intero(corpo, "limit", 20)
            offset = _intero(corpo, "offset", 0)
            pagina = _intero(corpo, "page", offset // max(limite, 1) + 1 if offset else 1)
        except (TypeError, ValueError) as exc:
            raise _preview_error(request, uid, "invalid pagination") from exc
        if limite < 1 or limite > _explorer.MAX_LIMIT:
            raise _preview_error(request, uid, "Explorer pages contain at most 20 records")
        if pagina < 1 or pagina > _explorer.MAX_PAGE or offset < 0 or offset > 80:
            raise _preview_error(request, uid, "Explorer preview stops at page 5 / offset 80")
        token = corpo.get("cursor")
        if not token:
            if pagina != 1 or offset:
                raise _preview_error(request, uid, "deep paging requires the returned cursor")
            return None, 1, limite
        try:
            interno, pagina_cursore = _explorer.open_cursor(token, uid, kind)
        except _explorer.TokenError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        pagina_attesa = (offset // limite + 1 if offset else pagina
                         if "page" in corpo else pagina_cursore)
        if pagina_attesa != pagina_cursore:
            raise _preview_error(request, uid, "cursor/page mismatch")
        return interno, pagina_cursore, limite

    def _cursore_risposta(uid: str, kind: str, interno: str | None,
                          pagina: int) -> tuple[str | None, bool]:
        cap = bool(interno) and pagina >= _explorer.MAX_PAGE
        if not interno or cap:
            return None, cap
        return _explorer.seal_cursor(uid, kind, interno, pagina + 1), False

    def _riferimento(riga: dict) -> str:
        ats = riga.get("ats") or "unknown"
        slug = riga.get("company_slug") or "anonymous-" + str(riga.get("id") or "")
        base = f"{ats}:{slug}"
        if riga.get("source_type") not in ("aggregator", "staffing", "public_board"):
            return base
        employer = _explorer.employer_key(riga.get("company"))
        if not employer:
            employer = "anonymous" + re.sub(
                r"[^a-z0-9]", "", str(riga.get("id") or "unknown").lower())
        return f"{base}::employer:{employer}"

    def _board_non_scoped(riferimento: str, profilo: dict) -> bool:
        if "::employer:" in riferimento:
            return False
        return (_explorer.is_aggregator(profilo)
                or bool(profilo.get("_multi_employer")))

    def _controlla_origine(request: Request) -> None:
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            raise HTTPException(403, "cross-site request rejected")
        origin = request.headers.get("origin")
        if origin and origin not in origini:
            raise HTTPException(403, "origin rejected")

    @app.post("/portale/cerca")
    async def cerca_nel_portale(request: Request, uid: str = Depends(utente)):
        try:
            corpo = await request.json()
        except Exception:                            # noqa: BLE001
            corpo = {}
        corpo = corpo or {}
        if any(corpo.get(k) for k in ("company", "domain", "company_domain", "company_slug")):
            raise HTTPException(400, {"code": "company_filter_unavailable",
                                      "message": "Use /v1/jobs for company/domain filters"})
        _freno_portale(request, uid)
        cursore, pagina, limite = _prepara_ricerca(corpo, request, uid, "jobs")
        filtri = {k: v for k, v in corpo.items()
                  if k in ("country", "category", "ats", "seniority",
                           "language", "remote", "q", "technology", "dal")}
        try:
            if corpo.get("stato") == "chiuse":
                righe, prossimo, totale = await run_in_threadpool(
                    _dati.cerca_chiuse_portale, filtri, cursore, limite)
            elif filtri.get("q"):
                righe, prossimo, totale = await run_in_threadpool(
                    _vivo.cerca_portale, filtri, cursore, limite)
            else:
                righe, prossimo, totale = await run_in_threadpool(
                    _dati.cerca_portale, filtri, cursore, limite)
        except ValueError as exc:
            raise HTTPException(400, "invalid filters or cursor") from exc
        righe = await run_in_threadpool(_vivo.completa_titoli_portale, righe)
        riferimenti = [_riferimento(r) for r in righe]
        sue = await run_in_threadpool(_chiavi.rivelate_per_utente, uid, "azienda", riferimenti)
        profili = await run_in_threadpool(_dati.profili_aziende, riferimenti)
        data = []
        for riga, rif in zip(righe, riferimenti, strict=True):
            rivelata = rif in sue
            token = _explorer.reveal_token(uid, rif)
            data.append(_explorer.job_dto(riga, rivelata, token,
                                          _explorer.reveal_group(rif),
                                          profili.get(rif)))
        prossimo, cap = _cursore_risposta(uid, "jobs", prossimo, pagina)
        return {"data": data, "next_cursor": prossimo, "totale": totale,
                "cap_reached": cap}

    @app.post("/portale/cerca-aziende")
    async def cerca_aziende_nel_portale(request: Request, uid: str = Depends(utente)):
        try:
            corpo = await request.json()
        except Exception:                            # noqa: BLE001
            corpo = {}
        corpo = corpo or {}
        if any(corpo.get(k) for k in ("q", "company", "domain", "company_domain",
                                      "company_slug")):
            raise HTTPException(400, {"code": "company_filter_unavailable",
                                      "message": "Use /v1/companies for company search"})
        _freno_portale(request, uid)
        cursore, pagina, limite = _prepara_ricerca(corpo, request, uid, "companies")
        filtri = {k: v for k, v in corpo.items() if k in ("country", "industry", "technology")}
        try:
            righe, prossimo, totale = await run_in_threadpool(
                _dati.cerca_aziende_portale, filtri, cursore, limite)
        except ValueError as exc:
            raise HTTPException(400, "invalid filters or cursor") from exc
        riferimenti = [r["ref"] for r in righe]
        sue = await run_in_threadpool(_chiavi.rivelate_per_utente, uid, "azienda", riferimenti)
        data = [_explorer.company_dto(r, r["ref"] in sue,
                                      _explorer.reveal_token(uid, r["ref"]),
                                      _explorer.reveal_group(r["ref"])) for r in righe]
        prossimo, cap = _cursore_risposta(uid, "companies", prossimo, pagina)
        return {"data": data, "next_cursor": prossimo, "totale": totale,
                "cap_reached": cap}

    @app.post("/portale/offerta-dettaglio")
    async def dettaglio_offerta_portale(request: Request, uid: str = Depends(utente)):
        try:
            corpo = await request.json()
        except Exception:                            # noqa: BLE001
            corpo = {}
        offerta_id = str((corpo or {}).get("id") or "").strip()
        if not offerta_id:
            raise HTTPException(400, "missing id")
        _freno_portale(request, uid)
        riga = await run_in_threadpool(_vivo.rivela_offerta, offerta_id)
        if riga is None:
            raise HTTPException(404, "posting not found")
        rif = _riferimento(riga)
        rivelata = await run_in_threadpool(_chiavi.e_rivelata, uid, "azienda", rif)
        job_rivelata = rivelata or await run_in_threadpool(
            _chiavi.e_rivelata, uid, "job", offerta_id)
        profili = await run_in_threadpool(_dati.profili_aziende, [rif])
        token = _explorer.reveal_token(uid, rif)
        gruppo = _explorer.reveal_group(rif)
        offerta = _explorer.detail_dto(riga, rivelata, token, gruppo, profili.get(rif),
                                       job_rivelata)
        return {"offerta": offerta, "rivelata": job_rivelata,
                "job_rivelata": job_rivelata, "azienda_rivelata": rivelata,
                "reveal_token": token, "reveal_group": gruppo,
                **({"company_ref": rif} if rivelata else {})}

    @app.post("/portale/rivela")
    async def rivela_nel_portale(request: Request, uid: str = Depends(utente)):
        _controlla_origine(request)
        raise HTTPException(
            410, "Per-job unlocks are no longer available; reveal the company for 3 credits")

    @app.post("/portale/rivela-azienda")
    async def rivela_azienda_nel_portale(request: Request, uid: str = Depends(utente)):
        _controlla_origine(request)
        try:
            corpo = await request.json()
        except Exception:                            # noqa: BLE001
            corpo = {}
        token = str((corpo or {}).get("reveal_token") or "").strip()
        if not token:
            raise HTTPException(400, "missing reveal_token")
        _freno_portale(request, uid)
        try:
            rif = _explorer.company_from_token(token, uid)
        except _explorer.TokenError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        riga = await run_in_threadpool(_dati.rivela_azienda, rif)
        if riga is None:
            raise HTTPException(404, "company not found")
        if _board_non_scoped(rif, riga):
            raise HTTPException(409, "Open an individual job to reveal its employer")
        riga = _explorer.revealed_company_profile(riga)
        riga["company_ref"] = rif
        try:
            esito = await run_in_threadpool(_chiavi.rivela_per_utente, uid, "azienda", rif)
        except _chiavi.ContatoreNonDisponibile as exc:
            raise HTTPException(503, "reveal temporarily unavailable; retry with the same token") from exc
        if esito == "account_sospeso":
            raise HTTPException(403, "account paused — contact support")
        if esito == "senza_crediti":
            raise HTTPException(429, "3 credits required — top up from the dashboard")
        if esito == "ok":
            await run_in_threadpool(_chiavi.registra_uso, uid, "portale",
                                    "company unlock", rif,
                                    crediti=_chiavi.COSTO_COMPANY)
        saldo = await run_in_threadpool(_chiavi.saldo_per_utente, uid)
        return {"azienda": riga, "esito": esito,
                "reveal_token": _explorer.reveal_token(uid, rif),
                "reveal_group": _explorer.reveal_group(rif), "saldo": saldo}

    @app.post("/portale/token-azienda")
    async def rinnova_token_azienda(request: Request, uid: str = Depends(utente)):
        _controlla_origine(request)
        try:
            corpo = await request.json()
        except Exception:                            # noqa: BLE001
            corpo = {}
        rif = str((corpo or {}).get("company_ref") or "").strip()
        if not rif or len(rif) > 300 or ":" not in rif:
            raise HTTPException(400, "invalid company_ref")
        _freno_portale(request, uid)
        if not await run_in_threadpool(_chiavi.e_rivelata, uid, "azienda", rif):
            raise HTTPException(402, "reveal the company first")
        return {"reveal_token": _explorer.reveal_token(uid, rif),
                "reveal_group": _explorer.reveal_group(rif)}

    @app.post("/portale/azienda-jobs")
    async def jobs_dell_azienda(request: Request, uid: str = Depends(utente)):
        try:
            corpo = await request.json()
        except Exception:                            # noqa: BLE001
            corpo = {}
        token = str((corpo or {}).get("reveal_token") or "").strip()
        if not token:
            raise HTTPException(400, "missing reveal_token")
        _freno_portale(request, uid)
        try:
            rif = _explorer.company_from_token(token, uid)
        except _explorer.TokenError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        if not await run_in_threadpool(_chiavi.e_rivelata, uid, "azienda", rif):
            raise HTTPException(402, "reveal the company first")
        profilo = await run_in_threadpool(_dati.rivela_azienda, rif)
        if profilo is not None and _board_non_scoped(rif, profilo):
            raise HTTPException(409, "Open an individual job to reveal its employer")
        righe = await run_in_threadpool(_dati.azienda_jobs_portale, rif)
        righe = await run_in_threadpool(_vivo.completa_titoli_portale, righe)
        return {"data": righe}

    @app.get("/portale/catalogo/{campo}")
    async def catalogo_export_portale(
            campo: str, q: str = Query(default="", max_length=80),
            limite: int = Query(default=25, alias="limit", ge=1, le=30),
            uid: str = Depends(utente)):
        if campo not in _dati._CATALOGO_CAMPI:
            raise HTTPException(404, "unknown catalog field")
        try:
            valori = await run_in_threadpool(_dati.catalogo_export, campo, q, limite)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse(valori, headers={"Cache-Control": "private, max-age=30"})

    @app.get("/portale/export/{nome}")
    def export_dal_portale(nome: str, request: Request, uid: str = Depends(utente)):
        """Daily Bulk incluso in Scale attivo: nessun addebito, ma sessione,
        rate-limit, entitlement e last_used restano controllati."""
        if nome not in ("offerte", "aziende"):
            raise HTTPException(404, "unknown export")
        _freno_portale(request, uid)
        attive = [k for k in _chiavi.lista_per_utente(uid)
                  if not k["revocata_il"]]
        if not attive:
            raise HTTPException(402, "create an API key first")
        abbonamento = _chiavi.abbonamento_per_utente(uid)
        cliente = {**abbonamento, "user_id": uid,
                   "monthly_tier": _chiavi.volume_per_utente(uid)}
        api_clienti_v1._richiedi_volume(
            cliente, api_clienti_v1.SOGLIA_BULK, "the daily bulk export")
        percorso = (_dati.stato_export().get("file") or {}).get(nome)
        if not percorso or not os.path.isfile(percorso):
            raise HTTPException(404, "today's export is not ready yet")
        _chiavi.registra_servizio_chiave(attive[0]["id"])
        return StreamingResponse(
            _dati.stream_export_senza_pii(percorso), media_type="application/gzip",
            headers={"Content-Disposition": "attachment; filename=\""
                     + os.path.basename(percorso) + "\""})

    # ── l'export filtrato del portale: stessa tariffa della Public API ──
    EXPORT_COSTO_POSTING = 1
    EXPORT_COSTO_COMPANY = 3
    # il tetto tiene conto di Cloudflare: oltre i 100 secondi di attesa la
    # risposta cade; misurato il 28/09: 83k righe = ~55s di preparazione.
    EXPORT_TETTO_RIGHE = 100_000

    def _max_rows_export(corpo: dict) -> int:
        presenti = [(k, corpo[k]) for k in ("max_rows", "limit")
                    if k in corpo and corpo[k] is not None]
        if not presenti:
            return EXPORT_TETTO_RIGHE
        convertiti = []
        for nome, valore in presenti:
            if isinstance(valore, bool) or (
                    isinstance(valore, float) and not valore.is_integer()):
                raise HTTPException(400, f"{nome} must be an integer")
            try:
                n = int(valore)
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, f"{nome} must be an integer") from exc
            if not 1 <= n <= EXPORT_TETTO_RIGHE:
                raise HTTPException(
                    400, f"{nome} must be between 1 and {EXPORT_TETTO_RIGHE}")
            convertiti.append(n)
        if len(set(convertiti)) != 1:
            raise HTTPException(400, "max_rows and limit must match when both are sent")
        return convertiti[0]

    def _preventivo_export(tipo: str, matching_rows: int, saldo: int,
                           max_rows: int) -> dict:
        unit_cost = EXPORT_COSTO_COMPANY if tipo == "companies" else EXPORT_COSTO_POSTING
        export_rows = min(matching_rows, max_rows)
        exact_cost = export_rows * unit_cost
        return {
            "matching_rows": matching_rows,
            "export_rows": export_rows,
            "unit_cost": unit_cost,
            "exact_cost": exact_cost,
            "affordable_rows": saldo // unit_cost,
            "max_allowed_rows": EXPORT_TETTO_RIGHE,
            "max_rows": max_rows,
            "balance_after_export": saldo - exact_cost,
            # Campi legacy mantenuti durante l'aggiornamento del portale.
            "righe": matching_rows, "crediti": exact_cost, "saldo": saldo,
            "tipo": tipo, "tetto": EXPORT_TETTO_RIGHE,
            "troppo": matching_rows > EXPORT_TETTO_RIGHE,
        }

    def _crediti_insufficienti_export(preventivo: dict) -> HTTPException:
        return HTTPException(402, {
            "error": "insufficient_credits",
            "message": f"this export costs {preventivo['exact_cost']} credits; "
                       f"your balance is {preventivo['saldo']}",
            "required_credits": preventivo["exact_cost"],
            "balance": preventivo["saldo"],
            "affordable_rows": preventivo["affordable_rows"],
            "max_allowed_rows": preventivo["max_allowed_rows"],
        })

    def _filtri_export(corpo: dict, tipo: str) -> dict:
        try:
            return _dati.normalizza_filtri_export(corpo, tipo)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/portale/export/stima")
    async def stima_export_portale(request: Request, uid: str = Depends(utente)):
        try:
            corpo = await request.json()
        except Exception:                            # noqa: BLE001
            corpo = {}
        if not isinstance(corpo, dict):
            corpo = {}
        tipo = "companies" if corpo.get("tipo") == "companies" else "postings"
        max_rows = _max_rows_export(corpo)
        filtri = _filtri_export(corpo, tipo)
        stima = _dati.stima_export_aziende if tipo == "companies" else _dati.stima_export
        righe = await run_in_threadpool(stima, filtri)
        saldo = await run_in_threadpool(_chiavi.saldo_per_utente, uid)
        return _preventivo_export(tipo, righe, saldo, max_rows)

    @app.post("/portale/export/scarica")
    async def scarica_export_portale(request: Request, uid: str = Depends(utente)):
        try:
            corpo = await request.json()
        except Exception:                            # noqa: BLE001
            corpo = {}
        if not isinstance(corpo, dict):
            corpo = {}
        tipo = "companies" if corpo.get("tipo") == "companies" else "postings"
        max_rows = _max_rows_export(corpo)
        filtri = _filtri_export(corpo, tipo)
        # DuckDB e Postgres qui sotto sono chiamate SINCROME e pesanti:
        # sul thread dell'event loop un export lento inchioda TUTTA l'API.
        stima = _dati.stima_export_aziende if tipo == "companies" else _dati.stima_export
        righe = await run_in_threadpool(stima, filtri)
        saldo = await run_in_threadpool(_chiavi.saldo_per_utente, uid)
        preventivo = _preventivo_export(tipo, righe, saldo, max_rows)
        if preventivo["export_rows"] == 0:
            raise HTTPException(404, "no rows match these filters — 0 credits charged")
        if saldo < preventivo["exact_cost"]:
            raise _crediti_insufficienti_export(preventivo)

        # Il file viene completato prima dell'addebito: errori di generazione
        # non bruciano crediti e il costo finale usa solo le righe scritte.
        import tempfile
        fd, percorso = tempfile.mkstemp(suffix=".jsonl.gz",
                                        prefix="export-portale-")
        os.close(fd)
        archivio = None
        addebito_fatto = False

        def _pulisci_export_temp(include_archivio: bool = True):
            for path in (percorso, archivio if include_archivio else None):
                if not path:
                    continue
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    continue
                except OSError:
                    log.warning("cleanup export temporaneo non riuscito: %s", path,
                                exc_info=True)

        try:
            scrivi = (_dati.scrivi_export_aziende if tipo == "companies"
                      else _dati.scrivi_export)
            scritte = await run_in_threadpool(
                scrivi, filtri, percorso, preventivo["export_rows"])
            if scritte > preventivo["export_rows"]:
                raise HTTPException(
                    500, "export row limit was not enforced — 0 credits charged")
            if scritte == 0:
                raise HTTPException(404, "no rows match these filters — 0 credits charged")
            costo = scritte * preventivo["unit_cost"]
            filename = f"nivult-{tipo}-{scritte}-righe.jsonl.gz"
            directory = os.environ.get("PORTAL_EXPORT_DIR", "/opt/nivult/exports/filtered")
            os.makedirs(directory, exist_ok=True)
            archivio = os.path.join(directory, secrets.token_hex(16) + ".jsonl.gz")
            await run_in_threadpool(shutil.move, percorso, archivio)
            try:
                pagato = await run_in_threadpool(
                    _chiavi.spendi_export_archiviato_per_utente, uid, costo,
                    dettaglio=f"{scritte} rows" + (f" · {filtri}" if filtri else ""),
                    percorso=archivio, filename=filename, tipo=tipo,
                    filtri=filtri, righe=scritte)
            except _chiavi.ContatoreNonDisponibile as exc:
                raise HTTPException(503, "billing temporarily unavailable; retry") from exc
            if not pagato:
                saldo_attuale = await run_in_threadpool(_chiavi.saldo_per_utente, uid)
                effettivo = _preventivo_export(tipo, scritte, saldo_attuale, scritte)
                raise _crediti_insufficienti_export(effettivo)
            addebito_fatto = True
            return FileResponse(
                archivio, media_type="application/gzip", filename=filename,
                headers={"X-Credits-Charged": str(costo), "X-Export-Rows": str(scritte),
                         "X-Download-Id": pagato["download_id"]})
        except BaseException:
            _pulisci_export_temp(include_archivio=not addebito_fatto)
            raise

    @app.get("/portale/export-file/{download_id}")
    def scarica_export_archiviato(download_id: str, request: Request,
                                  uid: str = Depends(utente)):
        _freno_portale(request, uid)
        try:
            download_id = str(UUID(download_id))
        except ValueError as exc:
            raise HTTPException(400, "invalid download id") from exc
        record = _chiavi.export_file_per_utente(uid, download_id)
        if record is None:
            raise HTTPException(404, "export not found")
        if not os.path.isfile(record["percorso"]):
            raise HTTPException(410, "this export is no longer available")
        return FileResponse(
            record["percorso"], media_type="application/gzip",
            filename=record["filename"],
            headers={"X-Credits-Charged": "0",
                     "X-Export-Rows": str(record["righe"]),
                     "X-Download-Id": record["download_id"]})

    def _bel_nome(rif: str) -> str:
        """Dal rif tecnico a un nome presentabile, quando il record non si
        risolve piu' (l'azienda non e' nell'export di oggi): «workday:stryker»
        diventa «Stryker», «jsonld:careers-jrmc» diventa «Careers Jrmc».
        Mai il rif grezzo in pagina (04/10/2026)."""
        slug = rif.split(":", 1)[-1]
        # «careers.recordati.com» -> «recordati»; «dm-jobs.it» -> «dm-jobs»
        parti = slug.split(".")
        if len(parti) > 1:
            nucleo = parti[1] if parti[0] in (
                "careers", "jobs", "job", "talent", "workats", "career",
                "recruiting", "join") and len(parti) > 2 else parti[0]
        else:
            nucleo = slug
        return re.sub(r"[-_]+", " ", nucleo).strip().title() or rif

    def _favicon(dominio: str | None) -> str | None:
        """Il logo d'azienda dal dominio: il favicon di Google SCARICATO da
        noi e servito dal nostro endpoint /favicon (07/10/2026). Prima era
        il link diretto a google.com in pagina: su nivult.com la CSP della
        zona (img-src 'self' + api.nivult.com) lo bloccava e i loghi
        sparivano dalla finestra dei consumi — e il link diretto avrebbe
        comunque detto a Google chi apre il pannello e quando."""
        if not dominio:
            return None
        return f"{oauth.api_url()}/favicon/{dominio}"

    def _logo_rif(riferimento: str | None) -> str | None:
        """Il logo come nella lista delle offerte: /logo/<slug>, l'endpoint
        che pesca dall'archivio dei jobs (11/10/2026). Prima qui si usava
        solo il favicon del dominio: piu' povero, e per aziende senza
        dominio risolto il logo mancava mentre nelle offerte c'era.
        Il frontend ha gia' il ripiego a lettera se il 404 arriva."""
        if not riferimento or ":" not in riferimento:
            return None
        return f"{oauth.api_url()}/logo/{riferimento.split(':', 1)[1]}"

    @app.get("/portale/uso")
    def uso_del_portale(uid: str = Depends(utente), conn=Depends(connessione)):
        """Il registro dei consumi (04/10/2026): chi paga vede DOVE vanno i
        crediti — per via (api/portale/export), per giorno, evento per
        evento — e le aziende sbloccate coi nomi. La card Credits mostra
        il saldo; questo risponde a «dove sono finiti?».

        Ogni evento porta un TITOLO leggibile (il nome dell'azienda, il
        titolo dell'offerta) e, quando c'e', il LOGO dal dominio: gli
        identificativi tecnici restano come dettaglio secondario, mai
        come testo principale."""
        riepilogo = _chiavi.riepilogo_per_utente(uid)
        extra = riepilogo["crediti_extra"]
        mensile = riepilogo["residui_mensili"]
        # Lo speso del mese lo dice il contatore che fa scattare i 429,
        # non il registro: portale_uso nasce il 04/10/2026 e le chiamate
        # API e gli export di prima non sono dettagliati da nessuna parte.
        speso_reale = int(riepilogo["usati"])
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT via, count(*), sum(crediti) FROM portale_uso "
                    "WHERE user_id = %s AND at >= date_trunc('month', now()) "
                    "GROUP BY via", (uid,))
                per_via = {r[0]: {"eventi": r[1], "crediti": r[2]}
                           for r in cur.fetchall()}
                cur.execute(
                    "SELECT at::date, sum(crediti) FROM portale_uso "
                    "WHERE user_id = %s AND at > now() - interval '30 days' "
                    "GROUP BY 1 ORDER BY 1", (uid,))
                per_giorno = [{"giorno": str(r[0]), "crediti": r[1]}
                              for r in cur.fetchall()]
                cur.execute(
                    "SELECT u.at, u.via, u.azione, u.dettaglio, u.crediti, "
                    "       f.id::text, f.tipo, f.righe "
                    "FROM portale_uso u "
                    "LEFT JOIN portale_export_files f ON f.usage_id = u.id "
                    "WHERE u.user_id = %s ORDER BY u.at DESC LIMIT 60", (uid,))
                eventi = [{"at": r[0].isoformat(), "via": r[1],
                           "azione": r[2], "dettaglio": r[3], "crediti": r[4],
                           "download_id": r[5], "tipo_export": r[6],
                           "righe_export": r[7]} for r in cur.fetchall()]
                cur.execute(
                    "SELECT riferimento, revealed_at FROM portale_rivelazioni "
                    "WHERE user_id = %s AND tipo = 'azienda' "
                    "ORDER BY revealed_at DESC LIMIT 60", (uid,))
                rivelazioni = [{"ref": r[0], "at": r[1].isoformat()}
                               for r in cur.fetchall()]
        except Exception:                                # noqa: BLE001
            # la tabella arriva con la migrazione 0069: prima di allora la
            # pagina mostra i saldi e basta, mai un 500
            per_via, per_giorno, eventi, rivelazioni = {}, [], [], []
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT at, crediti, importo_cent, valuta "
                    "FROM portale_acquisti WHERE user_id = %s "
                    "ORDER BY at DESC LIMIT 30", (uid,))
                acquisti = [{"at": r[0].isoformat(), "crediti": r[1],
                             "importo_cent": r[2], "valuta": r[3]}
                            for r in cur.fetchall()]
        except Exception:                                # noqa: BLE001
            # migrazione 0070: come sopra, mai un 500 per lo storico
            acquisti = []
        nomi = {}
        loghi = {}
        for r in rivelazioni:
            ats, separator, slug = r["ref"].partition(":")
            r["ats"] = ats if separator else None
            r["company_slug"] = slug if separator else None
            r["company_domain"] = None
            r["logo_url"] = None
            try:
                a = _dati.rivela_azienda(r["ref"])
                if a:
                    r["nome"] = a.get("company") or a.get("legal_name")
                    r["company_domain"] = (a.get("domain") or a.get("company_domain")
                                           or a.get("website"))
                    r["logo_url"] = a.get("logo") or a.get("logo_url")
                    r["logo"] = _logo_rif(r["ref"]) or _favicon(a.get("domain"))
            except Exception:                            # noqa: BLE001
                pass
            if not r.get("nome"):
                # l'azienda non e' nell'export di oggi: il nome si ricava
                # dal rif, presentabile — mai il rif grezzo in pagina
                r["nome"] = _bel_nome(r["ref"])
            if not r.get("logo"):
                r["logo"] = _logo_rif(r["ref"])
            r["logo_url"] = r["logo_url"] or r.get("logo")
            nomi[r["ref"]] = r["nome"]
            if r.get("logo"):
                loghi[r["ref"]] = r["logo"]
        for e in eventi:
            # il titolo leggibile dell'evento: il nome dell'azienda per gli
            # sblocchi, «titolo · azienda» per le offerte, il file per gli
            # export. L'identificativo tecnico resta in `dettaglio`.
            if e["azione"] == "company unlock" and e["dettaglio"]:
                e["titolo"] = nomi.get(e["dettaglio"]) or _bel_nome(e["dettaglio"])
                e["logo"] = loghi.get(e["dettaglio"]) or _logo_rif(e["dettaglio"])
            elif e["azione"] == "filtered export" and e["download_id"]:
                unita = "company" if e["tipo_export"] == "companies" else "job"
                plurale = "s" if e["righe_export"] != 1 else ""
                e["titolo"] = f"{e['righe_export']} {unita} record{plurale}"
            elif e["azione"] == "posting unlock" and e["dettaglio"]:
                try:
                    o = _vivo.rivela_offerta(e["dettaglio"])
                    if o:
                        e["titolo"] = " · ".join(x for x in (
                            o.get("title"), o.get("organization")) if x)
                        # il logo dell'azienda dell'offerta, dalla sua
                        # scheda: una lettura in piu', solo per gli sblocchi
                        rif_o = (f"{o['ats']}:{o['company_slug']}"
                                 if o.get("ats") and o.get("company_slug")
                                 else None)
                        if rif_o:
                            if rif_o in loghi:
                                e["logo"] = loghi[rif_o]
                            else:
                                az = _dati.rivela_azienda(rif_o)
                                if az and az.get("domain"):
                                    e["logo"] = _favicon(az["domain"])
                                    loghi[rif_o] = e["logo"]
                except Exception:                        # noqa: BLE001
                    pass
        speso_dettagliato = sum(v["crediti"] or 0 for v in per_via.values())
        non_dettagliati = max(0, speso_reale - speso_dettagliato)
        return {"saldo": {"gratis": mensile, "acquistati": extra,
                          "spesi_mese": speso_reale},
                "per_via": per_via, "per_giorno": per_giorno,
                "eventi": eventi, "rivelazioni": rivelazioni,
                # crediti spesi prima che il registro esistesse: nel totale
                # ci sono, riga per riga no — la pagina lo dice esplicitamente
                "non_dettagliati": non_dettagliati,
                "registro_dal": "2026-10-04",
                "acquisti": acquisti}

    @app.get("/me/volumi")
    def volumi_vendibili(uid: str = Depends(utente)):
        """I volumi comprabili (configurati su Creem da noi)."""
        return {"volumi": _billing.volumi()}

    @app.post("/me/checkout")
    async def checkout_crediti(request: Request, uid: str = Depends(utente),
                               conn=Depends(connessione)):
        try:
            corpo = await request.json()
        except Exception:                            # noqa: BLE001
            corpo = {}
        piano = (corpo or {}).get("plan")
        intervallo = (corpo or {}).get("interval") or "month"
        with conn.cursor() as cur:
            cur.execute("SELECT email, creem_subscription_id, subscription_status, "
                        "subscription_plan, subscription_interval "
                        "FROM users WHERE id = %s", (uid,))
            riga = cur.fetchone()
        try:
            if piano:
                subscription_id = riga[1] if riga else None
                attiva = (bool(subscription_id) and riga[2]
                          in ("active", "scheduled_cancel"))
                if attiva and riga[3] == piano and riga[4] == intervallo:
                    return {"already_active": True}
                if attiva:
                    await run_in_threadpool(
                        _billing.upgrade_abbonamento, subscription_id, piano, intervallo)
                    return {"upgraded": True}
                url = await run_in_threadpool(
                    _billing.checkout_abbonamento, uid, riga[0] if riga else "",
                    piano, intervallo,
                    os.environ.get("SITE_URL", "").rstrip("/") + "/account?pagato=1",
                    os.environ.get("SITE_URL", "").rstrip("/") + "/account")
                return {"checkout_url": url}
            volume = (corpo or {}).get("volume") or 0
            url = await run_in_threadpool(
                _billing.checkout, uid, riga[0] if riga else "", volume,
                success_url=os.environ.get("SITE_URL", "").rstrip("/")
                + "/account?pagato=1",
                cancel_url=os.environ.get("SITE_URL", "").rstrip("/")
                + "/account")
        except _billing.BillingNonPronto:
            raise HTTPException(503, "checkout is not live yet — "
                                "write to hello@nivult.com")
        return {"checkout_url": url}

    @app.post("/me/billing-portal")
    async def billing_portal(uid: str = Depends(utente), conn=Depends(connessione)):
        with conn.cursor() as cur:
            cur.execute("SELECT creem_customer_id FROM users WHERE id = %s", (uid,))
            riga = cur.fetchone()
        customer_id = riga[0] if riga else None
        if not customer_id:
            raise HTTPException(404, "no billing customer is linked to this account")
        try:
            url = await run_in_threadpool(_billing.customer_portal, customer_id)
        except _billing.BillingNonPronto:
            raise HTTPException(503, "customer portal temporarily unavailable")
        return {"customer_portal_link": url}

    @app.post("/webhooks/creem")
    async def webhook_creem(request: Request):
        corpo = await request.body()
        if not _billing.firma_valida(
                corpo, request.headers.get("creem-signature")):
            raise HTTPException(401, "firma non valida")
        try:
            evento = json.loads(corpo)
        except ValueError:
            raise HTTPException(400, "corpo non JSON")
        esito = await run_in_threadpool(_billing.applica_pagamento, database_url(), evento)
        return {"esito": esito}

    def _campo_testo(v) -> str | None:
        """Un campo del corpus che puo' essere testo o lista: reso testo.

        Le liste incollate senza separatore («EnvironmentHybrid Working
        Model…») erano il primo difetto visibile della finestra.
        """
        if isinstance(v, list):
            v = " · ".join(str(x).strip() for x in v if str(x).strip())
        v = (str(v).strip() if v is not None else "")
        return v or None

    def _profilo_match(cur, uid: str, match_id: str):
        cur.execute(
            "SELECT c.id::text, c.families, c.seniority, c.skills, c.languages, "
            "       c.years_experience, u.locale "
            "FROM matches m JOIN users u ON u.id = m.user_id "
            "LEFT JOIN user_cvs c ON c.id = m.cv_id "
            "WHERE m.id = %s AND m.user_id = %s", (match_id, uid))
        pr = cur.fetchone()
        if pr is not None and pr[0] is not None:
            return pr[1:]
        cur.execute(
            "SELECT c.families, c.seniority, c.skills, c.languages, "
            "       c.years_experience, u.locale "
            "FROM user_cvs c JOIN users u ON u.id = c.user_id "
            "WHERE c.user_id = %s AND c.status = 'active'", (uid,))
        return cur.fetchone()

    @app.get("/me/offerte/{match_id}")
    def dettaglio_offerta(match_id: UUID, lingua: str | None = None,
                          uid: str = Depends(utente),
                          conn=Depends(connessione)):
        """Dettaglio del match, anche fuori discovery o dalla soglia odierna."""
        match_key = str(match_id)
        with conn.cursor(row_factory=dict_row) as cur:
            r = _riga_offerta(cur, uid, match_key, completa=True)
        analisi = r["analysis"]
        raw = r["raw"] or {}
        purgata = r["purged_at"] is not None
        offerta_evidenza = {
            "title": r["title"],
            "ai_experience_level": raw.get("ai_experience_level"),
            "ai_key_skills": raw.get("ai_key_skills"),
            "ai_requirements_summary": raw.get("ai_requirements_summary"),
        }
        with conn.cursor() as cur:
            pr = _profilo_match(cur, uid, match_key)
        profilo_evidenza = {
            "ruolo": ", ".join(pr[0] or []),
            "seniority": pr[1] or "—",
            "competenze": list(pr[2] or []),
            "lingue": list(pr[3] or []),
        } if pr else {}
        r["evidence"] = evidenze(profilo_evidenza, offerta_evidenza)

        from nivult.delivery.testi import LINGUA_PER_GLM
        lingua_voluta = LINGUA_PER_GLM.get(lingua or "", None)
        da_scrivere = (analisi is None
                       or "perche" not in analisi
                       or (lingua_voluta
                           and analisi.get("lang") != lingua_voluta))
        if da_scrivere and not purgata and os.environ.get("GLM_API_KEY") and pr:
            from nivult.matching.llm import (GLM, analizza_allineamento,
                                             profilo_come_testo)
            profilo = {
                "ruolo": ", ".join(pr[0] or []),
                "seniority": pr[1] or "—",
                "competenze": list(pr[2] or []),
                "lingue": list(pr[3] or []),
                "sedi": [],
                "note": (f"{pr[4]} anni di esperienza" if pr[4] else None),
            }
            offerta = {
                "id": match_key,
                "title": r["title"],
                "organization": r["organization"],
                "cities": list(r["cities"] or []),
                **{c: raw.get(c) for c in (
                    "ai_experience_level", "ai_work_arrangement",
                    "ai_visa_sponsorship", "ai_key_skills",
                    "ai_requirements_summary",
                    "ai_core_responsibilities", "ai_benefits",
                    "ai_working_hours")},
            }
            try:
                nuova = analizza_allineamento(
                    GLM(), profilo_come_testo(profilo), offerta,
                    lingua=(lingua_voluta
                            or LINGUA_PER_GLM.get(pr[5], "English")))
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE matches SET analysis = %s "
                        "WHERE id = %s AND user_id = %s",
                        (Json(nuova), match_key, uid))
                conn.commit()
                analisi = nuova
                r["analysis"] = analisi
            except Exception as exc:
                log.warning("analisi allineamento fallita per %s: %s",
                            match_key, exc)

        with conn.cursor(row_factory=dict_row) as cur:
            _carica_collezioni(cur, uid, r)
        return _serializza_offerta(r, lingua or "en", dettaglio=True)

    @app.put("/me/offerte/{match_id}/stato")
    def aggiorna_stato_offerta(match_id: UUID, corpo: StatoOfferta,
                               uid: str = Depends(utente),
                               conn=Depends(connessione)):
        match_key = str(match_id)
        adesso = datetime.now(timezone.utc)
        if corpo.scartata:
            if corpo.motivo_scarto is None:
                raise HTTPException(422, "motivo_scarto richiesto")
            if corpo.salvata or corpo.candidatura != "not_started":
                raise HTTPException(422, "scarto incompatibile con salvataggio o candidatura")
        elif corpo.motivo_scarto is not None:
            raise HTTPException(422, "motivo_scarto senza scarto")

        for nome, attivo in (
                ("salvata_il", corpo.salvata),
                ("scartata_il", corpo.scartata),
                ("archiviata_il", corpo.in_archivio),
                ("applicata_il", corpo.candidatura != "not_started")):
            valore = getattr(corpo, nome)
            if valore is None:
                continue
            if not attivo:
                raise HTTPException(422, f"{nome} incoerente con lo stato richiesto")
            if valore.tzinfo is None or valore.utcoffset() is None:
                raise HTTPException(422, f"{nome} deve includere il fuso orario")
            if valore > adesso:
                raise HTTPException(422, f"{nome} non puo' essere nel futuro")
        if (corpo.prossima_azione_il is not None
                and (corpo.prossima_azione_il.tzinfo is None
                     or corpo.prossima_azione_il.utcoffset() is None)):
            raise HTTPException(422, "prossima_azione_il deve includere il fuso orario")

        with conn.cursor(row_factory=dict_row) as cur:
            attuale = _riga_offerta(cur, uid, match_key)
        campi = corpo.model_fields_set
        note = _campo_testo(corpo.note) if "note" in campi else attuale["note"]
        azione = (_campo_testo(corpo.prossima_azione)
                  if "prossima_azione" in campi else attuale["next_action"])
        azione_il = (corpo.prossima_azione_il
                     if "prossima_azione_il" in campi else attuale["next_action_at"])
        if azione is None and azione_il is not None:
            raise HTTPException(422, "prossima_azione_il senza prossima_azione")
        desiderato = (corpo.salvata, corpo.scartata, corpo.motivo_scarto,
                      corpo.in_archivio, corpo.candidatura, note, azione, azione_il)
        if _stato_attuale(attuale) == desiderato:
            return _serializza_offerta(attuale, "en")
        if corpo.base_version != attuale["action_version"]:
            return _conflitto_stato(attuale)

        timestamp_legacy = [corpo.salvata_il, corpo.scartata_il,
                            corpo.archiviata_il, corpo.applicata_il]
        if attuale["action_version"] != 0 and any(
                valore is not None for valore in timestamp_legacy):
            raise HTTPException(
                422, "i timestamp del client sono ammessi solo sul replay a versione zero")

        saved_at = ((attuale["saved_at"] or corpo.salvata_il or adesso)
                    if corpo.salvata else None)
        dismissed_at = ((attuale["dismissed_at"] or corpo.scartata_il or adesso)
                        if corpo.scartata else None)
        archived_at = ((attuale["archived_at"] or corpo.archiviata_il or adesso)
                       if corpo.in_archivio else None)
        applied_at = ((attuale["applied_at"] or corpo.applicata_il or adesso)
                      if corpo.candidatura != "not_started" else None)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE matches SET saved_at = %s, dismissed_at = %s, "
                    "  dismiss_reason = %s, archived_at = %s, "
                    "  application_status = %s, applied_at = %s, "
                    "  note = %s, next_action = %s, next_action_at = %s, "
                    "  action_version = action_version + 1, user_action_at = %s "
                    "WHERE id = %s AND user_id = %s AND action_version = %s",
                    (saved_at, dismissed_at,
                     corpo.motivo_scarto if corpo.scartata else None,
                     archived_at, corpo.candidatura, applied_at,
                     note, azione, azione_il, adesso,
                     match_key, uid, corpo.base_version))
                if cur.rowcount != 1:
                    with conn.cursor(row_factory=dict_row) as lettura:
                        canonico = _riga_offerta(lettura, uid, match_key)
                    if _stato_attuale(canonico) == desiderato:
                        return _serializza_offerta(canonico, "en")
                    return _conflitto_stato(canonico)
        except psycopg.errors.CheckViolation:
            conn.rollback()
            raise HTTPException(422, "stato personale incoerente") from None

        with conn.cursor(row_factory=dict_row) as cur:
            aggiornata = _riga_offerta(cur, uid, match_key)
        risposta = _serializza_offerta(aggiornata, "en")
        conn.commit()
        return risposta

    def _blocca_match(cur, uid: str, match_id: str) -> None:
        cur.execute("SELECT id FROM matches WHERE id = %s AND user_id = %s FOR UPDATE",
                    (match_id, uid))
        if cur.fetchone() is None:
            raise HTTPException(404, "posting not found")

    def _incrementa_versione(cur, uid: str, match_id: str) -> None:
        cur.execute(
            "UPDATE matches SET action_version = action_version + 1, "
            "user_action_at = now() WHERE id = %s AND user_id = %s",
            (match_id, uid))

    def _dati_colloquio(corpo: ColloquioOfferta) -> tuple:
        if corpo.inizio.tzinfo is None or corpo.inizio.utcoffset() is None:
            raise HTTPException(422, "inizio deve includere il fuso orario")
        return corpo.inizio, _campo_testo(corpo.note), corpo.promemoria_minuti

    @app.get("/me/offerte/{match_id}/colloqui")
    def colloqui_offerta(match_id: UUID, uid: str = Depends(utente),
                         conn=Depends(connessione)):
        with conn.cursor(row_factory=dict_row) as cur:
            riga = _riga_offerta(cur, uid, str(match_id))
            _carica_collezioni(cur, uid, riga)
        return {"colloqui": riga["colloqui"]}

    @app.post("/me/offerte/{match_id}/colloqui")
    def crea_colloquio(match_id: UUID, corpo: ColloquioOfferta,
                       uid: str = Depends(utente), conn=Depends(connessione)):
        match_key = str(match_id)
        inizio, note, promemoria = _dati_colloquio(corpo)
        with conn.cursor() as cur:
            _blocca_match(cur, uid, match_key)
            cur.execute(
                "INSERT INTO match_interviews "
                "(match_id, user_id, starts_at, note, reminder_minutes) "
                "VALUES (%s,%s,%s,%s,%s)", (match_key, uid, inizio, note, promemoria))
            _incrementa_versione(cur, uid, match_key)
        with conn.cursor(row_factory=dict_row) as cur:
            risposta = _offerta_canonica(cur, uid, match_key)
        conn.commit()
        return risposta

    @app.put("/me/offerte/{match_id}/colloqui/{colloquio_id}")
    def aggiorna_colloquio(match_id: UUID, colloquio_id: UUID, corpo: ColloquioOfferta,
                           uid: str = Depends(utente), conn=Depends(connessione)):
        match_key = str(match_id)
        inizio, note, promemoria = _dati_colloquio(corpo)
        with conn.cursor() as cur:
            _blocca_match(cur, uid, match_key)
            cur.execute(
                "UPDATE match_interviews SET starts_at = %s, note = %s, "
                "reminder_minutes = %s WHERE id = %s AND match_id = %s AND user_id = %s",
                (inizio, note, promemoria, str(colloquio_id), match_key, uid))
            if cur.rowcount != 1:
                raise HTTPException(404, "interview not found")
            _incrementa_versione(cur, uid, match_key)
        with conn.cursor(row_factory=dict_row) as cur:
            risposta = _offerta_canonica(cur, uid, match_key)
        conn.commit()
        return risposta

    @app.delete("/me/offerte/{match_id}/colloqui/{colloquio_id}")
    def elimina_colloquio(match_id: UUID, colloquio_id: UUID,
                          uid: str = Depends(utente), conn=Depends(connessione)):
        match_key = str(match_id)
        with conn.cursor() as cur:
            _blocca_match(cur, uid, match_key)
            cur.execute(
                "DELETE FROM match_interviews WHERE id = %s AND match_id = %s "
                "AND user_id = %s", (str(colloquio_id), match_key, uid))
            if cur.rowcount != 1:
                raise HTTPException(404, "interview not found")
            _incrementa_versione(cur, uid, match_key)
        with conn.cursor(row_factory=dict_row) as cur:
            risposta = _offerta_canonica(cur, uid, match_key)
        conn.commit()
        return risposta

    DOCUMENTI_MIME = {
        "application/pdf": "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain": "text/plain",
    }
    DOCUMENTI_ESTENSIONI = {
        ".pdf": DOCUMENTI_MIME["application/pdf"],
        ".docx": DOCUMENTI_MIME[
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
        ".txt": DOCUMENTI_MIME["text/plain"],
    }
    DOCUMENTO_MAX_BYTES = 10 * 1024 * 1024
    DOCUMENTI_MAX_PER_MATCH = 10
    DOCUMENTI_MAX_BYTES_PER_MATCH = 50 * 1024 * 1024

    def _mime_documento(nome: str, mime: str) -> str | None:
        mime = (mime or "").split(";", 1)[0].strip().lower()
        if mime in DOCUMENTI_MIME:
            return DOCUMENTI_MIME[mime]
        return DOCUMENTI_ESTENSIONI.get(os.path.splitext(nome.lower())[1])

    def _nome_documento(nome: str | None) -> str:
        nome = os.path.basename((nome or "").replace("\\", "/"))
        nome = "".join(c for c in nome if c.isprintable()).strip()
        return (nome or "documento")[:255]

    @app.get("/me/offerte/{match_id}/documenti")
    def documenti_offerta(match_id: UUID, uid: str = Depends(utente),
                          conn=Depends(connessione)):
        with conn.cursor(row_factory=dict_row) as cur:
            riga = _riga_offerta(cur, uid, str(match_id))
            _carica_collezioni(cur, uid, riga)
        return {"documenti": riga["documenti"]}

    @app.post("/me/offerte/{match_id}/documenti")
    async def carica_documento(match_id: UUID, file: UploadFile = File(...),
                               uid: str = Depends(utente), conn=Depends(connessione)):
        match_key = str(match_id)
        nome = _nome_documento(file.filename)
        mime = _mime_documento(nome, file.content_type or "")
        if mime is None:
            raise HTTPException(422, "sono ammessi soltanto PDF, DOCX e TXT")
        dati = await file.read(DOCUMENTO_MAX_BYTES + 1)
        if len(dati) > DOCUMENTO_MAX_BYTES:
            raise HTTPException(413, "il documento supera i 10 MB")
        if not dati:
            raise HTTPException(422, "il documento e' vuoto")

        esito = await run_in_threadpool(crypto.cifra, dati)
        chiave = f"documenti/{uid}/{match_key}/{secrets.token_hex(16)}"
        await run_in_threadpool(storage.salva, chiave, esito.dati)
        try:
            with conn.cursor() as cur:
                _blocca_match(cur, uid, match_key)
                cur.execute(
                    "SELECT count(*), COALESCE(sum(size_bytes), 0) FROM match_documents "
                    "WHERE match_id = %s AND user_id = %s", (match_key, uid))
                conteggio, byte_attuali = cur.fetchone()
                if conteggio >= DOCUMENTI_MAX_PER_MATCH:
                    raise HTTPException(409, "massimo 10 documenti per offerta")
                if byte_attuali + len(dati) > DOCUMENTI_MAX_BYTES_PER_MATCH:
                    raise HTTPException(413, "massimo 50 MB di documenti per offerta")
                cur.execute(
                    "INSERT INTO match_documents "
                    "(match_id, user_id, original_filename, mime_type, size_bytes, sha256, "
                    " storage_key, encryption_algo, encrypted_dek, nonce, auth_tag, kek_version) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,'aes-256-gcm',%s,%s,%s,1)",
                    (match_key, uid, nome, mime, len(dati),
                     hashlib.sha256(dati).hexdigest(), chiave,
                     Binary(esito.encrypted_dek), Binary(esito.nonce), Binary(esito.auth_tag)))
                _incrementa_versione(cur, uid, match_key)
            with conn.cursor(row_factory=dict_row) as cur:
                risposta = _offerta_canonica(cur, uid, match_key)
            conn.commit()
        except Exception:
            conn.rollback()
            await run_in_threadpool(storage.elimina, chiave)
            raise
        return risposta

    def _riga_documento(cur, uid: str, match_id: str, documento_id: str):
        cur.execute(
            "SELECT original_filename, mime_type, storage_key, encrypted_dek "
            "FROM match_documents WHERE id = %s AND match_id = %s AND user_id = %s",
            (documento_id, match_id, uid))
        riga = cur.fetchone()
        if riga is None:
            raise HTTPException(404, "document not found")
        return riga

    @app.get("/me/offerte/{match_id}/documenti/{documento_id}")
    @app.get("/me/offerte/{match_id}/documenti/{documento_id}/file")
    async def scarica_documento(match_id: UUID, documento_id: UUID,
                                uid: str = Depends(utente), conn=Depends(connessione)):
        with conn.cursor() as cur:
            nome, mime, chiave, encrypted_dek = _riga_documento(
                cur, uid, str(match_id), str(documento_id))
        try:
            cifrato = await run_in_threadpool(storage.leggi, chiave)
            dati = await run_in_threadpool(crypto.decifra, cifrato, encrypted_dek)
        except Exception as exc:
            log.warning("documento %s non leggibile: %s", documento_id, exc)
            raise HTTPException(502, "document temporarily unavailable") from None
        ascii_nome = nome.encode("ascii", "replace").decode().replace('"', "'")
        return Response(
            content=dati, media_type=mime,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Content-Disposition": (
                    f'attachment; filename="{ascii_nome}"; '
                    f"filename*=UTF-8''{quote(nome, safe='')}")
            })

    @app.delete("/me/offerte/{match_id}/documenti/{documento_id}")
    async def elimina_documento(match_id: UUID, documento_id: UUID,
                                uid: str = Depends(utente), conn=Depends(connessione)):
        match_key = str(match_id)
        with conn.cursor() as cur:
            _blocca_match(cur, uid, match_key)
            cur.execute(
                "DELETE FROM match_documents WHERE id = %s AND match_id = %s "
                "AND user_id = %s RETURNING storage_key",
                (str(documento_id), match_key, uid))
            riga = cur.fetchone()
            if riga is None:
                raise HTTPException(404, "document not found")
            chiave = riga[0]
            _incrementa_versione(cur, uid, match_key)
        with conn.cursor(row_factory=dict_row) as cur:
            risposta = _offerta_canonica(cur, uid, match_key)
        conn.commit()
        try:
            await run_in_threadpool(storage.elimina, chiave)
        except Exception as exc:
            log.warning("blob del documento %s non rimosso: %s", documento_id, exc)
        return risposta

    def _offerte_per_riepilogo(cur, uid: str, ids: list[str]) -> list[dict]:
        return [_offerta_canonica(cur, uid, match_id) for match_id in ids]

    @app.get("/me/riepilogo-settimanale")
    def riepilogo_settimanale(uid: str = Depends(utente), conn=Depends(connessione)):
        adesso = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            cur.execute("SELECT timezone FROM users WHERE id = %s", (uid,))
            riga = cur.fetchone()
        try:
            fuso = ZoneInfo((riga[0] if riga else None) or "UTC")
        except Exception:
            fuso = timezone.utc
        locale = adesso.astimezone(fuso)
        inizio_locale = (locale - timedelta(days=locale.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        inizio_settimana = inizio_locale.astimezone(timezone.utc)
        fra_trenta_giorni = adesso + timedelta(days=30)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT application_status, count(*) FROM matches "
                "WHERE user_id = %s AND application_status <> 'not_started' "
                "GROUP BY application_status", (uid,))
            conteggi = dict(cur.fetchall())
            cur.execute(
                "SELECT count(*) FROM matches WHERE user_id = %s "
                "AND applied_at >= %s AND applied_at < %s",
                (uid, inizio_settimana, inizio_settimana + timedelta(days=7)))
            candidature = cur.fetchone()[0]
            cur.execute(
                "SELECT dismiss_reason, count(*) FROM matches WHERE user_id = %s "
                "AND dismiss_reason IS NOT NULL GROUP BY dismiss_reason "
                "ORDER BY count(*) DESC, dismiss_reason LIMIT 8", (uid,))
            motivi = [{"motivo": motivo, "count": numero}
                      for motivo, numero in cur.fetchall()]
            cur.execute("SELECT count(*) FROM digests WHERE user_id = %s AND status = 'sent'",
                        (uid,))
            digest_ricevuti = cur.fetchone()[0]
            cur.execute(
                "SELECT i.match_id::text, min(i.starts_at) AS primo "
                "FROM match_interviews i WHERE i.user_id = %s "
                "AND i.starts_at >= %s AND i.starts_at < %s "
                "GROUP BY i.match_id ORDER BY primo, i.match_id LIMIT 10",
                (uid, adesso, fra_trenta_giorni))
            ids_colloqui = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT id::text FROM matches WHERE user_id = %s "
                "AND next_action_at >= %s AND next_action_at < %s "
                "ORDER BY next_action_at, id LIMIT 10",
                (uid, adesso, fra_trenta_giorni))
            ids_azioni = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT m.id::text FROM matches m JOIN jobs j ON j.id = m.job_id "
                "WHERE m.user_id = %s AND m.saved_at IS NOT NULL "
                "AND m.dismissed_at IS NULL AND m.archived_at IS NULL "
                "AND m.application_status <> 'rejected' "
                "AND j.date_valid_through >= %s AND j.date_valid_through < %s "
                "ORDER BY j.date_valid_through, m.id LIMIT 10",
                (uid, adesso, fra_trenta_giorni))
            ids_scadenze = [r[0] for r in cur.fetchall()]

        with conn.cursor(row_factory=dict_row) as cur:
            colloqui = _offerte_per_riepilogo(cur, uid, ids_colloqui)
            azioni = _offerte_per_riepilogo(cur, uid, ids_azioni)
            scadenze = _offerte_per_riepilogo(cur, uid, ids_scadenze)
        return {
            "pipeline": {stato: int(conteggi.get(stato, 0))
                         for stato in ("applied", "interview", "offer", "rejected")},
            "candidature_questa_settimana": candidature,
            "colloqui_prossimi": colloqui,
            "azioni_prossime": azioni,
            "scadenze": scadenze,
            "motivi_scarto": motivi,
            "digest_ricevuti": digest_ricevuti,
        }

    # «Pflegefachkraft (m/w/d)», «Comptable unique F/H», «(d/f/m)»: il
    # suffisso di genere obbligatorio negli annunci tedeschi e francesi.
    # Nel digest resta — il titolo la' e' la chiave per ritrovare l'annuncio
    # — ma nella vetrina della home e' rumore. Solo gruppi di lettere
    # singole separate da barre: «HR Business Partner (Senior/Lead)» non
    # viene toccato.
    _GENERE = re.compile(
        r"\s*[\(\[](?:[mwfdxhu]\s*/\s*){1,3}[mwfdxhu][\)\]]"
        r"|\s+[MWFDH]/[MWFDH](?:/[MWFDX])?\s*$", re.IGNORECASE)

    def _senza_genere(titolo: str) -> str:
        return _GENERE.sub("", titolo).rstrip(" -–·,") or titolo

    @app.get("/vetrina")
    def vetrina(conn=Depends(connessione)):
        """Un assaggio VERO del corpus, per il riquadro della home.

        Rotta pubblica e senza punteggi, deliberatamente: il punteggio è
        contro un CV e il visitatore non ne ha uno — un numero qui sarebbe
        una recita. La prova che il motore lavora sono le offerte stesse:
        aziende vere, loghi veri, datate a ieri. Casuali a ogni richiesta,
        così due aperture non mostrano mai la stessa vetrina.

        Niente URL delle offerte: questo è un assaggio, non una bacheca
        gratuita. Il prodotto è il digest.

        E niente CONTEGGI né DATE. I conteggi erano la dimensione esatta del
        nostro indice; le date ne dicono la freschezza. Entrambi servivano a
        chiunque interroghi la rotta, concorrenti compresi — e una rotta
        pubblica è pubblica per tutti: toglierli solo dal sito avrebbe
        lasciato il dato qui, a portata di curl. Il filtro sui sette giorni
        resta, ma vive nella query e non esce.
        """
        with conn.cursor() as cur:
            # Solo offerte il cui logo ESISTE davvero: o la fonte porta
            # l'immagine, o l'archivio ce l'ha già. Un monogramma in mezzo ai
            # loghi veri leggerebbe come un buco — la vetrina è la faccia del
            # prodotto, e i ripieghi vanno bene ovunque tranne qui. Si
            # escludono anche i fallimenti noti (bytes NULL in cache).
            # Un giro per PAESE, non un pescaggio cieco.
            #
            # `ORDER BY random()` da solo restituisce il corpus com'è, e il
            # corpus è sbilanciato per costruzione: France Travail da sola
            # porta l'80% delle offerte con logo, quindi il riquadro usciva
            # quasi tutto francese mentre la pagina accanto promette 44
            # paesi. Un visitatore tedesco leggeva una vetrina di annunci
            # francesi e ne traeva la conclusione ovvia.
            #
            # `ROW_NUMBER` numera le offerte dentro ciascun paese; ordinare
            # per quel numero prende prima una offerta per paese, poi la
            # seconda di ciascuno, e così via. Si adatta da solo: con tre
            # paesi ne dà sei a testa, con dodici ne dà una o due, senza
            # nessuna quota scritta a mano da tenere aggiornata.
            # E un solo annuncio per DATORE, prima ancora del giro per
            # paese: Schaeffler pubblica dieci varianti dello stesso ruolo
            # (d/f/m, d/m/w...) e senza questo filtro tre righe su sei
            # erano sue — una vetrina monopolizzata da un'azienda dice
            # «corpus piccolo» esattamente come una monopolizzata da un
            # paese.
            cur.execute(
                "WITH per_datore AS ( "
                "  SELECT j.title, j.organization, (j.cities)[1] AS citta, "
                "         (j.countries)[1] AS paese, "
                "         COALESCE(j.org_linkedin_slug, j.domain_derived) AS logo, "
                "         row_number() OVER (PARTITION BY j.organization "
                "                            ORDER BY random()) AS doppione "
                "  FROM jobs j "
                "  LEFT JOIN company_logos cl "
                "    ON cl.chiave = COALESCE(j.org_linkedin_slug, j.domain_derived) "
                "  WHERE j.status = 'active' AND j.duplicate_of_job_id IS NULL "
                "    AND j.organization IS NOT NULL "
                "    AND j.date_posted > now() - interval '7 days' "
                "    AND COALESCE(j.org_linkedin_slug, j.domain_derived) IS NOT NULL "
                "    AND (cl.bytes IS NOT NULL OR (cl.chiave IS NULL AND "
                "         (j.org_logo_permalink IS NOT NULL OR j.organization_logo IS NOT NULL))) "
                "), pescabili AS ( "
                "  SELECT *, row_number() OVER (PARTITION BY paese "
                "                               ORDER BY random()) AS giro "
                "  FROM per_datore WHERE doppione = 1 "
                "), scelte AS ( "
                "  SELECT * FROM pescabili ORDER BY giro, random() LIMIT 18 "
                ") "
                # Rimescolate alla fine: senza, il riquadro scorrerebbe un
                # paese per riga in ordine fisso e la rotazione si vedrebbe.
                "SELECT title, organization, citta, paese, logo "
                "FROM scelte ORDER BY random()")
            righe = cur.fetchall()
        return Response(
            content=json.dumps({
                "offerte": [{
                    # I suffissi di genere («(m/w/d)», «F/H») restano nel
                    # digest — lì il titolo e' la chiave per ritrovare
                    # l'annuncio — ma in vetrina sono rumore burocratico,
                    # come il codice di dipartimento davanti alle citta'
                    # francesi.
                    "titolo": _senza_genere(r[0]), "azienda": r[1],
                    "citta": r[2], "paese": r[3],
                    "logo": f"/logo/{r[4]}" if r[4] else None,
                } for r in righe],
            }),
            media_type="application/json",
            # Casuale per richiesta: una cache a monte la congelerebbe.
            headers={"Cache-Control": "no-store"})

    @app.get("/logo/{chiave}")
    def logo(chiave: str, conn=Depends(connessione)):
        """Il logo di un'azienda, dal nostro archivio.

        Scaricato al primo bisogno e conservato: mai collegato al volo. Nelle
        email un'immagine remota resta un rettangolo vuoto perche' i client la
        bloccano, e sul sito collegare il CDN di LinkedIn direbbe a LinkedIn
        ogni volta che qualcuno apre il proprio pannello.

        Rotta pubblica: sono loghi aziendali, dati pubblici, e legarla alla
        sessione impedirebbe di usarli nelle email — che e' meta' del motivo
        per cui esiste.
        """
        chiave = chiave.strip().lower()[:200]
        if not chiave:
            raise HTTPException(404, "logo assente")

        with conn.cursor() as cur:
            # Un fallimento non è per sempre: la riga con bytes NULL evita di
            # riprovare a ogni visita, ma dopo una settimana si riprova — un
            # timeout momentaneo non deve cancellare il logo di un'azienda
            # per l'eternità.
            cur.execute("SELECT mime, bytes FROM company_logos "
                        "WHERE chiave = %s AND (bytes IS NOT NULL "
                        "   OR fetched_at > now() - interval '7 days')",
                        (chiave,))
            r = cur.fetchone()

            if r is None:
                # Primo bisogno: si cerca una fonte fra le offerte di questa
                # azienda e si prova a scaricarla, una volta sola.
                # Ordinato per la catena di CLAUDE.md, non a caso: la stessa
                # azienda ha decine di offerte e solo alcune portano il
                # permalink (83% contro 49%). Con un LIMIT 1 non ordinato si
                # pesca una riga senza logo e si ripiega inutilmente.
                cur.execute(
                    "SELECT COALESCE(org_logo_permalink, organization_logo), "
                    "       domain_derived FROM jobs "
                    "WHERE COALESCE(org_linkedin_slug, domain_derived) = %s "
                    "ORDER BY (org_logo_permalink IS NOT NULL) DESC, "
                    "         (organization_logo IS NOT NULL) DESC "
                    "LIMIT 1", (chiave,))
                fonte = cur.fetchone()
                url, dominio = (fonte or (None, None))
                # Terzo anello della catena: il favicon grande del dominio
                # via Google (20/10/2026: prima era logo.dev, che ora vuole
                # un token a pagamento e risponde 401; Clearbit e' morto del
                # tutto). s2/favicons e' gratis e affidabile, 128px bastano
                # per le card.
                if not url and dominio:
                    url = ("https://www.google.com/s2/favicons?"
                           f"domain={dominio}&sz=128")
                if not url:
                    # Quarto anello (28/09/2026): il corpus ATS. Il B2C
                    # conosce 780 aziende, il portale 66.000 — li' il logo
                    # lo porta ats_companies (logo_url, o brandfetch dal
                    # dominio cacciato dal demone). Senza questo anello la
                    # vetrina dei clienti era un muro di avatar-iniziale.
                    try:
                        from nivult.ponte_ats import ats_database_url
                        with psycopg.connect(ats_database_url()) as ca, \
                                ca.cursor() as cura:
                            cura.execute(
                                "SELECT logo_url, logo_domain "
                                "FROM ats_companies WHERE slug = %s "
                                "ORDER BY (logo_url IS NOT NULL) DESC "
                                "LIMIT 1", (chiave,))
                            riga_ats = cura.fetchone()
                    except Exception:                            # noqa: BLE001
                        riga_ats = None
                    if riga_ats:
                        url = riga_ats[0]
                        # 29/09/2026: brandfetch e' MORTO (CDN 403 su tutto,
                        # search API 429). 21.206 tenant portano un suo URL
                        # in logo_url — scaricarli fallisce sempre, e senza
                        # ripiego il portale diventava un muro di «?»
                        # (Recordati il primo visto). Il ripiego gratuito
                        # che funziona: i favicon di Google sul dominio.
                        if url and "cdn.brandfetch.io" in url:
                            url = None
                        if not url and riga_ats[1]:
                            url = (f"https://www.google.com/s2/favicons"
                                   f"?domain={riga_ats[1]}&sz=128")
                mime = dati = None
                if url:
                    # Lo scarico passa da `_scarica_logo`, che valida l'host
                    # e ogni redirect contro gli IP privati: l'URL viene dai
                    # dati delle offerte, cioe' da terzi, e senza questo era
                    # un SSRF. Il tipo si riconosce dai BYTE, non dall'header:
                    # l'S3 che ospita questi loghi li serve tutti come
                    # `binary/octet-stream`, e fidarsi dell'header scartava
                    # immagini perfettamente valide.
                    contenuto = _scarica_logo(url)
                    if contenuto:
                        mime = _tipo_immagine(contenuto)
                        if mime:
                            dati = contenuto
                # La riga si scrive anche quando il download fallisce: senza,
                # ogni visita riproverebbe lo stesso scarico che non riesce.
                cur.execute(
                    "INSERT INTO company_logos (chiave, mime, bytes, origine) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (chiave) DO UPDATE "
                    "SET mime = EXCLUDED.mime, bytes = EXCLUDED.bytes, "
                    "    origine = EXCLUDED.origine, fetched_at = now()",
                    (chiave, mime, Binary(dati) if dati else None, url))
                conn.commit()
                r = (mime, dati)

        mime, dati = r
        if not dati:
            # L'ultimo anello e' il monogramma, e lo disegna il sito: qui
            # basta dire che non c'e' nulla da mostrare.
            raise HTTPException(404, "logo assente")
        intestazioni = {"Cache-Control": "public, max-age=604800, immutable",
                        "X-Content-Type-Options": "nosniff"}
        if mime == "image/svg+xml":
            # Un SVG può contenere script, e questo arriva da terzi: dentro un
            # <img> non eseguirebbe comunque, ma aperto direttamente sì. La
            # sandbox lo neutralizza senza rompere l'uso da immagine.
            intestazioni["Content-Security-Policy"] = "sandbox"
        return Response(content=bytes(dati), media_type=mime or "image/png",
                        headers=intestazioni)

    @app.get("/favicon/{dominio}")
    def favicon_dominio(dominio: str, conn=Depends(connessione)):
        """Il favicon di un dominio, scaricato una volta da Google e servito
        dal nostro archivio (07/10/2026).

        Nato per la finestra dei consumi del portale: i loghi arrivano da
        `_favicon`, e servirli da 'self' e' l'unico modo di passare la CSP
        della vetrina senza allargarla a google.com. La chiave in
        company_logos e' il dominio stesso: contiene sempre un punto, gli
        slug aziendali mai — niente collisioni con /logo/{chiave}.
        Stessa politica dei loghi: un fallimento si ricorda una settimana,
        poi si riprova."""
        dominio = dominio.strip().lower()[:200]
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", dominio or ""):
            raise HTTPException(404, "dominio non valido")
        with conn.cursor() as cur:
            cur.execute("SELECT mime, bytes FROM company_logos "
                        "WHERE chiave = %s AND (bytes IS NOT NULL "
                        "   OR fetched_at > now() - interval '7 days')",
                        (dominio,))
            r = cur.fetchone()
            if r is None:
                contenuto = _scarica_logo(
                    f"https://www.google.com/s2/favicons"
                    f"?domain={dominio}&sz=128")
                mime = _tipo_immagine(contenuto) if contenuto else None
                cur.execute(
                    "INSERT INTO company_logos (chiave, mime, bytes, origine) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (chiave) DO UPDATE "
                    "SET mime = EXCLUDED.mime, bytes = EXCLUDED.bytes, "
                    "    origine = EXCLUDED.origine, fetched_at = now()",
                    (dominio, mime,
                     Binary(contenuto) if mime else None, "google-favicon"))
                conn.commit()
                r = (mime, contenuto if mime else None)
        mime, dati = r
        if not dati:
            raise HTTPException(404, "favicon assente")
        return Response(content=bytes(dati),
                        media_type=mime or "image/png",
                        headers={"Cache-Control":
                                 "public, max-age=604800, immutable",
                                 "X-Content-Type-Options": "nosniff"})

    @app.get("/ricerca/famiglia")
    def famiglia_per_ruolo(ruolo: str, uid: str = Depends(utente),
                           conn=Depends(connessione)):
        """Solo la classificazione, senza aprire niente: l'onboarding la usa
        per mostrare subito su quale scaffale cadra' la ricerca."""
        ruolo = ruolo.strip()
        if not ruolo or len(ruolo) > 120:
            raise HTTPException(422, "ruolo mancante o troppo lungo")
        with conn.cursor() as cur:
            famiglia = _famiglia_dal_ruolo(cur, ruolo)
        conn.commit()
        return {"famiglia": famiglia}

    @app.get("/me/cluster")
    def miei_cluster(uid: str = Depends(utente), conn=Depends(connessione)):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.id::text, c.family, c.country, uc.languages, "
                "       uc.min_seniority, uc.max_seniority, uc.work_arrangements, "
                "       uc.employment_types, uc.accepted_employer_kinds, "
                "       uc.needs_visa_sponsorship, uc.min_headcount, uc.max_headcount, "
                "       uc.wants, uc.target_role, uc.industries, uc.min_salary_annual, "
                "       uc.salary_currency, uc.desired_technologies, uc.min_score, uc.id::text, "
                "       uc.frequency, uc.send_weekday, uc.send_monthday, "
                "       uc.send_hour_local, uc.timezone, uc.next_digest_at, uc.last_digest_at "
                "FROM user_clusters uc JOIN clusters c ON c.id = uc.cluster_id "
                "WHERE uc.user_id = %s AND c.status = 'active' "
                "ORDER BY c.family, c.country", (uid,))
            return [{"id": r[19], "cluster_id": r[0], "famiglia": r[1], "paese": r[2],
                     "filtri": {
                         "languages": r[3] or [], "min_seniority": r[4],
                         "max_seniority": r[5], "work_arrangements": r[6] or [],
                         "employment_types": r[7] or [],
                         "accepted_employer_kinds": r[8],
                         "needs_visa_sponsorship": r[9],
                         "min_headcount": r[10], "max_headcount": r[11],
                         "wants": r[12], "target_role": r[13],
                         "industries": r[14] or [],
                         "min_salary_annual": float(r[15]) if r[15] is not None else None,
                         "salary_currency": r[16],
                         "desired_technologies": r[17] or [],
                         "min_score": r[18],
                         "frequency": r[20], "send_weekday": r[21],
                         "send_monthday": r[22], "send_hour_local": r[23],
                         "timezone": r[24]},
                     "next_digest_at": r[25].isoformat() if r[25] else None,
                     "last_digest_at": r[26].isoformat() if r[26] else None}
                    for r in cur.fetchall()]

    @app.put("/me/cluster/{cluster_id}", status_code=204)
    def iscrivi_cluster(cluster_id: str, filtri: FiltriCluster,
                        uid: str = Depends(utente), conn=Depends(connessione)):
        """Aggiorna una ricerca per UUID, o un vecchio cluster non ambiguo."""
        with conn.cursor() as cur:
            subscription_id, cluster_id = _ricerca_identificata(cur, uid, cluster_id)
            cur.execute("SELECT status FROM clusters WHERE id = %s", (cluster_id,))
            r = cur.fetchone()
            if not r or r[0] != "active":
                raise HTTPException(404, "cluster inesistente o non attivo")
            if subscription_id is None:
                ruolo_chiave = (filtri.target_role or "").strip().lower()
                cur.execute("SELECT 1 FROM user_clusters "
                            "WHERE user_id = %s AND cluster_id = %s AND target_role_key = %s",
                            (uid, cluster_id, ruolo_chiave))
                if cur.fetchone() is None:
                    cur.execute(
                        "SELECT q.max_searches, "
                        "  (SELECT count(*) FROM user_clusters uc JOIN clusters c "
                        "     ON c.id = uc.cluster_id "
                        "   WHERE uc.user_id = %s AND c.status = 'active') "
                        "FROM users u JOIN plan_quotas q ON q.plan = u.plan "
                        "WHERE u.id = %s", (uid, uid))
                    tetto, attuali = cur.fetchone()
                    if attuali >= tetto:
                        raise HTTPException(
                            409,
                            f"Your plan covers {tetto} searches, and you have {attuali}. "
                            f"Stop one from your panel, or move up a plan.")
            _valida_filtri(cur, filtri)
            # La lista vuota dei tipi datore accettati non è esprimibile in
            # tabella (CHECK cardinality > 0): il default li accetta tutti.
            try:
                ricerca_id = _scrivi_iscrizione(
                    cur, uid, cluster_id, filtri, subscription_id)
            except psycopg.errors.UniqueViolation:
                conn.rollback()
                raise HTTPException(409, "a search with this role already exists")
            except psycopg.errors.CheckViolation as exc:
                conn.rollback()
                raise HTTPException(422, "pianificazione o filtri incoerenti") from exc
            _anticipa_primo_digest(
                cur, uid,
                nuova_ricerca_id=ricerca_id if subscription_id is None else None)
        conn.commit()

    @app.delete("/me/cluster/{cluster_id}", status_code=204)
    def disiscrivi_cluster(cluster_id: str, uid: str = Depends(utente),
                           conn=Depends(connessione)):
        with conn.cursor() as cur:
            subscription_id, _ = _ricerca_identificata(cur, uid, cluster_id)
            cur.execute("DELETE FROM user_clusters WHERE user_id = %s AND id = %s",
                        (uid, subscription_id))
        conn.commit()

    # --- collegamento Telegram ---------------------------------------------
    #
    # Un bot Telegram NON puo' scrivere per primo: senza che sia l'utente ad
    # aprire la conversazione non esiste nessun chat_id. Il giro e' quindi
    # obbligato, e sono queste tre rotte: si crea un gettone, si mostra
    # t.me/<bot>?start=<gettone>, e quando l'utente preme START Telegram
    # consegna l'update al webhook che risolve il gettone.

    @app.post("/me/telegram/collega")
    def telegram_collega(uid: str = Depends(utente), conn=Depends(connessione)):
        """Apre un collegamento: -> il deep link da toccare o inquadrare."""
        if not telegram_mod.configurato():
            raise HTTPException(503, "Telegram non configurato su questo server")
        with conn.cursor() as cur:
            # I gettoni aperti di questo utente si chiudono: se qualcuno
            # riapre il pannello, il QR di prima non deve restare valido in
            # giro. Uno alla volta, sempre.
            cur.execute("UPDATE telegram_link_tokens SET consumed_at = now(), "
                        "  chat_id = '(annullato)' "
                        "WHERE user_id = %s AND consumed_at IS NULL", (uid,))
            gettone = secrets.token_urlsafe(32)
            cur.execute(
                "INSERT INTO telegram_link_tokens (user_id, token_hash, expires_at) "
                "VALUES (%s, %s, now() + interval '10 minutes')",
                (uid, hashlib.sha256(gettone.encode()).hexdigest()))
        conn.commit()
        return {"link": telegram_mod.link_collegamento(gettone),
                "bot": telegram_mod.utente_bot(),
                "scade_tra_secondi": 600}

    @app.get("/me/telegram/stato")
    def telegram_stato(uid: str = Depends(utente), conn=Depends(connessione)):
        """Lo interroga la pagina mentre l'utente e' dentro Telegram.

        Rotta minuscola e non /me apposta: viene chiesta ogni due secondi
        finche' il pannello e' aperto, e /me fa molto piu' lavoro di quanto
        serva per rispondere a una domanda sola.
        """
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_chat_id IS NOT NULL, delivery_channels "
                        "FROM users WHERE id = %s", (uid,))
            r = cur.fetchone()
        if not r:
            raise HTTPException(404, "utente inesistente")
        return {"collegato": r[0], "canali": r[1]}

    @app.delete("/me/telegram", status_code=204)
    def telegram_scollega(uid: str = Depends(utente), conn=Depends(connessione)):
        """Stacca la chat. Se era il canale di consegna, si torna all'email.

        Non si puo' lasciare delivery_channels a 'telegram' senza chat_id: il
        vincolo users_channel_address_ck lo rifiuta, ed e' giusto cosi' —
        sarebbe un utente che non riceve piu' niente senza saperlo.
        """
        with conn.cursor() as cur:
            cur.execute(
                # Via la chat E via il canale dall'insieme: il vincolo
                # users_channels_ck non ammette 'telegram' senza chat_id.
                # Se era l'unico canale, resta l'email.
                "UPDATE users SET telegram_chat_id = NULL, delivery_failures = 0, "
                "  delivery_channels = CASE "
                "    WHEN array_remove(delivery_channels, 'telegram') = '{}' "
                "    THEN ARRAY['email'] "
                "    ELSE array_remove(delivery_channels, 'telegram') END "
                "WHERE id = %s", (uid,))
        conn.commit()

    def _chat_amministratore(conn) -> str:
        """Il chat id di chi amministra: quello dell'utente ADMIN_EMAIL
        (default il proprietario), letto dal motore a ogni messaggio. Non
        e' in un file: se il collegamento Telegram cambia, cambia da solo."""
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT telegram_chat_id FROM users WHERE email = %s AND telegram_chat_id IS NOT NULL",
                            (os.environ.get("ADMIN_EMAIL", "g.ranno@outlook.com"),))
                r = cur.fetchone()
            return str(r[0]) if r else ""
        except Exception:                             # noqa: BLE001
            conn.rollback()
            return ""

    def _consegna_chat(chat_id: str, testo: str) -> None:
        """Il messaggio va in un file leggibile solo da root, e chat.sh
        parte in un'unita' propria. Il testo NON passa per la riga di
        comando: e' dell'amministratore, ma resta un dato da non mostrare
        in `ps`."""
        import subprocess
        import tempfile
        import time
        try:
            os.makedirs("/var/lib/nivult-chat", mode=0o700, exist_ok=True)
            fd, path = tempfile.mkstemp(prefix="msg-", suffix=".txt", dir="/var/lib/nivult-chat")
            with os.fdopen(fd, "w") as f:
                f.write(testo)
            subprocess.Popen(["systemd-run", "--collect", "--quiet",
                              "--unit", f"nivult-chat-{int(time.time()*1000)}",
                              "-p", "OOMScoreAdjust=500",
                              "/opt/nivult/engine/deploy/chat.sh", chat_id, path],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:                      # noqa: BLE001
            telegram_mod.invia_testo(chat_id, f"Non riesco ad avviare la chat sul server: {type(exc).__name__}")

    @app.post("/telegram/webhook", include_in_schema=False)
    async def telegram_webhook(request: Request, conn=Depends(connessione)):
        """Dove Telegram consegna il «/start <gettone>».

        **Rotta pubblica, e per questo autenticata dall'header segreto.**
        Telegram rimanda il `secret_token` impostato con setWebhook in
        X-Telegram-Bot-Api-Secret-Token a ogni chiamata. Senza quel controllo
        chiunque potrebbe costruire un POST e collegare la PROPRIA chat
        all'account di un altro, cioe' dirottargli addosso i digest. E'
        il punto piu' delicato di tutta la funzione.

        Si risponde 200 in ogni caso: a Telegram non si racconta niente, e un
        errore lo farebbe ritentare in eterno su un update che non migliora.
        """
        atteso = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
        ricevuto = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if not atteso or not ricevuto or not secrets.compare_digest(ricevuto, atteso):
            raise HTTPException(403, "no")

        try:
            corpo = await request.json()
        except Exception:                                # noqa: BLE001
            # un corpo non-JSON non e' un nostro guasto: 200 e basta,
            # come promesso nel docstring (a Telegram non si racconta)
            return {"ok": True}
        msg = (corpo or {}).get("message") or {}
        chat_id = str(((msg.get("chat") or {}).get("id") or "")).strip()
        testo = (msg.get("text") or "").strip()
        if not chat_id or not testo:
            return {"ok": True}
        # La chat dell'amministratore e' un canale a doppio senso: tutto
        # cio' che NON e' un /start viene consegnato a deploy/chat.sh
        # (Claude sul server), in un'unita' systemd separata cosi' la
        # risposta a Telegram arriva subito. L'amministratore e' UNO, e lo
        # decide il suo chat id collegato nel motore, non un testo.
        if not testo.startswith("/start") and chat_id == _chat_amministratore(conn):
            _consegna_chat(chat_id, testo)
            return {"ok": True}
        if not testo.startswith("/start"):
            return {"ok": True}
        pezzi = testo.split(maxsplit=1)
        gettone = pezzi[1].strip() if len(pezzi) > 1 else ""
        if not gettone:
            telegram_mod.invia_testo(chat_id, _marchio(TG_SENZA_GETTONE))
            return {"ok": True}

        with conn.cursor() as cur:
            # Consumo atomico nell'UPDATE condizionato, come per il magic
            # link: due START con lo stesso gettone, uno solo vince.
            cur.execute(
                "UPDATE telegram_link_tokens SET consumed_at = now(), chat_id = %s "
                "WHERE token_hash = %s AND consumed_at IS NULL AND expires_at > now() "
                "RETURNING user_id::text",
                (chat_id, hashlib.sha256(gettone.encode()).hexdigest()))
            r = cur.fetchone()
            if not r:
                conn.rollback()
                telegram_mod.invia_testo(chat_id, _marchio(TG_SCADUTO))
                return {"ok": True}
            uid = r[0]
            # Una chat appartiene a un utente solo: l'indice unico parziale
            # lo impone, e qui si traduce in un messaggio comprensibile
            # invece che in un 500 muto.
            try:
                cur.execute(
                    "UPDATE users SET telegram_chat_id = %s, delivery_failures = 0 "
                    "WHERE id = %s", (chat_id, uid))
            except psycopg.errors.UniqueViolation:
                conn.rollback()
                telegram_mod.invia_testo(chat_id, _marchio(TG_GIA_COLLEGATA))
                return {"ok": True}
            cur.execute("SELECT locale, display_name, frequency "
                        "FROM users WHERE id = %s", (uid,))
            locale, nome, freq = cur.fetchone() or ("en", None, "weekly")
        conn.commit()
        telegram_mod.invia_testo(
            chat_id, _benvenuto(locale, nome, freq, "telegram"))
        return {"ok": True}

    # --- collegamento WhatsApp ---------------------------------------------
    #
    # Il verso e' lo stesso di Telegram ma per una ragione diversa: su
    # WhatsApp potremmo scrivere noi per primi (a pagamento, con template),
    # pero' se per collegare bastasse digitare un numero nel pannello,
    # chiunque potrebbe inserire il numero di qualcun altro e fargli piovere
    # addosso i propri digest. La prova di possesso la da' l'utente
    # scrivendoci per primo: wa.me con testo precompilato "NIVULT <gettone>".
    # Non c'e' webhook: si legge l'inbox Zernio quando la pagina chiede lo
    # stato — e' il polling della pagina a fare da motore.

    @app.post("/me/whatsapp/collega")
    def whatsapp_collega(uid: str = Depends(utente), conn=Depends(connessione)):
        if not whatsapp_mod.configurato():
            raise HTTPException(503, "WhatsApp non configurato su questo server")
        with conn.cursor() as cur:
            cur.execute("UPDATE whatsapp_link_tokens SET consumed_at = now(), "
                        "  phone_e164 = '(annullato)' "
                        "WHERE user_id = %s AND consumed_at IS NULL", (uid,))
            gettone = secrets.token_urlsafe(32)
            cur.execute(
                "INSERT INTO whatsapp_link_tokens (user_id, token_hash, expires_at) "
                "VALUES (%s, %s, now() + interval '10 minutes')",
                (uid, hashlib.sha256(gettone.encode()).hexdigest()))
        conn.commit()
        return {"link": whatsapp_mod.link_collegamento(gettone),
                "numero": f"+{whatsapp_mod.numero_bot()}",
                "scade_tra_secondi": 600}

    @app.get("/me/whatsapp/stato")
    def whatsapp_stato(uid: str = Depends(utente), conn=Depends(connessione)):
        """Interrogata dalla pagina ogni pochi secondi durante il collegamento.

        A differenza di Telegram non c'e' un webhook che ci chiama: e' QUESTA
        rotta a leggere l'inbox Zernio e a consumare i gettoni che trova. Il
        lavoro si fa solo se l'utente ha un gettone aperto — cioe' solo
        mentre qualcuno sta davvero collegando.
        """
        with conn.cursor() as cur:
            cur.execute("SELECT whatsapp_e164 IS NOT NULL, delivery_channels "
                        "FROM users WHERE id = %s", (uid,))
            r = cur.fetchone()
            if not r:
                raise HTTPException(404, "utente inesistente")
            if r[0]:
                return {"collegato": True, "canali": r[1]}
            cur.execute("SELECT count(*) FROM whatsapp_link_tokens "
                        "WHERE user_id = %s AND consumed_at IS NULL "
                        "  AND expires_at > now()", (uid,))
            aperti = cur.fetchone()[0]
        if not aperti:
            return {"collegato": False, "canali": r[1]}

        try:
            trovati = whatsapp_mod.cerca_collegamenti()
        except Exception:
            # Un buco di rete verso Zernio non deve rompere il polling: la
            # pagina richiedera' fra due secondi.
            return {"collegato": False, "canali": r[1]}

        for tr in trovati:
            if not tr.get("telefono"):
                continue
            with conn.cursor() as cur:
                # Consumo atomico: stesso UPDATE condizionato del magic link.
                cur.execute(
                    "UPDATE whatsapp_link_tokens SET consumed_at = now(), "
                    "  phone_e164 = %s "
                    "WHERE token_hash = %s AND consumed_at IS NULL "
                    "  AND expires_at > now() RETURNING user_id::text",
                    (tr["telefono"], tr["gettone_hash"]))
                riga = cur.fetchone()
                if not riga:
                    conn.rollback()
                    continue
                proprietario = riga[0]
                try:
                    cur.execute(
                        "UPDATE users SET whatsapp_e164 = %s, "
                        "  whatsapp_conversation_id = %s, delivery_failures = 0 "
                        "WHERE id = %s",
                        (tr["telefono"], tr["conversazione"], proprietario))
                except psycopg.errors.UniqueViolation:
                    conn.rollback()
                    try:
                        whatsapp_mod.invia_testo(tr["conversazione"],
                                                 _marchio(WA_GIA_COLLEGATO))
                    except Exception:
                        pass
                    continue
                cur.execute("SELECT locale, display_name, frequency "
                            "FROM users WHERE id = %s", (proprietario,))
                loc, nome_wa, freq_wa = cur.fetchone() or ("en", None, "weekly")
            conn.commit()
            # La finestra di 24 ore e' aperta dal messaggio dell'utente:
            # questa conferma e' gratuita. Se fallisce, il collegamento
            # resta valido — la conferma e' cortesia, non condizione.
            try:
                whatsapp_mod.invia_testo(
                    tr["conversazione"],
                    _benvenuto(loc, nome_wa, freq_wa, "whatsapp"))
            except Exception:
                pass

        with conn.cursor() as cur:
            cur.execute("SELECT whatsapp_e164 IS NOT NULL, delivery_channels "
                        "FROM users WHERE id = %s", (uid,))
            r = cur.fetchone()
        return {"collegato": r[0], "canali": r[1]}

    @app.delete("/me/whatsapp", status_code=204)
    def whatsapp_scollega(uid: str = Depends(utente), conn=Depends(connessione)):
        """Stacca il numero. Se era il canale di consegna, si torna all'email:
        il vincolo users_channel_address_ck non ammette la via di mezzo."""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET whatsapp_e164 = NULL, "
                "  whatsapp_conversation_id = NULL, delivery_failures = 0, "
                "  delivery_channels = CASE "
                "    WHEN array_remove(delivery_channels, 'whatsapp') = '{}' "
                "    THEN ARRAY['email'] "
                "    ELSE array_remove(delivery_channels, 'whatsapp') END "
                "WHERE id = %s", (uid,))
        conn.commit()

    # --- token push dell'app nativa -----------------------------------------
    # Il token identifica un dispositivo: formato chiuso, tetto per account e
    # registrazione seriale per impedire accumuli e raffiche di notifiche.

    @app.post("/me/push/token", status_code=204)
    def push_registra(corpo: PushTokenRegistrazione, uid: str = Depends(utente),
                      conn=Depends(connessione)):
        """Registra (o rinfresca) il token push del dispositivo.

        Idempotente per costruzione: la stessa coppia (utente, token) si
        aggiorna e basta, quindi l'app puo' ri-registrarsi a ogni avvio.
        """
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (uid,))
            cur.execute("SELECT count(*) FROM user_push_tokens "
                        "WHERE user_id = %s AND token <> %s", (uid, corpo.token))
            if cur.fetchone()[0] >= MAX_PUSH_TOKENS_PER_USER:
                cur.execute("SELECT 1 FROM user_push_tokens "
                            "WHERE user_id = %s AND token = %s", (uid, corpo.token))
                if cur.fetchone() is None:
                    raise HTTPException(409, "massimo 10 dispositivi per account")
            cur.execute(
                "INSERT INTO user_push_tokens (user_id, token, platform) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id, token) DO UPDATE "
                "SET platform = EXCLUDED.platform, updated_at = now()",
                (uid, corpo.token, corpo.platform))
        conn.commit()
        # Il token NON si logga intero: e' un dato quasi-personale.
        log.info("push: token %s… registrato (%s)",
                 corpo.token[:12], corpo.platform)

    @app.delete("/me/push/token", status_code=204)
    def push_rimuovi(corpo: PushTokenRimozione, uid: str = Depends(utente),
                     conn=Depends(connessione)):
        """Dimentica il token (logout, disinstallazione, opt-out nell'app).

        204 anche se il token non c'e': la rimozione e' idempotente, e
        confessare se esisteva non aiuterebbe nessuno.
        """
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_push_tokens "
                        "WHERE user_id = %s AND token = %s",
                        (uid, corpo.token))
        conn.commit()
        log.info("push: token %s… rimosso", corpo.token[:12])

    @app.put("/me")
    def aggiorna_me(pref: PreferenzeUtente, uid: str = Depends(utente),
                    conn=Depends(connessione)):
        """Aggiorna le preferenze di consegna. I campi a null azzerano (serve
        per togliere il weekday quando si passa a daily); i campi assenti non
        si toccano. La coerenza fra frequenza e giorno si controlla qui per
        dare un 422 parlante — poi la ribadisce il vincolo sul database."""
        campi = pref.model_dump(exclude_unset=True)
        if "frequency" in campi:
            f = campi["frequency"]
            if f not in ("daily", "weekly", "monthly"):
                raise HTTPException(422, "frequency: daily, weekly o monthly")
            # Il giorno che non appartiene alla nuova frequenza si AZZERA,
            # non si rifiuta.
            #
            # Prima si leggevano i valori vecchi dalla riga e li si trattava
            # come se il chiamante li avesse mandati: chi passava da
            # settimanale a giornaliero riceveva «daily non ammette
            # send_weekday» per un valore che non aveva spedito e che non
            # poteva togliere. L'onboarding parte da «weekly», quindi
            # bastava salvare una volta e poi cambiare idea per restare
            # bloccati sul quarto passo, con il pulsante che sembrava morto.
            #
            # Un residuo della frequenza precedente non e' un errore del
            # chiamante: e' memoria nostra, e tocca a noi ripulirla. Resta
            # invece un 422 se la contraddizione e' ESPLICITA nella stessa
            # richiesta — «daily» insieme a un send_weekday scritto a mano
            # e' un chiamante confuso, e dirglielo aiuta.
            with conn.cursor() as cur:
                cur.execute("SELECT send_weekday, send_monthday FROM users "
                            "WHERE id = %s", (uid,))
                vecchio_w, vecchio_m = cur.fetchone()
            if f == "daily":
                if campi.get("send_weekday") is not None or campi.get("send_monthday") is not None:
                    raise HTTPException(422, "daily non ammette send_weekday né send_monthday")
                campi["send_weekday"] = None
                campi["send_monthday"] = None
            elif f == "weekly":
                if campi.get("send_monthday") is not None:
                    raise HTTPException(422, "weekly non ammette send_monthday")
                w = campi.get("send_weekday", vecchio_w)
                if w is None:
                    raise HTTPException(422, "weekly richiede send_weekday (1=lunedì … 7=domenica)")
                campi["send_weekday"] = w
                campi["send_monthday"] = None
            elif f == "monthly":
                if campi.get("send_weekday") is not None:
                    raise HTTPException(422, "monthly non ammette send_weekday")
                m = campi.get("send_monthday", vecchio_m)
                if m is None:
                    raise HTTPException(422, "monthly richiede send_monthday (1–28)")
                campi["send_monthday"] = m
                campi["send_weekday"] = None
        if "delivery_channels" in campi:
            # L'INSIEME dei canali: dedupe con ordine conservato, mai vuoto,
            # solo valori noti. Gli indirizzi si controllano nel DATABASE,
            # non nel payload: il payload non puo' portarli (vedi
            # PreferenzeUtente), e un canale senza recapito collegato
            # sarebbe un utente che smette di ricevere senza saperlo.
            canali = list(dict.fromkeys(campi["delivery_channels"] or []))
            if not canali:
                raise HTTPException(422, "serve almeno un canale")
            for c in canali:
                if c not in ("email", "telegram", "whatsapp", "app"):
                    raise HTTPException(422, "delivery_channels: email, "
                                        "telegram, whatsapp o app")
            # 'app' non ha recapito da controllare: il recapito e' l'account
            # stesso (la tab Digest legge digest_items), non un indirizzo
            # esterno da collegare come telegram/whatsapp.
            with conn.cursor() as cur:
                cur.execute("SELECT telegram_chat_id IS NOT NULL, "
                            "whatsapp_e164 IS NOT NULL FROM users "
                            "WHERE id = %s", (uid,))
                ha_tg, ha_wa = cur.fetchone()
            if "telegram" in canali and not ha_tg:
                raise HTTPException(422, "telegram non e' collegato: prima "
                                    "il collegamento, poi il canale")
            if "whatsapp" in canali and not ha_wa:
                raise HTTPException(422, "whatsapp non e' collegato: prima "
                                    "il collegamento, poi il canale")
            campi["delivery_channels"] = canali
        if not campi:
            raise HTTPException(422, "nessun campo riconosciuto")
        if "delivery_email" in campi:
            campi["delivery_email"] = str(campi["delivery_email"])

        assegnazioni = ", ".join(f"{c} = %s" for c in campi)
        with conn.cursor() as cur:
            try:
                cur.execute(f"UPDATE users SET {assegnazioni} WHERE id = %s",
                            (*campi.values(), uid))
            except psycopg.errors.CheckViolation as exc:
                raise HTTPException(422, f"valore rifiutato dal vincolo: {exc.diag.message_detail or exc}")
            except psycopg.errors.InvalidTextRepresentation as exc:
                raise HTTPException(422, f"valore malformato: {exc}")

            # Il primo next_digest_at nasce QUI. Finora lo scriveva solo il
            # worker, dopo un invio: chi si iscriveva restava con NULL, e
            # `next_digest_at IS NOT NULL` nella query degli utenti dovuti lo
            # rendeva non-dovuto per sempre. Ogni iscritto era inerte, e il
            # pannello lo nascondeva stampando la frequenza al posto della
            # data. Si ricalcola a ogni cambio d'orario, non solo la prima
            # volta: spostare l'ora e restare sul vecchio slot sarebbe lo
            # stesso bug al contrario.
            if {"frequency", "send_hour_local", "send_weekday",
                    "send_monthday", "timezone"} & set(campi):
                cur.execute(
                    "SELECT frequency, send_hour_local, send_weekday, "
                    "       send_monthday, timezone FROM users WHERE id = %s",
                    (uid,))
                f, ora, wd, md, tz = cur.fetchone()
                adesso = datetime.now(timezone.utc)
                try:
                    slot = calcola_slot(f, ora, wd, md, tz, adesso)
                except Exception as exc:
                    raise HTTPException(422, f"orario non calcolabile: {exc}")

                cur.execute("UPDATE users SET next_digest_at = %s WHERE id = %s",
                            (slot, uid))
                if {"send_hour_local", "timezone"} & set(campi):
                    cur.execute(
                        "SELECT id::text, frequency, send_weekday, send_monthday, "
                        "       send_hour_local, timezone FROM user_clusters "
                        "WHERE user_id = %s AND frequency IS NOT NULL "
                        "  AND ((%s AND send_hour_local IS NULL) "
                        "       OR (%s AND timezone IS NULL))",
                        (uid, "send_hour_local" in campi, "timezone" in campi))
                    override_fallback = cur.fetchall()
                    for ricerca_id, f_r, wd_r, md_r, ora_r, tz_r in override_fallback:
                        try:
                            slot_r = calcola_slot(
                                f_r, ora if ora_r is None else ora_r, wd_r, md_r,
                                tz_r or tz, adesso)
                        except Exception as exc:
                            raise HTTPException(422, f"orario non calcolabile: {exc}")
                        cur.execute(
                            "UPDATE user_clusters SET next_digest_at = %s "
                            "WHERE id = %s AND user_id = %s",
                            (slot_r, ricerca_id, uid))
                _anticipa_primo_digest(cur, uid)
        conn.commit()
        return me(uid=uid, conn=conn)

    # --- CV ----------------------------------------------------------------

    def _analizza_cv_connessione(testo: str) -> dict:
        with psycopg.connect(database_url()) as analisi_conn:
            return _analizza_cv(analisi_conn, testo)

    @app.post("/me/cv")
    async def carica_cv(file: UploadFile = File(...),
                        uid: str = Depends(utente),
                        conn=Depends(connessione)):
        """Carica il CV: cifrato a busta prima di toccare lo storage, con il
        profilo proposto da GLM nella risposta perché l'utente lo confermi.

        Il testo del CV non si logga MAI: è un dato personale come tutto il
        resto del file.
        """
        dati = await file.read(cv.MAX_BYTES + 1)
        if len(dati) > cv.MAX_BYTES:
            raise HTTPException(413, "il file supera i 20 MB: troppo grande per un CV")
        try:
            testo = await run_in_threadpool(
                cv.estrai_testo, file.filename or "", file.content_type or "", dati)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        try:
            profilo = await run_in_threadpool(_analizza_cv_connessione, testo)
        except RuntimeError as exc:
            # Il dettaglio del fornitore resta nei log, dove serve a noi. A chi
            # sta caricando il CV non dice nulla di azionabile, e "credito
            # esaurito" e' un fatto nostro che non va scaricato su di lui.
            # Conta invece dirgli che il suo file non e' il problema e che
            # nulla e' stato salvato a meta'.
            log.error("estrazione del profilo fallita: %s", exc)
            raise HTTPException(
                502,
                "The engine could not read your CV right now — nothing to do "
                "with your file, and nothing was saved. Try again in a few "
                "minutes.",
            )

        esito = await run_in_threadpool(crypto.cifra, dati)
        chiave = f"cv/{uid}/{secrets.token_hex(8)}"
        await run_in_threadpool(storage.salva, chiave, esito.dati)
        da_eliminare: list[str] = []
        try:
            with conn.cursor() as cur:
                # Il CV attivo diventa storico: la riga resta (dice quale
                # versione ha prodotto un dato match, e ne conserva il
                # profilo), il file cifrato no — un blob personale che non
                # serve più a nulla non deve accumularsi nel bucket.
                #
                # Le chiavi si RACCOLGONO qui ma si eliminano DOPO il commit:
                # eliminandole subito, un INSERT fallito più sotto avrebbe
                # rimesso 'active' il CV vecchio... senza più il suo file.
                cur.execute("UPDATE user_cvs SET status = 'superseded', "
                            "       superseded_at = now() "
                            "WHERE user_id = %s AND status = 'active' "
                            "RETURNING storage_key", (uid,))
                da_eliminare = [r[0] for r in cur.fetchall()]
                cur.execute(
                    "INSERT INTO user_cvs (user_id, storage_key, original_filename, "
                    "  mime_type, sha256, families, seniority, skills, languages, "
                    "  years_experience, raw_extraction, encryption_algo, "
                    "  encrypted_dek, nonce, auth_tag, kek_version) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'aes-256-gcm',"
                    "  %s,%s,%s,1) RETURNING id::text",
                    (uid, chiave, file.filename, file.content_type,
                     hashlib.sha256(dati).hexdigest(), profilo["families"],
                     profilo["seniority"], profilo["skills"],
                     Json(profilo["languages"]),
                     profilo["years_experience"],
                     Json(profilo["raw_extraction"]),
                     Binary(esito.encrypted_dek),
                     Binary(esito.nonce),
                     Binary(esito.auth_tag)))
                cv_id = cur.fetchone()[0]
                # Un CV nuovo riapre solo i match mai consegnati e senza
                # azioni personali: feedback e tracker restano allo stesso id.
                riaperti = funnel_matching.elimina_match_da_rivalutare(cur, uid)
                _anticipa_primo_digest(cur, uid, inizializza_override=True)
            conn.commit()
        except Exception:
            # La riga non c'è, il file cifrato non deve restare orfano.
            await run_in_threadpool(storage.elimina, chiave)
            raise
        for vecchia in da_eliminare:
            try:
                await run_in_threadpool(storage.elimina, vecchia)
            except Exception as exc:  # noqa: BLE001
                # Il blob orfano è un residuo, non un guasto: si logga e si va.
                log.warning("blob del CV precedente non rimosso (%s): %s",
                            vecchia, exc)
        return {"id": cv_id, "profilo": profilo, "match_riaperti": riaperti}

    @app.get("/me/cv")
    def leggi_cv(lingua: str | None = None, uid: str = Depends(utente),
                 conn=Depends(connessione)):
        """Il profilo del CV attivo: ciò che l'onboarding precompila e il
        pannello mostra. Il file vero sta in /me/cv/file."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id::text, families, seniority, skills, languages, years_experience, "
                "       uploaded_at, mime_type, original_filename, raw_extraction "
                "FROM user_cvs WHERE user_id = %s AND status = 'active'", (uid,))
            r = cur.fetchone()
        if not r:
            raise HTTPException(404, "nessun CV caricato")
        # I quattro campi «di racconto» vivono in raw_extraction, gia'
        # ripuliti dall'estrazione. Un CV caricato prima che esistessero
        # non li ha: si risponde vuoto e il pannello non li disegna —
        # meglio una scheda piu' corta di un campo che dice «—».
        estratto = r[9] if isinstance(r[9], dict) else {}

        # La sintesi e' prosa NOSTRA (scritta da GLM all'estrazione), e la
        # prosa segue la lingua del pannello che la chiede: si traduce una
        # volta per lingua e si salva nel dizionario headline_i18n. I fatti
        # del CV (ruoli, titoli, certificazioni) restano nella lingua del
        # CV: sono citazioni, non racconto.
        sintesi = estratto.get("headline")
        consigli = estratto.get("cv_tips") or []
        from nivult.delivery.testi import LINGUA_PER_GLM
        voluta = LINGUA_PER_GLM.get(lingua or "")
        if voluta and (sintesi or consigli):
            tr_sintesi = estratto.get("headline_i18n") or {}
            tr_consigli = estratto.get("cv_tips_i18n") or {}
            manca = ((sintesi and voluta not in tr_sintesi)
                     or (consigli and voluta not in tr_consigli))
            if manca and os.environ.get("GLM_API_KEY"):
                # UNA chiamata per lingua: sintesi e consigli insieme,
                # salvati nei rispettivi dizionari — le visite successive
                # leggono dall'archivio.
                try:
                    from nivult.matching.llm import GLM
                    risposta = GLM().chat([
                        {"role": "system", "content":
                         f"Traduci in {voluta} mantenendo nomi propri, "
                         f"tecnologie e sigle. Rispondi SOLO con JSON: "
                         f'{{"headline": "...", "tips": ["..."]}}'},
                        {"role": "user", "content": json.dumps(
                            {"headline": sintesi or "",
                             "tips": consigli}, ensure_ascii=False)},
                    ], max_tokens=300)
                    dati = json.loads(re.search(r"\{.*\}", risposta,
                                                re.S).group(0))
                    if sintesi and dati.get("headline"):
                        tr_sintesi[voluta] = str(dati["headline"])[:200]
                    if consigli and isinstance(dati.get("tips"), list):
                        tr_consigli[voluta] = [str(x)[:200]
                                               for x in dati["tips"]][:3]
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE user_cvs SET raw_extraction = "
                            "jsonb_set(jsonb_set(raw_extraction, "
                            "'{headline_i18n}', %s::jsonb, true), "
                            "'{cv_tips_i18n}', %s::jsonb, true) "
                            "WHERE user_id = %s AND status = 'active'",
                            (Json(tr_sintesi), Json(tr_consigli), uid))
                    conn.commit()
                except Exception as exc:
                    log.warning("traduzione profilo fallita per %s: %s",
                                uid, exc)
            sintesi = tr_sintesi.get(voluta) or sintesi
            consigli = tr_consigli.get(voluta) or consigli

        return {"id": r[0], "families": r[1], "seniority": r[2], "skills": r[3],
                "languages": r[4], "years_experience": r[5],
                "caricato_il": r[6], "tipo": r[7], "nome_file": r[8],
                "sintesi": sintesi,
                "ruoli": estratto.get("roles") or [],
                "certificazioni": estratto.get("certifications") or [],
                "formazione": estratto.get("education") or [],
                "ricerche_consigliate": estratto.get("suggested_searches") or [],
                "consigli_cv": consigli}

    # I tipi che accettiamo in caricamento, e gli UNICI che si riservono.
    # Non si rimanda mai indietro il content-type dichiarato dal client: un
    # file caricato come text/html e riservito tale sarebbe una pagina
    # ospitata sul nostro dominio, scritta da chi l'ha caricata.
    TIPI_CV = {
        "application/pdf": "application/pdf",
        "text/plain": "text/plain; charset=utf-8",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }

    @app.put("/me/cv/profilo")
    def aggiorna_profilo_cv(corpo: ProfiloCv, uid: str = Depends(utente),
                            conn=Depends(connessione)):
        """Corregge il profilo letto dal CV. -> il profilo salvato.

        Vale la stessa regola dell'estrazione: cio' che sta fuori dai
        vocabolari si SCARTA, non si corregge — un valore inventato su cui
        poi si filtra e' peggio di un campo vuoto. Le competenze sono testo
        libero (le scrive il candidato, non un vocabolario), quindi si
        ripuliscono e si tagliano a quindici come fa GLM.
        """
        with conn.cursor() as cur:
            cur.execute("SELECT family FROM job_families")
            famiglie = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT code FROM experience_levels")
            livelli = {r[0] for r in cur.fetchall()}
            cur.execute("SELECT api_value FROM filter_values "
                        "WHERE parameter = 'ai_language'")
            lingue = {r[0] for r in cur.fetchall()}

            fam = [f for f in dict.fromkeys(corpo.families) if f in famiglie][:3]
            if not fam:
                raise HTTPException(422, "serve almeno una famiglia "
                                    "professionale del catalogo")
            lin = [l for l in dict.fromkeys(corpo.languages) if l in lingue][:10]
            sen = corpo.seniority if corpo.seniority in livelli else None
            skl = [s.strip()[:80] for s in dict.fromkeys(corpo.skills)
                   if isinstance(s, str) and s.strip()][:15]
            anni = corpo.years_experience
            if anni is not None:
                anni = min(max(int(anni), 0), 70)

            cur.execute(
                "UPDATE user_cvs SET families = %s, seniority = %s, skills = %s, "
                "  languages = %s, years_experience = %s "
                "WHERE user_id = %s AND status = 'active' RETURNING id",
                (fam, sen, skl, Json(lin), anni, uid))
            if not cur.fetchone():
                raise HTTPException(404, "nessun CV caricato")

            # Come per un CV nuovo: si rivalutano solo i match non consegnati
            # e mai toccati dall'utente; feedback e tracker non si cancellano.
            riaperti = funnel_matching.elimina_match_da_rivalutare(cur, uid)
        conn.commit()
        log.info("profilo CV corretto da %s: %d match riaperti", uid, riaperti)
        return {"families": fam, "seniority": sen, "skills": skl,
                "languages": lin, "years_experience": anni,
                "riaperti": riaperti}

    @app.get("/me/cv/file")
    def leggi_cv_file(uid: str = Depends(utente), conn=Depends(connessione)):
        """Il CV vero dell'utente, decifrato, per l'anteprima nel pannello.

        È il suo file e lo rivede lui: la sessione è l'unica chiave, e la
        query filtra per user_id — non esiste un id da indovinare.

        Le intestazioni sono la parte che conta:
        - `no-store`, perché un CV in una cache condivisa è un CV altrove;
        - `nosniff` più un content-type dalla NOSTRA lista, così nessun
          caricamento può farsi servire come HTML dal nostro dominio;
        - `sandbox` in CSP, perché un PDF può contenere script e qui viene
          mostrato dentro un iframe — la stessa difesa già usata per gli
          SVG dei loghi.
        """
        with conn.cursor() as cur:
            cur.execute(
                "SELECT storage_key, encrypted_dek, mime_type, original_filename "
                "FROM user_cvs WHERE user_id = %s AND status = 'active'", (uid,))
            r = cur.fetchone()
        if not r:
            raise HTTPException(404, "nessun CV caricato")
        chiave, dek, tipo, nome = r
        tipo_servito = TIPI_CV.get(tipo or "", "application/octet-stream")
        try:
            dati = crypto.decifra(storage.leggi(chiave), bytes(dek))
        except Exception as exc:
            log.warning("CV non leggibile per %s: %s", uid, exc)
            raise HTTPException(503, "il file non e' leggibile in questo momento")
        from urllib.parse import quote
        nome_file = (nome or "cv").replace("\r", "").replace("\n", "")[:120]
        nome_ascii = (nome_file.replace('"', "").replace("\\", "_")
                      .encode("ascii", "replace").decode("ascii")) or "cv"
        disposizione = (f'inline; filename="{nome_ascii}"; '
                        f"filename*=UTF-8''{quote(nome_file, safe='')}")
        return Response(
            content=dati,
            media_type=tipo_servito,
            headers={
                "Cache-Control": "no-store, private",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "Content-Disposition": disposizione,
            })

    # L'API dei clienti B2B (dataset di offerte e aziende), versionata nel
    # percorso come /v1. E' l'unico router incluso: tutto il resto qui sono
    # decoratori, ma il /v1 ha regole proprie (chiavi, crediti, campi che
    # non si rinominano mai) e vive in un modulo suo.
    app.include_router(api_clienti_v1.router)

    # Le prenotazioni delle call da nivult.com/booking (10/10/2026):
    # pubbliche, niente sessione — l'identita' e' il token nella mail.
    booking_mod.registra(app)

    return app


app = create_app()
