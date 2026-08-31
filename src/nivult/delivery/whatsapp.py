"""Consegna via WhatsApp, attraverso Zernio (API ufficiale Meta, zero ricarico).

Valgono le stesse regole di trasparenza dell'email e di Telegram: etichetta
della fonte sempre, mai un nome di ripiego sul datore, stipendio mostrato
quando c'è.

**Il collegamento lo inizia l'utente, e non è un vezzo.** Un'azienda su
WhatsApp può scrivere per prima, ma se per collegare bastasse digitare un
numero nel pannello, chiunque potrebbe inserire il numero di qualcun altro e
fargli piovere addosso i propri digest. La prova di possesso la dà l'utente
scrivendoci per primo: wa.me/<numero>?text=NIVULT <gettone> — il messaggio
arriva nella nostra inbox Zernio e il gettone dice chi sta collegando.
Effetto collaterale prezioso: quel primo messaggio apre la finestra di
servizio di 24 ore, dentro cui le risposte sono gratuite e senza template.

**Fuori dalla finestra si consegna SOLO con template approvati da Meta.**
Direct Send è spento sul nostro WABA (verificato: la chiamata risponde
«Direct Send is not enabled»). I template sono `nivult_digest_1/2/3` per
nove lingue — creati da `scripts/whatsapp_templates.py` — e Meta li ha
classificati MARKETING, non utility: ~0,07–0,11 € a messaggio secondo il
paese del destinatario.

Configurazione: ZERNIO_API_KEY, ZERNIO_WHATSAPP_ACCOUNT_ID,
ZERNIO_WHATSAPP_NUMBER (cifre E.164 senza +, per il link wa.me).
"""

from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import quote

import httpx

from nivult.delivery.email import _data, etichetta_link, stipendio
from nivult.delivery.testi import t

API = "https://zernio.com/api/v1"

# Le parole che il template promette di onorare («Reply STOP to stop this
# digest»). Confronto sul messaggio intero normalizzato, non per contenimento:
# «non voglio STOPPARE» non è una disiscrizione.
PAROLE_STOP = {"STOP", "UNSUBSCRIBE", "BASTA"}

# users.locale -> codice lingua dei template Meta. Il portoghese del sito è
# europeo, e per Meta «pt» da solo non esiste.
LINGUA_TEMPLATE = {"en": "en", "it": "it", "fr": "fr", "de": "de", "es": "es",
                   "pt": "pt_PT", "nl": "nl", "pl": "pl", "sv": "sv"}


class OptOut(RuntimeError):
    """Il destinatario ha chiesto di smettere, o Zernio rifiuta l'invio come
    opt-out. Non si ritenta: si torna all'email e lo si dice."""


class TemplateNonPronto(RuntimeError):
    """Il template per questa lingua non è (ancora) approvato.

    Non è un guasto del canale: è una finestra temporanea — Meta rivede in
    ore — e il canale scelto dall'utente non va mollato per questo. Il
    worker consegna via email QUESTO digest e riprova WhatsApp al prossimo.
    """


def configurato() -> bool:
    return bool(os.environ.get("ZERNIO_API_KEY")
                and os.environ.get("ZERNIO_WHATSAPP_ACCOUNT_ID"))


def _chiavi() -> tuple[str, str]:
    k = os.environ.get("ZERNIO_API_KEY")
    a = os.environ.get("ZERNIO_WHATSAPP_ACCOUNT_ID")
    if not (k and a):
        raise RuntimeError("WhatsApp non configurato: servono ZERNIO_API_KEY "
                           "e ZERNIO_WHATSAPP_ACCOUNT_ID")
    return k, a


def numero_bot() -> str:
    return os.environ.get("ZERNIO_WHATSAPP_NUMBER", "390698236573")


def link_collegamento(gettone: str) -> str:
    """Il link wa.me che apre la chat col nostro numero, testo precompilato.

    Su telefono si tocca; su computer apre WhatsApp Web. Il QR lo disegna il
    sito dal link, come per Telegram.
    """
    return f"https://wa.me/{numero_bot()}?text={quote(f'NIVULT {gettone}')}"


