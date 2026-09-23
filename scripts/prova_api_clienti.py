"""Banco locale dell'API clienti /v1. Nessuna risorsa esterna.

- il data layer e' un modulo FINTO registrato in sys.modules PRIMA di
  importare l'app: nivult.api_clienti.dati vero non viene mai importato,
  e niente tocca DuckDB o Postgres;
- lo store delle chiavi e' un dict in memoria (si sostituiscono
  chiavi._leggi e chiavi._consuma, il confine col database);
- gli export sono file .jsonl.gz finti scritti in una directory temporanea.

    .venv/bin/python scripts/prova_api_clienti.py
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
import tempfile
import types
from datetime import date
from pathlib import Path

# ── il data layer finto, registrato PRIMA di importare l'app ──────────────────────────────────────

_dati = types.ModuleType("nivult.api_clienti.dati")

_TMP = Path(tempfile.mkdtemp(prefix="nivult-banco-"))

OFFERTE = [{"id": f"o{i}", "title": f"Ruolo {i}",
            "country": "DE" if i % 2 else "FR"} for i in range(7)]
AZIENDE = {"acme": {"slug": "acme", "piattaforma": "linkedin",
                    "name": "Acme", "country": "DE",
                    "technologies": ["python", "postgres"]},
           "globex": {"slug": "globex", "piattaforma": "linkedin",
                      "name": "Globex", "country": "FR",
                      "technologies": []}}
CAMBIAMENTI = [{"tipo": "nuova", "id": "o5"}, {"tipo": "chiusa", "id": "o1"}]

# L'export del giorno: offerte c'e' davvero, aziende punta a un file che
# manca — serve a provare il 404 della rotta di download.
FILE_OFFERTE = _TMP / "offerte-2026-09-23.jsonl.gz"
with gzip.open(FILE_OFFERTE, "wt", encoding="utf-8") as f:
    for o in OFFERTE[:2]:
        f.write(json.dumps(o) + "\n")


def _pagina_finta(righe: list[dict], cursore: str | None,
                  limite: int) -> tuple[list[dict], str | None]:
    """Cursore = offset in chiaro: nel finto non serve opacita'."""
    da = int(cursore) if cursore else 0
    pagina = righe[da:da + limite]
    prossimo = str(da + limite) if da + limite < len(righe) else None
    return pagina, prossimo


_dati.ultima_chiamata = {}


def _offerte(filtri, cursore, limite):
    _dati.ultima_chiamata["offerte"] = dict(filtri)
    righe = OFFERTE
    if "country" in filtri:
        righe = [r for r in righe if r["country"] == filtri["country"]]
    return _pagina_finta(righe, cursore, limite)


def _aziende(filtri, cursore, limite):
    _dati.ultima_chiamata["aziende"] = dict(filtri)
    righe = list(AZIENDE.values())
    if "country" in filtri:
        righe = [r for r in righe if r["country"] == filtri["country"]]
    return _pagina_finta(righe, cursore, limite)


_dati.apri = lambda percorso=None: None
_dati.offerte = _offerte
_dati.aziende = _aziende
_dati.azienda = lambda piattaforma, slug: AZIENDE.get(slug)
_dati.cambiamenti = lambda da, cursore, limite: _pagina_finta(
    CAMBIAMENTI, cursore, limite)
_dati.copertura = lambda: {"campi": {"salary": 0.41, "country": 1.0},
                           "marca": "banco"}
_dati.stato_export = lambda: {
    "data": "2026-09-23", "righe": {"offerte": 2, "aziende": 1},
    "file": {"offerte": str(FILE_OFFERTE),
             "aziende": str(_TMP / "aziende-non-ce.jsonl.gz")}}

sys.modules["nivult.api_clienti.dati"] = _dati

# ── lo store delle chiavi finto ───────────────────────────────────────────────────────────────────

from nivult.api_clienti import chiavi  # noqa: E402


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


