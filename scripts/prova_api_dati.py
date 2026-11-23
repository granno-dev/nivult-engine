"""Banco del data layer clienti: export SINTETICI, zero servizi esterni.

`API_CLIENTI_DB` viene sempre forzato nella directory temporanea e ogni
builder usa `attesa_minuti=0`: il banco non puo' sovrascrivere il DuckDB
reale ne' restare in attesa. Gli artefatti sono rimossi all'uscita.

  - 30 offerte: 12 IT / 10 DE / 8 FR; due SENZA posted_at, provate sia
    nell'API sia nel keyset del portale; due con la STESSA posted_at a
    cavallo della pagina da 20;
  - un file offerte di IERI con 5 righe: il builder sceglie quello di oggi;
  - 8 aziende, una senza employees e un omonimo in un altro paese che non
    deve essere agganciato dal fallback;
  - 7 eventi validi, inclusi due `updated` con formati temporali misti;
    un file troppo vecchio, gzip/JSON corrotti e un timestamp invalido;
  - errore di resync oltre retention, anche quando il flusso e' vuoto.

Uso:  .venv/bin/python scripts/prova_api_dati.py   (esce 0 solo se passa tutto)
"""
from __future__ import annotations

import atexit
import datetime as dt
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

from nivult.api_clienti import aggiorna, dati, vivo
from nivult.ats import dettagli, esporta, estrai_extra

OGGI = dt.date.today()
IERI = OGGI - dt.timedelta(days=1)
UTC = dt.timezone.utc
T = dt.datetime(2026, 9, 22, 12, 0, tzinfo=UTC)  # il pari e' qui

ko = 0


def verifica(nome: str, cond: bool, dettaglio: str = "") -> None:
    global ko
    ko += 0 if cond else 1
    print(("  ok  " if cond else "  KO  ") + nome
          + (f"   [{dettaglio}]" if dettaglio else ""))


def _scrivi(percorso: str, righe: list[dict]) -> None:
    os.makedirs(os.path.dirname(percorso), exist_ok=True)
    with gzip.open(percorso, "wt", encoding="utf-8") as f:
        for r in righe:
            # come _riga di esporta: i None non si scrivono proprio
            f.write(json.dumps({k: v for k, v in r.items() if v is not None},
                               ensure_ascii=False) + "\n")


def _offerte() -> list[dict]:
    righe = []
    for n in range(1, 31):
        if n <= 19:            # le 19 piu' nuove: T+19h ... T+1h
            posted = (T + dt.timedelta(hours=20 - n)).isoformat()
        elif n <= 21:          # off-20 e off-21: il pari, a T
            posted = T.isoformat()
        elif n <= 28:          # le 7 piu' vecchie: T-1h ... T-7h
            posted = (T - dt.timedelta(hours=n - 21)).isoformat()
        else:                  # off-29, off-30: senza data, in fondo
            posted = None
        paese = "IT" if n <= 12 else "DE" if n <= 22 else "FR"
        righe.append({
            "id": f"off-{n:02d}",
            "title": f"Python developer {n}" if n <= 4 else f"Annuncio numero {n}",
            "ats": "greenhouse" if n % 2 else "lever",
            "company_slug": (f"az-null-{n}" if n >= 29 else
                             f"az-{(n - 1) % 8 + 1}"),
            "company": (f"Senza data {n}" if n >= 29 else
                        f"Azienda {(n - 1) % 8 + 1}"),
            "url": f"https://example.org/j/{n}",
            "country": paese,
            "city": "Milano" if paese == "IT" else "Berlino",
            "language": {"IT": "it", "DE": "de", "FR": "fr"}[paese],
            "seniority": "senior" if n <= 10 else "mid" if n <= 20 else "junior",
            "remote": "remote" if n <= 5 else ("onsite" if n <= 10 else None),
            "category": "tech" if n <= 15 else "operations",
            "technologies": (["Python", "Docker"] if n <= 6 else
                             ["Python"] if n <= 10 else
                             ["SAP"] if n <= 15 else
                             ["React"] if n <= 18 else []),
            "posted_at": posted,
            "salary_min": 30000 + n,
            "description": ("Requisito Zephyrquix nel testo completo" if n == 30
                            else f"Testo lungo dell'annuncio {n}"),
            "contact_email": "recruiter@example.com" if n == 1 else None,
            "recruiter": ({"nome": "Mara", "email": "mara@example.com"}
                          if n == 1 else None),
        })
    return righe


