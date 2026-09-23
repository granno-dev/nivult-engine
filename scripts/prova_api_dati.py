"""Banco del data layer clienti: export SINTETICI in /tmp, zero database.

Il builder legge solo file, quindi si prova intero senza toccare ne' il
Postgres di produzione ne' gli export veri. I numeri sono scelti per
far fallire il banco se qualcosa si sposta:

  - 30 offerte: 12 IT / 10 DE / 8 FR; due SENZA posted_at (devono
    finire in fondo, NULLS LAST); off-20 e off-21 con la STESSA
    posted_at a cavallo della pagina da 20 — se lo spareggio su id non
    funziona, la seconda pagina salta o ripete una riga;
  - un file offerte di IERI con 5 righe: il builder deve scegliere
    quello di oggi, non il primo che trova;
  - 8 aziende, di cui una senza employees (i filtri sui dipendenti
    devono escluderla, non schiantarsi);
  - 5 eventi di flusso oggi + un file novita-* di 40 giorni fa che con
    --flusso-giorni 30 NON deve entrare.

Uso:  .venv/bin/python scripts/prova_api_dati.py   (esce 0 solo se passa tutto)
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import subprocess
import sys
import tempfile

from nivult.api_clienti import aggiorna, dati

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
            "company_slug": f"az-{(n - 1) % 8 + 1}",
            "company": f"Azienda {(n - 1) % 8 + 1}",
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
            "description": f"Testo lungo dell'annuncio {n}",
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
    {"ats": "workable", "company_slug": "theta-cons", "company": "Theta Consulting",
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
         "first_seen": _ore(7, 0)},
        {"event": "closed", "id": "fl-c1", "title": "Vecchia uno",
         "ats": "lever", "company_slug": "beta-farma",
         "url": "https://example.org/f/9", "country": "IT",
         "posted_at": "2026-08-01T10:00:00+00:00", "closed_at": _ore(6, 30)},
        {"event": "new", "id": "fl-n2", "title": "Nuova due", "ats": "lever",
         "company_slug": "alfa-sistemi", "company": "Alfa Sistemi",
         "url": "https://example.org/f/2", "country": "IT",
         "first_seen": _ore(7, 5)},
        {"event": "new", "id": "fl-n3", "title": "Nuova tre", "ats": "workable",
         "company_slug": "eta-media", "company": "Eta Media",
         "url": "https://example.org/f/3", "country": "IT",
         "first_seen": _ore(9, 0)},
        {"event": "closed", "id": "fl-c2", "title": "Vecchia due",
         "ats": "lever", "company_slug": "delta-retail",
         "url": "https://example.org/f/8", "country": "DE",
         "posted_at": "2026-08-03T10:00:00+00:00", "closed_at": _ore(10, 15)},
    ]


MANIFEST = {"date": OGGI.isoformat(), "rows": 30,
            "coverage": {"salary_observed": 33.3, "description": 96.7,
                         "technologies": 50.0}}


def main() -> int:
    cartella = tempfile.mkdtemp(prefix="nivult-api-prova-")
    print(f"export sintetici in {cartella}")
    _scrivi(f"{cartella}/offerte-attive-{OGGI}.jsonl.gz", _offerte())
    _scrivi(f"{cartella}/offerte-attive-{IERI}.jsonl.gz", _offerte()[:5])  # esca
    _scrivi(f"{cartella}/aziende-segnali-{OGGI}.jsonl.gz", AZIENDE)
    _scrivi(f"{cartella}/flusso/novita-{OGGI:%Y%m%d}-080000.jsonl.gz", _flusso())
    vecchio = OGGI - dt.timedelta(days=40)
    _scrivi(f"{cartella}/flusso/novita-{vecchio:%Y%m%d}-120000.jsonl.gz",
            [{"event": "new", "id": "fl-vecchio", "title": "Troppo vecchia",
              "ats": "lever", "company_slug": "x", "url": "https://x",
              "country": "IT", "first_seen": f"{vecchio}T12:00:00+00:00"}])
    with open(f"{cartella}/manifest-ultimo.json", "w") as f:
        json.dump(MANIFEST, f)

    print("\n== il builder, via CLI come fara' cron ==")
    r = subprocess.run([sys.executable, "-m", "nivult.api_clienti.aggiorna",
                        "--cartella", cartella, "--flusso-giorni", "30"],
                       capture_output=True, text=True)
    verifica("python -m nivult.api_clienti.aggiorna esce 0",
             r.returncode == 0, r.stderr.strip()[-300:])
    db = f"{cartella}/api-clienti.duckdb"
    verifica("il db esiste", os.path.exists(db))

    dati.apri(db)
    st = dati.stato_export()
    verifica("stato ok", st.get("stato") == "ok", str(st))
    verifica("conteggi in meta (30/8/5, non 5 dell'esca)",
             (st.get("offerte"), st.get("aziende"), st.get("flusso")) == (30, 8, 5),
             f"{st.get('offerte')}/{st.get('aziende')}/{st.get('flusso')}")
    verifica("data_export = oggi", st.get("data_export") == OGGI.isoformat())
    verifica("copertura dal manifest", dati.copertura() == MANIFEST["coverage"],
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

    print("\n== offerte: filtri ==")
    verifica("country=IT -> 12", len(dati.offerte({"country": "IT"}, None, 500)[0]) == 12)
    tec = dati.offerte({"technology": "python"}, None, 500)[0]  # minuscolo apposta
    verifica("technology=python (case-insensitive) -> off-01..10",
             {r["id"] for r in tec} == {f"off-{n:02d}" for n in range(1, 11)},
             str(len(tec)))
    verifica("q=python su title -> 4",
             len(dati.offerte({"q": "python"}, None, 500)[0]) == 4)
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

    print("\n== aziende ==")
    a1, ac1 = dati.aziende({}, None, 3)
    a2, ac2 = dati.aziende({}, ac1, 3)
    a3, ac3 = dati.aziende({}, ac2, 3)
    verifica("pagine 3+3+2 e poi basta",
             (len(a1), len(a2), len(a3)) == (3, 3, 2) and ac3 is None)
    verifica("8 aziende, nessuna due volte",
             len({(r["ats"], r["company_slug"]) for r in a1 + a2 + a3}) == 8)
    verifica("ordine per nome",
             [r["company"] for r in a1] == ["Alfa Sistemi", "Beta Farma", "Delta Retail"],
             str([r["company"] for r in a1]))
    em = dati.aziende({"employees_min": 1000}, None, 500)[0]
    verifica("employees_min=1000 -> Beta, Delta (Theta senza dato resta fuori)",
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

    print("\n== flusso (delta feed) ==")
    tutti, _ = dati.cambiamenti("", None, 500)
    verifica("5 eventi: il file di 40 giorni fa resta fuori",
             len(tutti) == 5, str([r["id"] for r in tutti]))
    verifica("ordinati per t (closed_at delle chiuse, first_seen delle nuove)",
             [r["id"] for r in tutti] == ["fl-c1", "fl-n1", "fl-n2", "fl-n3", "fl-c2"])
    recenti, _ = dati.cambiamenti(_ore(8, 0), None, 500)
    verifica("da=08:00 -> solo fl-n3 e fl-c2",
             [r["id"] for r in recenti] == ["fl-n3", "fl-c2"])
    f1, fc1 = dati.cambiamenti("", None, 2)
    f2, fc2 = dati.cambiamenti("", fc1, 2)
    f3, fc3 = dati.cambiamenti("", fc2, 2)
    verifica("paginato 2+2+1 e poi basta",
             (len(f1), len(f2), len(f3)) == (2, 2, 1) and fc3 is None)
    verifica("senza doppioni fra le pagine",
             len({r["id"] for r in f1 + f2 + f3}) == 5)
    verifica("l'evento chiuso porta il suo closed_at",
             f1[0]["event"] == "closed" and "closed_at" in f1[0])

    print("\n== il builder riscrive il file MENTRE lo stiamo leggendo ==")
    meta2 = aggiorna.costruisci(cartella, 30)
    verifica("rilancio idempotente, stessi numeri",
             (meta2["offerte"], meta2["aziende"], meta2["flusso"]) == (30, 8, 5))
    verifica("il lettore con la connessione aperta continua a rispondere",
             len(dati.offerte({"country": "IT"}, None, 500)[0]) == 12)

    print("\n== export mancanti ==")
    vuota = tempfile.mkdtemp(prefix="nivult-api-prova-vuota-")
    meta3 = aggiorna.costruisci(vuota, 30)
    verifica("stato dichiarato", meta3["stato"] == "mancano gli export di oggi")
    dati.apri(f"{vuota}/api-clienti.duckdb")
    verifica("stato_export dal db vuoto",
             dati.stato_export().get("stato") == "mancano gli export di oggi")
    vuote, cur = dati.offerte({}, None, 10)
    verifica("offerte vuote senza crash", vuote == [] and cur is None)

    print(f"\ncontrolli falliti: {ko}")
    return 1 if ko else 0


if __name__ == "__main__":
    raise SystemExit(main())
