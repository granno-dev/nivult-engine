"""Consegna via WhatsApp, self-hosted: wuzapi sul N5 (04/10/2026).

Fino al 30/09 il canale passava da Zernio (Cloud API Meta ufficiale): template
approvati a ~0,07–0,11 € a messaggio, finestra di servizio di 24 ore, Direct
Send spento. Zernio e' stato chiuso. Primo tentativo di casa: Evolution API
(Baileys) — ma la sua versione stabile non accoppia piu' con il WhatsApp
attuale («impossibile collegare dispositivo», issue 2696) e la 2.4 richiede
la licenza. Motore definitivo: wuzapi, il wrapper REST di whatsmeow (Go,
websocket diretto, niente browser), in Docker sul N5.

I messaggi in arrivo NON si leggono da wuzapi (whatsmeow non archivia):
arrivano via webhook al ricevitore deploy/wa-webhook.py, che li scrive in
nivult_ats.wa_inbox — e questo modulo la legge in pull, come faceva con
l'inbox Zernio. Postgres e' la coda: un webhook perso non esiste.

Regole identiche agli altri canali: etichetta della fonte sempre, mai un
nome di ripiego sul datore, stipendio mostrato quando c'e'. E il collegamento
lo inizia SEMPRE l'utente scrivendoci per primo (wa.me/<numero>?text=NIVULT
<gettone>): la prova di possesso non cambia motore.

Configurazione: WUZAPI_URL (http://100.119.200.7:8078), WUZAPI_TOKEN (il
token dell'utente «nivult» su wuzapi), WUZAPI_NUMBER (cifre E.164 senza +,
per il link wa.me). L'inbox vuole ATS_DATABASE_URL nel contesto.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from nivult.delivery.email import _data, etichetta_link, stipendio
from nivult.delivery.testi import t

# Le parole che promettiamo di onorare nel piede del digest. Confronto sul
# messaggio intero normalizzato: «non voglio STOPPARE» non e' una rinuncia.
PAROLE_STOP = {"STOP", "UNSUBSCRIBE", "BASTA"}

# WhatsApp taglia i messaggi lunghi male: sotto i 4.000 caratteri si resta
# sempre in un messaggio solo, e in pratica mai oltre due.
LIMITE_MSG = 4000


class OptOut(RuntimeError):
    """Il destinatario ha chiesto di smettere, o il numero non e' su
    WhatsApp (canale morto). Non si ritenta: si stacca il canale e lo si
    dice via email."""


class TemplateNonPronto(RuntimeError):
    """Resta per compatibilita' col worker (lo cattura): con il motore
    self-hosted i template Meta non esistono piu' e questo non si alza mai.
    """


def _url() -> str:
    return os.environ.get("WUZAPI_URL", "http://100.119.200.7:8078") \
        .rstrip("/")


def configurato() -> bool:
    return bool(os.environ.get("WUZAPI_TOKEN"))


def numero_bot() -> str:
    return os.environ.get("WUZAPI_NUMBER", "390000000000")


def link_collegamento(gettone: str) -> str:
    """Il link wa.me che apre la chat col nostro numero, testo precompilato.
    Su telefono si tocca; su computer apre WhatsApp Web. Il QR lo disegna il
    sito dal link, come per Telegram.
    """
    return f"https://wa.me/{numero_bot()}?text={quote(f'NIVULT {gettone}')}"


def _http(metodo: str, percorso: str, **kw) -> dict:
    k = os.environ.get("WUZAPI_TOKEN")
    if not k:
        raise RuntimeError("WhatsApp non configurato: manca WUZAPI_TOKEN")
    r = httpx.request(metodo, f"{_url()}{percorso}", timeout=30.0,
                      headers={"Token": k}, **kw)
    try:
        d = r.json()
    except Exception:
        d = {}
    # wuzapi risponde {code, data, success}: un 200 con success=false e' un
    # errore travestito, si tratta come tale
    msg = str((d.get("data") or {}).get("Details")
              or (d.get("data") or {}).get("error") or d.get("error") or "")
    if r.status_code >= 400 or (isinstance(d.get("success"), bool)
                                and not d["success"]):
        # Il numero non e' su WhatsApp: canale morto, non un guasto nostro.
        if "no such" in msg.lower() or "not on whatsapp" in msg.lower() \
                or "exists" in msg.lower():
            raise OptOut(msg or f"HTTP {r.status_code}")
        raise RuntimeError(f"wuzapi {percorso} ({r.status_code}): "
                           f"{(msg or 'errore')[:200]}")
    return d


def _db():
    """L'inbox vive in nivult_ats (scritta dal ricevitore sul N5): qui la
    leggiamo, in pull, come si faceva con l'inbox di Zernio."""
    import psycopg
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    return psycopg.connect(dsn)


def cerca_collegamenti() -> list[dict]:
    """I messaggi «NIVULT <gettone>» arrivati al nostro numero.

    -> [{gettone_hash, telefono, conversazione}] per ogni gettone trovato.

    Il gettone in chiaro non lo conserviamo (in tabella c'e' solo lo
    sha256, come per i magic link), quindi non possiamo cercarlo
    direttamente — ma chi consuma e' comunque solo chi ha il messaggio
    giusto, e l'hash fa da giudice. «conversazione» e' il telefono stesso:
    con wuzapi la conversazione SI CHIAMA col numero, niente id opachi.
    """
    trovati: list[dict] = []
    with _db() as conn:
        righe = conn.execute(
            "SELECT telefono, testo FROM wa_inbox WHERE NOT da_noi "
            "ORDER BY ricevuto_at DESC LIMIT 200").fetchall()
    for telefono, testo in righe:
        match = re.search(r"NIVULT\s+([A-Za-z0-9_-]{20,64})", testo or "")
        if match:
            trovati.append({
                "gettone_hash": hashlib.sha256(
                    match.group(1).encode()).hexdigest(),
                "telefono": telefono,
                "conversazione": telefono,
            })
    return trovati


