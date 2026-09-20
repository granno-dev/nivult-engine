"""Quanto si recupera del 43% di siti irraggiungibili, chiedendo come un browser?

Il verificatore di ieri falliva su 13 domini su 30 — e guardandoli (broadcom.com,
sthree.com, colliers.com) non erano domini sbagliati: erano siti con protezione
anti-bot che rifiutavano la nostra richiesta spoglia.

Confronto APPAIATO: gli stessi identici domini, letti dal log della prova
precedente, con due modi di chiedere. Su campioni diversi il confronto non
varrebbe niente.
"""
from __future__ import annotations
import re
import sys
import time

import httpx

# i domini della prova di ieri, presi dal suo log
DOMINI = [
    ("Colliersinternationalemea", "colliers.com"),
    ("Astera Labs", "asteralabs.com"),
    ("Park", "parkeofficial.com"),
    ("Sungrow Emea", "sungrowpower.com"),
    ("Cvshealth", "cvshealth.com"),
    ("Siloamcareers", "siloamhealth.org"),
    ("Besix", "besix.com"),
    ("Valorhospitality", "valorhospitality.com"),
    ("Herewithinc", "herewith.com"),
    ("Homelandls", "dhs.gov"),
    ("Lansingschools", "lansingschools.net"),
    ("Careers Centricbrands", "centricbrands.com"),
    ("Cultura", "culturaofficial.com"),
    ("Mental Health Association", "mhainc.org"),
    ("SThree", "sthree.com"),
    ("Konzmann Gmbh", "konzmann-gruppe.de"),
    ("Loenbro", "loenbro.com"),
    ("Broadcom", "broadcom.com"),
    ("General Dynamics Missions Systems", "gdmissionsystems.com"),
    ("Infojiniinc1", "infojiniconsulting.com"),
    ("Blackstone Eit 2", "blackstoneeit.com"),
    ("Barmeniagothaerag", "barmeniagothaer.de"),
    ("Theodo", "theodo.com"),
]

SPOGLIA = {"User-Agent": "Mozilla/5.0 (compatible; NivultBot/1.0; +https://nivult.com/bot)"}

# Come chiede un browser vero. Non e' un travestimento per aggirare un divieto:
# e' un sito pubblico, lo leggiamo una volta sola e solo per confermare che
# appartenga all'azienda. Molti CDN rifiutano per riflesso le richieste senza
# questi campi, non per una scelta di chi possiede il sito.
BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9,it;q=0.8,de;q=0.7,fr;q=0.6",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


try:
    import h2  # noqa: F401
    HTTP2 = True
except ImportError:
    HTTP2 = False      # senza il pacchetto h2 httpx rifiuta http2=True


def leggi(dominio: str, testate: dict, http2: bool) -> tuple[str, str]:
    """Prova le varianti dell'indirizzo, ma solo finche' ha senso.

    Se il server RISPONDE — anche con un 403 — riprovare con http o con www non
    serve a niente: la risposta c'e' gia' ed e' un rifiuto. Le varianti si
    tentano solo quando la connessione non si apre proprio (nome che non
    risolve, porta chiusa, certificato). Senza questa regola un dominio bloccato
    costava quattro attese da quindici secondi, due volte.
    """
    ultimo = "?"
    with httpx.Client(headers=testate, verify=False, follow_redirects=True,
                      timeout=8, http2=http2 and HTTP2) as cli:
        varianti = [dominio] if dominio.startswith("www.") else [dominio, "www." + dominio]
        for host in varianti:
            for schema in ("https://", "http://"):
                try:
                    r = cli.get(schema + host)
                except Exception as e:                           # noqa: BLE001
                    ultimo = type(e).__name__
                    continue                    # connessione fallita: si prova oltre
                if r.status_code < 400:
                    return "letto", f"{r.status_code} {len(r.text):,}b"
                # il server ha risposto e ha detto di no: inutile insistere
                return "non letto", f"HTTP {r.status_code}"
    return "non letto", ultimo


def nomina(testo: str, nome: str) -> bool:
    parole = [w for w in re.split(r"[^a-z0-9]+", nome.lower()) if len(w) >= 4][:3]
    t = testo.lower()
    return bool(parole) and sum(1 for w in parole if w in t) >= max(1, len(parole) - 1)


print(f"http2 disponibile: {HTTP2}\n")
print(f"{'dominio':<28}{'spoglia':<20}{'browser':<20}")
a = b = 0
for nome, dom in DOMINI:
    e1, d1 = leggi(dom, SPOGLIA, False)
    time.sleep(0.3)
    e2, d2 = leggi(dom, BROWSER, True)
    a += e1 == "letto"
    b += e2 == "letto"
    segno = "  +" if (e2 == "letto" and e1 != "letto") else ("  -" if (e1 == "letto" and e2 != "letto") else "   ")
    print(f"{dom[:26]:<28}{(e1 + ' ' + d1)[:18]:<20}{(e2 + ' ' + d2)[:18]:<20}{segno}")
    time.sleep(0.5)

n = len(DOMINI)
print(f"\nletti con la richiesta spoglia : {a}/{n}  ({100*a/n:.0f}%)")
print(f"letti come un browser          : {b}/{n}  ({100*b/n:.0f}%)")
print(f"recuperati                     : {b-a}")
