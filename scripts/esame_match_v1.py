"""Il plico d'esame del distillato: coppie mai viste dallo studente,
col suo punteggio. Chi esamina (umano o LLM) giudica alla cieca.

Lo split di validation si ricostruisce IDENTICO a allena_match_v1.py
(stesso seme, stesso shuffle): le coppie dell'esame non sono entrate
in training. Sopra, un campione stratificato per fascia di punteggio
dello studente: passa / forse / scarta.

  python scripts/esame_match_v1.py
"""
from __future__ import annotations

import gzip
import json
import random

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

TRAIN = "/opt/nivult/match-train.jsonl.gz"
MODELLO = "/opt/nivult/modelli/match-v1"
OUT = "/opt/nivult/esame-match-v1.jsonl"
SEME = 42


def split_val(righe):
    random.seed(SEME)
    random.shuffle(righe)
    utenti = sorted({r["user_id"] for r in righe})
    random.shuffle(utenti)
    train_utenti = set(utenti[: max(1, int(len(utenti) * 0.85))])
    return [r for r in righe if r["user_id"] not in train_utenti]


def main() -> int:
    with gzip.open(TRAIN, "rt", encoding="utf-8") as f:
        righe = [json.loads(l) for l in f]
    val = split_val(righe)
    print(f"coppie in validation (mai viste dallo studente): {len(val)}")

    tok = AutoTokenizer.from_pretrained(MODELLO)
    mod = AutoModelForSequenceClassification.from_pretrained(MODELLO)
    mod.eval()
    with torch.no_grad():
        for i in range(0, len(val), 8):
            lotto = val[i:i + 8]
            enc = tok([r["profilo"] for r in lotto],
                      [r["offerta"] for r in lotto], padding=True,
                      truncation=True, max_length=512, return_tensors="pt")
            punteggi = (mod(**enc).logits[:, 0] * 100).tolist()
            for r, p in zip(lotto, punteggi):
                r["score_studente"] = round(p, 1)

    # stratificato: 20 alti, 20 medi, 20 bassi secondo lo studente
    val.sort(key=lambda r: r["score_studente"])
    n = len(val)
    terzo = n // 3
    campione = (random.sample(val[:terzo], min(20, terzo))
                + random.sample(val[terzo:2 * terzo], min(20, terzo))
                + random.sample(val[2 * terzo:], min(20, n - 2 * terzo)))
    random.shuffle(campione)
    with open(OUT, "w", encoding="utf-8") as f:
        for r in campione:
            f.write(json.dumps({
                "profilo": r["profilo"], "offerta": r["offerta"],
                "score_studente": r["score_studente"],
                "score_maestro": r["score"], "fonte": r.get("fonte", "?"),
            }, ensure_ascii=False) + "\n")
    print(f"plico: {len(campione)} coppie -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
