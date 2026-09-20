"""SearXNG funziona? Ha prodotto 1 dominio su 691.

Misurato da solo il 18/09 rendeva il 18%. Nella cascata rende lo 0,1%. O e' rotto,
o la cascata non ci arriva quasi mai (e' l'ultima fonte: se le tre prima trovano,
lui non viene chiamato), o le sue proposte non passano il giudice.
Le tre cose si distinguono guardando, non ragionando.
"""
from __future__ import annotations
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SEARX = os.environ.get("SEARX_URL", "http://100.119.200.7:8899/search")
NOMI = ["Airwallex", "Phasebiolabs", "Groundnews", "Prosphire", "Deeplab"]

print(f"indirizzo usato dal cacciatore: {SEARX}\n")
for nome in NOMI:
    q = urllib.parse.quote(nome)
    url = f"{SEARX}?q={q}&format=json"
    t0 = time.time()
    try:
        rq = urllib.request.Request(url, headers={"User-Agent": "nivult/1.0"})
        with urllib.request.urlopen(rq, timeout=25) as r:
            stato, corpo = r.status, r.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        print(f"  {nome:<16}HTTP {e.code}")
        continue
    except Exception as e:                                       # noqa: BLE001
        print(f"  {nome:<16}errore: {str(e)[:60]}")
        continue
    dt = time.time() - t0
    try:
        d = json.loads(corpo)
    except Exception:                                            # noqa: BLE001
        print(f"  {nome:<16}stato {stato}, {len(corpo)} byte, NON e' JSON: {corpo[:70]!r}")
        continue
    ris = d.get("results") or []
    print(f"  {nome:<16}stato {stato}, {len(ris)} risultati in {dt:.1f}s")
    for x in ris[:3]:
        print(f"      {(x.get('url') or '')[:74]}")
    time.sleep(1)
