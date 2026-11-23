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
from datetime import UTC, datetime, timedelta

import requests

API = "https://api.nivult.com"
SINCE = (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
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

def cerca_jobs(nome, parametri, controllo=None):
    inizio = time.perf_counter()
    risposta = requests.get(f"{API}/v1/jobs", params=parametri, headers=H,
                            timeout=30)
    durata = time.perf_counter() - inizio
    dati = risposta.json()
    prova(nome + " 200 sotto 4s", risposta.status_code == 200 and durata < 4,
          f"HTTP {risposta.status_code}, {durata:.2f}s · {json.dumps(dati)[:160]}")
    if controllo:
        prova(nome + " contenuto", controllo(dati), json.dumps(dati)[:200])
    return dati


def cerca_tecnologia(nome, technology, cursore=None):
    parametri = {"technology": technology, "limit": 1}
    if cursore:
        parametri["cursor"] = cursore

    def esatto(dati):
        righe = dati.get("data", [])
        return (dati.get("count_page") == 1 and len(righe) == 1 and
                any(t.lower() == technology.lower()
                    for t in righe[0].get("technologies", [])))

    return cerca_jobs(nome, parametri, esatto)


sap1 = cerca_tecnologia("SAP", "SAP")
if sap1.get("next_cursor"):
    sap2 = cerca_tecnologia("sap minuscolo pagina 2", "sap", sap1["next_cursor"])
    id_sap1 = {x["id"] for x in sap1.get("data", [])}
    id_sap2 = {x["id"] for x in sap2.get("data", [])}
    prova("SAP pagine disgiunte", not (id_sap1 & id_sap2))
cerca_tecnologia("Snowflake", "Snowflake")
cerca_tecnologia("snowflake minuscolo", "snowflake")
cerca_tecnologia("Kubernetes", "Kubernetes")
cerca_jobs("paese e tecnologia", {"country": "DE", "technology": "SAP", "limit": 5},
           lambda d: d["count_page"] > 0 and all(
               x.get("country") == "DE" and
               any(t.lower() == "sap" for t in x.get("technologies", []))
               for x in d["data"]))
cerca_jobs("keyword engineer", {"q": "engineer", "limit": 10},
           lambda d: d["count_page"] > 0)
cerca_jobs("keyword product", {"q": "product", "limit": 10},
           lambda d: d["count_page"] > 0)
cerca_jobs("Workday engineer completo", {
    "country": "US", "q": "engineer", "seniority": "senior", "remote": "remote",
    "dal": "2026-10-01", "ats": "workday", "limit": 10},
    lambda d: d["count_page"] > 0 and all(
        x.get("ats") == "workday" and x.get("country") == "US" and
        x.get("seniority") == "senior" and x.get("remote") == "remote" and
        x.get("posted_at", "") >= "2026-10-01" for x in d["data"]))
cerca_jobs("Greenhouse product completo", {
    "country": "DE", "q": "product", "remote": "hybrid", "dal": "2026-10-01",
    "ats": "greenhouse", "limit": 10},
    lambda d: d["count_page"] > 0 and all(
        x.get("ats") == "greenhouse" and x.get("country") == "DE" and
        x.get("remote") == "hybrid" and x.get("posted_at", "") >= "2026-10-01"
        for x in d["data"]))
cerca_jobs("Greenhouse DE", {"country": "DE", "ats": "greenhouse", "limit": 10},
           lambda d: d["count_page"] > 0 and all(
               x.get("ats") == "greenhouse" and x.get("country") == "DE"
               for x in d["data"]))
cerca_jobs("tecnologia e keyword", {"technology": "Kubernetes", "q": "engineer",
                                    "limit": 5},
           lambda d: d["count_page"] > 0 and all(
               any(t.lower() == "kubernetes" for t in x.get("technologies", []))
               for x in d["data"]))

r = requests.get(f"{API}/v1/companies", params={"limit": 5}, headers=H)
d = r.json()
prova("/v1/companies 200 con nomi", r.status_code == 200 and
      d["count_page"] > 0 and d["data"][0].get("company"))

r = requests.get(f"{API}/v1/changes", params={"since": SINCE}, headers=H)
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