def _tec(nome: str, attivi: int) -> dict:
    return {"technology": nome, "active_jobs": attivi, "jobs": attivi + 2,
            "first_verified_at": "2026-08-01", "last_verified_at": "2026-09-20"}


AZIENDE = [
    {"ats": "greenhouse", "company_slug": "alfa-sistemi", "company": "Alfa Sistemi",
     "country": "IT", "industry": "software", "employees": 120,
     "technologies": [_tec("Python", 3), _tec("Docker", 1)], "jobs_posted_30d": 4},
    {"ats": "lever", "company_slug": "beta-farma", "company": "Beta Farma",
     "country": "IT", "industry": "pharma", "employees": 2500,
     "technologies": [_tec("SAP", 2)]},
    {"ats": "greenhouse", "company_slug": "gamma-log", "company": "Gamma Logistica",
     "country": "DE", "industry": "logistics", "employees": 40, "technologies": []},
    {"ats": "lever", "company_slug": "delta-retail", "company": "Delta Retail",
     "country": "DE", "industry": "retail", "employees": 6000,
     "technologies": [_tec("Python", 1)]},
    {"ats": "greenhouse", "company_slug": "epsilon-energy", "company": "Epsilon Energy",
     "country": "FR", "industry": "energy", "employees": 900, "technologies": []},
    {"ats": "lever", "company_slug": "zeta-food", "company": "Zeta Food",
     "country": "FR", "industry": "food", "employees": 8, "technologies": []},
    {"ats": "workable", "company_slug": "eta-media", "company": "Eta Media",
     "country": "IT", "industry": "media", "employees": 300,
     "technologies": [_tec("React", 1)]},
    {"ats": "greenhouse", "company_slug": "theta-cons", "company": "Azienda 1",
     "country": "DE", "industry": "consulting", "employees": None,
     "technologies": []},
]


def _ore(h: int, m: int) -> str:
    return dt.datetime(OGGI.year, OGGI.month, OGGI.day, h, m,
                       tzinfo=UTC).isoformat()


def _flusso() -> list[dict]:
    return [
        {"event": "new", "id": "fl-n1", "title": "Nuova uno", "ats": "lever",
         "company_slug": "alfa-sistemi", "company": "Alfa Sistemi",
         "url": "https://example.org/f/1", "country": "IT",
         "contact_email": "recruiter@example.com", "recruiter": {"nome": "Mara"},
         "first_seen": _ore(7, 0)},
        {"event": "closed", "id": "fl-c1", "title": "Vecchia uno",
         "ats": "lever", "company_slug": "beta-farma",
         "url": "https://example.org/f/9", "country": "IT", "remote": "hybrid",
         "description": "Requisito Xylophonic presente solo nel testo",
         "skills": ["Python"],
         "posted_at": "2026-08-01T10:00:00+00:00", "closed_at": _ore(6, 30)},
        {"event": "new", "id": "fl-n2", "title": "Nuova due", "ats": "lever",
         "company_slug": "alfa-sistemi", "company": "Alfa Sistemi",
         "url": "https://example.org/f/2", "country": "IT",
         "first_seen": _ore(7, 5)},
        {"event": "updated", "id": "fl-u1", "title": "Aggiornata uno",
         "ats": "lever", "company_slug": "beta-farma", "country": "IT",
         "category": "tech", "first_seen": "2026-09-01 10:00:00+00:00",
         "updated_at": _ore(7, 30)},
        {"event": "updated", "id": "fl-u2", "title": "Aggiornata due",
         "ats": "lever", "company_slug": "delta-retail", "country": "DE",
         "category": "operations", "first_seen": "2026-09-02T10:00:00Z",
         "updated_at": f"{OGGI} 10:30:00+02:00"},
        {"event": "new", "id": "fl-n3", "title": "Nuova tre", "ats": "workable",
         "company_slug": "eta-media", "company": "Eta Media",
         "url": "https://example.org/f/3", "country": "IT",
         "first_seen": _ore(9, 0)},
        {"event": "closed", "id": "fl-c2", "title": "Vecchia due",
         "ats": "lever", "company_slug": "delta-retail",
         "url": "https://example.org/f/8", "country": "DE",
         "posted_at": "2026-08-03T10:00:00+00:00", "closed_at": _ore(10, 15)},
        {"event": "new", "id": "fl-senza-tempo", "title": "Data corrotta",
         "ats": "lever", "first_seen": "non-una-data"},
    ]


