#!/usr/bin/env python3
"""Valuta il giudice locale 4B sul holdout v3, senza GLM o nuovi dati utente."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from metriche_match_v2 import carica_banco, carica_banco_v3, metriche, metriche_ranking
from nivult.matching.llm import MaestroLocale, valuta_offerta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://100.119.200.7:8090")
    ap.add_argument("--dataset", choices=("v2", "v3"), default="v3")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--split", choices=("validation", "test"))
    args = ap.parse_args()
    os.environ["MAESTRO_URL"] = args.url
    righe = carica_banco_v3() if args.dataset == "v3" else carica_banco()
    if args.split:
        righe = [r for r in righe if r["split"] == args.split]
    if args.limit:
        righe = righe[:args.limit]
    out = Path(args.out)
    predette = {}
    if out.exists():
        for r in map(json.loads, out.read_text().splitlines()):
            if "score" in r:
                predette[r["pair_id"]] = r
    modello = MaestroLocale()
    profili = json.loads(Path(f"dati/match-{args.dataset}/profili.json").read_text())
    errori = []
    for n, r in enumerate(righe, 1):
        if r["pair_id"] in predette:
            continue
        try:
            desiderio = r.get("_wants") or profili[r["profile_key"]]["wants"]
            score, reason, _ = valuta_offerta(
                modello, r["profilo"], dict(r, _wants=desiderio))
            predette[r["pair_id"]] = {"pair_id": r["pair_id"], "score": score,
                                      "reason": reason, "model": modello.model}
        except Exception as exc:  # noqa: BLE001
            errori.append({"pair_id": r["pair_id"], "error": str(exc)[:200]})
        with out.open("w") as f:
            for pair_id in sorted(predette):
                f.write(json.dumps(predette[pair_id], ensure_ascii=False) + "\n")
            for errore in errori:
                f.write(json.dumps(errore, ensure_ascii=False) + "\n")
        print(f"{n}/{len(righe)} errori={len(errori)}", flush=True)

    scores = {pair_id: float(r["score"]) for pair_id, r in predette.items()}
    if len(scores) != len(righe):
        print(f"incompleto: {len(scores)}/{len(righe)}, errori={len(errori)}")
        return 1
    for split in ("validation", "test"):
        subset = [r for r in righe if r["split"] == split]
        if not subset:
            continue
        print(split, "gate", metriche(subset, scores)["complessivo"])
        print(split, "ranking", metriche_ranking(subset, scores)["complessivo"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
