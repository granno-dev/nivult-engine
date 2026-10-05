#!/usr/bin/env python3
"""L'esame di match-v0 con lo sweep del cutoff (05/10/2026).

Il modello salvato da allena_match_v0.py decide «dentro il digest» con
una probabilita': il cutoff 0.5 era arbitrario. Qui si misura
precision/recall a ogni cutoff, sullo STESSO val split del training
(md5 sul job_id, deterministico) — e si sceglie il punto che rispetta
la promessa del digest: pochi e giusti.

    python scripts/esame_match_v0.py [matcher-v0.jsonl] [modello_dir]
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", "1")

import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from allena_match_v0 import Scorer, carica, split_per_offerta  # noqa: E402


def main() -> int:
    percorso = sys.argv[1] if len(sys.argv) > 1 else "/opt/nivult/matcher-v0.jsonl"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "/opt/nivult/modelli/match-v0"
    from transformers import AutoTokenizer

    _, val = split_per_offerta(carica(percorso))
    tok = AutoTokenizer.from_pretrained(out_dir
                                        if os.path.exists(f"{out_dir}/tokenizer_config.json")
                                        else "jhu-clsp/mmBERT-base")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    modello = Scorer("jhu-clsp/mmBERT-base").to(device)
    modello.load_state_dict(torch.load(f"{out_dir}/pesi.pt",
                                       map_location=device))
    modello.eval()

    prob, passato = [], []
    with torch.no_grad():
        for i in range(0, len(val), 32):
            b = val[i:i + 32]
            testi = [f"[PROFILO]\n{r['profilo']}\n[OFFERTA]\n{r['offerta']}"
                     for r in b]
            enc = tok(testi, truncation=True, max_length=512,
                      padding=True, return_tensors="pt").to(device)
            _, p_out = modello(**enc)
            prob += [float(torch.sigmoid(x)) for x in p_out]
            passato += [r["passato"] for r in b]

    print(f"val: {len(val)} righe, {sum(passato)} passate vere")
    print(f"{'cutoff':>7} {'prec':>6} {'rec':>6} {'F1':>6} {'mandati':>8}")
    migliore = (0.0, 0.5)
    for taglio in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        tp = sum(1 for p, w in zip(prob, passato) if p >= taglio and w)
        fp = sum(1 for p, w in zip(prob, passato) if p >= taglio and not w)
        fn = sum(1 for p, w in zip(prob, passato) if p < taglio and w)
        prec = tp / max(1, tp + fp)
        rec = tp / max(1, tp + fn)
        f1 = 2 * prec * rec / max(1e-9, prec + rec)
        if f1 > migliore[0]:
            migliore = (f1, taglio)
        print(f"{taglio:>7.1f} {prec:>6.2f} {rec:>6.2f} {f1:>6.2f} {tp + fp:>8}")
    print(f"miglior cutoff: {migliore[1]} (F1 {migliore[0]:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