MANIFEST = {"date": OGGI.isoformat(), "rows": 30,
            "coverage": {"salary_observed": 33.3, "description": 96.7,
                         "technologies": 50.0, "contact_email": 7.0,
                         "recruiter": 4.0}}


def _verifica_funzioni_output() -> None:
    r = [None] * 50
    for i, value in {0: "off-1", 1: "Title", 2: "greenhouse", 3: "acme",
                     4: "Acme", 5: "https://example.org/off-1", 6: "IT",
                     9: "it", 10: "mid", 12: [], 13: 30000, 14: 40000,
                     15: "EUR", 19: "Description", 20: "tech",
                     21: "full_time", 22: []}.items():
        r[i] = value
    out = esporta._riga_attiva(r, {})
    verifica("serializer export senza contact_email e recruiter",
             "contact_email" not in out and "recruiter" not in out
             and out["languages_required"] == [])

    campi = ("id", "title", "platform_id", "slug", "company", "url", "country",
             "city", "location", "lang", "seniority", "remote", "skills",
             "salary_min", "salary_max", "salary_currency", "posted_at", "first_seen",
             "last_seen", "description", "category", "employment_type",
             "languages_required", "external_id", "department", "orario", "durata",
             "salary_period", "salary_da", "salary_text", "posted_at_estimated",
             "tecnologie", "sintesi", "sintesi_da", "management_level",
             "is_decision_maker", "shift_schedule", "work_hours", "is_urgently_hiring",
             "benefits", "valid_through", "state", "postal_code", "latitude",
             "longitude", "geo_da", "applicants_count", "is_easy_apply", "job_sources",
             "content_updated_at")
    j = {k: None for k in campi}
    j.update({"id": "off-1", "title": "Title", "platform_id": "greenhouse",
              "slug": "acme", "company": "Acme", "url": "https://example.org/off-1",
              "country": "IT", "category": "tech", "seniority": "mid",
              "salary_min": 30000, "skills": [], "languages_required": [],
              "tecnologie": [], "benefits": [], "job_sources": [],
              "contact_email": "recruiter@example.com",
              "recruiter": {"nome": "Mara"}})
    out = vivo._riga_export(j)
    verifica("risposta live senza contact_email e recruiter",
             "contact_email" not in out and "recruiter" not in out)

    out = dettagli.dettagli(("off-1", "Title", {"recruiter": {"name": "Mara"}},
                             None, None, "mid", "Description"))
    verifica("arricchimento dettagli non estrae piu' recruiter", "recruiter" not in out)
    verifica("estrai_extra non ha piu' il parser email",
             not hasattr(estrai_extra, "_email_da_testo"))

    class _ConnessioneRicerca:
        sql = ""

        def execute(self, sql, par):
            self.sql = sql
            return self

        def fetchall(self):
            return []

    conn = _ConnessioneRicerca()
    with mock.patch.object(vivo, "_conn", return_value=conn):
        righe, cursore = vivo.offerte(
            {"country": "US", "q": "Teamcenter", "dal": "2026-10-22"}, None, 25)
    verifica("q= usa una CTE materializzata prima dell'ORDER BY",
             righe == [] and cursore is None
             and "WITH matched AS MATERIALIZED" in conn.sql
             and "plainto_tsquery('simple', %(q)s)" in conn.sql)
    conn = _ConnessioneRicerca()
    with mock.patch.object(vivo, "_conn", return_value=conn):
        vivo.offerte({"country": "US"}, None, 25)
    verifica("senza q= resta il percorso ordinato via indice",
             "WITH matched AS MATERIALIZED" not in conn.sql)


