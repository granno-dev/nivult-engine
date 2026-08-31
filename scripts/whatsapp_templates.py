#!/usr/bin/env python3
"""Crea (o riallinea) i template WhatsApp del digest: nivult_digest_1/2/3.

    python scripts/whatsapp_templates.py            # crea i mancanti
    python scripts/whatsapp_templates.py --stato    # solo l'elenco con gli esiti

Un template per CONTEGGIO di offerte e per LINGUA — 27 in tutto. Il conteggio
sta nel testo fisso e non in una variabile, per tre ragioni che si tengono:

  1. un template ha caselle fisse: «digest da 3» con 2 offerte non parte,
     quindi la famiglia 1/2/3 serve comunque — e a quel punto il numero
     scritto a mano è gratis;
  2. i plurali: il polacco ne ha tre, e in una variabile numerica il testo
     fisso attorno non può accordarsi. Col numero nel fisso ogni lingua è
     scritta giusta da un umano;
  3. meno variabili: Meta rifiuta i template con troppe variabili per la
     loro lunghezza (misurato ieri: 14 su questo corpo non passano, 8 sì).

Ogni offerta occupa tre variabili: riga (titolo — datore · città), punteggio
con motivazione, URL di candidatura. Le motivazioni arrivano da GLM già
nella lingua del lettore.

La categoria è MARKETING perché l'ha deciso Meta, non noi: i due template di
prova inviati come UTILITY sono stati riclassificati d'ufficio. Dichiararla
subito evita di fingere un costo che non avremo.

⚠ Il piede promette «STOP»: il worker mantiene la promessa leggendo la
conversazione prima di ogni invio (`whatsapp.ha_chiesto_stop`). Cambiare il
piede senza cambiare quella funzione è una bugia stampata in ogni digest.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nivult.config import load_dotenv  # noqa: E402
from nivult.delivery.whatsapp import LINGUA_TEMPLATE  # noqa: E402

API = "https://zernio.com/api/v1"

# (intestazione per 1/2/3 offerte, etichetta «Candidati», piede).
# Scritti a mano, non tradotti a macchina: sono il canale, non un contorno.
TESTI = {
    "en": (["Your Nivult digest: one role cleared your bar today, from the career pages companies publish on themselves.",
            "Your Nivult digest: two roles cleared your bar today, from the career pages companies publish on themselves.",
            "Your Nivult digest: the top three roles that cleared your bar today, from the career pages companies publish on themselves."],
           "Apply", "You can manage your searches and preferences at any time on nivult.com. To stop receiving this digest, reply STOP."),
    "it": (["Il tuo digest Nivult: un'offerta ha superato la tua soglia oggi, dalle pagine carriere che le aziende pubblicano in proprio.",
            "Il tuo digest Nivult: due offerte hanno superato la tua soglia oggi, dalle pagine carriere che le aziende pubblicano in proprio.",
            "Il tuo digest Nivult: le tre migliori offerte sopra la tua soglia oggi, dalle pagine carriere che le aziende pubblicano in proprio."],
           "Candidati", "Puoi gestire le tue ricerche e preferenze in qualsiasi momento su nivult.com. Per non ricevere più questo digest, rispondi STOP."),
    "fr": (["Votre digest Nivult : une offre a dépassé votre seuil aujourd'hui, depuis les pages carrières que les entreprises publient elles-mêmes.",
            "Votre digest Nivult : deux offres ont dépassé votre seuil aujourd'hui, depuis les pages carrières que les entreprises publient elles-mêmes.",
            "Votre digest Nivult : les trois meilleures offres au-dessus de votre seuil aujourd'hui, depuis les pages carrières que les entreprises publient elles-mêmes."],
           "Postuler", "Vous pouvez gérer vos recherches et préférences à tout moment sur nivult.com. Pour ne plus recevoir ce digest, répondez STOP."),
    "de": (["Dein Nivult-Digest: eine Stelle hat heute deine Schwelle überschritten — von den Karriereseiten, die Unternehmen selbst pflegen.",
            "Dein Nivult-Digest: zwei Stellen haben heute deine Schwelle überschritten — von den Karriereseiten, die Unternehmen selbst pflegen.",
            "Dein Nivult-Digest: die drei besten Stellen über deiner Schwelle heute — von den Karriereseiten, die Unternehmen selbst pflegen."],
           "Bewerben", "Deine Suchen und Einstellungen kannst du jederzeit auf nivult.com verwalten. Um diesen Digest abzubestellen, antworte STOP."),
    "es": (["Tu resumen Nivult: una oferta superó tu umbral hoy, desde las páginas de empleo que las empresas publican por su cuenta.",
            "Tu resumen Nivult: dos ofertas superaron tu umbral hoy, desde las páginas de empleo que las empresas publican por su cuenta.",
            "Tu resumen Nivult: las tres mejores ofertas por encima de tu umbral hoy, desde las páginas de empleo que las empresas publican por su cuenta."],
           "Candidatura", "Puedes gestionar tus búsquedas y preferencias en cualquier momento en nivult.com. Para dejar de recibir este resumen, responde STOP."),
    "pt": (["O teu resumo Nivult: uma oferta passou o teu limiar hoje, das páginas de carreiras que as empresas publicam por conta própria.",
            "O teu resumo Nivult: duas ofertas passaram o teu limiar hoje, das páginas de carreiras que as empresas publicam por conta própria.",
            "O teu resumo Nivult: as três melhores ofertas acima do teu limiar hoje, das páginas de carreiras que as empresas publicam por conta própria."],
           "Candidatar", "Podes gerir as tuas pesquisas e preferências a qualquer momento em nivult.com. Para deixares de receber este resumo, responde STOP."),
    "nl": (["Je Nivult-digest: één vacature kwam vandaag boven je lat uit, van de carrièrepagina's die bedrijven zelf publiceren.",
            "Je Nivult-digest: twee vacatures kwamen vandaag boven je lat uit, van de carrièrepagina's die bedrijven zelf publiceren.",
            "Je Nivult-digest: de drie beste vacatures boven je lat vandaag, van de carrièrepagina's die bedrijven zelf publiceren."],
           "Solliciteer", "Je zoekopdrachten en voorkeuren beheer je op elk moment op nivult.com. Wil je deze digest niet meer ontvangen, antwoord dan STOP."),
    "pl": (["Twój digest Nivult: jedna oferta przekroczyła dziś Twój próg — ze stron karier, które firmy publikują same.",
            "Twój digest Nivult: dwie oferty przekroczyły dziś Twój próg — ze stron karier, które firmy publikują same.",
            "Twój digest Nivult: trzy najlepsze oferty powyżej Twojego progu dziś — ze stron karier, które firmy publikują same."],
           "Aplikuj", "Swoimi wyszukiwaniami i preferencjami możesz zarządzać w każdej chwili na nivult.com. Aby nie otrzymywać więcej tego digestu, odpowiedz STOP."),
    "sv": (["Din Nivult-sammanfattning: en tjänst klarade din ribba i dag, från karriärsidorna som företagen själva publicerar.",
            "Din Nivult-sammanfattning: två tjänster klarade din ribba i dag, från karriärsidorna som företagen själva publicerar.",
            "Din Nivult-sammanfattning: de tre bästa tjänsterna över din ribba i dag, från karriärsidorna som företagen själva publicerar."],
           "Ansök", "Du kan hantera dina sökningar och inställningar när som helst på nivult.com. Svara STOP för att sluta ta emot denna sammanfattning."),
}

ESEMPI_RIGA = ["Ward Manager, Cardiology — Amsterdam UMC · Utrecht",
               "Unit Lead, Emergency — Erasmus MC · Rotterdam",
               "Head of Nursing — Charite · Berlin"]
ESEMPI_MOTIVO = ["94/100 — You ran a ward of thirty and the posting asks exactly that",
                 "89/100 — Same specialty, one step up from your current role",
                 "87/100 — Your languages match and the unit size fits your CV"]
ESEMPI_URL = ["https://jobs.example.com/a1b2c3",
              "https://jobs.example.com/d4e5f6",
              "https://jobs.example.com/g7h8i9"]


# La v2 e' la risposta a un messaggio vero giudicato dal proprietario «un
# muro di testo»: tutto attaccato, niente gerarchia, i link incollati al
# motivo. WhatsApp non ha HTML ma ha *grassetto*, _corsivo_ e le righe
# vuote — e bastano: titolo in grassetto (e' la riga che si scandisce),
# una riga vuota fra le offerte, la freccia sulla riga d'azione, il piede
# in corsivo perche' e' contorno e deve sembrare contorno. La formattazione
# sta nel CORPO FISSO, mai nei parametri: Meta la vieta la' dentro.
VERSIONE = "v3"


def corpo(locale: str, n: int) -> tuple[str, list[str]]:
    """La variante «D» scelta dal proprietario sul confronto visivo:
    saluto personale, testata col nome in grassetto, niente numeri ne'
    etichetta sul link (il blu del link basta), separatore centrale fra
    le offerte, piede in corsivo.

    Il saluto costa UNA variabile in piu': porta il digest_3 a dieci.
    Meta rifiuta i template dove le variabili soverchiano il testo fisso
    (14 non passavano su un corpo piu' magro), ma il corpo v3 e' piu'
    lungo del vecchio: se il 3 venisse respinto, la spia e' il conteggio
    «rifiutati» di questo script, non un guasto silenzioso.
    """
    from nivult.delivery.testi import t as _t
    intestazioni, _, piede = TESTI[locale]
    saluto = _t(locale)["saluto"]
    testa = intestazioni[n - 1]
    prima, due_punti, resto = testa.partition(":")
    testata = (f"*{prima.strip()}* — {resto.strip()}" if due_punti else testa)
    righe = [f"{saluto} {{{{1}}}},", "", testata, ""]
    esempi: list[str] = ["Giuseppe"]
    for i in range(n):
        v = 3 * i + 1
        if i:
            righe += ["· · ·", ""]
        righe += [f"*{{{{{v + 1}}}}}*",
                  f"{{{{{v + 2}}}}}",
                  f"{{{{{v + 3}}}}}",
                  ""]
        esempi += [ESEMPI_RIGA[i], ESEMPI_MOTIVO[i], ESEMPI_URL[i]]
    righe.append(f"_{piede}_")
    return "\n".join(righe), esempi


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stato", action="store_true")
    args = ap.parse_args(argv)

    chiave = os.environ.get("ZERNIO_API_KEY")
    account = os.environ.get("ZERNIO_WHATSAPP_ACCOUNT_ID")
    if not (chiave and account):
        print("Servono ZERNIO_API_KEY e ZERNIO_WHATSAPP_ACCOUNT_ID.")
        return 2
    h = {"Authorization": f"Bearer {chiave}"}

    r = httpx.get(f"{API}/whatsapp/templates", headers=h,
                  params={"accountId": account}, timeout=30.0).json()
    esistenti = {(tp["name"], tp["language"]): tp["status"]
                 for tp in r.get("templates", [])}

    if args.stato:
        for (nome, lingua), stato in sorted(esistenti.items()):
            # Il vecchio {nome:less18} esplodeva PRIMA che la .replace
            # potesse salvarlo: l'f-string valuta subito lo specifier.
            print(f"  {nome:<22} {lingua:<6} {stato}")
        return 0

    creati = respinti = gia = 0
    for locale, lingua_meta in LINGUA_TEMPLATE.items():
        for n in (1, 2, 3):
            nome = f"nivult_digest_{n}_{VERSIONE}"
            if (nome, lingua_meta) in esistenti:
                gia += 1
                continue
            testo, esempi = corpo(locale, n)
            r = httpx.post(f"{API}/whatsapp/templates", headers=h, json={
                "accountId": account, "name": nome,
                "category": "MARKETING", "language": lingua_meta,
                "components": [{"type": "body", "text": testo,
                                "example": {"body_text": [esempi]}}],
            }, timeout=60.0).json()
            if r.get("success"):
                creati += 1
                print(f"  inviato  {nome} {lingua_meta}")
            else:
                respinti += 1
                print(f"  RIFIUTATO {nome} {lingua_meta}: "
                      f"{str(r.get('error'))[:120]}")
    print(f"\n{creati} inviati in revisione · {gia} già presenti · {respinti} rifiutati")
    return 1 if respinti else 0


if __name__ == "__main__":
    sys.exit(main())
