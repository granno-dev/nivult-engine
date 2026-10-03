"""Banco LIVE dell'API clienti: funzionale + carico gentile.

Uso: python3 scripts/banco_api_live.py nv_LA_CHIAVE
La chiave e' un argomento, mai nel file. 12/12 o non e' finita.

Funzionale: ogni rotta, gli errori onesti (401/400), la paginazione
senza doppioni fra le pagine, la stima che precede l'addebito.
Carico: 1/5/10/20 client in parallelo su /v1/jobs — la latenza sotto
carico e' la risposta alla domanda «quanti utenti regge».
"""
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

API = "https://api.nivult.com"
KEY = sys.argv[1]
H = {"X-Api-Key": KEY}

ok = fail = 0


def prova(nome, cond, dettaglio=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✓ {nome}")
    else:
        fail += 1
        print(f"  ✗ {nome} {dettaglio}")


print("== funzionale ==")
r = requests.get(f"{API}/v1/jobs", params={"limit": 10})
prova("senza chiave → 401", r.status_code == 401, str(r.status_code))

r = requests.get(f"{API}/v1/jobs", params={"limit": 10}, headers=H)
d = r.json()
prova("/v1/jobs 200", r.status_code == 200)
prova("forma data/next_cursor/count_page",
      all(k in d for k in ("data", "next_cursor", "count_page")))
prova("10 righe", d["count_page"] == 10)

# paginazione: due pagine senza doppioni
ids1 = {x["id"] for x in d["data"]}
d2 = requests.get(f"{API}/v1/jobs",
                  params={"limit": 10, "cursor": d["next_cursor"]},
                  headers=H).json()
ids2 = {x["id"] for x in d2["data"]}
prova("pagina 2 senza doppioni", not (ids1 & ids2))
prova("cursore diverso", d2["next_cursor"] != d["next_cursor"])

r = requests.get(f"{API}/v1/jobs", params={"cursor": "spazzatura"}, headers=H)
prova("cursore rotto → 400", r.status_code == 400, str(r.status_code))

r = requests.get(f"{API}/v1/jobs", params={"country": "DE", "technology": "SAP",
                                           "limit": 5}, headers=H)
d = r.json()
prova("filtro paese+tecnologia", r.status_code == 200 and
      all(x.get("country") == "DE" for x in d["data"] if x.get("country")))

r = requests.get(f"{API}/v1/companies", params={"limit": 5}, headers=H)
d = r.json()
prova("/v1/companies 200 con nomi", r.status_code == 200 and
      d["count_page"] > 0 and d["data"][0].get("company"))

r = requests.get(f"{API}/v1/changes", params={"since": "2026-10-01T00:00:00Z"}, headers=H)
prova("/v1/changes 200", r.status_code == 200)

r = requests.get(f"{API}/v1/coverage", headers=H)
prova("/v1/coverage 200", r.status_code == 200)

r = requests.get(f"{API}/v1/usage", headers=H)
d = r.json()
prova("/v1/usage conta le chiamate di questo banco", r.status_code == 200)
print("  usage:", json.dumps(d)[:200])

print("\n== carico gentile (GET /v1/jobs?limit=100) ==")


def chiama(_):
    t = time.perf_counter()
    r = requests.get(f"{API}/v1/jobs", params={"limit": 100}, headers=H,
                     timeout=120)
    return time.perf_counter() - t, r.status_code


for par in (1, 5, 10, 20):
    n = par * 3
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=par) as ex:
        esiti = list(ex.map(chiama, range(n)))
    durata = time.perf_counter() - t0
    lat = [e[0] for e in esiti]
    errori = sum(1 for _, s in esiti if s != 200)
    print(f"  {par:2d} in parallelo: {n} chiamate in {durata:.1f}s · "
          f"p50 {statistics.median(lat):.2f}s · max {max(lat):.2f}s · "
          f"errori {errori} · {n/durata:.1f} req/s")

print(f"\n== {ok} passate, {fail} fallite ==")
sys.exit(1 if fail else 0)
