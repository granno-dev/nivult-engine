#!/usr/bin/env python3
"""Reed.co.uk come DATASET per la stima dello stipendio, non come fonte del digest.

Reed e' una job board (regola sui link: `job_board`, per ora fuori dai
digest). Il suo valore e' un altro: quasi ogni annuncio dichiara
`minimumSalary`/`maximumSalary` in sterline, con titolo, datore, luogo e
descrizione completa. E' il materiale per insegnare al modello a STIMARE
lo stipendio quando l'annuncio non lo dice: si nasconde la cifra nel testo,
si chiede la stima, si misura contro il valore dichiarato.

API: GET https://www.reed.co.uk/api/1.0/search  (Basic auth, chiave come utente)
     parametri keywords, locationName, resultsToTake (max 100), resultsToSkip
     GET https://www.reed.co.uk/api/1.0/jobs/{id}  (descrizione integrale)

    REED_API_KEY=... python scripts/scarica_reed.py --out /opt/nivult/reed --max 60000

Scorre per area (le grandi citta' del Regno Unito) per superare il tetto di
paginazione di una singola ricerca; dedup per jobId; rispetta un ritmo
gentile (2 richieste/s). Output: reed-AAAA-MM-GG.jsonl.gz con i campi grezzi
piu' `description` integrale.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import date

import httpx

BASE = "https://www.reed.co.uk/api/1.0"
AREE = ["London", "Manchester", "Birmingham", "Leeds", "Glasgow", "Edinburgh", "Bristol", "Liverpool",
        "Sheffield", "Newcastle", "Nottingham", "Cardiff", "Belfast", "Leicester", "Southampton",
        "Reading", "Cambridge", "Oxford", "Milton Keynes", "Brighton", "Coventry", "Aberdeen", "Norwich",
        "Plymouth", "Exeter", "York", "Hull", "Derby", "Stoke-on-Trent", "Swindon", "Luton", "Portsmouth"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/opt/nivult/reed")
    ap.add_argument("--max", type=int, default=60000)
    ap.add_argument("--dettaglio", action="store_true", help="scarica anche la descrizione integrale (1 richiesta per annuncio)")
    a = ap.parse_args()
    key = os.environ.get("REED_API_KEY")
    if not key:
        print("manca REED_API_KEY", file=sys.stderr)
        return 2
    os.makedirs(a.out, exist_ok=True)
    out = os.path.join(a.out, f"reed-{date.today().isoformat()}.jsonl.gz")
    visti: set[int] = set()
    n = con_stipendio = 0
    t0 = time.time()
    with httpx.Client(auth=(key, ""), timeout=30, headers={"User-Agent": "nivult-dataset/1.0"}) as cli, \
            gzip.open(out, "wt") as f:
        for area in AREE:
            skip = 0
            while n < a.max:
                r = cli.get(f"{BASE}/search", params={"keywords": "", "locationName": area, "distanceFromLocation": 15,
                                                       "resultsToTake": 100, "resultsToSkip": skip})
                if r.status_code == 429:
                    time.sleep(30)
                    continue
                if r.status_code != 200:
                    print(f"{area}: HTTP {r.status_code} {r.text[:120]}", file=sys.stderr)
                    break
                d = r.json()
                risultati = d.get("results") or []
                if not risultati:
                    break
                for j in risultati:
                    jid = j.get("jobId")
                    if not jid or jid in visti:
                        continue
                    visti.add(jid)
                    if a.dettaglio:
                        try:
                            rd = cli.get(f"{BASE}/jobs/{jid}")
                            if rd.status_code == 200:
                                j["description"] = rd.json().get("jobDescription")
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.5)
                    j["area"] = area
                    f.write(json.dumps(j, ensure_ascii=False) + "\n")
                    n += 1
                    con_stipendio += 1 if (j.get("minimumSalary") or j.get("maximumSalary")) else 0
                skip += 100
                if skip >= int(d.get("totalResults") or 0):
                    break
                time.sleep(0.5)
            print(f"{area}: {n} annunci finora, {con_stipendio} con stipendio, {int(time.time()-t0)}s", flush=True)
            if n >= a.max:
                break
    print(f"FINE {out}: {n} annunci, {con_stipendio} con stipendio ({100*con_stipendio/max(n,1):.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
