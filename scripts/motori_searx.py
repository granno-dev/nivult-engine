"""Quali motori di SearXNG rispondono ancora dal nostro IP?

DuckDuckGo va in timeout a ogni richiesta e Google CSE risponde «unusual traffic
from your network»: l'istanza e' viva ma non ha piu' un motore che funzioni, e
restituisce 200 con zero risultati. Il cacciatore la interroga 500-700 volte
l'ora dall'IP di casa, ed e' esattamente il comportamento che fa scattare i
blocchi.

Si prova un motore alla volta, con pause vere, per sapere su chi si puo' contare.
"""
from __future__ import annotations
import json
import time
import urllib.error
import urllib.parse
import urllib.request

SEARX = "http://100.119.200.7:8899/search"
MOTORI = ["duckduckgo", "brave", "mojeek", "startpage", "bing", "qwant",
          "wikipedia", "google", "yahoo", "presearch"]
DOMANDA = "Airwallex"

print(f"ricerca di prova: «{DOMANDA}»\n")
print(f"{'motore':<14}{'stato':>7}{'risultati':>11}  {'tempo':>6}  primo risultato")
for m in MOTORI:
    url = f"{SEARX}?q={urllib.parse.quote(DOMANDA)}&format=json&engines={m}"
    t0 = time.time()
    try:
        rq = urllib.request.Request(url, headers={"User-Agent": "nivult/1.0"})
        with urllib.request.urlopen(rq, timeout=30) as r:
            d = json.loads(r.read(200_000).decode("utf-8", "replace"))
        ris = d.get("results") or []
        primo = (ris[0].get("url") if ris else "") or ""
        print(f"{m:<14}{200:>7}{len(ris):>11}  {time.time()-t0:>5.1f}s  {primo[:48]}")
    except urllib.error.HTTPError as e:
        print(f"{m:<14}{e.code:>7}{'-':>11}  {time.time()-t0:>5.1f}s")
    except Exception as e:                                       # noqa: BLE001
        print(f"{m:<14}{'err':>7}{'-':>11}  {time.time()-t0:>5.1f}s  {str(e)[:40]}")
    time.sleep(3)
