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
           "Apply", "You chose these searches yourself and can change them any time at nivult.com. Reply STOP to stop this digest."),
    "it": (["Il tuo digest Nivult: un'offerta ha superato la tua soglia oggi, dalle pagine carriere che le aziende pubblicano in proprio.",
            "Il tuo digest Nivult: due offerte hanno superato la tua soglia oggi, dalle pagine carriere che le aziende pubblicano in proprio.",
            "Il tuo digest Nivult: le tre migliori offerte sopra la tua soglia oggi, dalle pagine carriere che le aziende pubblicano in proprio."],
           "Candidati", "Le ricerche le hai scelte tu e puoi cambiarle quando vuoi su nivult.com. Rispondi STOP per non ricevere più il digest."),
    "fr": (["Votre digest Nivult : une offre a dépassé votre seuil aujourd'hui, depuis les pages carrières que les entreprises publient elles-mêmes.",
            "Votre digest Nivult : deux offres ont dépassé votre seuil aujourd'hui, depuis les pages carrières que les entreprises publient elles-mêmes.",
            "Votre digest Nivult : les trois meilleures offres au-dessus de votre seuil aujourd'hui, depuis les pages carrières que les entreprises publient elles-mêmes."],
           "Postuler", "Vous avez choisi ces recherches vous-même et pouvez les modifier à tout moment sur nivult.com. Répondez STOP pour ne plus recevoir ce digest."),
    "de": (["Dein Nivult-Digest: eine Stelle hat heute deine Schwelle überschritten — von den Karriereseiten, die Unternehmen selbst pflegen.",
            "Dein Nivult-Digest: zwei Stellen haben heute deine Schwelle überschritten — von den Karriereseiten, die Unternehmen selbst pflegen.",
            "Dein Nivult-Digest: die drei besten Stellen über deiner Schwelle heute — von den Karriereseiten, die Unternehmen selbst pflegen."],
           "Bewerben", "Diese Suchen hast du selbst gewählt und kannst sie jederzeit auf nivult.com ändern. Antworte STOP, um den Digest abzubestellen."),
    "es": (["Tu resumen Nivult: una oferta superó tu umbral hoy, desde las páginas de empleo que las empresas publican por su cuenta.",
            "Tu resumen Nivult: dos ofertas superaron tu umbral hoy, desde las páginas de empleo que las empresas publican por su cuenta.",
            "Tu resumen Nivult: las tres mejores ofertas por encima de tu umbral hoy, desde las páginas de empleo que las empresas publican por su cuenta."],
           "Candidatura", "Estas búsquedas las elegiste tú y puedes cambiarlas cuando quieras en nivult.com. Responde STOP para dejar de recibir el resumen."),
    "pt": (["O teu resumo Nivult: uma oferta passou o teu limiar hoje, das páginas de carreiras que as empresas publicam por conta própria.",
            "O teu resumo Nivult: duas ofertas passaram o teu limiar hoje, das páginas de carreiras que as empresas publicam por conta própria.",
            "O teu resumo Nivult: as três melhores ofertas acima do teu limiar hoje, das páginas de carreiras que as empresas publicam por conta própria."],
           "Candidatar", "Estas pesquisas foste tu que as escolheste e podes mudá-las quando quiseres em nivult.com. Responde STOP para deixar de receber o resumo."),
    "nl": (["Je Nivult-digest: één vacature kwam vandaag boven je lat uit, van de carrièrepagina's die bedrijven zelf publiceren.",
            "Je Nivult-digest: twee vacatures kwamen vandaag boven je lat uit, van de carrièrepagina's die bedrijven zelf publiceren.",
            "Je Nivult-digest: de drie beste vacatures boven je lat vandaag, van de carrièrepagina's die bedrijven zelf publiceren."],
           "Solliciteer", "Deze zoekopdrachten koos je zelf en je kunt ze altijd wijzigen op nivult.com. Antwoord STOP om deze digest te stoppen."),
    "pl": (["Twój digest Nivult: jedna oferta przekroczyła dziś Twój próg — ze stron karier, które firmy publikują same.",
            "Twój digest Nivult: dwie oferty przekroczyły dziś Twój próg — ze stron karier, które firmy publikują same.",
            "Twój digest Nivult: trzy najlepsze oferty powyżej Twojego progu dziś — ze stron karier, które firmy publikują same."],
           "Aplikuj", "Te wyszukiwania wybrałeś sam i możesz je zmienić w każdej chwili na nivult.com. Odpowiedz STOP, aby przestać otrzymywać digest."),
    "sv": (["Din Nivult-sammanfattning: en tjänst klarade din ribba i dag, från karriärsidorna som företagen själva publicerar.",
            "Din Nivult-sammanfattning: två tjänster klarade din ribba i dag, från karriärsidorna som företagen själva publicerar.",
            "Din Nivult-sammanfattning: de tre bästa tjänsterna över din ribba i dag, från karriärsidorna som företagen själva publicerar."],
           "Ansök", "Sökningarna valde du själv och kan ändra när du vill på nivult.com. Svara STOP för att sluta få sammanfattningen."),
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


def corpo(locale: str, n: int) -> tuple[str, list[str]]:
    intestazioni, applica, piede = TESTI[locale]
    righe = [intestazioni[n - 1]]
    esempi: list[str] = []
    for i in range(n):
        v = 3 * i
        righe += [f"{i + 1}. {{{{{v + 1}}}}}",
                  f"{{{{{v + 2}}}}}",
                  f"{applica}: {{{{{v + 3}}}}}"]
        esempi += [ESEMPI_RIGA[i], ESEMPI_MOTIVO[i], ESEMPI_URL[i]]
    righe.append(piede)
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
            print(f"  {nome:less18} {lingua:6} {stato}"
                  .replace(":less18", ":<18"))
        return 0

    creati = respinti = gia = 0
    for locale, lingua_meta in LINGUA_TEMPLATE.items():
        for n in (1, 2, 3):
            nome = f"nivult_digest_{n}"
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