def main() -> int:
    _verifica_funzioni_output()
    cartella = tempfile.mkdtemp(prefix="nivult-api-prova-")
    cartelle = [cartella]
    db = f"{cartella}/api-clienti.duckdb"
    db_precedente = os.environ.get("API_CLIENTI_DB")
    os.environ["API_CLIENTI_DB"] = db

    def pulisci() -> None:
        dati.apri(f"{cartella}/chiusura.duckdb")
        for percorso in cartelle:
            shutil.rmtree(percorso, ignore_errors=True)
        if db_precedente is None:
            os.environ.pop("API_CLIENTI_DB", None)
        else:
            os.environ["API_CLIENTI_DB"] = db_precedente

    atexit.register(pulisci)
    print(f"export sintetici in {cartella}")
    _scrivi(f"{cartella}/offerte-attive-{OGGI}.jsonl.gz", _offerte())
    _scrivi(f"{cartella}/offerte-attive-{IERI}.jsonl.gz", _offerte()[:5])  # esca
    _scrivi(f"{cartella}/aziende-segnali-{OGGI}.jsonl.gz", AZIENDE)
    _scrivi(f"{cartella}/flusso/novita-{OGGI:%Y%m%d}-080000.jsonl.gz", _flusso())
    with open(f"{cartella}/flusso/novita-{OGGI:%Y%m%d}-060000.jsonl.gz", "wb") as f:
        f.write(b"non e' un gzip valido")
    with gzip.open(f"{cartella}/flusso/novita-{OGGI:%Y%m%d}-060100.jsonl.gz",
                   "wt", encoding="utf-8") as f:
        f.write("{json corrotto\n")
    vecchio = OGGI - dt.timedelta(days=40)
    _scrivi(f"{cartella}/flusso/novita-{vecchio:%Y%m%d}-120000.jsonl.gz",
            [{"event": "new", "id": "fl-vecchio", "title": "Troppo vecchia",
              "ats": "lever", "company_slug": "x", "url": "https://x",
              "country": "IT", "first_seen": f"{vecchio}T12:00:00+00:00"}])
    with open(f"{cartella}/manifest-ultimo.json", "w") as f:
        json.dump(MANIFEST, f)

    print("\n== il builder, via CLI come fara' cron ==")
    r = subprocess.run([sys.executable, "-m", "nivult.api_clienti.aggiorna",
                        "--cartella", cartella, "--flusso-giorni", "30",
                        "--attesa-export", "0"],
                       capture_output=True, text=True,
                       env={**os.environ, "API_CLIENTI_DB": db})
    verifica("python -m nivult.api_clienti.aggiorna esce 0",
             r.returncode == 0, r.stderr.strip()[-300:])
    verifica("il db esiste nella sabbia API_CLIENTI_DB", os.path.exists(db))

    dati.apri()
    verifica("il lettore usa il db isolato", dati._percorso == db)
    st = dati.stato_export()
    verifica("stato ok", st.get("stato") == "ok", str(st))
    verifica("conteggi in meta (30/8/7, non 5 dell'esca)",
             (st.get("offerte"), st.get("aziende"), st.get("flusso")) == (30, 8, 7),
             f"{st.get('offerte')}/{st.get('aziende')}/{st.get('flusso')}")
    verifica("data_export = oggi", st.get("data_export") == OGGI.isoformat())
    attesa = {k: v for k, v in MANIFEST["coverage"].items()
              if k not in {"contact_email", "recruiter"}}
    verifica("copertura dal manifest senza metriche PII", dati.copertura() == attesa,
             str(dati.copertura()))

    print("\n== offerte: paginazione completa, due pagine da 20 ==")
    p1, c1 = dati.offerte({}, None, 20)
    p2, c2 = dati.offerte({}, c1, 20)
    verifica("pagina 1: 20 righe e un cursore", len(p1) == 20 and bool(c1))
    verifica("pagina 2: 10 righe, cursore chiuso", len(p2) == 10 and c2 is None)
    ids = [r["id"] for r in p1 + p2]
    verifica("nessun buco, nessun doppione",
             sorted(ids) == [f"off-{n:02d}" for n in range(1, 31)])
    verifica("il pari a cavallo: pag1 chiude con off-20, pag2 apre con off-21",
             p1[-1]["id"] == "off-20" and p2[0]["id"] == "off-21",
             f"{p1[-1]['id']} -> {p2[0]['id']}")
    verifica("i senza-data in fondo",
             [r["id"] for r in p2[-2:]] == ["off-29", "off-30"],
             str([r["id"] for r in p2[-2:]]))
    date = [r.get("posted_at") for r in p1 + p2 if r.get("posted_at")]
    verifica("posted_at davvero DESC", date == sorted(date, reverse=True))
    verifica("la riga e' quella dell'export, intera",
             p1[0]["description"] == "Testo lungo dell'annuncio 1"
             and p1[0]["salary_min"] == 30001)

    def senza_pii(valore):
        if isinstance(valore, dict):
            return all(k not in {"contact_email", "recruiter"} and senza_pii(v)
                       for k, v in valore.items())
        if isinstance(valore, list):
            return all(senza_pii(v) for v in valore)
        return True

    verifica("paginazione API senza campi PII, anche annidati",
             all(senza_pii(r) for r in p1 + p2))
    verifica("reveal da snapshot precedente senza campi PII",
             senza_pii(dati.rivela_offerta("off-01")))
    export_pulito = f"{cartella}/export-senza-pii.jsonl.gz"
    dati.scrivi_export({}, export_pulito)
    with gzip.open(export_pulito, "rt", encoding="utf-8") as f:
        righe_pulite = [json.loads(linea) for linea in f]
    verifica("download filtrato senza campi PII",
             len(righe_pulite) == 30 and all(senza_pii(r) for r in righe_pulite))

    print("\n== offerte: filtri ==")
    verifica("country=IT -> 12", len(dati.offerte({"country": "IT"}, None, 500)[0]) == 12)
    tec = dati.offerte({"technology": "python"}, None, 500)[0]  # minuscolo apposta
    verifica("technology=python (case-insensitive) -> off-01..10",
             {r["id"] for r in tec} == {f"off-{n:02d}" for n in range(1, 11)},
             str(len(tec)))
    verifica("q=python su title -> 4",
             len(dati.offerte({"q": "python"}, None, 500)[0]) == 4)
    verifica("q=annuncio presente solo nella descrizione -> 30",
             len(dati.offerte({"q": "annuncio"}, None, 500)[0]) == 30)
    verifica("remote=True -> 5", len(dati.offerte({"remote": True}, None, 500)[0]) == 5)
    verifica("remote=False -> 5", len(dati.offerte({"remote": False}, None, 500)[0]) == 5)
    verifica("remote=onsite (stringa) -> 5", len(dati.offerte({"remote": "onsite"}, None, 500)[0]) == 5)
    verifica("dal=T -> 21 (19 nuove + il pari)",
             len(dati.offerte({"dal": T.isoformat()}, None, 500)[0]) == 21)
    verifica("country=IT + ats=greenhouse -> 6",
             len(dati.offerte({"country": "IT", "ats": "greenhouse"}, None, 500)[0]) == 6)
    verifica("seniority=senior + category=tech -> 10",
             len(dati.offerte({"seniority": "senior", "category": "tech"},
                              None, 500)[0]) == 10)
    verifica("language=fr -> 8", len(dati.offerte({"language": "fr"}, None, 500)[0]) == 8)
    verifica("limite oltre il tetto non schianta",
             len(dati.offerte({}, None, 9999)[0]) == 30)
    try:
        dati.offerte({}, "!!marzapane!!", 10)
        verifica("cursore marcio -> ValueError", False)
    except ValueError:
        verifica("cursore marcio -> ValueError", True)

    print("\n== portale: keyset anche con posted_at NULL ==")
    port1, pc1, _ = dati.cerca_portale({}, None, 25)
    port2, pc2, _ = dati.cerca_portale({}, pc1, 25)
    verifica("il primo cursore del portale punta a una riga NULL",
             len(port1) == 25 and port1[-1]["id"] == "off-29" and bool(pc1),
             f"{len(port1)}: {port1[-1]['id'] if port1 else '-'}")
    verifica("la pagina dopo NULL non si ripete e si chiude",
             [r["id"] for r in port2] == ["off-30"] and pc2 is None,
             str([r["id"] for r in port2]))
    verifica("portale senza buchi ne' doppioni",
             len({r["id"] for r in port1 + port2}) == 26)
    testo_portale, _, totale_testo = dati.cerca_portale(
        {"q": "Zephyrquix"}, None, 25)
    verifica("portale q trova una parola solo nella descrizione",
             [r["id"] for r in testo_portale] == ["off-30"] and totale_testo == 1)
    chiuse_testo, _, _ = dati.cerca_chiuse_portale(
        {"q": "Vecchia uno", "country": "IT", "remote": "hybrid"}, None, 25)
    verifica("chiuse: titolo, paese e workplace nello snapshot",
             [r["id"] for r in chiuse_testo] == ["fl-c1"])

    print("\n== aziende ==")
    a1, ac1 = dati.aziende({}, None, 3)
    a2, ac2 = dati.aziende({}, ac1, 3)
    a3, ac3 = dati.aziende({}, ac2, 3)
    verifica("pagine 3+3+2 e poi basta",
             (len(a1), len(a2), len(a3)) == (3, 3, 2) and ac3 is None)
    verifica("8 aziende, nessuna due volte",
             len({(r["ats"], r["company_slug"]) for r in a1 + a2 + a3}) == 8)
    verifica("ordine per nome",
             [r["company"] for r in a1] == ["Alfa Sistemi", "Azienda 1", "Beta Farma"],
             str([r["company"] for r in a1]))
    em = dati.aziende({"employees_min": 1000}, None, 500)[0]
    verifica("employees_min=1000 -> Beta, Delta (chi non ha il dato resta fuori)",
             {r["company"] for r in em} == {"Beta Farma", "Delta Retail"})
    ex = dati.aziende({"employees_max": 50}, None, 500)[0]
    verifica("employees_max=50 -> Gamma, Zeta",
             {r["company"] for r in ex} == {"Gamma Logistica", "Zeta Food"})
    verifica("q=alfa -> 1", len(dati.aziende({"q": "alfa"}, None, 500)[0]) == 1)
    at = dati.aziende({"technology": "sap"}, None, 500)[0]
    verifica("technology=sap -> Beta Farma",
             [r["company"] for r in at] == ["Beta Farma"])
    ap = dati.aziende({"technology": "python"}, None, 500)[0]
    verifica("technology=python -> Alfa, Delta",
             {r["company"] for r in ap} == {"Alfa Sistemi", "Delta Retail"})
    verifica("country=IT -> 3", len(dati.aziende({"country": "IT"}, None, 500)[0]) == 3)
    verifica("industry=software -> 1",
             len(dati.aziende({"industry": "software"}, None, 500)[0]) == 1)

    alfa = dati.azienda("greenhouse", "alfa-sistemi")
    verifica("azienda singola trovata", alfa is not None
             and alfa["company"] == "Alfa Sistemi")
    verifica("technologies dell'azienda intere (con active_jobs ecc.)",
             alfa is not None
             and [t["technology"] for t in alfa["technologies"]] == ["Python", "Docker"]
             and alfa["technologies"][0]["active_jobs"] == 3)
    verifica("azienda assente -> None",
             dati.azienda("greenhouse", "non-ce") is None)
    snella = dati.rivela_azienda("greenhouse:az-1")
    verifica("il fallback non aggancia un omonimo di un altro paese",
             snella is not None and snella.get("profilo") == "snello"
             and snella.get("country") == "IT"
             and snella.get("company") == "Azienda 1", str(snella))

    print("\n== flusso (delta feed) ==")
    tutti, _ = dati.cambiamenti("", None, 500)
    verifica("7 eventi validi: vecchio, file corrotti e timestamp marcio fuori",
             len(tutti) == 7, str([r["id"] for r in tutti]))
    verifica("delta feed senza campi PII", all(senza_pii(r) for r in tutti))
    verifica("updated_at ordina gli update; first_seen resta il tempo delle new",
             [r["id"] for r in tutti] == ["fl-c1", "fl-n1", "fl-n2", "fl-u1",
                                          "fl-u2", "fl-n3", "fl-c2"],
             str([r["id"] for r in tutti]))
    aggiornati = [r for r in tutti if r["event"] == "updated"]
    verifica("i due updated conservano updated_at e il payload",
             [r["id"] for r in aggiornati] == ["fl-u1", "fl-u2"]
             and all(r.get("updated_at") and r.get("category") for r in aggiornati))
    recenti, _ = dati.cambiamenti(_ore(8, 0), None, 500)
    verifica("da=08:00 -> updated mixed-date, nuova e chiusa",
             [r["id"] for r in recenti] == ["fl-u2", "fl-n3", "fl-c2"])
    f1, fc1 = dati.cambiamenti("", None, 2)
    f2, fc2 = dati.cambiamenti("", fc1, 2)
    f3, fc3 = dati.cambiamenti("", fc2, 2)
    f4, fc4 = dati.cambiamenti("", fc3, 2)
    verifica("paginato 2+2+2+1 e poi basta",
             (len(f1), len(f2), len(f3), len(f4)) == (2, 2, 2, 1) and fc4 is None)
    verifica("senza doppioni fra le pagine",
             len({r["id"] for r in f1 + f2 + f3 + f4}) == 7)
    verifica("l'evento chiuso porta il suo closed_at",
             f1[0]["event"] == "closed" and "closed_at" in f1[0])
    try:
        dati.cambiamenti(f"{vecchio}T00:00:00+00:00", None, 10)
        verifica("since oltre retention -> errore di resync", False)
    except dati.DeltaRetentionError as e:
        dettaglio = e.as_dict()
        verifica("since oltre retention -> errore machine-readable di resync",
                 e.code == "delta_retention_exceeded" and e.resync_required
                 and dettaglio["action"] == "resync"
                 and dettaglio["retention_since"].startswith(str(OGGI - dt.timedelta(days=30))),
                 str(dettaglio))

    print("\n== il builder riscrive il file MENTRE lo stiamo leggendo ==")
    meta2 = aggiorna.costruisci(cartella, 30, attesa_minuti=0)
    verifica("rilancio idempotente, stessi numeri",
             (meta2["offerte"], meta2["aziende"], meta2["flusso"]) == (30, 8, 7))
    verifica("il lettore con la connessione aperta continua a rispondere",
             len(dati.offerte({"country": "IT"}, None, 500)[0]) == 12)

    print("\n== export mancanti, attesa zero e retention senza eventi ==")
    vuota = tempfile.mkdtemp(prefix="nivult-api-prova-vuota-")
    cartelle.append(vuota)
    vuota_db = f"{vuota}/api-clienti.duckdb"
    with mock.patch.object(aggiorna.time, "sleep") as dormi:
        meta3 = aggiorna.costruisci(vuota, 30, db_path=vuota_db, attesa_minuti=0)
    verifica("attesa_minuti=0 non dorme mai", not dormi.called)
    verifica("stato dichiarato", meta3["stato"] == "mancano gli export di oggi")
    dati.apri(vuota_db)
    verifica("stato_export dal db vuoto",
             dati.stato_export().get("stato") == "mancano gli export di oggi")
    vuote, cur = dati.offerte({}, None, 10)
    verifica("offerte vuote senza crash", vuote == [] and cur is None)
    try:
        dati.cambiamenti(f"{vecchio}T00:00:00+00:00", None, 10)
        verifica("retention valida anche con flusso vuoto", False)
    except dati.DeltaRetentionError:
        verifica("retention valida anche con flusso vuoto (non min(t))", True)

    mista = tempfile.mkdtemp(prefix="nivult-api-prova-mista-")
    cartelle.append(mista)
    _scrivi(f"{mista}/offerte-attive-{OGGI}.jsonl.gz", _offerte()[:2])
    _scrivi(f"{mista}/aziende-segnali-{IERI}.jsonl.gz", AZIENDE[:2])
    try:
        aggiorna.costruisci(mista, 30, attesa_minuti=0)
        verifica("date non allineate rifiutate", False)
    except SystemExit as exc:
        verifica("date non allineate rifiutate", "non allineati" in str(exc))
    verifica("il db misto non viene creato", not os.path.exists(f"{mista}/api-clienti.duckdb"))

    print(f"\ncontrolli falliti: {ko}")
    return 1 if ko else 0


if __name__ == "__main__":
    raise SystemExit(main())
