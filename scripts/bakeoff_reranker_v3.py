#!/usr/bin/env python3
"""Bake-off cross-encoder locali sulle stesse viste e holdout match-v3."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from bakeoff_match_v3 import (
    _costruisci_viste,
    _limita_per_profilo,
    _memoria_gpu,
    _memoria_ram,
)
from bench_reranker import BgeM3, Qwen3
from metriche_match_v2 import carica_banco, carica_banco_v3, metriche_ranking

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", choices=("qwen3-reranker", "bge-reranker"), required=True)
    ap.add_argument("--dataset", choices=("v2", "v3"), default="v2")
    ap.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    ap.add_argument("--batch-size", type=int, default=2)
    ap.add_argument("--max-length", type=int, default=512)
    ap.add_argument("--limit-per-profilo", type=int)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    inizio_totale = time.perf_counter()
    if args.batch_size <= 0 or args.max_length <= 0:
        raise SystemExit("batch size e lunghezza devono essere positivi")
    import torch
    if args.device == "cuda" and not torch.cuda.is_available():
        raise SystemExit("CUDA richiesto ma non disponibile")

    righe = carica_banco_v3(ROOT) if args.dataset == "v3" else carica_banco(ROOT)
    righe = _limita_per_profilo(righe, args.limit_per_profilo)
    viste = _costruisci_viste(righe)
    inizio = time.perf_counter()
    if args.model == "qwen3-reranker":
        modello = Qwen3(max_length=args.max_length)
    else:
        modello = BgeM3()
    modello.mod.to(args.device).eval()
    caricamento = time.perf_counter() - inizio

    scores = {}
    tempi = {}
    for vista in viste:
        coppie = [(viste[vista]["query"][r["profile_key"]],
                   viste[vista]["documenti"][r["pair_id"]]) for r in righe]
        inizio = time.perf_counter()
        valori = modello.punteggi(coppie, batch=args.batch_size)
        tempi[vista] = time.perf_counter() - inizio
        scores[vista] = {r["pair_id"]: float(v) for r, v in zip(righe, valori)}
    ranking = {
        vista: {split: metriche_ranking([r for r in righe if r["split"] == split],
                                        scores[vista])
                for split in ("validation", "test")}
        for vista in viste
    }
    memoria = _memoria_ram()
    memoria["gpu"] = _memoria_gpu(torch, args.device)
    rapporto = {
        "schema_version": "match-v3-bakeoff-1",
        "model": args.model,
        "dataset": args.dataset,
        "device": args.device,
        "rows": len(righe),
        "batch_size": args.batch_size,
        "max_length": args.max_length,
        "timings_seconds": {"model_load": caricamento, "encoding": tempi,
                            "total": time.perf_counter() - inizio_totale},
        "memory": memoria,
        "scores": scores,
        "ranking": ranking,
    }
    Path(args.out).write_text(json.dumps(rapporto, ensure_ascii=False, indent=2,
                                         allow_nan=False) + "\n")
    print(f"Rapporto scritto in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
