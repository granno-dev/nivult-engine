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
import json
import logging
import ipaddress
import re
import os
import secrets
from datetime import datetime, timezone

import httpx
import psycopg
from psycopg import Binary
from psycopg.types.json import Json
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field

from nivult import auth, oauth
from nivult import crypto, cv, storage
from nivult.config import database_url, load_dotenv
from nivult.delivery import telegram as telegram_mod
from nivult.delivery import whatsapp as whatsapp_mod
from nivult.matching.llm import GLM
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
    # ~Nivult~ nel dialetto del canale: HTML su Telegram, asterischi su
    # WhatsApp. Il resto del testo non porta markup.
    if canale == "telegram":
        return testo.replace("~Nivult~", "<b>Nivult</b>")
    return testo.replace("~Nivult~", "*Nivult*")


WA_GIA_COLLEGATO = ("This WhatsApp number is already connected to another "
                    "Nivult account. Disconnect it there first.")


load_dotenv()
log = logging.getLogger("nivult.api")


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
    # La lingua della pagina da cui l'utente sta chiedendo il link: decide la
    # lingua dell'EMAIL, e alla prima richiesta diventa la lingua dell'account.
    locale: str | None = Field(default=None, pattern="^[a-z]{2}$")


class ConsumoLink(BaseModel):
    token: str


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
    min_headcount: int | None = None
    max_headcount: int | None = None
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
            ua=request.headers.get("User-Agent"),
            locale=corpo.locale)
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

    # ── Accesso con Google e Microsoft ──────────────────────────────────────
    #
    # Sono navigazioni di primo livello del browser, non chiamate XHR: qui il
    # CORS non c'entra nulla, e la risposta è sempre un 302.
    #
    # Il giro termina su /verify del sito con un gettone monouso, la stessa
    # pagina in cui atterra il magic link. Così il token di sessione non entra
    # mai in una URL, e il sito non guadagna una rotta.

    def _al_sito(percorso: str) -> RedirectResponse:
        return RedirectResponse(f"{oauth.site_url()}{percorso}", status_code=302)

    @app.get("/auth/oauth/{provider}/start")
    def oauth_start(provider: str, conn=Depends(connessione)):
        # Rotta non autenticata che scrive una riga: le righe sono minuscole,
        # vivono dieci minuti, e ogni passaggio pota le scadute. Se un giorno
        # servisse un freno, va messo qui.
        try:
            return RedirectResponse(oauth.inizia(conn, provider), status_code=302)
        except oauth.OAuthError as e:
            return _al_sito(f"/login?errore={e.codice}")

    @app.get("/auth/oauth/{provider}/callback")
    def oauth_callback(provider: str, code: str = "", state: str = "",
                       error: str = "", conn=Depends(connessione)):
        if error:
            # L'utente ha annullato sulla schermata del provider: non è un
            # guasto, si torna al login senza drammi.
            return _al_sito("/login?errore=accesso_annullato")
        try:
            gettone = oauth.concludi(conn, provider, code, state)
        except oauth.OAuthError as e:
            return _al_sito(f"/login?errore={e.codice}")
        return _al_sito(f"/verify?token={gettone}")

    @app.get("/me")
    def me(uid: str = Depends(utente), conn=Depends(connessione)):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT email::text, plan, subscription_status, delivery_channels, "
                "frequency, timezone, email_verified_at IS NOT NULL, status, "
                "next_digest_at, last_digest_at, send_hour_local, send_weekday, "
                "send_monthday, delivery_email::text, display_name, locale, "
                "telegram_chat_id IS NOT NULL, whatsapp_e164 IS NOT NULL "
                "FROM users WHERE id = %s", (uid,))
            r = cur.fetchone()
        if not r:
            raise HTTPException(404, "utente inesistente")
        return {"id": uid, "email": r[0], "piano": r[1], "abbonamento": r[2],
                "canali": r[3], "frequenza": r[4], "fuso": r[5],
                "email_verificata": r[6], "stato": r[7],
                # La data vera, non la frequenza ripetuta: e' l'unica cosa
                # che dice se il prodotto sta per fare qualcosa.
                "prossimo_digest": r[8].isoformat() if r[8] else None,
                "ultimo_digest": r[9].isoformat() if r[9] else None,
                "ora_invio": r[10], "giorno_settimana": r[11],
                "giorno_mese": r[12], "email_consegna": r[13],
                "nome": r[14], "locale": r[15],
                # Non gli indirizzi: al sito serve sapere SE un canale si
                # puo' scegliere, non a quale chat o numero consegniamo.
                "telegram_collegato": r[16], "whatsapp_collegato": r[17]}

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

    def _scrivi_iscrizione(cur, uid: str, cluster_id: str,
                           filtri: FiltriCluster) -> None:
        """L'iscrizione a un cluster con i suoi filtri, in un posto solo.

        La usano sia la PUT su una ricerca esistente sia l'apertura di una
        nuova: due copie di questa INSERT vorrebbero dire che un campo nuovo
        arriva in un percorso e non nell'altro, e il filtro mancante non si
        vedrebbe — semplicemente non filtrerebbe.
        """
        tipi = filtri.accepted_employer_kinds or \
            ["direct", "staffing_agency", "undisclosed"]
        cur.execute(
            "INSERT INTO user_clusters (user_id, cluster_id, languages, "
            "  min_seniority, max_seniority, work_arrangements, employment_types, "
            "  needs_visa_sponsorship, accepted_employer_kinds, min_headcount, "
            "  max_headcount, wants, target_role, industries) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_id, cluster_id) DO UPDATE SET "
            "  languages = EXCLUDED.languages, "
            "  min_seniority = EXCLUDED.min_seniority, "
            "  max_seniority = EXCLUDED.max_seniority, "
            "  work_arrangements = EXCLUDED.work_arrangements, "
            "  employment_types = EXCLUDED.employment_types, "
            "  needs_visa_sponsorship = EXCLUDED.needs_visa_sponsorship, "
            "  accepted_employer_kinds = EXCLUDED.accepted_employer_kinds, "
            "  min_headcount = EXCLUDED.min_headcount, "
            "  max_headcount = EXCLUDED.max_headcount, "
            "  wants = EXCLUDED.wants, "
            "  target_role = EXCLUDED.target_role, "
            "  industries = EXCLUDED.industries, "
            "  is_paused = false",
            (uid, cluster_id, filtri.languages, filtri.min_seniority,
             filtri.max_seniority, filtri.work_arrangements,
             filtri.employment_types, filtri.needs_visa_sponsorship, tipi,
             filtri.min_headcount, filtri.max_headcount,
             (filtri.wants or "").strip() or None,
             (filtri.target_role or "").strip() or None,
             filtri.industries))

    @app.post("/me/ricerca", status_code=201)
    def apri_ricerca(corpo: NuovaRicerca, uid: str = Depends(utente),
                     conn=Depends(connessione)):
        """Apre una ricerca e ci iscrive l'utente, creando il cluster se non
        c'e' ancora.

        Il tetto del piano si verifica QUI e non solo nel sito: e' anche il
        freno sui crediti. Ogni ricerca e' un cluster che entra
        nell'ingestione notturna e consuma il tetto mensile della fonte
        finche' resta attiva, quindi il numero non e' una regola commerciale
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

            cur.execute("SELECT c.id::text FROM user_clusters uc "
                        "JOIN clusters c ON c.id = uc.cluster_id "
                        "WHERE uc.user_id = %s AND c.family = %s AND c.country = %s",
                        (uid, famiglia, paese))
            gia = cur.fetchone()
            # Cambiare i filtri di una ricerca che si ha gia' non consuma una
            # posizione: e' la stessa ricerca.
            if not gia and attuali >= tetto:
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
            _scrivi_iscrizione(cur, uid, cluster_id, corpo.filtri)
            cur.execute("SELECT status, last_successful_fetch_at IS NOT NULL "
                        "FROM clusters WHERE id = %s", (cluster_id,))
            _, gia_letto = cur.fetchone()
        conn.commit()
        # `nuovo` dice al sito se il primo digest deve aspettare la prima
        # ingestione notturna: un mercato appena aperto non ha ancora offerte,
        # e non dirlo farebbe sembrare guasto un prodotto che sta lavorando.
        return {"id": cluster_id, "nuovo": not gia_letto, "famiglia": famiglia}

    @app.get("/me/offerte")
    def mie_offerte(uid: str = Depends(utente), conn=Depends(connessione),
                    limite: int = 40):
        """Le offerte che hanno superato la soglia per questo utente.

        Solo quelle passate: le scartate sono rumore, e mostrarle
        contraddirebbe la promessa — leggiamo tutto perche' te ne arrivi
        poco. Il conteggio del letto resta pero' nell'intestazione, perche'
        e' il lavoro fatto.
        """
        with conn.cursor() as cur:
            cur.execute(
                "SELECT m.score, m.reason, m.evaluated_at, "
                "       j.title, j.organization, j.url, j.link_kind, "
                "       j.employer_kind, j.cities, j.salary, "
                "       COALESCE(j.org_linkedin_slug, j.domain_derived), "
                "       j.purged_at IS NOT NULL "
                "FROM matches m JOIN jobs j ON j.id = m.job_id "
                "WHERE m.user_id = %s AND m.passed "
                "ORDER BY m.evaluated_at DESC, m.score DESC LIMIT %s",
                (uid, min(limite, 100)))
            righe = cur.fetchall()
            cur.execute("SELECT count(*) FROM matches WHERE user_id = %s", (uid,))
            lette = cur.fetchone()[0]
        return {
            "valutate": lette,
            "offerte": [{
                "punteggio": r[0], "motivo": r[1],
                "quando": r[2].isoformat() if r[2] else None,
                "titolo": r[3], "azienda": r[4], "url": r[5],
                # Serve al sito per l'etichetta di trasparenza: "candidatura
                # diretta" oppure "via <agenzia nazionale>".
                "link_kind": r[6], "tipo_datore": r[7],
                "citta": (r[8] or [None])[0], "stipendio": r[9],
                "logo": f"/logo/{r[10]}" if r[10] else None,
                "archiviata": r[11],
            } for r in righe],
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
                # Terzo anello della catena in CLAUDE.md: Logo.dev dal dominio.
                if not url and dominio:
                    url = f"https://img.logo.dev/{dominio}?size=128&format=png"
                mime = dati = None
                if url:
                    try:
                        with httpx.Client(timeout=8, follow_redirects=True) as c:
                            risp = c.get(url)
                        if risp.status_code == 200 and len(risp.content) <= 512_000:
                            # Il tipo si riconosce dai BYTE, non dall'header:
                            # l'S3 che ospita questi loghi li serve tutti come
                            # `binary/octet-stream`, e fidarsi dell'header
                            # scartava immagini perfettamente valide.
                            mime = _tipo_immagine(risp.content)
                            if mime:
                                dati = risp.content
                    except httpx.HTTPError:
                        pass
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
                "       uc.wants, uc.target_role, uc.industries "
                "FROM user_clusters uc JOIN clusters c ON c.id = uc.cluster_id "
                "WHERE uc.user_id = %s AND c.status = 'active' "
                "ORDER BY c.family, c.country", (uid,))
            return [{"id": r[0], "famiglia": r[1], "paese": r[2],
                     "filtri": {
                         "languages": r[3] or [], "min_seniority": r[4],
                         "max_seniority": r[5], "work_arrangements": r[6] or [],
                         "employment_types": r[7] or [],
                         "accepted_employer_kinds": r[8],
                         "needs_visa_sponsorship": r[9],
                         "min_headcount": r[10], "max_headcount": r[11],
                         "wants": r[12], "target_role": r[13],
                         "industries": r[14] or []}}
                    for r in cur.fetchall()]

    @app.put("/me/cluster/{cluster_id}", status_code=204)
    def iscrivi_cluster(cluster_id: str, filtri: FiltriCluster,
                        uid: str = Depends(utente), conn=Depends(connessione)):
        """Iscriviti al cluster con questi filtri, o aggiorna i filtri se già
        iscritto. Sostituzione integrale: ciò che non c'è nel corpo torna al
        default. Il pannello preferenze legge e riscrive tutto il blocco."""
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM clusters WHERE id = %s", (cluster_id,))
            r = cur.fetchone()
            if not r or r[0] != "active":
                raise HTTPException(404, "cluster inesistente o non attivo")
            _valida_filtri(cur, filtri)
            # La lista vuota dei tipi datore accettati non è esprimibile in
            # tabella (CHECK cardinality > 0): il default li accetta tutti.
            _scrivi_iscrizione(cur, uid, cluster_id, filtri)
        conn.commit()

    @app.delete("/me/cluster/{cluster_id}", status_code=204)
    def disiscrivi_cluster(cluster_id: str, uid: str = Depends(utente),
                           conn=Depends(connessione)):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_clusters WHERE user_id = %s "
                        "AND cluster_id = %s", (uid, cluster_id))
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

        corpo = await request.json()
        msg = (corpo or {}).get("message") or {}
        chat_id = str(((msg.get("chat") or {}).get("id") or "")).strip()
        testo = (msg.get("text") or "").strip()
        if not chat_id or not testo.startswith("/start"):
            return {"ok": True}
        pezzi = testo.split(maxsplit=1)
        gettone = pezzi[1].strip() if len(pezzi) > 1 else ""
        if not gettone:
            telegram_mod.invia_testo(chat_id, TG_SENZA_GETTONE)
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
                telegram_mod.invia_testo(chat_id, TG_SCADUTO)
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
                telegram_mod.invia_testo(chat_id, TG_GIA_COLLEGATA)
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
                                                 WA_GIA_COLLEGATO)
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
            # Se la frequenza cambia, i campi giorno arrivano nello stesso
            # corpo o restano quelli che ci sono già: li si legge entrambi.
            with conn.cursor() as cur:
                cur.execute("SELECT send_weekday, send_monthday FROM users "
                            "WHERE id = %s", (uid,))
                vecchio_w, vecchio_m = cur.fetchone()
            w = campi.get("send_weekday", vecchio_w)
            m = campi.get("send_monthday", vecchio_m)
            if f == "daily" and (w is not None or m is not None):
                raise HTTPException(422, "daily non ammette send_weekday né send_monthday")
            if f == "weekly" and m is not None:
                raise HTTPException(422, "weekly non ammette send_monthday")
            if f == "weekly" and w is None:
                raise HTTPException(422, "weekly richiede send_weekday (1=lunedì … 7=domenica)")
            if f == "monthly" and w is not None:
                raise HTTPException(422, "monthly non ammette send_weekday")
            if f == "monthly" and m is None:
                raise HTTPException(422, "monthly richiede send_monthday (1–28)")
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
                if c not in ("email", "telegram", "whatsapp"):
                    raise HTTPException(422, "delivery_channels: email, "
                                        "telegram o whatsapp")
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
                try:
                    slot = calcola_slot(f, ora, wd, md, tz,
                                        datetime.now(timezone.utc))
                except Exception as exc:
                    raise HTTPException(422, f"orario non calcolabile: {exc}")

                # IL PRIMO DIGEST NON ASPETTA LO SLOT.
                #
                # `calcola_slot` dà la prossima occorrenza dell'orario
                # scelto: fino a un giorno per chi lo vuole quotidiano, fino
                # a un MESE per chi lo vuole mensile. Significa che qualcuno
                # finiva l'iscrizione, caricava il CV, sceglieva le ricerche
                # — e poi non riceveva niente per settimane. Il momento in
                # cui una persona ha appena consegnato il proprio CV è
                # esattamente quello in cui va mostrato che il motore
                # funziona; un mese dopo si è già dimenticata di noi.
                #
                # Solo la PRIMA volta, e solo se il digest può davvero
                # nascere: senza CV attivo o senza una ricerca attiva il
                # worker registrerebbe un fallimento invece di una
                # consegna, che è peggio dell'attesa. Dal secondo in poi
                # comanda la frequenza scelta.
                cur.execute(
                    "SELECT u.last_digest_at IS NULL "
                    "   AND u.next_digest_at IS NULL "
                    "   AND EXISTS (SELECT 1 FROM user_cvs cv "
                    "               WHERE cv.user_id = u.id AND cv.status = 'active') "
                    "   AND EXISTS (SELECT 1 FROM user_clusters uc "
                    "               JOIN clusters c ON c.id = uc.cluster_id "
                    "               WHERE uc.user_id = u.id AND NOT uc.is_paused "
                    "                 AND c.status = 'active') "
                    "FROM users u WHERE u.id = %s", (uid,))
                primo = cur.fetchone()[0]
                cur.execute("UPDATE users SET next_digest_at = %s WHERE id = %s",
                            (datetime.now(timezone.utc) if primo else slot, uid))
        conn.commit()
        return me(uid=uid, conn=conn)

    # --- CV ----------------------------------------------------------------

    @app.post("/me/cv")
    async def carica_cv(file: UploadFile = File(...),
                        uid: str = Depends(utente),
                        conn=Depends(connessione)):
        """Carica il CV: cifrato a busta prima di toccare lo storage, con il
        profilo proposto da GLM nella risposta perché l'utente lo confermi.

        Il testo del CV non si logga MAI: è un dato personale come tutto il
        resto del file.
        """
        dati = await file.read()
        try:
            testo = cv.estrai_testo(file.filename or "", file.content_type or "", dati)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        try:
            profilo = _analizza_cv(conn, testo)
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

        esito = crypto.cifra(dati)
        chiave = f"cv/{uid}/{secrets.token_hex(8)}"
        storage.salva(chiave, esito.dati)
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
                cur.execute("UPDATE user_cvs SET status = 'superseded' "
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
                # Un CV nuovo è un giudizio nuovo: i match mai consegnati si
                # riaprono, così vengono rivalutati contro il profilo vero.
                # Quelli già entrati in un digest restano — sono il registro
                # di ciò che l'utente ha ricevuto, e l'anti-ripetizione su
                # quelli è una promessa, non un ostacolo.
                cur.execute(
                    "DELETE FROM matches m WHERE m.user_id = %s "
                    "AND NOT EXISTS (SELECT 1 FROM digest_items di "
                    "                WHERE di.match_id = m.id)", (uid,))
                riaperti = cur.rowcount
            conn.commit()
        except Exception:
            # La riga non c'è, il file cifrato non deve restare orfano.
            storage.elimina(chiave)
            raise
        for vecchia in da_eliminare:
            try:
                storage.elimina(vecchia)
            except Exception as exc:  # noqa: BLE001
                # Il blob orfano è un residuo, non un guasto: si logga e si va.
                log.warning("blob del CV precedente non rimosso (%s): %s",
                            vecchia, exc)
        return {"id": cv_id, "profilo": profilo, "match_riaperti": riaperti}

    @app.get("/me/cv")
    def leggi_cv(uid: str = Depends(utente), conn=Depends(connessione)):
        """Il profilo del CV attivo: ciò che l'onboarding precompila e il
        pannello mostra. Il file vero sta in /me/cv/file."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT families, seniority, skills, languages, years_experience, "
                "       uploaded_at, mime_type, original_filename FROM user_cvs "
                "WHERE user_id = %s AND status = 'active'", (uid,))
            r = cur.fetchone()
        if not r:
            raise HTTPException(404, "nessun CV caricato")
        return {"families": r[0], "seniority": r[1], "skills": r[2],
                "languages": r[3], "years_experience": r[4],
                "caricato_il": r[5], "tipo": r[6], "nome_file": r[7]}

    # I tipi che accettiamo in caricamento, e gli UNICI che si riservono.
    # Non si rimanda mai indietro il content-type dichiarato dal client: un
    # file caricato come text/html e riservito tale sarebbe una pagina
    # ospitata sul nostro dominio, scritta da chi l'ha caricata.
    TIPI_CV = {"application/pdf": "application/pdf",
               "text/plain": "text/plain; charset=utf-8"}

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
        return Response(
            content=dati,
            media_type=tipo_servito,
            headers={
                "Cache-Control": "no-store, private",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
                # `inline`: l'anteprima lo mostra, non lo scarica. Il nome
                # fra virgolette e ripulito: un a-capo in un header lo
                # spezzerebbe in due.
                "Content-Disposition":
                    'inline; filename="%s"' % (
                        (nome or "cv").replace('"', "").replace("\r", "")
                                      .replace("\n", "")[:120]),
            })

    return app


app = create_app()
