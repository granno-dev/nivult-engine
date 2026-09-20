"""Le cinque piattaforme cieche espongono il sito dell'azienda da qualche parte?

Col metodo del logo danno zero perche' disegnano la bacheca in JavaScript: la
pagina che scarichiamo e' vuota. Ma i dati che il JavaScript mostra devono pur
arrivare da qualche parte, e quel «da qualche parte» e' un'API pubblica.

Qui si PROVA, tenant per tenant, e si cerca nella risposta qualunque cosa somigli
a un sito aziendale. Non si scrive niente in archivio: e' una sonda.

  python sonda_api.py
"""
from __future__ import annotations
import json
import re
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; NivultBot/1.0; +https://nivult.com/bot)"
# host che NON sono l'azienda: se trovo uno di questi non e' una risposta utile
NON_AZIENDA = re.compile(
    r"(ashbyhq|ashbyprd|bamboohr|lever\.co|workable|myworkday|workday|greenhouse|"
    r"cloudfront|amazonaws|googleapis|gstatic|google|facebook|twitter|linkedin|"
    r"youtube|instagram|cdn\.|schema\.org|w3\.org|jsdelivr|unpkg)", re.I)
SITO = re.compile(r"https?://([a-z0-9][-a-z0-9.]*\.[a-z]{2,})", re.I)

CAMPIONI = {
    "Ashby":    ["bjakcareer", "airwallex", "airapps"],
    "BambooHR": ["theweitzcompany", "armstrongfluidtechnology", "quiktrakcontractor"],
    "Lever":    ["boxlunch", "globalelitecareers", "aogarciaagency"],
    "Workable": ["kreyco", "afg", "cxg"],
}

# gli indirizzi da provare per ogni piattaforma, in ordine
INDIRIZZI = {
    "Ashby": [
        "https://api.ashbyhq.com/posting-api/job-board/{s}",
        "https://api.ashbyhq.com/posting-api/job-board/{s}?includeCompensation=true",
    ],
    "BambooHR": [
        "https://{s}.bamboohr.com/careers/list",
        "https://{s}.bamboohr.com/jobs/embed2.php",
    ],
    "Lever": [
        "https://api.lever.co/v0/postings/{s}?mode=json",
    ],
    "Workable": [
        "https://apply.workable.com/api/v1/widget/accounts/{s}",
        "https://www.workable.com/api/accounts/{s}",
    ],
}


def prendi(url: str) -> tuple[int, str]:
    rq = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, */*"})
    try:
        with urllib.request.urlopen(rq, timeout=25) as r:
            return r.status, r.read(400_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:                                       # noqa: BLE001
        return 0, str(e)[:60]


def siti_candidati(testo: str) -> list[str]:
    """Qualunque host nella risposta che non sia della piattaforma o di un servizio."""
    fuori, visti = [], set()
    for h in SITO.findall(testo):
        h = h.lower().strip(".")
        if h in visti or NON_AZIENDA.search(h) or h.count(".") > 3:
            continue
        visti.add(h)
        fuori.append(h)
    return fuori[:6]


for piatt, slugs in CAMPIONI.items():
    print(f"\n{'='*74}\n{piatt}")
    for s in slugs:
        for modello in INDIRIZZI[piatt]:
            url = modello.format(s=s)
            st, corpo = prendi(url)
            if st != 200 or not corpo:
                print(f"  {s:<26}{st:<5}{url.split('/')[2]}{'  (vuoto)' if st == 200 else ''}")
                time.sleep(1.5)
                continue
            cand = siti_candidati(corpo)
            # si cercano anche i campi che NOMINANO un sito, piu' affidabili
            campi = re.findall(r'"([a-zA-Z]*(?:[Ww]ebsite|[Uu]rl|[Dd]omain)[a-zA-Z]*)"\s*:\s*"([^"]{4,90})"', corpo)
            campi = [(k, v) for k, v in campi if not NON_AZIENDA.search(v)][:4]
            print(f"  {s:<26}{st:<5}{len(corpo):>7} byte")
            if campi:
                for k, v in campi:
                    print(f"      campo  {k:<22}{v[:56]}")
            if cand:
                print(f"      host   {', '.join(cand)}")
            if not campi and not cand:
                print("      nessun sito aziendale nella risposta")
            time.sleep(1.5)
            break                       # il primo indirizzo che risponde basta