def invia_testo(conversazione_id: str, testo: str) -> str:
    """Un messaggio libero: la conferma di collegamento, l'ultimo saluto
    dopo uno STOP. La conversazione e' il telefono (E.164)."""
    numero = conversazione_id.lstrip("+")
    d = _http("POST", "/chat/send/text",
              json={"Phone": numero, "Body": testo})
    return str((d.get("data") or {}).get("Id") or "")


def ha_chiesto_stop(conversazione_id: str) -> bool:
    """L'ULTIMO messaggio in arrivo dalla conversazione e' una richiesta di
    stop? L'ultimo e non «uno qualsiasi»: chi ha scritto STOP mesi fa e poi
    si e' ricollegato ha gia' detto qualcosa di piu' recente. Il piede del
    digest promette che STOP funziona: questa e' la funzione che mantiene
    la promessa, chiamata prima di ogni invio."""
    try:
        with _db() as conn:
            righe = conn.execute(
                "SELECT testo FROM wa_inbox WHERE telefono = %s "
                "AND NOT da_noi ORDER BY ricevuto_at DESC LIMIT 5",
                (conversazione_id,)).fetchall()
    except Exception:
        return False  # non riuscire a leggere non e' una richiesta di stop
    for (testo,) in righe:
        testo = (testo or "").strip().upper().rstrip(".!")
        if not testo:
            continue
        # Il primo messaggio non vuoto dal fondo decide. I nostri invii
        # non sono in questa lista (da_noi false) quindi niente falsi
        # positivi.
        return testo in PAROLE_STOP
    return False


def _wa(s: str) -> str:
    """Nessuna sanificazione particolare: testo libero. Solo la norma del
    buon senso — mai piu' di quanto WhatsApp taglia."""
    return (s or "").strip()


def _compila(items: list[dict], locale: str, nome: str | None) -> list[str]:
    """Il digest nel dialetto di WhatsApp: *grassetto*, _corsivo_, URL in
    chiaro (WhatsApp le linka da solo). Stessa struttura del messaggio
    Telegram, stesse etichette di testi.py — qui non si traduce nulla, le
    motivazioni arrivano gia' nella lingua giusta da GLM."""
    x = t(locale)
    oggi = _data(datetime.now(timezone.utc), locale)
    primo = ((nome or "").strip().split() or [x["saluto_fallback"]])[0]
    testa = (f"{x['saluto']} {primo},\n\n"
             f"*Nivult* — {x['digest_del'].format(data=oggi)}")

    blocchi: list[str] = []
    for it in items:
        meta = [m for m in [
            ", ".join(it.get("cities") or []) or None,
            stipendio(it.get("salary"), locale) or None,
            # date_posted puo' mancare: senza data la riga «pubblicata»
            # non si scrive proprio (un None qui mandava in crisi _data)
            (x["pubblicata"].format(data=_data(it["date_posted"], locale))
             if it.get("date_posted") else None),
            (x["agenzia"] if it.get("employer_kind") == "staffing_agency"
             else None),
        ] if m]
        datore = it.get("organization") or x["datore_non_dichiarato"]
        citta = ", ".join(it.get("cities") or [])
        sotto = " · ".join(z for z in [datore, citta] if z)
        meta_fini = [m for m in meta if m and m != citta]
        blocchi.append(
            f"*{it['score']}* · *{_wa(it['title'])}*\n"
            f"{_wa(sotto)}\n"
            + (f"_{_wa(' · '.join(meta_fini))}_\n" if meta_fini else "")
            + f"{_wa(it['reason'])}\n"
            f"{_wa(it['url'])}")

    piede = f"_{x['piede']}_"

    # Si impacchetta finche' ci sta, poi si va a capo di messaggio: il piede
    # viaggia con l'ultimo pezzo, cosi' chiude il digest e non un frammento.
    messaggi: list[str] = []
    corrente = testa
    for i, b in enumerate(blocchi):
        coda = f"\n\n{piede}" if i == len(blocchi) - 1 else ""
        sep = "\n\n· · ·\n\n" if i else "\n\n"
        if len(corrente) + len(sep) + len(b) + len(coda) > LIMITE_MSG:
            messaggi.append(corrente)
            corrente = b + coda
        else:
            corrente = f"{corrente}{sep}{b}{coda}"
    messaggi.append(corrente)
    return messaggi


def invia(telefono_e164: str, items: list[dict], locale: str = "en",
          nome: str | None = None) -> tuple[str, str]:
    """Spedisce un digest come testo libero. -> (message_id, conversation_id).

    Ritorna l'id del PRIMO messaggio: e' quello che il destinatario vede in
    cima, l'ancora per ritrovare la consegna. La conversazione e' il
    telefono stesso — niente id opachi da inseguire.
    """
    if not items:
        raise ValueError("un digest WhatsApp senza offerte non esiste")
    primo_id = ""
    for corpo in _compila(items, locale, nome):
        mid = invia_testo(telefono_e164, corpo)
        if not primo_id:
            primo_id = mid
    return primo_id, telefono_e164
