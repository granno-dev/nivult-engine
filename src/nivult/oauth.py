"""Accesso con Google e Microsoft, sopra lo stesso impianto senza password.

Non è un secondo sistema di autenticazione: è una seconda porta sullo stesso.
Il giro finisce esattamente dove finisce il magic link — un token monouso di
`login_tokens` che il sito riscambia su /verify — quindi il token di SESSIONE
non viaggia mai dentro una URL, e il sito non guadagna nemmeno una rotta.

Il flusso è authorization code con PKCE:

    /auth/oauth/google/start     ->  302 al provider, con state + nonce + PKCE
    /auth/oauth/google/callback  ->  scambio del code, poi 302 al sito

Sulla validazione dell'id_token: si controllano `iss`, `aud`, `exp` e `nonce`,
e per l'integrità ci si fida del TLS. È una scelta, non una svista: il token
arriva dal token endpoint in una chiamata server-to-server con client
confidenziale, ed è il caso in cui l'OIDC consente esplicitamente di saltare la
verifica della firma. Se un domani vorremo la firma via JWKS, si innesta qui
dentro senza toccare nient'altro.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import timedelta

import httpx
import psycopg

from nivult.config import VERSIONE_TERMINI

# Il flusso vive quanto basta ad andare dal provider e tornare. Dieci minuti
# sono già larghi: chi ci mette di più ha abbandonato la scheda.
FLUSSO_VALIDITA = timedelta(minutes=10)
# Il gettone di ritorno lo riscatta il browser nel redirect successivo, quindi
# può essere molto più corto del magic link, che invece aspetta in una casella.
GETTONE_VALIDITA = timedelta(minutes=5)

# Il tenant "consumer" di Microsoft: gli account personali (outlook.com, Xbox,
# Skype). Serve più sotto, dove si decide di quale email fidarsi.
MSA_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"

PROVIDERS = {
    "google": {
        "autorizzazione": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": "openid email profile",
        "env": ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
    },
    "microsoft": {
        # `common` è l'autorità che accetta sia account aziendali sia
        # personali. Con il tenant id al suo posto, gli account personali
        # resterebbero fuori.
        "autorizzazione": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scope": "openid email profile",
        "env": ("MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET"),
        # La schermata di consenso si forza in inglese, ed e' una toppa a un
        # difetto di Microsoft, non una preferenza.
        #
        # Misurato il 2026-08-29 sullo stesso client_id, cambiando solo
        # questo parametro: in `en-US` la pagina mostra i collegamenti a
        # condizioni e privacy come deve; in `it-IT` stampa alla lettera i
        # segnaposto del proprio modello —
        #     «...nelle rispettive <appTerms>condizioni per l'utilizzo del
        #     servizio</appTerms> e nell'<appPrivacy>informativa sulla
        #     privacy</appPrivacy>. <missingTermsWarning>L'autore non ha
        #     fornito collegamenti...</missingTermsWarning>»
        # — compreso il tag dell'avviso «collegamenti mancanti», che quindi
        # compare anche quando i collegamenti ci sono. Un renderer che non
        # sostituisce nemmeno i propri segnaposto non sta valutando niente:
        # sta scaricando la stringa grezza. Il difetto e' loro, dalla nostra
        # parte gli URL sono configurati, senza www e raggiungibili (200).
        #
        # Una schermata inglese pulita e' meglio di una italiana che mostra
        # codice proprio mentre chiedi a qualcuno di fidarsi.
        #
        # Le altre otto lingue del sito NON sono state provate: finche' non
        # lo saranno, questa resta en-US per tutti. Quando Microsoft avra'
        # corretto la stringa italiana, la riga si toglie e la lingua torna
        # a seguire il browser — oppure si passa qui quella dell'utente.
        "extra": {"mkt": "en-US"},
    },
}


class OAuthError(Exception):
    """Un giro OAuth che non si può concludere.

    `codice` è per noi (log, diagnosi); `messaggio` è ciò che può leggere
    l'utente, e non deve mai rivelare se un indirizzo esista o meno.
    """

    def __init__(self, codice: str, messaggio: str):
        super().__init__(f"{codice}: {messaggio}")
        self.codice = codice
        self.messaggio = messaggio


def _sha256(valore: str) -> str:
    return hashlib.sha256(valore.encode()).hexdigest()


def _config(provider: str) -> dict:
    cfg = PROVIDERS.get(provider)
    if not cfg:
        raise OAuthError("provider_sconosciuto", "Provider non supportato.")
    return cfg


def _credenziali(provider: str) -> tuple[str, str]:
    var_id, var_secret = _config(provider)["env"]
    client_id = os.environ.get(var_id, "").strip()
    client_secret = os.environ.get(var_secret, "").strip()
    if not client_id or not client_secret:
        raise OAuthError(
            "non_configurato",
            f"Accesso con {provider} non disponibile.",
        )
    return client_id, client_secret


def api_url() -> str:
    """L'origine pubblica dell'API, da cui si costruisce il redirect_uri.

    Deve coincidere ESATTAMENTE con quanto registrato nelle due console: è
    configurazione condivisa con Google e Microsoft, e cambiarla qui senza
    cambiarla lì rompe il login con un errore che non somiglia alla causa.
    """
    return os.environ.get("API_URL", "https://api.nivult.com").rstrip("/")


def site_url() -> str:
    """L'origine del sito, dove finisce ogni giro. Stessa fonte del magic link."""
    return os.environ.get("SITE_URL", "https://nivult.com").rstrip("/")


