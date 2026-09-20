"""Com'e' fatta davvero la risposta di ognuna delle quattro API?

La sonda precedente pescava con una regex su un testo TRONCATO a 400 KB: se il
sito dell'azienda sta in fondo non lo vede, e se sta in un campo con un nome che
non avevo previsto nemmeno. Qui si legge il JSON intero e si stampa la struttura,
cosi' si guarda dove il dato sta davvero invece di indovinare dove cercarlo.
"""
from __future__ import annotations
import json
import re
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; NivultBot/1.0; +https://nivult.com/bot)"

PROVE = [
    ("Ashby",    "airwallex",        "https://api.ashbyhq.com/posting-api/job-board/airwallex"),
    ("BambooHR", "theweitzcompany",  "https://theweitzcompany.bamboohr.com/careers/list"),
    ("Lever",    "boxlunch",         "https://api.lever.co/v0/postings/boxlunch?mode=json"),
    ("Workable", "cxg",              "https://apply.workable.com/api/v1/widget/accounts/cxg"),
]


def prendi(url: str):
    rq = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, */*"})
    try:
        with urllib.request.urlopen(rq, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")     # INTERO, non troncato
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:                                           # noqa: BLE001
        return 0, str(e)[:80]


def descrivi(v, prof=0, chiave="", fuori=None):
    """Stampa la forma del JSON, tagliando le liste lunghe: serve la forma, non i dati."""
    pad = "  " * prof
    if prof > 3:
        return
    if isinstance(v, dict):
        print(f"{pad}{chiave}{{}}  ({len(v)} campi: {', '.join(list(v)[:10])})")
        for k, x in list(v.items())[:14]:
            if isinstance(x, (dict, list)):
                descrivi(x, prof + 1, k + " ", fuori)
            elif isinstance(x, str) and re.search(r"(http|www\.|\.com|\.co\.|\.io|\.net)", x):
                print(f"{pad}  {k} = {x[:70]}")
    elif isinstance(v, list):
        print(f"{pad}{chiave}[{len(v)}]")
        if v:
            descrivi(v[0], prof + 1, "[0] ", fuori)


for piatt, slug, url in PROVE:
    print(f"\n{'='*76}\n{piatt}  —  {slug}\n{url}")
    st, corpo = prendi(url)
    print(f"  stato {st}, {len(corpo):,} byte")
    if st != 200 or not corpo:
        print(f"  {corpo[:80]}")
        continue
    try:
        d = json.loads(corpo)
    except Exception:                                                # noqa: BLE001
        print("  non e' JSON. Prime righe:")
        print("   ", corpo[:300].replace("\n", " "))
        time.sleep(2)
        continue
    descrivi(d)
    time.sleep(2)
