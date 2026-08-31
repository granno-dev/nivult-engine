"""Consegna via Telegram, e collegamento dell'account.

Valgono le stesse regole di trasparenza dell'email, che sono decisioni di
prodotto e non dettagli di resa:

  - l'etichetta della fonte si mostra SEMPRE («candidatura diretta», «via
    France Travail»);
  - su datore non dichiarato non si stampa MAI un nome di ripiego;
  - lo stipendio si MOSTRA quando c'è, non si filtra mai.

**Un bot non può scrivere per primo.** Finché non è l'utente ad aprire la
conversazione non esiste nessun `chat_id`, e non c'è modo di aggirarlo: è il
motivo per cui il collegamento passa da un gettone monouso e da
`t.me/<bot>?start=<gettone>`, e per cui `telegram_link_tokens` esiste.

Configurazione: TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME,
TELEGRAM_WEBHOOK_SECRET. Senza il token la consegna rifiuta di partire —
meglio un errore esplicito di un digest che sembra spedito.

    python -m nivult.delivery.telegram --registra-webhook
    python -m nivult.delivery.telegram --stato
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import httpx

from nivult.config import load_dotenv
from nivult.delivery.email import FONTI, _data, etichetta_link, stipendio
from nivult.delivery.testi import t

API = "https://api.telegram.org"

# Telegram taglia a 4096 caratteri. Si spezza sul confine fra un'offerta e
# l'altra, mai a metà: un messaggio che finisce a metà di una motivazione
# sembra un guasto, e la seconda parte arriva senza contesto.
LIMITE = 4096


class BotBloccato(RuntimeError):
    """L'utente ha bloccato il bot, o ha cancellato la chat.

    Telegram risponde 403 e continuerà a farlo per sempre: non è un errore
    da ritentare, è un canale che non esiste più. Il worker la distingue
    dalle altre proprio per questo.
    """


def configurato() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN"))


def _token() -> str:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        raise RuntimeError("Telegram non configurato: serve TELEGRAM_BOT_TOKEN")
    return tok


def utente_bot() -> str:
    return os.environ.get("TELEGRAM_BOT_USERNAME", "NivultBot")


def link_collegamento(gettone: str) -> str:
    """Il deep link che apre la chat col bot e gli passa il gettone.

    Su telefono si tocca; su computer si inquadra come QR. Il QR lo disegna
    il sito nel browser: il link ce l'ha già, e generarlo lì evita un giro di
    rete e un'immagine da servire.
    """
    return f"https://t.me/{utente_bot()}?start={gettone}"


def _esc(s: str) -> str:
    """Escape per il parse_mode HTML di Telegram.

    Telegram accetta pochissimi tag (b, i, u, s, a, code, pre) e nessun
    contenitore: niente div, niente p. L'impaginazione la fanno gli a capo.
    """
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def compila(items: list[dict], locale: str = "en",
            nome: str | None = None) -> list[str]:
    """-> i messaggi da spedire, nella lingua dell'utente.

    Una lista e non una stringa: sopra i 4096 caratteri Telegram rifiuta, e
    un digest ricco li supera. Le motivazioni dentro gli item arrivano già
    nella lingua giusta, generate così da GLM — qui non si traduce nulla.
    """
    x = t(locale)
    oggi = _data(datetime.now(timezone.utc), locale)
    # Lo stesso vestito del template WhatsApp v3, nel dialetto di Telegram:
    # saluto col primo nome (o il ripiego della lingua), poi la testata.
    primo = ((nome or "").strip().split() or [x["saluto_fallback"]])[0]
    testa = (f"{_esc(x['saluto'])} {_esc(primo)},\n\n"
             f"<b>Nivult</b> — {_esc(x['digest_del'].format(data=oggi))}")

    blocchi: list[str] = []
    for pos, it in enumerate(items, start=1):
        meta = [m for m in [
            ", ".join(it.get("cities") or []) or None,
            stipendio(it.get("salary"), locale) or None,
            x["pubblicata"].format(data=_data(it["date_posted"], locale)),
            (x["agenzia"] if it.get("employer_kind") == "staffing_agency"
             else None),
        ] if m]
        datore = it.get("organization") or x["datore_non_dichiarato"]
        # Il punteggio come ancora visiva al posto del numero d'ordine, il
        # titolo che E' il link, datore e citta' su una riga: le stesse
        # scelte della finestra del pannello, nel dialetto di Telegram.
        citta = ", ".join(it.get("cities") or [])
        sotto = " · ".join(z for z in [datore, citta] if z)
        meta_fini = [m for m in meta if m and m != citta]
        blocchi.append(
            f"<b>{it['score']}</b> · <a href=\"{_esc(it['url'])}\">"
            f"<b>{_esc(it['title'])}</b></a>\n"
            f"{_esc(sotto)}\n"
            + (f"<i>{_esc(' · '.join(meta_fini))}</i>\n" if meta_fini else "")
            + f"{_esc(it['reason'])}\n"
            f'<a href="{_esc(it["url"])}">'
            f'{_esc(etichetta_link(it, locale))} →</a>')

    piede = f"<i>{_esc(x['piede'])}</i>"

    # Si impacchetta finché ci sta, poi si va a capo di messaggio. Il piede
    # viaggia con l'ultimo pezzo, così chiude il digest e non un frammento.
    messaggi: list[str] = []
    corrente = testa
    # «· · ·» fra un'offerta e l'altra, come nel template WhatsApp: mai
    # prima della prima ne' quando il blocco apre un messaggio nuovo — un
    # separatore in testa separerebbe dal nulla.
    for i, b in enumerate(blocchi):
        coda = f"\n\n{piede}" if i == len(blocchi) - 1 else ""
        sep = "\n\n· · ·\n\n" if i else "\n\n"
        if len(corrente) + len(sep) + len(b) + len(coda) > LIMITE:
            messaggi.append(corrente)
            corrente = b + coda
        else:
            corrente = f"{corrente}{sep}{b}{coda}"
    messaggi.append(corrente)
    return messaggi


def _chiama(metodo: str, payload: dict) -> dict:
    r = httpx.post(f"{API}/bot{_token()}/{metodo}", json=payload, timeout=30.0)
    d = r.json()
    if not d.get("ok"):
        codice = d.get("error_code")
        desc = str(d.get("description", ""))
        # 403 = bloccato o chat cancellata. 400 "chat not found" è lo stesso
        # caso visto da un altro angolo: la chat non c'è più.
        if codice == 403 or "chat not found" in desc.lower():
            raise BotBloccato(desc or "il bot e' stato bloccato")
        raise RuntimeError(f"Telegram {metodo} ({codice}): {desc[:200]}")
    return d["result"]


def invia(chat_id: str, items: list[dict], locale: str = "en",
          nome: str | None = None) -> str:
    """Spedisce un digest. Ritorna l'id del PRIMO messaggio.

    Il primo e non l'ultimo: è quello che il destinatario vede in cima, ed è
    l'ancora con cui si ritrova la consegna se qualcuno chiede conto.
    """
    ids: list[str] = []
    for corpo in compila(items, locale, nome):
        res = _chiama("sendMessage", {
            "chat_id": chat_id,
            "text": corpo,
            "parse_mode": "HTML",
            # Senza questo Telegram appiccica l'anteprima del primo link e il
            # digest diventa una colonna di riquadri con l'offerta numero uno
            # ripetuta in grande.
            "link_preview_options": {"is_disabled": True},
        })
        ids.append(str(res["message_id"]))
    return ids[0]


def invia_testo(chat_id: str, testo: str) -> str:
    """Un messaggio semplice: la conferma di collegamento, gli avvisi."""
    res = _chiama("sendMessage", {
        "chat_id": chat_id, "text": testo, "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True}})
    return str(res["message_id"])


def registra_webhook(url: str) -> dict:
    """Punta il bot al nostro webhook, con il segreto condiviso.

    `secret_token` è ciò che rende la rotta difendibile: Telegram lo rimanda
    nell'header X-Telegram-Bot-Api-Secret-Token a ogni chiamata. Senza,
    quella rotta è pubblica e un POST costruito a mano collegherebbe la chat
    di un estraneo all'account di qualcun altro — cioè gli dirotterebbe i
    digest addosso. È il punto più delicato di tutta la funzione.
    """
    segreto = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if not segreto:
        raise RuntimeError("serve TELEGRAM_WEBHOOK_SECRET")
    return _chiama("setWebhook", {
        "url": url,
        "secret_token": segreto,
        # Ci interessano solo i messaggi: senza filtro Telegram manderebbe
        # anche modifiche, reazioni e stati, che finirebbero nei log senza
        # essere mai letti da nessuno.
        "allowed_updates": ["message"],
        "drop_pending_updates": True,
    })


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registra-webhook", action="store_true")
    ap.add_argument("--url", default=None,
                    help="di norma {API_URL}/telegram/webhook")
    ap.add_argument("--stato", action="store_true")
    args = ap.parse_args(argv)

    if not configurato():
        print("TELEGRAM_BOT_TOKEN non impostato.")
        return 2

    if args.registra_webhook:
        url = args.url or f"{os.environ.get('API_URL', '').rstrip('/')}/telegram/webhook"
        if not url.startswith("https://"):
            print(f"il webhook deve essere https, ricevuto: {url!r}")
            return 2
        registra_webhook(url)
        print(f"webhook registrato su {url}")

    if args.stato or not args.registra_webhook:
        me = _chiama("getMe", {})
        info = httpx.get(f"{API}/bot{_token()}/getWebhookInfo",
                         timeout=20.0).json()["result"]
        print(f"bot: @{me['username']} ({me['id']})")
        print(f"webhook: {info.get('url') or '(nessuno)'}")
        print(f"in attesa: {info.get('pending_update_count', 0)}")
        if info.get("last_error_message"):
            print(f"ultimo errore: {info['last_error_message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