CHIAVE = "nv-banco-principale"
CHIAVE_POVERA = "nv-banco-povera"
CHIAVE_REVOCATA = "nv-banco-revocata"
CHIAVE_STAGIONE_VECCHIA = "nv-banco-mese-scorso"
OGGI = date.today()

STORE = {
    _sha(CHIAVE): {"id": "k1", "label": "cliente di prova",
                   "crediti_mensili": 10000, "usati_mese": 0,
                   "mese_uso": OGGI, "revocata": False},
    _sha(CHIAVE_POVERA): {"id": "k2", "label": "cliente al limite",
                          "crediti_mensili": 2, "usati_mese": 0,
                          "mese_uso": OGGI, "revocata": False},
    _sha(CHIAVE_REVOCATA): {"id": "k3", "label": "cliente revocato",
                            "crediti_mensili": 10000, "usati_mese": 0,
                            "mese_uso": OGGI, "revocata": True},
    # Il conteggio e' di agosto: a settembre questa chiave deve ripartire
    # da zero da sola, senza nessun job di reset.
    _sha(CHIAVE_STAGIONE_VECCHIA): {"id": "k4", "label": "cliente del mese scorso",
                                    "crediti_mensili": 10000, "usati_mese": 9999,
                                    "mese_uso": date(2026, 8, 15),
                                    "revocata": False},
}
CONSUMI: list[str] = []
CONSUMI_FALLISCONO = False


def _leggi_finto(key_hash: str):
    rec = STORE.get(key_hash)
    # Come la SQL vera: le revocate non escono, sono indistinguibili
    # dalle mai esistite.
    if rec is None or rec["revocata"]:
        return None
    return dict(rec)


def _consuma_finto(chiave_id: str):
    if CONSUMI_FALLISCONO:
        raise RuntimeError("database giu' (simulato)")
    CONSUMI.append(chiave_id)
    for rec in STORE.values():
        if rec["id"] == chiave_id:
            rec["usati_mese"] += 1


chiavi._leggi = _leggi_finto
chiavi._consuma = _consuma_finto

# ── l'app vera, col router /v1 incluso ────────────────────────────────────────────────────────────

from fastapi.testclient import TestClient  # noqa: E402

from nivult.api.app import app  # noqa: E402

client = TestClient(app)
H = {"X-Api-Key": CHIAVE}

# ── le prove ──────────────────────────────────────────────────────────────────────────────────────

fallite: list[str] = []


def prova(nome: str, condizione: bool, dettaglio: str = "") -> None:
    print(f"  [{'ok' if condizione else 'FALLITA'}] {nome}"
          + (f" — {dettaglio}" if dettaglio and not condizione else ""))
    if not condizione:
        fallite.append(nome)


print("autenticazione")
r = client.get("/v1/jobs")
prova("senza chiave -> 401", r.status_code == 401)
r = client.get("/v1/jobs", headers={"X-Api-Key": "chiave-che-non-esiste"})
prova("chiave sconosciuta -> 401", r.status_code == 401)
r = client.get("/v1/jobs", headers={"X-Api-Key": CHIAVE_REVOCATA})
prova("chiave revocata -> 401", r.status_code == 401)
r = client.get("/v1/jobs", headers=H)
prova("con chiave -> 200", r.status_code == 200, r.text[:200])
corpo = r.json()
prova("forma della lista: data/next_cursor/count_page",
      set(corpo) == {"data", "next_cursor", "count_page"}
      and corpo["count_page"] == len(corpo["data"]))

print("filtri e paginazione")
r = client.get("/v1/jobs", params={"country": "DE"}, headers=H)
prova("i filtri arrivano al data layer",
      _dati.ultima_chiamata.get("offerte") == {"country": "DE"})
prova("il filtro country filtra", all(o["country"] == "DE" for o in r.json()["data"]))
visti: list[str] = []
cursore = None
while True:
    r = client.get("/v1/jobs", params={"limit": 3,
                                       **({"cursor": cursore} if cursore else {})},
                   headers=H)
    corpo = r.json()
    visti += [o["id"] for o in corpo["data"]]
    cursore = corpo["next_cursor"]
    if cursore is None:
        break
