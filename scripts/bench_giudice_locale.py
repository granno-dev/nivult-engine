"""Banco del giudice locale per il match CV×offerta — Stadio A (11/10/2026).

Domanda decisiva per il B2C gratis: un LLM piccolo IN CASA, con la rubrica
vera di produzione, concorda con GLM quanto basta per sostituirlo?

A differenza del banco dei reranker (bench_reranker.py), qui il modello
riceve la prompt IDENTICA della produzione: RUBRICA + profilo + offerta,
una coppia per chiamata, JSON {"score", "reason"} — via valuta_offerta
stessa, con un ChatModel che gira su transformers invece che su z.ai.

Modello di partenza: Qwen3-4B-Instruct-2507 (non-thinking, segue il
formato, sta nell'iGPU dell'N5). Se promette, si misura anche l'8B.

  python scripts/bench_giudice_locale.py coppie_match.jsonl.gz [--max N]
      [--modello Qwen/Qwen3-4B-Instruct-2507] [--salva predizioni.jsonl]
"""
from __future__ import annotations

import argparse
import gzip
import json
import time

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from nivult.matching.llm import valuta_offerta
from bench_reranker import auc, miglior_f1, pearson, spearman  # stesso dir


class GiudiceLocale:
    """Un ChatModel senza rete: la chat completion la fa transformers.
    Stessa superficie di GLM (chat + last_usage), cosi' valuta_offerta
    non sa di non essere su z.ai."""

    def __init__(self, modello_hf: str):
        self.dev = "cuda" if torch.cuda.is_available() else "cpu"
        self.tok = AutoTokenizer.from_pretrained(modello_hf)
        self.mod = AutoModelForCausalLM.from_pretrained(
            modello_hf, dtype=torch.bfloat16).to(self.dev)
        self.mod.eval()
        self.last_usage: dict = {}

    @torch.no_grad()
    def chat(self, messages: list[dict], *, temperature: float = 0.0,
             max_tokens: int = 4000, extra: dict | None = None) -> str:
        testo = self.tok.apply_chat_template(messages, tokenize=False,
                                             add_generation_prompt=True)
        enc = self.tok(testo, return_tensors="pt").to(self.dev)
        t0 = time.time()
        out = self.mod.generate(**enc, max_new_tokens=min(max_tokens, 150),
                                do_sample=False)
        nuovi = out[0][enc["input_ids"].shape[1]:]
        self.last_usage = {"input": int(enc["input_ids"].shape[1]),
                           "output": int(nuovi.shape[0]),
                           "secondi": round(time.time() - t0, 2)}
        return self.tok.decode(nuovi, skip_special_tokens=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("coppie")
    ap.add_argument("--max", type=int, default=None)
    ap.add_argument("--modello", default="Qwen/Qwen3-4B-Instruct-2507")
    ap.add_argument("--salva", default=None,
                    help="JSONL con score GLM e score locale per l'esame")
    args = ap.parse_args()

    with gzip.open(args.coppie, "rt", encoding="utf-8") as f:
        righe = [json.loads(l) for l in f]
    if args.max:
        righe = righe[: args.max]
    print(f"coppie: {len(righe)} | modello: {args.modello} | "
          f"dispositivo: {'rocm' if torch.cuda.is_available() else 'cpu'}",
          flush=True)

    giudice = GiudiceLocale(args.modello)
    pred, glm, passati = [], [], []
    errori = 0
    salva = open(args.salva, "w", encoding="utf-8") if args.salva else None
    t = time.time()
    for i, r in enumerate(righe):
        try:
            score, reason, uso = valuta_offerta(
                giudice, r["profilo"], r["job"], lingua="Italiano")
        except Exception as e:                          # noqa: BLE001
            errori += 1
            print(f"[{i}] ERRORE: {str(e)[:120]}", flush=True)
            continue
        pred.append(float(score))
        glm.append(float(r["score"]))
        passati.append(bool(r["passed"]))
        if salva:
            salva.write(json.dumps({"match_id": r["match_id"],
                                    "glm": r["score"], "locale": score,
                                    "reason_locale": reason},
                                   ensure_ascii=False) + "\n")
        if (i + 1) % 50 == 0:
            v = len(pred)
            print(f"[{i+1}/{len(righe)}] rho parziale: "
                  f"{spearman(glm, pred):.3f} — {uso.get('secondi')}s/coppia",
                  flush=True)
    if salva:
        salva.close()

    f1, acc = miglior_f1(passati, pred)
    print(json.dumps({
        "modello": args.modello,
        "coppie_valutate": len(pred), "errori": errori,
        "spearman": round(spearman(glm, pred), 3),
        "pearson": round(pearson(glm, pred), 3),
        "mae": round(sum(abs(a - b) for a, b in zip(glm, pred)) / len(pred), 1),
        "auc_passed": round(auc(passati, pred), 3),
        "best_f1": round(f1, 3), "accuracy_a_soglia_migliore": round(acc, 3),
        "secondi_totali": round(time.time() - t, 1),
        "coppie_al_secondo": round(len(pred) / (time.time() - t), 2),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
