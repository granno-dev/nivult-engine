#!/usr/bin/env python3
"""Una sola epoca di fine-tuning MiniLM sul matching CV-offerta."""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import numpy as np
import torch
from sklearn.isotonic import IsotonicRegression
from transformers import AutoModelForSequenceClassification, AutoTokenizer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from allena_match_micro import _report
from metriche_match_v2 import carica_banco_v3
from nivult.matching.llm import offerta_per_prompt
from nivult.matching.micro import guardrail

MODELLO = "MoritzLaurer/multilingual-MiniLMv2-L6-mnli-xnli"
PESI = {"buona": 4.0, "borderline": 2.0, "scartata": 1.0}


def _carica(path: Path, extra: Path | None) -> tuple[list[dict], list[dict]]:
    righe = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    train = [r for r in righe if r["split"] == "train"]
    if extra:
        train.extend(json.loads(x) for x in extra.read_text().splitlines() if x.strip())
    return train, [r for r in righe if r["split"] == "validation"]


def _batch(tokenizer, righe, max_length: int):
    offerte = [r.get("offerta") or offerta_per_prompt(
        r["job"], r.get("wants") or r["job"].get("_wants")) for r in righe]
    return tokenizer([r["profilo"] for r in righe], offerte,
                     padding=True, truncation=True, max_length=max_length,
                     return_tensors="pt")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--silver", type=Path, required=True)
    ap.add_argument("--extra-train", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--max-length", type=int, default=512)
    ap.add_argument("--threads", type=int, default=12)
    args = ap.parse_args()
    if args.epochs != 1:
        raise ValueError("il protocollo consente una sola epoca")
    torch.set_num_threads(args.threads)
    random.seed(42)
    torch.manual_seed(42)
    train, _ = _carica(args.silver, args.extra_train)
    validation = [r for r in carica_banco_v3(ROOT) if r["split"] == "validation"]
    tokenizer = AutoTokenizer.from_pretrained(MODELLO)
    model = AutoModelForSequenceClassification.from_pretrained(
        MODELLO, num_labels=1, dtype=torch.float32, ignore_mismatched_sizes=True)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=.01)
    perdita = torch.nn.MSELoss(reduction="none")
    inizio = time.time()
    for epoca in range(args.epochs):
        random.Random(42 + epoca).shuffle(train)
        model.train()
        for i in range(0, len(train), args.batch):
            lotto = train[i:i + args.batch]
            encoded = _batch(tokenizer, lotto, args.max_length)
            target = torch.tensor([r["score"] / 100 for r in lotto], dtype=torch.float32)
            pesi = torch.tensor([PESI[r["label"]] for r in lotto], dtype=torch.float32)
            predetto = model(**encoded).logits.view(-1)
            loss = (perdita(predetto, target) * pesi).mean()
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            if (i // args.batch + 1) % 25 == 0 or i + args.batch >= len(train):
                print(f"epoca={epoca + 1} batch={i // args.batch + 1} "
                      f"loss={loss.item():.5f} secondi={time.time() - inizio:.0f}", flush=True)
    model.eval()
    raw = []
    with torch.inference_mode():
        for i in range(0, len(validation), args.batch):
            encoded = _batch(tokenizer, validation[i:i + args.batch], args.max_length)
            raw.extend(model(**encoded).logits.view(-1).tolist())
    raw = np.array(raw) * 100
    y = np.array([r["score"] for r in validation], dtype=float)
    calibratore = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=100).fit(raw, y)
    pred = np.clip(calibratore.predict(raw), 0, 100)
    punteggi = {r["pair_id"]: guardrail(float(score), r["profilo"], r)
                for r, score in zip(validation, pred)}
    report = _report(validation, punteggi)
    gate = report["gate"]["complessivo"]
    f1 = report["buone_70"]["f1"]
    passa = gate["mae"] <= 15.98 and f1 >= .76 and gate["recall"] >= .89
    report["passa"] = passa
    report["secondi_training"] = time.time() - inizio
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "finetune-validation-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (args.out / "finetune-validation-raw.jsonl").write_text(
        "".join(json.dumps({"pair_id": r["pair_id"], "raw": float(score)})
                + "\n" for r, score in zip(validation, raw)))
    if passa:
        model.save_pretrained(args.out)
        tokenizer.save_pretrained(args.out)
    print(json.dumps({"passa": passa, "mae": gate["mae"], "f1_70": f1,
                      "recall_gate": gate["recall"], "precisione_gate": gate["precisione"],
                      "secondi_training": report["secondi_training"]},
                     ensure_ascii=False, indent=2), flush=True)
    return 0 if passa else 2


if __name__ == "__main__":
    raise SystemExit(main())
