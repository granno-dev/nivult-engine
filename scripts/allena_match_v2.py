#!/usr/bin/env python3
"""Fine-tuning breve da match-v1, con checkpoint su validation e test finale."""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import time
from pathlib import Path

import torch
from torch.utils.data import DataLoader
from transformers import AutoModelForSequenceClassification, AutoTokenizer, get_cosine_schedule_with_warmup

from allena_match_v1 import Coppie, raggruppa
from metriche_match_v2 import cancello, metriche


def leggi(path: str) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(x) for x in f]


def punteggi(mod, tok, righe, dev, bs: int) -> dict[str, float]:
    out = {}
    mod.eval()
    with torch.no_grad():
        for i in range(0, len(righe), bs):
            gruppo = righe[i:i + bs]
            enc = tok([r["profilo"] for r in gruppo], [r["offerta"] for r in gruppo],
                      padding=True, truncation=True, max_length=512,
                      return_tensors="pt").to(dev)
            valori = (mod(**enc).logits[:, 0] * 100).tolist()
            for r, p in zip(gruppo, valori):
                out[r["pair_id"]] = round(max(0.0, min(100.0, p)), 1)
    return out


def chiave(m: dict) -> tuple:
    return (m["complessivo"]["buone"], -m["complessivo"]["scartate"],
            m["complessivo"]["spearman"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--train", required=True)
    ap.add_argument("--val", required=True)
    ap.add_argument("--test", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--init-da", default="/opt/nivult/modelli/match-v1")
    ap.add_argument("--out", required=True)
    ap.add_argument("--passi", type=int, default=200)
    ap.add_argument("--checkpoint-ogni", type=int, default=50)
    ap.add_argument("--bs", type=int, default=4)
    ap.add_argument("--lr", type=float, default=5e-6)
    ap.add_argument("--seme", type=int, default=42)
    args = ap.parse_args()
    random.seed(args.seme)
    torch.manual_seed(args.seme)
    train, validation, test = leggi(args.train), leggi(args.val), leggi(args.test)
    tok = AutoTokenizer.from_pretrained(args.init_da)
    mod = AutoModelForSequenceClassification.from_pretrained(args.init_da)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    mod.to(dev).train()
    dl = DataLoader(Coppie(train, tok, 512), batch_size=args.bs, shuffle=True,
                    collate_fn=lambda l: raggruppa(l, tok.pad_token_id), drop_last=True)
    ott = torch.optim.AdamW(mod.parameters(), lr=args.lr, weight_decay=0.01)
    sched = get_cosine_schedule_with_warmup(ott, int(args.passi * 0.05), args.passi)
    best = chiave(metriche(validation, punteggi(mod, tok, validation, dev, args.bs)))
    mod.train()
    print("baseline_validation", best, "device", dev, flush=True)
    t0, passo, salvato = time.time(), 0, False
    while passo < args.passi:
        for lotto in dl:
            lotto = {k: v.to(dev) for k, v in lotto.items()}
            perdita = mod(**lotto).loss
            perdita.backward()
            torch.nn.utils.clip_grad_norm_(mod.parameters(), 1.0)
            ott.step()
            sched.step()
            ott.zero_grad(set_to_none=True)
            passo += 1
            if passo % 25 == 0:
                print(f"passo {passo}/{args.passi} loss {perdita.item():.4f} "
                      f"{(time.time() - t0) / 60:.1f} min", flush=True)
            if passo % args.checkpoint_ogni == 0:
                corrente = chiave(metriche(validation, punteggi(mod, tok, validation, dev, args.bs)))
                print("validation", passo, corrente, flush=True)
                if corrente > best:
                    best = corrente
                    tmp = Path(args.out + ".tmp")
                    mod.save_pretrained(tmp)
                    tok.save_pretrained(tmp)
                    if os.path.exists(args.out):
                        os.replace(args.out, f"{args.out}.prev-{passo}")
                    os.replace(tmp, args.out)
                    salvato = True
                mod.train()
            if passo >= args.passi:
                break
    if not salvato:
        print("nessun checkpoint migliora la baseline in validation; candidato non idoneo")
        return 3

    mod = AutoModelForSequenceClassification.from_pretrained(args.out).to(dev)
    candidato = metriche(test, punteggi(mod, tok, test, dev, args.bs))
    with open(args.baseline, encoding="utf-8") as f:
        punteggi_base = {r["pair_id"]: r["score_v1"] for r in map(json.loads, f)}
    baseline = metriche(test, punteggi_base)
    passa, errori = cancello(baseline, candidato)
    rapporto = {"checkpoint": args.out, "passa": passa, "errori": errori,
                "baseline": baseline, "candidato": candidato}
    with open(args.out + "/test-v2.json", "w", encoding="utf-8") as f:
        json.dump(rapporto, f, ensure_ascii=False, indent=2)
    print(json.dumps(rapporto, ensure_ascii=False, indent=2), flush=True)
    return 0 if passa else 4


if __name__ == "__main__":
    raise SystemExit(main())
