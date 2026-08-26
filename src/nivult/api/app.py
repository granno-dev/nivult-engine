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
from nivult.matching.llm import GLM
from nivult.matching.worker import calcola_slot

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
    delivery_channel: str | None = None
    delivery_email: EmailStr | None = None
    telegram_chat_id: str | None = None
    whatsapp_e164: str | None = None
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
                "SELECT email::text, plan, subscription_status, delivery_channel, "
                "frequency, timezone, email_verified_at IS NOT NULL, status, "
                "next_digest_at, last_digest_at, send_hour_local, send_weekday, "
                "send_monthday, delivery_email::text, display_name, locale "
                "FROM users WHERE id = %s", (uid,))
            r = cur.fetchone()
        if not r:
            raise HTTPException(404, "utente inesistente")
        return {"id": uid, "email": r[0], "piano": r[1], "abbonamento": r[2],
                "canale": r[3], "frequenza": r[4], "fuso": r[5],
                "email_verificata": r[6], "stato": r[7],
                # La data vera, non la frequenza ripetuta: e' l'unica cosa
                # che dice se il prodotto sta per fare qualcosa.
                "prossimo_digest": r[8].isoformat() if r[8] else None,
                "ultimo_digest": r[9].isoformat() if r[9] else None,
                "ora_invio": r[10], "giorno_settimana": r[11],
                "giorno_mese": r[12], "email_consegna": r[13],
                "nome": r[14], "locale": r[15]}

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
            cur.execute(
                "SELECT j.title, j.organization, (j.cities)[1], (j.countries)[1], "
                "       COALESCE(j.org_linkedin_slug, j.domain_derived) "
                "FROM jobs j "
                "LEFT JOIN company_logos cl "
                "  ON cl.chiave = COALESCE(j.org_linkedin_slug, j.domain_derived) "
                "WHERE j.status = 'active' AND j.duplicate_of_job_id IS NULL "
                "  AND j.organization IS NOT NULL "
                "  AND j.date_posted > now() - interval '7 days' "
                "  AND COALESCE(j.org_linkedin_slug, j.domain_derived) IS NOT NULL "
                "  AND (cl.bytes IS NOT NULL OR (cl.chiave IS NULL AND "
                "       (j.org_logo_permalink IS NOT NULL OR j.organization_logo IS NOT NULL))) "
                "ORDER BY random() LIMIT 18")
            righe = cur.fetchall()
        return Response(
            content=json.dumps({
                "offerte": [{
                    "titolo": r[0], "azienda": r[1], "citta": r[2],
                    "paese": r[3],
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
        if "delivery_channel" in campi:
            c = campi["delivery_channel"]
            if c not in ("email", "telegram", "whatsapp"):
                raise HTTPException(422, "delivery_channel: email, telegram o whatsapp")
            if c == "telegram" and not campi.get("telegram_chat_id"):
                raise HTTPException(422, "telegram richiede telegram_chat_id")
            if c == "whatsapp" and not campi.get("whatsapp_e164"):
                raise HTTPException(422, "whatsapp richiede whatsapp_e164")
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
                cur.execute("UPDATE users SET next_digest_at = %s WHERE id = %s",
                            (slot, uid))
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
        pannello mostra. Il file non si scarica da qui."""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT families, seniority, skills, languages, years_experience, "
                "       uploaded_at FROM user_cvs "
                "WHERE user_id = %s AND status = 'active'", (uid,))
            r = cur.fetchone()
        if not r:
            raise HTTPException(404, "nessun CV caricato")
        return {"families": r[0], "seniority": r[1], "skills": r[2],
                "languages": r[3], "years_experience": r[4],
                "caricato_il": r[5]}

    return app


app = create_app()
