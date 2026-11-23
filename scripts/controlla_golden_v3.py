#!/usr/bin/env python3
"""Integrità degli hard negatives match-v3, senza toccare il golden congelato."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAMPI = {"pair_id", "profile_key", "title", "score_manuale", "label", "motivo",
         "provenienza", "split"}


def main() -> int:
    path = ROOT / "dati/match-v3/hard-negatives.jsonl"
    righe = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    errori = []
    if not righe:
        errori.append("nessun hard negative")
    for r in righe:
        if set(r) != CAMPI:
            errori.append(f"campi non validi: {r.get('pair_id')}")
        if r.get("split") != "train":
            errori.append(f"split non train: {r.get('pair_id')}")
        if not r.get("provenienza") or not r.get("motivo"):
            errori.append(f"provenienza/motivo mancanti: {r.get('pair_id')}")
        atteso = "buona" if r["score_manuale"] >= 70 else \
            "borderline" if r["score_manuale"] >= 50 else "scartata"
        if r.get("label") != atteso:
            errori.append(f"label incoerente: {r.get('pair_id')}")
    ids = [r["pair_id"] for r in righe]
    duplicati = [i for i, n in Counter(ids).items() if n > 1]
    if duplicati:
        errori.append(f"ID duplicati: {duplicati}")
    congelati = set()
    for path in (ROOT / "dati/match-v3/candidati.jsonl",):
        congelati.update(json.loads(x)["pair_id"] for x in path.read_text().splitlines())
    overlap = sorted(congelati & set(ids))
    if overlap:
        errori.append(f"hard negative già nel golden congelato: {overlap}")
    if errori:
        for e in errori:
            print("FAIL", e)
        return 1
    print(f"OK: {len(righe)} hard negatives train-only, nessuna sovrapposizione al golden")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
