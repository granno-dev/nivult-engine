"""Banco locale del router /v1: nessun DB, DuckDB o servizio esterno.

    .venv/bin/python scripts/prova_api_clienti.py

I moduli ``dati`` e ``vivo`` sono finti e registrati prima dell'import; il
protocollo delle chiavi usa uno store in memoria con gli stessi esiti del DB.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import tempfile
import types
from datetime import date
from pathlib import Path

from nivult.api_clienti import dati as dati_reali

_TMP_DIR = tempfile.TemporaryDirectory(prefix="nivult-banco-")
_TMP = Path(_TMP_DIR.name)
_dati = types.ModuleType("nivult.api_clienti.dati")
_vivo = types.ModuleType("nivult.api_clienti.vivo")

OFFERTE = [{"id": f"o{i}", "title": f"Ruolo {i}",
            "country": "DE" if i % 2 else "FR"} for i in range(7)]
OFFERTE[0].update({"contact_email": "recruiter@example.com",
                   "recruiter": {"nome": "Mara"}})
AZIENDE = {"acme": {"slug": "acme", "piattaforma": "linkedin",
                    "name": "Acme", "country": "DE",
                    "technologies": ["python", "postgres"]},
           "globex": {"slug": "globex", "piattaforma": "linkedin",
                      "name": "Globex", "country": "FR", "technologies": []}}
CAMBIAMENTI = [{"tipo": "nuova", "id": "o5"}, {"tipo": "chiusa", "id": "o1"}]
FILE_OFFERTE = _TMP / "offerte-2026-09-23.jsonl.gz"
with gzip.open(FILE_OFFERTE, "wt", encoding="utf-8") as f:
    for o in OFFERTE[:2]:
        f.write(json.dumps(o) + "\n")


def _pagina_finta(righe, cursore, limite):
    da = int(cursore) if cursore else 0
    pagina = righe[da:da + limite]
    return pagina, str(da + limite) if da + limite < len(righe) else None


_dati.ultima_chiamata = {}
_dati.ultimo_limite = {}


def _offerte(filtri, cursore, limite):
    _dati.ultima_chiamata["offerte"] = dict(filtri)
    _dati.ultimo_limite["offerte"] = limite
    righe = OFFERTE
    if "country" in filtri:
        righe = [r for r in righe if r["country"] == filtri["country"]]
    return _pagina_finta(righe, cursore, limite)


def _aziende(filtri, cursore, limite):
    _dati.ultima_chiamata["aziende"] = dict(filtri)
    _dati.ultimo_limite["aziende"] = limite
    righe = list(AZIENDE.values())
    if "country" in filtri:
        righe = [r for r in righe if r["country"] == filtri["country"]]
    return _pagina_finta(righe, cursore, limite)


def _cambiamenti(da, cursore, limite):
    _dati.ultimo_limite["cambiamenti"] = limite
    return _pagina_finta(CAMBIAMENTI, cursore, limite)


_dati.apri = lambda percorso=None: None
_dati.offerte = _offerte
_dati.aziende = _aziende
_dati.azienda = lambda piattaforma, slug: AZIENDE.get(slug)
_dati.cambiamenti = _cambiamenti
_dati.copertura = lambda: {"campi": {"salary": 0.41, "country": 1.0}, "marca": "banco"}
_dati.stato_export = lambda: {
    "data": "2026-09-23", "righe": {"offerte": 2, "aziende": 1},
    "file": {"offerte": str(FILE_OFFERTE),
             "aziende": str(_TMP / "aziende-non-ce.jsonl.gz")}}
_dati.demo_stats = lambda: {"offerte": 7, "aziende": 2}
_dati.demo_tecnologie = lambda q: {"esempi": [AZIENDE["acme"]], "altre": 1}
_dati.stream_export_senza_pii = dati_reali.stream_export_senza_pii
_vivo.offerte = _offerte
_vivo.rivela_offerta = lambda offerta_id: next(
    (o for o in OFFERTE if o["id"] == offerta_id), None)
sys.modules["nivult.api_clienti.dati"] = _dati
sys.modules["nivult.api_clienti.vivo"] = _vivo

import nivult.api_clienti as _pacchetto  # noqa: E402

_pacchetto.dati = _dati
_pacchetto.vivo = _vivo

from nivult.api_clienti import chiavi  # noqa: E402

OGGI = date.today().replace(day=1)


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


STORE = {}
USERS = {}
CONSUMI = []
REGISTRO = []
CONSUMI_FALLISCONO = False


def _chiave(valore, kid, user_id, quota, usati=0, revocata=False, mese=OGGI):
    if user_id:
        USERS.setdefault(user_id, {"status": "active", "extra": 0})
    STORE[_sha(valore)] = {"id": kid, "label": kid, "user_id": user_id,
                           "crediti_mensili": quota, "usati_mese": usati,
                           "mese_uso": mese, "revocata": revocata}


CHIAVE = "nv-banco-principale"
CHIAVE_SECONDA = "nv-banco-seconda"
CHIAVE_POVERA = "nv-banco-povera"
CHIAVE_REVOCATA = "nv-banco-revocata"
CHIAVE_VECCHIA = "nv-banco-vecchia"
BULK499, BULK500 = "nv-bulk-499999", "nv-bulk-500000"
DELTA199, DELTA200 = "nv-delta-199999", "nv-delta-200000"
FREE = "nv-free-1000"
RATE_FREE_KEY, RATE_PAID_KEY = "nv-rate-free", "nv-rate-paid"
_chiave(FREE, "free-1", "u-free", 1000)
_chiave(RATE_FREE_KEY, "rate-free", "u-rate-free", 1000)
_chiave(RATE_PAID_KEY, "rate-paid", "u-rate-paid", 10000)
_chiave(CHIAVE, "main-1", "u-main", 10000)
_chiave(CHIAVE_SECONDA, "main-2", "u-main", 10000)
_chiave(CHIAVE_POVERA, "poor-1", "u-poor", 2)
_chiave(CHIAVE_REVOCATA, "rev-1", "u-rev", 10000, revocata=True)
_chiave(CHIAVE_VECCHIA, "old-1", None, 10000, usati=9999, mese=date(2026, 8, 15))
_chiave(BULK499, "b499", "u-b499", 499999)
_chiave(BULK500, "b500", "u-b500", 500000)
_chiave(DELTA199, "d199", "u-d199", 199999)
_chiave(DELTA200, "d200", "u-d200", 200000)
_chiave("nv-extra", "extra-1", "u-extra", 1)
USERS["u-extra"]["extra"] = 2
_chiave("nv-account-a", "acc-a", "u-account", 10, usati=4)
_chiave("nv-account-b", "acc-b", "u-account", 10)
_chiave("nv-account-old", "acc-old", "u-account", 10, usati=3, revocata=True)


def _record_per_id(chiave_id):
    return next((r for r in STORE.values() if r["id"] == chiave_id), None)


def _account(user_id):
    return [r for r in STORE.values() if r["user_id"] == user_id]


def _riepilogo(user_id):
    righe = _account(user_id)
    quota = max((r["crediti_mensili"] for r in righe), default=0)
    usati = sum(r["usati_mese"] for r in righe if r["mese_uso"] >= OGGI)
    extra = USERS[user_id]["extra"]
    return quota, usati, extra


def _leggi_finto(key_hash):
    rec = STORE.get(key_hash)
    if rec is None or rec["revocata"]:
        return None
    if rec["user_id"] and USERS[rec["user_id"]]["status"] != "active":
        return None
    return {k: v for k, v in rec.items() if k != "revocata"}


def _addebita_finto(chiave_id, via, azione, dettaglio):
    if CONSUMI_FALLISCONO:
        raise RuntimeError("database giu' (simulato)")
    rec = _record_per_id(chiave_id)
    if rec is None or rec["revocata"]:
        raise chiavi.ChiaveInvalida()
    uid = rec["user_id"]
    if uid and USERS[uid]["status"] != "active":
        raise chiavi.ChiaveInvalida()
    if rec["mese_uso"] < OGGI:
        rec["usati_mese"], rec["mese_uso"] = 0, OGGI
    if uid:
        quota, usati, extra = _riepilogo(uid)
        dal_mese = int(usati < quota)
        if not dal_mese and extra <= 0:
            raise chiavi.CreditiEsauriti(quota, OGGI, extra)
        rec["usati_mese"] += dal_mese
        USERS[uid]["extra"] -= 1 - dal_mese
        nuovo_extra = USERS[uid]["extra"]
        usati_dopo = usati + dal_mese
    else:
        quota = rec["crediti_mensili"]
        if rec["usati_mese"] >= quota:
            raise chiavi.CreditiEsauriti(quota, OGGI)
        rec["usati_mese"] += 1
        usati_dopo, nuovo_extra = rec["usati_mese"], 0
    CONSUMI.append(chiave_id)
    REGISTRO.append((uid, via, azione, dettaglio))
    return {"id": rec["id"], "label": rec["label"], "user_id": uid,
            "crediti_mensili": quota, "usati_mese": usati_dopo,
            "usati": usati_dopo, "usati_chiave": rec["usati_mese"],
            "mese_uso": OGGI, "crediti_extra": nuovo_extra,
            "residui_mensili": max(0, quota - usati_dopo),
            "saldo": max(0, quota - usati_dopo) + nuovo_extra}


chiavi._leggi = _leggi_finto
chiavi._addebita_chiave_db = _addebita_finto
chiavi.volume_per_utente = lambda uid: _riepilogo(uid)[0]

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from nivult.api_clienti import v1  # noqa: E402

app = FastAPI()
app.include_router(v1.router)
client = TestClient(app)
H = {"X-Api-Key": CHIAVE}
_rate_originale = v1._applica_rate_limit
v1._applica_rate_limit = lambda request, cliente: None
fallite = []


def prova(nome, condizione, dettaglio=""):
    print(f"  [{'ok' if condizione else 'FALLITA'}] {nome}"
          + (f" — {dettaglio}" if dettaglio and not condizione else ""))
    if not condizione:
        fallite.append(nome)


def _consumi():
    return len(CONSUMI)


print("autenticazione e addebito")
prova("senza chiave -> 401", client.get("/v1/jobs").status_code == 401)
prova("chiave sconosciuta -> 401",
      client.get("/v1/jobs", headers={"X-Api-Key": "ignota"}).status_code == 401)
prova("chiave revocata -> 401",
      client.get("/v1/jobs", headers={"X-Api-Key": CHIAVE_REVOCATA}).status_code == 401)
r = client.get("/v1/jobs", headers=H)
prova("con chiave -> 200", r.status_code == 200, r.text[:200])
prova("forma lista", set(r.json()) == {"data", "next_cursor", "count_page"})
prima = _consumi()
r = client.get("/v1/companies/linkedin/inesistente", headers=H)
prova("404 azienda senza addebito", r.status_code == 404 and _consumi() == prima)
CONSUMI_FALLISCONO = True
r = client.get("/v1/coverage", headers=H)
prova("contatore guasto -> 503 fail closed", r.status_code == 503)
prova("contatore guasto non registra consumi", _consumi() == prima)
CONSUMI_FALLISCONO = False

print("filtri, paginazione e fake vivo")
r = client.get("/v1/jobs", params={"country": "DE"}, headers=H)
prova("jobs passa per il fake vivo e filtra",
      _dati.ultima_chiamata.get("offerte") == {"country": "DE"}
      and all(o["country"] == "DE" for o in r.json()["data"]))
r = client.get("/v1/jobs", params={"ats": "bundesagentur"}, headers=H)
prova("alias Bundesagentur mappa al codice canonico", r.status_code == 200
      and _dati.ultima_chiamata.get("offerte") == {"ats": "bundesanstellung"})
r = client.get("/v1/jobs", params={"ats": "bundesanstellung"}, headers=H)
prova("codice canonico Bundesagentur resta invariato", r.status_code == 200
      and _dati.ultima_chiamata.get("offerte") == {"ats": "bundesanstellung"})
visti, cursore = [], None
while True:
    r = client.get("/v1/jobs", params={"limit": 3, **({"cursor": cursore} if cursore else {})},
                   headers=H)
    corpo = r.json()
    visti += [o["id"] for o in corpo["data"]]
    cursore = corpo["next_cursor"]
    if cursore is None:
        break
prova("next_cursor legge tutte le offerte una volta",
      sorted(visti) == sorted(o["id"] for o in OFFERTE))

print("crediti account, extra e reset")
r = client.get("/v1/usage", headers={"X-Api-Key": CHIAVE_POVERA})
prova("usage ha i campi esistenti e il saldo account",
      r.status_code == 200 and r.json()["crediti_mensili"] == 2
      and r.json()["residui"] == 1 and r.json()["saldo"] == 1)
client.get("/v1/usage", headers={"X-Api-Key": CHIAVE_POVERA})
r = client.get("/v1/usage", headers={"X-Api-Key": CHIAVE_POVERA})
prova("franchigia finita -> 429", r.status_code == 429 and "mese_uso" in r.json()["detail"])
r = client.get("/v1/usage", headers={"X-Api-Key": CHIAVE_VECCHIA})
prova("mese vecchio riparte da zero", r.status_code == 200 and r.json()["usati"] == 1)
for attesi in (2, 1, 0):
    r = client.get("/v1/usage", headers={"X-Api-Key": "nv-extra"})
    prova(f"usage con extra: saldo {attesi}", r.status_code == 200
          and r.json()["saldo"] == attesi and r.json()["residui"] == attesi)
prova("finiti franchigia ed extra -> 429",
      client.get("/v1/usage", headers={"X-Api-Key": "nv-extra"}).status_code == 429)
r = client.get("/v1/usage", headers={"X-Api-Key": "nv-account-a"})
prova("usage somma anche la chiave revocata",
      r.status_code == 200 and r.json()["usati"] == 8 and r.json()["crediti_mensili"] == 10,
      r.text[:200])
STORE[_sha("nv-account-a")]["revocata"] = True
_chiave("nv-account-c", "acc-c", "u-account", 10)
r = client.get("/v1/usage", headers={"X-Api-Key": "nv-account-c"})
prova("revoca e ricreazione non azzerano il consumo",
      r.status_code == 200 and r.json()["usati"] == 9, r.text[:200])
r = client.get("/v1/usage", headers={"X-Api-Key": "nv-account-b"})
prova("il saldo residuo resta condiviso fra chiavi", r.status_code == 200
      and r.json()["usati"] == 10 and r.json()["saldo"] == 0)
prova("quota account esaurita anche sulla nuova chiave",
      client.get("/v1/usage", headers={"X-Api-Key": "nv-account-c"}).status_code == 429)

print("changes: validazione, soglia 200K e RapidAPI")
prima = _consumi()
prova("changes senza since -> 400 gratis", client.get("/v1/changes", headers=H).status_code == 400
      and _consumi() == prima)
prova("since malformata -> 400 gratis",
      client.get("/v1/changes", params={"since": "no"}, headers=H).status_code == 400
      and _consumi() == prima)
r = client.get("/v1/changes", params={"since": "2026-09-01T00:00:00Z"}, headers=H)
prova("delta sotto 200K -> 403 gratis", r.status_code == 403 and _consumi() == prima)
r = client.get("/v1/changes", params={"since": "2026-09-01T00:00:00Z"},
               headers={"X-Api-Key": DELTA199})
prova("199.999 -> 403 gratis", r.status_code == 403 and _consumi() == prima)
r = client.get("/v1/changes", params={"since": "2026-09-01T00:00:00Z"},
               headers={"X-Api-Key": DELTA200})
prova("200.000 -> 200 e un addebito", r.status_code == 200 and _consumi() == prima + 1)
os.environ["RAPIDAPI_PROXY_SECRET"] = "segreto-finto"
RH = {"X-RapidAPI-Proxy-Secret": "segreto-finto", "X-RapidAPI-User": "buyer"}
r = client.get("/v1/changes", params={"since": "2026-09-01T00:00:00Z"}, headers=RH)
prova("RapidAPI conserva delta e nessun addebito", r.status_code == 200 and _consumi() == prima + 1)
r = client.get("/v1/usage", headers=RH)
prova("usage RapidAPI invariato", r.status_code == 200 and r.json()["via"] == "rapidapi")

print("companies, coverage ed export")
r = client.get("/v1/companies", params={"country": "DE"}, headers=H)
prova("lista aziende filtrata", [a["slug"] for a in r.json()["data"]] == ["acme"])
r = client.get("/v1/companies/linkedin/acme", headers=H)
prova("dettaglio azienda", r.status_code == 200 and r.json()["technologies"] == ["python", "postgres"])
r = client.get("/v1/coverage", headers=H)
prova("coverage invariata", r.status_code == 200 and r.json()["marca"] == "banco")
r = client.get("/v1/exports/latest", headers=H)
prova("manifest export", r.status_code == 200 and r.json()["data"] == "2026-09-23")
prima = _consumi()
r = client.get("/v1/exports/latest/offerte", headers={"X-Api-Key": BULK499})
prova("499.999 bulk -> 403 gratis", r.status_code == 403 and _consumi() == prima)
r = client.get("/v1/exports/latest/offerte", headers={"X-Api-Key": BULK500})
scaricate = gzip.decompress(r.content).decode().splitlines() if r.status_code == 200 else []
prova("500.000 bulk -> file e un addebito", len(scaricate) == 2 and _consumi() == prima + 1)
prova("bulk da file precedente senza campi PII",
      all("contact_email" not in riga and "recruiter" not in riga
          for riga in map(json.loads, scaricate)))
prima = _consumi()
r = client.get("/v1/exports/latest/aziende", headers={"X-Api-Key": BULK500})
prova("file mancante -> 404 gratis", r.status_code == 404 and _consumi() == prima)
r = client.get("/v1/exports/latest/altro", headers={"X-Api-Key": BULK500})
prova("nome export ignoto -> 404 gratis", r.status_code == 404 and _consumi() == prima)
r = client.get("/v1/exports/latest/offerte", headers=RH)
prova("bulk RapidAPI resta 403 senza addebito", r.status_code == 403 and _consumi() == prima)

print("cap free, limiti paganti e rate limiting")
HF = {"X-Api-Key": FREE}
r = client.get("/v1/jobs", params={"limit": 500}, headers=HF)
prova("free jobs: limit=500 troncato a 25",
      r.status_code == 200 and _dati.ultimo_limite["offerte"] == 25)
r = client.get("/v1/jobs", headers=HF)
prova("free jobs: anche il default diventa 25",
      r.status_code == 200 and _dati.ultimo_limite["offerte"] == 25)
r = client.get("/v1/companies", params={"limit": 500}, headers=HF)
prova("free companies: limit=500 troncato a 25",
      r.status_code == 200 and _dati.ultimo_limite["aziende"] == 25)
r = client.get("/v1/jobs", params={"limit": 500}, headers=H)
prova("tier pagante jobs: limit=500 invariato",
      r.status_code == 200 and _dati.ultimo_limite["offerte"] == 500)
r = client.get("/v1/changes", params={"since": "2026-09-01T00:00:00Z", "limit": 500},
               headers={"X-Api-Key": DELTA200})
prova("tier pagante changes: limit=500 invariato",
      r.status_code == 200 and _dati.ultimo_limite["cambiamenti"] == 500)
prova("il cap segue il monthly tier, incluso zero",
      v1._limite_per_tier({"monthly_tier": 0}, 500) == 25
      and v1._limite_per_tier({"monthly_tier": 1000}, 500) == 25
      and v1._limite_per_tier({"monthly_tier": 1001}, 500) == 500)

v1._applica_rate_limit = _rate_originale
v1._rate_limiter.clear()
prima = _consumi()
rate_free = [client.get("/v1/jobs", headers={"X-Api-Key": RATE_FREE_KEY})
             for _ in range(6)]
prova("free API: burst 5, sesta richiesta 429",
      [r.status_code for r in rate_free] == [200, 200, 200, 200, 200, 429])
prova("429 free ha Retry-After e non spende il sesto credito",
      int(rate_free[-1].headers.get("Retry-After", "0")) >= 1
      and _consumi() == prima + 5)
v1._rate_limiter.clear()
prima = _consumi()
rate_paid = [client.get("/v1/usage", headers={"X-Api-Key": RATE_PAID_KEY})
             for _ in range(31)]
prova("API pagante: burst 30, trentunesima richiesta 429",
      [r.status_code for r in rate_paid] == [200] * 30 + [429])
prova("429 pagante non spende oltre le 30 richieste ammesse",
      _consumi() == prima + 30)

from nivult.api import booking  # noqa: E402

class _ConnessioneFinta:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

booking.psycopg.connect = lambda dsn: _ConnessioneFinta()
booking._slot_giorno = lambda conn, giorno: []
booking_app = FastAPI()
booking.registra(booking_app)
booking_client = TestClient(booking_app)
booking._rate_limiter.clear()
slot_ip = {"CF-Connecting-IP": "203.0.113.50"}
slot_rate = [booking_client.get("/booking/slots", params={"day": "2026-11-23"},
                                headers=slot_ip) for _ in range(6)]
prova("booking slots: burst 5 per IP, poi 429",
      [r.status_code for r in slot_rate] == [200] * 5 + [429])
booking._rate_limiter.clear()
post_ip = {"CF-Connecting-IP": "203.0.113.51"}
honeypot = {"nome": "Bot", "email": "bot@example.com",
            "starts_at": "2027-01-01T10:00:00Z", "sito": "compilato"}
post_rate = [booking_client.post("/booking", json=honeypot, headers=post_ip)
             for _ in range(6)]
prova("booking POST: honeypot intatto, burst 5 poi 429",
      [r.status_code for r in post_rate] == [200] * 5 + [429]
      and int(post_rate[-1].headers.get("Retry-After", "0")) >= 1)

print("demo: IP e campioni EXPORT_DIR")
def _campione(nome, valore):
    with gzip.open(_TMP / nome, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"id": valore}) + "\n")

_campione("offerte-attive-campione-2026-09-23.jsonl.gz", "campione-vecchio")
_campione("offerte-attive-campione-2026-10-01.jsonl.gz", "campione-nuovo")
_campione("offerte-attive-2026-10-02.jsonl.gz", "corpus-intero")
_campione("aziende-segnali-campione-2026-10-01.jsonl.gz", "azienda-nuova")
os.environ["EXPORT_DIR"] = str(_TMP)
(_TMP / "offerte-attive-campione-ultimo.jsonl.gz").symlink_to(
    _TMP / "offerte-attive-campione-2026-09-23.jsonl.gz")
r = client.get("/v1/demo/export-campione")
prova("symlink campione valido ha priorita'", r.status_code == 200
      and json.loads(gzip.decompress(r.content))["id"] == "campione-vecchio")
(_TMP / "offerte-attive-campione-ultimo.jsonl.gz").unlink()
r = client.get("/v1/demo/export-campione")
prova("fallback al campione datato recente, mai corpus", r.status_code == 200
      and json.loads(gzip.decompress(r.content))["id"] == "campione-nuovo")
r = client.get("/v1/demo/sample-export?type=jobs")
prova("sample-export jobs restituisce il campione offerte", r.status_code == 200
      and json.loads(gzip.decompress(r.content))["id"] == "campione-nuovo")
r = client.get("/v1/demo/sample-export?type=companies")
prova("sample-export companies restituisce il campione aziende", r.status_code == 200
      and json.loads(gzip.decompress(r.content))["id"] == "azienda-nuova")
r = client.get("/v1/demo/sample-export")
prova("sample-export senza type usa jobs", r.status_code == 200
      and json.loads(gzip.decompress(r.content))["id"] == "campione-nuovo")
r = client.get("/v1/demo/sample-export?type=unknown")
prova("sample-export rifiuta type sconosciuto", r.status_code == 404)
r = client.get("/v1/demo/export-campione?tipo=aziende")
prova("legacy export-campione aziende resta attivo", r.status_code == 200
      and json.loads(gzip.decompress(r.content))["id"] == "azienda-nuova")
v1._demo_finestra.clear()
for _ in range(30):
    r = client.get("/v1/demo/stats", headers={"CF-Connecting-IP": "203.0.113.10"})
prova("trenta chiamate demo ammesse", r.status_code == 200)
prova("trentunesima dallo stesso IP -> 429",
      client.get("/v1/demo/stats", headers={"CF-Connecting-IP": "203.0.113.10"}).status_code == 429)
prova("altro IP valido ha finestra propria",
      client.get("/v1/demo/stats", headers={"CF-Connecting-IP": "203.0.113.11"}).status_code == 200)
v1._demo_finestra.clear()
for _ in range(30):
    client.get("/v1/demo/stats", headers={"CF-Connecting-IP": "non-un-ip"})
prova("CF-Connecting-IP invalido ricade sul bucket di fallback",
      client.get("/v1/demo/stats").status_code == 429)

print()
_TMP_DIR.cleanup()
if fallite:
    print(f"{len(fallite)} prove FALLITE: {', '.join(fallite)}")
    sys.exit(1)
print("tutte le prove del banco /v1 sono passate")
sys.exit(0)
