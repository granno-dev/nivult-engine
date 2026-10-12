"""Il distillato del match: un cross-encoder da 0,6B che impara il giudice
(12/10/2026).

Studente: bge-reranker-v2-m3 con la testa di classificazione a 1 uscita,
regressione sullo score del maestro (0-100, normalizzato). Maestri, in
ordine di fiducia: GLM (le ancore di esporta_coppie_match.py) e Qwen3-4B
locale (le etichette di distilla_etichetta.py) — Stadio A: rho 0.73,
AUC 0.94 contro GLM.

Lo split e' PER UTENTE: lo stesso CV non sta mai su entrambi i lati,
altrimenti la validation misura la memoria e non il giudizio.

Stile: come addestra_tecnologie.py — loop a mano, niente Trainer.

  python allena_match_v1.py --train /opt/nivult/match/train.jsonl.gz \
      --out /opt/nivult/modelli/match-v1 --passi 2000 --bs 8
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import time

import torch
from torch.utils.data import DataLoader, Dataset
from transformers import (AutoModelForSequenceClassification, AutoTokenizer,
                          get_cosine_schedule_with_warmup)

BASE = "BAAI/bge-reranker-v2-m3"


class Coppie(Dataset):
    def __init__(self, righe, tok, max_len):
        self.r, self.tok, self.max_len = righe, tok, max_len

    def __len__(self):
        return len(self.r)

    def __getitem__(self, i):
        r = self.r[i]
        enc = self.tok(r["profilo"], r["offerta"], truncation=True,
                       max_length=self.max_len)
        # lo score del maestro normalizzato 0-1: la testa impara la scala,
        # in produzione si rimoltiplica per 100
        enc["labels"] = torch.tensor([r["score"] / 100.0], dtype=torch.float)
        return enc


def raggruppa(lotto, pad):
    n = max(len(x["input_ids"]) for x in lotto)
    ii = torch.full((len(lotto), n), pad, dtype=torch.long)
    am = torch.zeros((len(lotto), n), dtype=torch.long)
    ll = torch.zeros((len(lotto), 1))
    tt = torch.ones((len(lotto), n), dtype=torch.long)   # token_type: coppia
    for k, x in enumerate(lotto):
        m = len(x["input_ids"])
        ii[k, :m] = torch.tensor(x["input_ids"])
        am[k, :m] = torch.tensor(x["attention_mask"])
        ll[k] = x["labels"]
        if "token_type_ids" in x:
            tt[k, :m] = torch.tensor(x["token_type_ids"])
    return {"input_ids": ii, "attention_mask": am,
            "token_type_ids": tt, "labels": ll}


def spearman(a, b):
    def ranghi(v):
        o = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, i in enumerate(o):
            r[i] = pos
        return r
    ra, rb, n = ranghi(a), ranghi(b), len(a)
    media = (n - 1) / 2
    num = sum((x - media) * (y - media) for x, y in zip(ra, rb))
    den = (sum((x - media) ** 2 for x in ra) * sum((y - media) ** 2 for y in rb)) ** 0.5
    return num / den if den else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--passi", type=int, default=2000)
    ap.add_argument("--bs", type=int, default=8)
    ap.add_argument("--max-len", type=int, default=512)
    ap.add_argument("--lr", type=float, default=1e-5)
    ap.add_argument("--seme", type=int, default=42)
    args = ap.parse_args()
    random.seed(args.seme)
    torch.manual_seed(args.seme)

    with gzip.open(args.train, "rt", encoding="utf-8") as f:
        righe = [json.loads(l) for l in f]
    random.shuffle(righe)
    # lo split per utente: mai lo stesso CV su entrambi i lati
    utenti = sorted({r["user_id"] for r in righe})
    random.shuffle(utenti)
    taglio = max(1, int(len(utenti) * 0.85))
    train_utenti = set(utenti[:taglio])
    train = [r for r in righe if r["user_id"] in train_utenti]
    val = [r for r in righe if r["user_id"] not in train_utenti]
    print(f"coppie: {len(righe)} | train {len(train)} su {taglio} utenti | "
          f"val {len(val)} su {len(utenti) - taglio}")

    tok = AutoTokenizer.from_pretrained(BASE)
    mod = AutoModelForSequenceClassification.from_pretrained(BASE, num_labels=1)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    mod.to(dev).train()
    dl = DataLoader(Coppie(train, tok, args.max_len), batch_size=args.bs,
                    shuffle=True, collate_fn=lambda l: raggruppa(l, tok.pad_token_id),
                    drop_last=True)
    ott = torch.optim.AdamW(mod.parameters(), lr=args.lr, weight_decay=0.01)
    passi_epoca = max(1, len(dl))
    sched = get_cosine_schedule_with_warmup(ott, int(args.passi * 0.05),
                                            args.passi)
    t0, passo = time.time(), 0
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
            if passo % 100 == 0:
                print(f"passo {passo}/{args.passi} — loss {perdita.item():.4f} "
                      f"— {(time.time() - t0) / 60:.0f} min", flush=True)
            if passo >= args.passi:
                break

    # validation: spearman contro il maestro, sulle coppie mai viste
    mod.eval()
    veri, predetti = [], []
    with torch.no_grad():
        for i in range(0, len(val), args.bs):
            lotto = val[i:i + args.bs]
            enc = tok([r["profilo"] for r in lotto], [r["offerta"] for r in lotto],
                      padding=True, truncation=True, max_length=args.max_len,
                      return_tensors="pt").to(dev)
            predetti += (mod(**enc).logits[:, 0] * 100).tolist()
            veri += [r["score"] for r in lotto]
    if veri:
        print(f"VAL: spearman {spearman(veri, predetti):.3f} su "
              f"{len(veri)} coppie mai viste")
    mod.save_pretrained(args.out)
    tok.save_pretrained(args.out)
    print(f"salvato in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