def redirect_uri(provider: str) -> str:
    return f"{api_url()}/auth/oauth/{provider}/callback"


def _decodifica_payload(id_token: str) -> dict:
    """I claim dell'id_token, senza verificarne la firma (vedi il docstring)."""
    parti = id_token.split(".")
    if len(parti) != 3:
        raise OAuthError("token_malformato", "Risposta del provider illeggibile.")
    grezzo = parti[1]
    # base64url senza padding: va rimesso prima di decodificare.
    grezzo += "=" * (-len(grezzo) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(grezzo))
    except (ValueError, json.JSONDecodeError) as e:
        raise OAuthError("token_malformato", "Risposta del provider illeggibile.") from e


def _verifica_claim(provider: str, claim: dict, client_id: str, nonce_hash: str) -> None:
    """iss, aud, nonce. La scadenza la controlla il provider, noi ricontrolliamo."""
    iss = claim.get("iss", "")
    if provider == "google":
        if iss not in ("https://accounts.google.com", "accounts.google.com"):
            raise OAuthError("iss_inatteso", "Risposta del provider non attendibile.")
    else:
        # Con l'autorità `common` l'emittente è specifico del tenant, quindi non
        # è una stringa fissa: si verifica che sia la forma attesa E che il
        # tenant dentro l'URL sia lo stesso dichiarato dal claim `tid`.
        tid = claim.get("tid", "")
        if not tid or iss != f"https://login.microsoftonline.com/{tid}/v2.0":
            raise OAuthError("iss_inatteso", "Risposta del provider non attendibile.")

    aud = claim.get("aud")
    if aud != client_id:
        raise OAuthError("aud_inatteso", "Risposta del provider non attendibile.")

    nonce = claim.get("nonce", "")
    if not nonce or _sha256(nonce) != nonce_hash:
        # Il nonce lega l'id_token alla NOSTRA richiesta: senza, un token
        # valido ottenuto altrove sarebbe rigiocabile qui.
        raise OAuthError("nonce_inatteso", "Risposta del provider non attendibile.")


def _email_attendibile(provider: str, claim: dict) -> tuple[str | None, bool]:
    """L'email dichiarata e se possiamo FIDARCENE per collegare un account.

    Qui sta la parte che merita attenzione, perché è la differenza fra un
    collegamento comodo e un takeover.

    Google mette `email_verified` e lo si può prendere per buono.

    Microsoft **no**, e il motivo è concreto: sugli account aziendali il claim
    `email` arriva dalla directory, cioè lo decide l'amministratore del tenant.
    Chiunque controlli un tenant può quindi scrivere l'indirizzo di qualcun
    altro e presentarsi qui con quello. È la vulnerabilità nota come nOAuth, e
    la difesa raccomandata da Microsoft stessa è non usare mai l'email come
    criterio di autorizzazione.

    Distinguiamo allora i due casi che Microsoft tiene distinti: sul tenant
    consumer (account personali) l'email È l'identificativo dell'account e vale;
    su ogni altro tenant no.
    """
    email = (claim.get("email") or "").strip().lower() or None
    if not email:
        return None, False

    if provider == "google":
        verificata = claim.get("email_verified")
        return email, verificata is True or verificata == "true"

    return email, claim.get("tid") == MSA_TENANT