def _http(metodo: str, percorso: str, **kw) -> dict:
    k, _ = _chiavi()
    r = httpx.request(metodo, f"{API}{percorso}", timeout=30.0,
                      headers={"Authorization": f"Bearer {k}"}, **kw)
    try:
        d = r.json()
    except Exception:
        d = {}
    if r.status_code == 409:
        # Il rifiuto esplicito di Zernio su un destinatario disiscritto:
        # documentato «never silently dropped», e qui nemmeno.
        raise OptOut(str(d.get("error") or "destinatario disiscritto"))
    if r.status_code >= 400 or d.get("error"):
        msg = str(d.get("error") or f"HTTP {r.status_code}")
        if "template" in msg.lower() and any(
                p in msg.lower() for p in ("approv", "not found", "exist",
                                           "pending", "paused")):
            raise TemplateNonPronto(msg)
        raise RuntimeError(f"Zernio {percorso} ({r.status_code}): {msg[:200]}")
    return d


def _testo_msg(m: dict) -> str:
    """Il testo di un messaggio, qualunque nome Zernio gli dia.

    Tollerante per scelta: la forma esatta dell'oggetto-messaggio non è
    documentata nello spec, e un KeyError qui fermerebbe collegamenti veri.
    """
    for k in ("text", "message", "content", "body"):
        v = m.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _telefono_conversazione(c: dict) -> str | None:
    for k in ("participantId", "participantUsername", "participant",
              "phoneNumber", "recipientId"):
        v = c.get(k)
        if isinstance(v, dict):
            v = v.get("id") or v.get("phoneNumber")
        if isinstance(v, str) and re.fullmatch(r"\+?\d{6,15}", v):
            return v if v.startswith("+") else f"+{v}"
    return None


def cerca_collegamenti() -> list[dict]:
    """I messaggi «NIVULT <gettone>» arrivati nella nostra inbox.

    -> [{gettone_hash, telefono, conversazione}] per ogni gettone trovato.

    Si cerca il prefisso fisso e si estrae il gettone dal testo: il gettone
    in chiaro non lo conserviamo (in tabella c'è solo lo sha256, come per i
    magic link), quindi non possiamo cercarlo direttamente — ma chi consuma
    è comunque solo chi ha il messaggio giusto, e l'hash fa da giudice.
    """
    _, account = _chiavi()
    d = _http("GET", "/inbox/conversations/search",
              params={"query": "NIVULT", "direction": "incoming",
                      "accountId": account, "limit": 10})

    # L'id di conversazione VERO sta nell'elenco, non nella ricerca: la
    # ricerca risponde {conversation: {id: <telefono>}, matches: [...]} —
    # misurato sul primo collegamento reale, dove il parser precedente
    # cercava l'id sul contenitore sbagliato e scartava tutto in silenzio.
    # La mappa telefono -> id si costruisce una volta per giro.
    lista = _http("GET", "/inbox/conversations",
                  params={"accountId": account, "limit": 50})
    conv_per_telefono: dict[str, str] = {}
    for c in lista.get("data") or []:
        tel = _telefono_conversazione(c)
        cid = c.get("id") or c.get("_id")
        if tel and cid:
            conv_per_telefono[tel] = str(cid)

    trovati: list[dict] = []
    for voce in d.get("data") or d.get("conversations") or []:
        # Le due forme viste: {conversation, matches} dalla ricerca vera,
        # o la conversazione nuda se un giorno cambiano di nuovo.
        conv = voce.get("conversation") or voce
        telefono = _telefono_conversazione(conv)
        # I matches portano gia' il testo: la seconda chiamata per i
        # messaggi serve solo se mancano.
        testi = [m.get("text") or "" for m in voce.get("matches") or []]
        if not testi:
            cid = conv.get("id") or conv.get("_id")
            if cid:
                m = _http("GET", f"/inbox/conversations/{cid}/messages",
                          params={"accountId": account, "limit": 20})
                testi = [_testo_msg(x)
                         for x in m.get("messages") or m.get("data") or []]
        for testo in testi:
            match = re.search(r"NIVULT\s+([A-Za-z0-9_-]{20,64})", testo)
            if match and telefono:
                trovati.append({
                    "gettone_hash": hashlib.sha256(
                        match.group(1).encode()).hexdigest(),
                    "telefono": telefono,
                    "conversazione": conv_per_telefono.get(telefono)
                        or str(conv.get("id") or ""),
                })
    return trovati


def invia_testo(conversazione_id: str, testo: str) -> str:
    """Un messaggio libero DENTRO la finestra di 24 ore: la conferma di
    collegamento, l'ultimo saluto dopo uno STOP. Gratis, senza template."""
    _, account = _chiavi()
    d = _http("POST", f"/inbox/conversations/{conversazione_id}/messages",
              json={"accountId": account, "message": testo})
    return str(d.get("id") or "")


