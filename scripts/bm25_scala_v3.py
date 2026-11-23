#!/usr/bin/env python3
"""Punteggi BM25 ruolo per la prova di scala match-v3."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from bakeoff_match_v3 import _costruisci_viste, _indici_bm25, _scores_bm25
from metriche_match_v2 import carica_banco_v3

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--distrattori", default=str(ROOT / "dati/match-v3/distrattori.jsonl"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    golden = carica_banco_v3(ROOT)
    profili = {r["profile_key"]: r["profilo"] for r in golden}
    distrattori = [json.loads(x) for x in Path(args.distrattori).read_text().split("\n")
                   if x.strip()]
    for r in distrattori:
        r["profilo"] = profili[r["profile_key"]]
        r["offerta"] = ""
    righe = golden + distrattori
    viste = _costruisci_viste(righe)
    scores = _scores_bm25(righe, viste, _indici_bm25(righe, viste))["role"]
    Path(args.out).write_text("".join(json.dumps({"pair_id": i, "score": scores[i]}) + "\n"
                                      for i in sorted(scores)))
    print("punteggi", len(scores))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