prova("seguendo next_cursor si leggono tutte le 7 offerte, una volta sola",
      sorted(visti) == sorted(o["id"] for o in OFFERTE),
      f"viste {len(visti)}")

print("crediti")
r = client.get("/v1/usage", headers={"X-Api-Key": CHIAVE_POVERA})
prova("usage risponde con mensili/usati/residui", r.status_code == 200
      and r.json()["crediti_mensili"] == 2
      and r.json()["residui"] == r.json()["crediti_mensili"] - r.json()["usati"])
r = client.get("/v1/usage", headers={"X-Api-Key": CHIAVE_POVERA})
prova("seconda chiamata ancora dentro i crediti", r.status_code == 200)
r = client.get("/v1/usage", headers={"X-Api-Key": CHIAVE_POVERA})
dettaglio = r.json().get("detail", {})
prova("a crediti finiti -> 429 con crediti_mensili e mese_uso nel corpo",
      r.status_code == 429 and dettaglio.get("crediti_mensili") == 2
      and "mese_uso" in dettaglio, r.text[:200])
CONSUMI_FALLISCONO = True
r = client.get("/v1/coverage", headers=H)
prova("se il conteggio fallisce la risposta parte lo stesso (200)",
      r.status_code == 200)
CONSUMI_FALLISCONO = False
r = client.get("/v1/usage", headers={"X-Api-Key": CHIAVE_STAGIONE_VECCHIA})
prova("il mese vecchio si azzera da solo (reset mensile)",
      r.status_code == 200 and r.json()["usati"] == 1,
      r.text[:200])

print("changes")
r = client.get("/v1/changes", headers=H)
prova("senza since -> 400", r.status_code == 400)
r = client.get("/v1/changes", params={"since": "non-una-data"}, headers=H)
prova("since malformata -> 400", r.status_code == 400)
r = client.get("/v1/changes", params={"since": "2026-09-01T00:00:00Z"}, headers=H)
prova("since valida -> 200", r.status_code == 200, r.text[:200])
prova("changes ha la forma delle liste", set(r.json()) == {"data", "next_cursor", "count_page"})

print("companies")
r = client.get("/v1/companies", params={"country": "DE"}, headers=H)
prova("lista aziende filtrata", [a["slug"] for a in r.json()["data"]] == ["acme"])
r = client.get("/v1/companies/linkedin/acme", headers=H)
prova("singola azienda con technologies",
      r.status_code == 200 and r.json()["technologies"] == ["python", "postgres"])
r = client.get("/v1/companies/linkedin/inesistente", headers=H)
prova("azienda assente -> 404", r.status_code == 404)

print("coverage ed export")
r = client.get("/v1/coverage", headers=H)
prova("coverage passa com'e' dal data layer",
      r.status_code == 200 and r.json().get("marca") == "banco")
r = client.get("/v1/exports/latest", headers=H)
corpo = r.json()
prova("exports/latest: data, righe e path dei file",
      r.status_code == 200 and corpo.get("data") == "2026-09-23"
      and "righe" in corpo and "file" in corpo)
r = client.get("/v1/exports/latest/offerte", headers=H)
scaricate = gzip.decompress(r.content).decode().splitlines() \
    if r.status_code == 200 else []
prova("download del file del giorno, gzip leggibile",
      r.status_code == 200 and len(scaricate) == 2
      and json.loads(scaricate[0])["id"] == "o0")
r = client.get("/v1/exports/latest/aziende", headers=H)
prova("file mancante su disco -> 404", r.status_code == 404)
r = client.get("/v1/exports/latest/altro", headers=H)
prova("nome fuori dalla lista chiusa -> 404", r.status_code == 404)

print()
if fallite:
    print(f"{len(fallite)} prove FALLITE: {', '.join(fallite)}")
    sys.exit(1)
print(f"tutte le prove passate (banco in {_TMP})")
sys.exit(0)
