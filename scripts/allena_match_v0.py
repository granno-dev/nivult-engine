#!/usr/bin/env python3
"""match-v0: il primo scorer del digest tutto nostro (05/10/2026).

Distilla GLM: 1.328 coppie (profilo, offerta) -> punteggio, nel formato
esatto del prompt che glm-5.2 vedeva (dati da allena_matcher_estrai.py).
Base mmBERT (la stessa di nivult-v1), regressione sullo score /100.
Si addestra sulla GPU del N5 in pochi minuti e poi vive su CPU — e' il
pezzo che spegne la bolletta GLM.

L'ESAME e' la parte che conta:
  - split PER OFFERTA (job_id), mai per riga: lo stesso annuncio in
    train e in valutazione misurerebbe la memoria, non il modello;
  - baseline a media (prevede sempre la media dei punteggi): se il
    modello non la batte di molto, non serve;
  - MAE sul punteggio e precision/recall/F1 sul confine passa/non-passa
    (la soglia e' PER RIGA, quella che l'utente aveva scelto).

    python scripts/allena_match_v0.py [matcher-v0.jsonl] [out_dir]
"""
from __future__ import annotations

import json
import os
import random
import sys

os.environ.setdefault("TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL", "1")

import torch
import torch.nn as nn

EPOCHE = int(os.environ.get("EPOCHE_MATCH", "6"))
LOTTO = 16
LR = 2e-5
MAX_LEN = 512        # profilo + offerta formato GLM: corti, mai oltre
SEME = 42


class Scorer(nn.Module):
    """mmBERT-base + media mascherata + DUE teste: punteggio (regressione)
    e passa/non-passa (classificazione). Solo-regressione non reggeva:
    con i 92 per cento di righe bocciate, la MSE tirava tutto verso la
    media e nessuna offerta superava mai la soglia (F1 0.00 al primo
    esame — misurato il 05/10/2026)."""

    def __init__(self, base: str, pos_weight: float = 10.0):
        super().__init__()
        from transformers import AutoModel
        self.enc = AutoModel.from_pretrained(base)
        n = self.enc.config.hidden_size
        self.testa_score = nn.Linear(n, 1)
        self.testa_pass = nn.Linear(n, 1)
        self.perdita_pass = nn.BCEWithLogitsLoss(
            pos_weight=torch.tensor(pos_weight))

    def forward(self, input_ids, attention_mask):
        h = self.enc(input_ids=input_ids,
                     attention_mask=attention_mask).last_hidden_state
        m = attention_mask.unsqueeze(-1).float()
        media = (h * m).sum(1) / m.sum(1).clamp(min=1e-6)
        return (self.testa_score(media).squeeze(-1),
                self.testa_pass(media).squeeze(-1))


def carica(percorso: str) -> list[dict]:
    righe = [json.loads(l) for l in open(percorso)]
    random.Random(SEME).shuffle(righe)
    return righe


def split_per_offerta(righe: list[dict], quota_val: float = 0.15):
    """Train/val per job_id: la stessa offerta non sta mai da entrambi
    i lati. Deterministico — md5 sul job_id: il primo giro usava hash(),
    che in Python cambia a ogni processo (05/10/2026)."""
    import hashlib
    def in_val(r):
        h = hashlib.md5(r["job_id"].encode()).hexdigest()
        return (int(h[:8], 16) % 1000) / 1000 < quota_val
    val = [r for r in righe if in_val(r)]
    train = [r for r in righe if not in_val(r)]
    return train, val