def ha_chiesto_stop(conversazione_id: str) -> bool:
    """L'ULTIMO messaggio della conversazione è una richiesta di stop?

    L'ultimo e non «uno qualsiasi»: chi ha scritto STOP mesi fa e poi si è
    ricollegato ha già detto qualcosa di più recente, e uno stop antico non
    deve perseguitarlo. Il template promette che STOP funziona: questa è la
    funzione che mantiene la promessa, chiamata prima di ogni invio.
    """
    _, account = _chiavi()
    try:
        d = _http("GET", f"/inbox/conversations/{conversazione_id}/messages",
                  params={"accountId": account, "limit": 50})
    except Exception:
        return False  # non riuscire a leggere non è una richiesta di stop
    msgs = d.get("messages") or d.get("data") or []
    for m in reversed(msgs):
        testo = _testo_msg(m).strip().upper().rstrip(".!")
        if not testo:
            continue
        # Il primo messaggio non vuoto dal fondo decide. I nostri invii
        # (template) contengono la parola STOP nel piede: per questo si
        # confronta il messaggio INTERO, che nei nostri non è mai solo STOP.
        return testo in PAROLE_STOP
    return False


def _param(s: str, massimo: int = 300) -> str:
    """Un valore di variabile che Meta accetta: niente a capo, niente tab,
    mai più di quattro spazi consecutivi. La regola è misurata, non teorica:
    violarla fa fallire l'invio con l'errore 132000."""
    return re.sub(r"\s+", " ", s or "").strip()[:massimo]


def invia(telefono_e164: str, items: list[dict], locale: str = "en",
          nome: str | None = None) -> tuple[str, str]:
    """Spedisce un digest come template. -> (message_id, conversation_id).

    Al massimo TRE offerte: i template hanno un numero fisso di caselle, e
    tre è il taglio scelto (nivult_digest_1/2/3). Le offerte oltre le tre
    NON vanno passate qui: il chiamante le lascia fuori da digest_items e
    il recupero del digest successivo le riprende — è il meccanismo che
    esiste già per gli invii falliti.
    """
    if not items:
        raise ValueError("un digest WhatsApp senza offerte non esiste")
    if len(items) > 3:
        raise ValueError("al massimo tre offerte per template: il taglio è "
                         "del chiamante, non un dettaglio")
    x = t(locale)
    _, account = _chiavi()

    parametri: list[str] = []
    for it in items:
        dove = ", ".join(filter(None, [
            ", ".join(it.get("cities") or []) or None,
            stipendio(it.get("salary"), locale) or None]))
        datore = it.get("organization") or x["datore_non_dichiarato"]
        riga = f"{it['title']} — {datore}" + (f" · {dove}" if dove else "")
        parametri += [_param(riga),
                      _param(f"{it['score']}/100 — {it['reason']}"),
                      _param(it["url"], 500)]

    # Il saluto della v3: il primo nome della persona, o il ripiego della
    # lingua per chi un nome non l'ha lasciato — «Gentile candidato» e'
    # meno bello di «Gentile Giuseppe», ma «Gentile ,» non esiste.
    primo = ((nome or "").strip().split() or [x["saluto_fallback"]])[0]

    # Prima la v3 (la variante scelta dal proprietario: saluto, testata in
    # grassetto, separatore fra le offerte, piede in corsivo), e finche'
    # Meta non l'ha approvata si ripiega sulla v1 — che NON ha la
    # variabile del saluto, percio' cambia anche la lista dei parametri.
    # Un digest brutto e' meglio di un digest saltato, e il giorno in cui
    # la v3 e' approvata il passaggio avviene da solo, senza deploy.
    ultima_ecc: TemplateNonPronto | None = None
    for nome_tpl, params in ((f"nivult_digest_{len(items)}_v3",
                              [_param(primo, 60)] + parametri),
                             (f"nivult_digest_{len(items)}", parametri)):
        try:
            d = _http("POST", "/inbox/conversations", json={
                "accountId": account,
                "participantId": telefono_e164,
                "templateName": nome_tpl,
                "templateLanguage": LINGUA_TEMPLATE.get(locale, "en"),
                "templateParams": params,
            })
            return (str(d.get("id") or d.get("messageId") or ""),
                    str(d.get("conversationId") or ""))
        except TemplateNonPronto as ecc:
            ultima_ecc = ecc
            continue
    raise ultima_ecc