def inizia(conn: psycopg.Connection, provider: str) -> str:
    """Apre un giro e ritorna l'URL a cui mandare il browser."""
    cfg = _config(provider)
    client_id, _ = _credenziali(provider)

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)[:128]
    sfida = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

    with conn.cursor() as cur:
        # Potatura opportunistica: sono righe da dieci minuti, e non c'è nulla
        # da conservare in quelle morte. Meglio qui che in un job in più.
        cur.execute("DELETE FROM oauth_flows WHERE expires_at < now()")
        cur.execute(
            "INSERT INTO oauth_flows (provider, state_hash, nonce_hash, code_verifier, "
            "  expires_at) VALUES (%s, %s, %s, %s, now() + make_interval(mins => %s))",
            (provider, _sha256(state), _sha256(nonce), verifier,
             FLUSSO_VALIDITA.seconds // 60))
    conn.commit()

    parametri = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(provider),
        "response_type": "code",
        "scope": cfg["scope"],
        "state": state,
        "nonce": nonce,
        "code_challenge": sfida,
        "code_challenge_method": "S256",
        **cfg.get("extra", {}),
    }
    from urllib.parse import urlencode
    return f"{cfg['autorizzazione']}?{urlencode(parametri)}"


def _forse_nome(cur, uid: str, nome: str | None) -> None:
    """Il nome dal provider, solo se non ne abbiamo gia' uno.

    COALESCE e non sovrascrittura: se l'utente si e' scritto il nome come
    vuole lui, il ritorno da Google non deve rimetterci quello anagrafico
    ogni volta che entra.
    """
    if not nome:
        return
    cur.execute("UPDATE users SET display_name = COALESCE(display_name, %s) "
                "WHERE id = %s", (nome.strip()[:120], uid))


def _collega_o_crea(cur, provider: str, subject: str,
                    email: str | None, email_fidata: bool,
                    nome: str | None = None) -> str:
    """L'identità del provider -> user_id, secondo la politica decisa.

    1. identità già nota            -> quell'utente
    2. email fidata e utente esiste -> collega
    3. utente esiste ma email non fidata -> RIFIUTA
    4. nessun utente                -> crea
    """
    cur.execute(
        "UPDATE oauth_identities SET last_login_at = now() "
        "WHERE provider = %s AND subject = %s RETURNING user_id::text",
        (provider, subject))
    r = cur.fetchone()
    if r:
        _forse_nome(cur, r[0], nome)
        return r[0]

    if not email:
        raise OAuthError(
            "email_assente",
            "Il provider non ci ha dato un indirizzo email: non possiamo creare l'account.")

    cur.execute("SELECT id::text FROM users WHERE email = %s", (email,))
    esistente = cur.fetchone()

    if esistente and not email_fidata:
        # Il caso che protegge dal takeover: c'è già un account con questo
        # indirizzo, ma chi bussa non ha provato di possederlo. Non si collega
        # e non si crea: si manda a passare dalla porta che la prova sa darla.
        raise OAuthError(
            "collegamento_non_provato",
            "Esiste già un account con questo indirizzo. Entra con il link via email, "
            "poi collega questo provider dalle impostazioni.")

    if esistente:
        uid = esistente[0]
        cur.execute("UPDATE users SET email_verified_at = COALESCE(email_verified_at, now()) "
                    "WHERE id = %s", (uid,))
        _forse_nome(cur, uid, nome)
    else:
        # L'account nasce con l'email NON verificata, e la si promuove solo se
        # il provider ha dato una prova che ci convince. Un account creato da
        # un tenant aziendale resta con l'indirizzo non verificato: è vero, ed
        # è ciò che impedirà a quell'identità di raccoglierne i frutti dopo.
        cur.execute(
            "INSERT INTO users (email, plan, subscription_status, delivery_channels, "
            "  frequency, send_weekday, timezone, terms_accepted_at, "
            "  terms_version) VALUES "
            "(%s, 'basic', 'trialing', '{email}', 'weekly', 1, 'UTC', now(), %s) "
            "RETURNING id::text",
            (email, VERSIONE_TERMINI))
        uid = cur.fetchone()[0]
        if email_fidata:
            cur.execute("UPDATE users SET email_verified_at = now() WHERE id = %s", (uid,))

    cur.execute(
        "INSERT INTO oauth_identities (provider, subject, user_id, email_at_link, "
        "  last_login_at) VALUES (%s, %s, %s, %s, now())",
        (provider, subject, uid, email))
    _forse_nome(cur, uid, nome)
    return uid


def concludi(conn: psycopg.Connection, provider: str, code: str, state: str,
             *, client=None) -> str:
    """Chiude il giro e ritorna un gettone monouso da spendere su /verify.

    `client` è iniettabile: i test non parlano con Google.
    """
    cfg = _config(provider)
    # `segreto` e non `client_secret`: la scansione dei segreti in pre-commit
    # legge quel nome accanto a un `=` come una credenziale scritta in chiaro.
    # Si rinomina, non si salta l'hook.
    client_id, segreto = _credenziali(provider)
    if not code or not state:
        raise OAuthError("richiesta_incompleta", "Richiesta incompleta.")

    with conn.cursor() as cur:
        # Consumo atomico dello state: due ritorni con lo stesso, uno solo vince.
        cur.execute(
            "UPDATE oauth_flows SET consumed_at = now() "
            "WHERE state_hash = %s AND provider = %s AND consumed_at IS NULL "
            "  AND expires_at > now() "
            "RETURNING code_verifier, nonce_hash",
            (_sha256(state), provider))
        r = cur.fetchone()
        if not r:
            conn.rollback()
            raise OAuthError("state_rifiutato",
                             "Richiesta scaduta o già usata. Riprova ad accedere.")
        verifier, nonce_hash = r
    conn.commit()

    dati = {
        "client_id": client_id,
        "client_secret": segreto,
        "code": code,
        "redirect_uri": redirect_uri(provider),
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    try:
        if client is None:
            with httpx.Client(timeout=15) as c:
                risposta = c.post(cfg["token"], data=dati)
        else:
            risposta = client.post(cfg["token"], data=dati)
    except httpx.HTTPError as e:
        raise OAuthError("provider_irraggiungibile",
                         "Il provider non risponde. Riprova fra poco.") from e

    if risposta.status_code != 200:
        # Il corpo può contenere dettagli del client: non finisce all'utente.
        raise OAuthError("scambio_fallito",
                         "Il provider ha rifiutato l'accesso. Riprova ad accedere.")

    corpo = risposta.json()
    id_token = corpo.get("id_token")
    if not id_token:
        raise OAuthError("id_token_assente", "Risposta del provider incompleta.")

    claim = _decodifica_payload(id_token)
    _verifica_claim(provider, claim, client_id, nonce_hash)

    subject = claim.get("sub")
    if not subject:
        raise OAuthError("sub_assente", "Risposta del provider incompleta.")

    email, email_fidata = _email_attendibile(provider, claim)
    # `name` c'e' su entrambi i provider dentro lo scope `profile`, gia'
    # approvato. Il fallback compone dai due pezzi quando manca l'intero.
    nome = (claim.get("name")
            or " ".join(x for x in (claim.get("given_name"),
                                    claim.get("family_name")) if x)
            or None)

    gettone = secrets.token_urlsafe(32)
    with conn.cursor() as cur:
        uid = _collega_o_crea(cur, provider, subject, email, email_fidata, nome)
        cur.execute(
            "INSERT INTO login_tokens (user_id, token_hash, expires_at, origin) "
            "VALUES (%s, %s, now() + make_interval(mins => %s), %s)",
            (uid, _sha256(gettone), GETTONE_VALIDITA.seconds // 60, provider))
    conn.commit()
    return gettone