def main() -> int:
    percorso = sys.argv[1] if len(sys.argv) > 1 else "/opt/nivult/matcher-v0.jsonl"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "/opt/nivult/modelli/match-v0"
    from transformers import AutoTokenizer

    righe = carica(percorso)
    train, val = split_per_offerta(righe)
    media = sum(r["score"] for r in train) / len(train)
    mae_base = sum(abs(r["score"] - media) for r in val) / len(val)
    print(f"train {len(train)} | val {len(val)} | baseline-a-media MAE {mae_base:.1f}",
          flush=True)

    base = "/opt/nivult/modelli/nivult-v1"   # tokenizer mmBERT gia' in casa
    if not os.path.exists(f"{base}/tokenizer_config.json"):
        base = "jhu-clsp/mmBERT-base"
    tok = AutoTokenizer.from_pretrained(base)
    # i pesi della base NON stanno nella cartella di v1 (li' c'e' la testa
    # addestrata): l'encoder si scarica da HuggingFace, la rete ce l'ha
    device = "cuda" if torch.cuda.is_available() else "cpu"
    modello = Scorer("jhu-clsp/mmBERT-base").to(device)

    def batch(righe_b):
        testi = [f"[PROFILO]\n{r['profilo']}\n[OFFERTA]\n{r['offerta']}"
                 for r in righe_b]
        enc = tok(testi, truncation=True, max_length=MAX_LEN,
                  padding=True, return_tensors="pt").to(device)
        y = torch.tensor([r["score"] / 100 for r in righe_b],
                         dtype=torch.float32, device=device)
        yp = torch.tensor([1.0 if r["passato"] else 0.0 for r in righe_b],
                          dtype=torch.float32, device=device)
        return enc, y, yp

    opt = torch.optim.AdamW(modello.parameters(), lr=LR, weight_decay=0.01)
    perdita_score = nn.MSELoss()
    for epoca in range(EPOCHE):
        modello.train()
        random.Random(SEME + epoca).shuffle(train)
        tot = 0.0
        for i in range(0, len(train), LOTTO):
            enc, y, yp = batch(train[i:i + LOTTO])
            s_out, p_out = modello(**enc)
            l = perdita_score(s_out, y) + modello.perdita_pass(p_out, yp)
            opt.zero_grad(); l.backward(); opt.step()
            tot += float(l) * len(y)
        print(f"epoca {epoca + 1}/{EPOCHE} loss train {tot / len(train):.4f}",
              flush=True)

    # l'esame
    modello.eval()
    pred, vero, passato, soglie, prob = [], [], [], [], []
    with torch.no_grad():
        for i in range(0, len(val), LOTTO):
            enc, _, _ = batch(val[i:i + LOTTO])
            s_out, p_out = modello(**enc)
            pred += [float(x) * 100 for x in s_out]
            prob += [float(torch.sigmoid(x)) for x in p_out]
            vero += [r["score"] for r in val[i:i + LOTTO]]
            passato += [r["passato"] for r in val[i:i + LOTTO]]
            soglie += [r["soglia"] for r in val[i:i + LOTTO]]
    mae = sum(abs(p - v) for p, v in zip(pred, vero)) / len(pred)
    tp = sum(1 for p, s, w in zip(pred, soglie, passato) if p >= s and w)
    fp = sum(1 for p, s, w in zip(pred, soglie, passato) if p >= s and not w)
    fn = sum(1 for p, s, w in zip(pred, soglie, passato) if p < s and w)
    prec = tp / max(1, tp + fp)
    rec = tp / max(1, tp + fn)
    f1 = 2 * prec * rec / max(1e-9, prec + rec)
    # la testa binaria: cutoff 0.5, e' lei che decide il digest
    tp2 = sum(1 for p, w in zip(prob, passato) if p >= 0.5 and w)
    fp2 = sum(1 for p, w in zip(prob, passato) if p >= 0.5 and not w)
    fn2 = sum(1 for p, w in zip(prob, passato) if p < 0.5 and w)
    prec2 = tp2 / max(1, tp2 + fp2)
    rec2 = tp2 / max(1, tp2 + fn2)
    f1_2 = 2 * prec2 * rec2 / max(1e-9, prec2 + rec2)
    print(f"ESAME val ({len(val)} offerte mai viste):")
    print(f"  MAE punteggio {mae:.1f} (baseline {mae_base:.1f})")
    print(f"  regressione alla soglia: precision {prec:.2f} recall {rec:.2f} F1 {f1:.2f}")
    print(f"  testa binaria (cutoff 0.5): precision {prec2:.2f} recall {rec2:.2f} F1 {f1_2:.2f}")
    print(f"  predetti passati: {tp2 + fp2} su {sum(passato)} veri")

    os.makedirs(out_dir, exist_ok=True)
    torch.save(modello.state_dict(), f"{out_dir}/pesi.pt")
    json.dump({"base": "jhu-clsp/mmBERT-base", "max_len": MAX_LEN,
                   "train": len(train), "mae_val": round(mae, 2),
                   "f1_confine_regressione": round(f1, 3),
                   "f1_confine_binaria": round(f1_2, 3),
                   "addestrato_il": "2026-10-05"},
              open(f"{out_dir}/config-match.json", "w"), indent=1)
    print(f"salvato in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
