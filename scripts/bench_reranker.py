"""Banco dei reranker per il match CV×offerta (11/10/2026).

Domanda: un cross-encoder locale gratuito puo' sostituire GLM come
giudice del digest? Si misura l'accordo con i punteggi GLM sulle coppie
VERE esportate da scripts/esporta_coppie_match.py — mai su benchmark
altrui. Chi vince diventa la base del distillato.

Candidati:
  - BAAI/bge-reranker-v2-m3 (568M, MIT): il default multilingua
  - Qwen/Qwen3-Reranker-0.6B (Apache 2.0): il piu' forte della classe
    sui benchmark multilingua 2025

Misure: Spearman e Pearson contro lo score GLM (ordine e forma), AUC e
accuracy sulla decisione vera del digest (score >= soglia della riga).

Gira sull'N5 (torch ROCm) o sul Mac mini. Solo transformers, niente
FlagEmbedding: il bge ha la testa di classificazione e il Qwen3 si
legge dai logit yes/no, come da scheda del modello.

  python scripts/bench_reranker.py coppie_match.jsonl.gz [--max N]
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import time

import torch
from transformers import (AutoModelForCausalLM, AutoModelForSequenceClassification,
                          AutoTokenizer)

ISTRUZIONE = ("You are an expert recruiter judging whether THIS job posting "
              "deserves THIS candidate's time: could they do it, and would it "
              "be a sensible step for them? Score high when role and level fit. "
              "Never penalize what the ad does NOT say: ads are written in very "
              "different ways, and a short ad is not a worse match. Penalize "
              "only stated mismatches: a different profession, a far-off level, "
              "a language the candidate does not speak. The country is already "
              "chosen by the candidate's search: the city never matters, "
              "penalize only a different country. The candidate's target role "
              "matters more than their history; if they named a whole "
              "professional family, any role in that family at the right level "
              "is plausible.")


def carica(percorso: str, massimo: int | None) -> list[dict]:
    with gzip.open(percorso, "rt", encoding="utf-8") as f:
        righe = [json.loads(l) for l in f]
    return righe[:massimo] if massimo else righe


def spearman(a: list[float], b: list[float]) -> float:
    def ranghi(v):
        ordine = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, i in enumerate(ordine):
            r[i] = pos
        return r
    ra, rb = ranghi(a), ranghi(b)
    n = len(a)
    media = (n - 1) / 2
    num = sum((x - media) * (y - media) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - media) ** 2 for x in ra) *
                    sum((y - media) ** 2 for y in rb))
    return num / den if den else 0.0


def pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else 0.0


def auc(positivi: list[bool], punteggi: list[float]) -> float:
    coppie = sorted(zip(punteggi, positivi))
    pos = sum(positivi)
    neg = len(positivi) - pos
    if not pos or not neg:
        return float("nan")
    somma = 0.0
    for i, (_, p) in enumerate(coppie):
        if p:
            somma += i  # ranghi 0-based: somma dei ranghi dei positivi
    return (somma - pos * (pos - 1) / 2) / (pos * neg)


class BgeM3:
    nome = "bge-reranker-v2-m3"

    def __init__(self):
        self.tok = AutoTokenizer.from_pretrained("BAAI/bge-reranker-v2-m3")
        self.mod = AutoModelForSequenceClassification.from_pretrained(
            "BAAI/bge-reranker-v2-m3", dtype=torch.float32)
        self.mod.eval()

    @torch.no_grad()
    def punteggi(self, coppie: list[tuple[str, str]], batch: int = 16) -> list[float]:
        out = []
        for i in range(0, len(coppie), batch):
            lotto = coppie[i:i + batch]
            enc = self.tok([q for q, _ in lotto], [d for _, d in lotto],
                           padding=True, truncation=True, max_length=512,
                           return_tensors="pt")
            out += self.mod(**enc).logits[:, 0].tolist()
        return out


class Qwen3:
    """La scheda del modello: giudizio yes/no, il punteggio e' la
    probabilita' di "yes". Lo 0.6B e' un LM causale, non un classificatore."""
    nome = "Qwen3-Reranker-0.6B"
    SISTEMA = ("Judge whether the Document meets the requirements based on "
               "the Query and the Instruct provided. Note that the answer "
               "can only be \"yes\" or \"no\".")

    def __init__(self):
        self.tok = AutoTokenizer.from_pretrained("Qwen/Qwen3-Reranker-0.6B",
                                                 padding_side="left")
        self.mod = AutoModelForCausalLM.from_pretrained(
            "Qwen/Qwen3-Reranker-0.6B", dtype=torch.float32)
        self.mod.eval()
        self.no = self.tok.encode("no", add_special_tokens=False)[0]
        self.yes = self.tok.encode("yes", add_special_tokens=False)[0]

    @torch.no_grad()
    def punteggi(self, coppie: list[tuple[str, str]], batch: int = 8) -> list[float]:
        out = []
        for i in range(0, len(coppie), batch):
            lotto = coppie[i:i + batch]
            testi = []
            for q, d in lotto:
                msg = [{"role": "system", "content": self.SISTEMA},
                       {"role": "user", "content":
                        f"<Instruct>: {ISTRUZIONE}\n<Query>: {q}\n<Document>: {d}"}]
                testi.append(self.tok.apply_chat_template(
                    msg, tokenize=False, add_generation_prompt=True))
            enc = self.tok(testi, padding=True, truncation=True,
                           max_length=1024, return_tensors="pt")
            logit = self.mod(**enc).logits[:, -1, :]
            # logit diff, non softmax: la probabilita' satura a ~1 su tutto
            # (zero-shot, 11/10/2026) e la scala diventa inutilizzabile;
            # la differenza di logit conserva la graduazione
            out += (logit[:, self.yes] - logit[:, self.no]).float().tolist()
        return out


def miglior_f1(passati: list[bool], punteggi: list[float]) -> tuple[float, float]:
    """La soglia migliore sulle coppie stesse: i reranker non hanno la scala
    0-100 di GLM, confrontare soglie assolute sarebbe disonesto. La sweep
    dice quanto SEPARA il modello una volta tarata la sua scala."""
    migliore = (0.0, 0.0)
    for t in sorted(set(punteggi)):
        predetti = [p >= t for p in punteggi]
        tp = sum(1 for a, b in zip(predetti, passati) if a and b)
        fp = sum(1 for a, b in zip(predetti, passati) if a and not b)
        fn = sum(1 for a, b in zip(predetti, passati) if not a and b)
        if not tp:
            continue
        prec, rec = tp / (tp + fp), tp / (tp + fn)
        f1 = 2 * prec * rec / (prec + rec)
        acc = sum(1 for a, b in zip(predetti, passati) if a == b) / len(passati)
        if f1 > migliore[0]:
            migliore = (f1, acc)
    return migliore


def valuta(nome: str, glm: list[float],
           passati: list[bool], punteggi: list[float]) -> dict:
    f1, acc = miglior_f1(passati, punteggi)
    return {"modello": nome,
            "spearman": round(spearman(glm, punteggi), 3),
            "pearson": round(pearson(glm, punteggi), 3),
            "auc_passed": round(auc(passati, punteggi), 3),
            "best_f1": round(f1, 3),
            "accuracy_a_soglia_migliore": round(acc, 3)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("coppie")
    ap.add_argument("--max", type=int, default=None)
    ap.add_argument("--solo", choices=["bge", "qwen"], default=None)
    args = ap.parse_args()

    righe = carica(args.coppie, args.max)
    glm = [float(r["score"]) for r in righe]
    passati = [bool(r["passed"]) for r in righe]
    coppie = [(r["profilo"], r["offerta"]) for r in righe]
    print(f"coppie: {len(righe)} | passed GLM: {sum(passati)} "
          f"({100 * sum(passati) / len(righe):.0f}%)")

    modelli = []
    if args.solo != "qwen":
        modelli.append(BgeM3)
    if args.solo != "bge":
        modelli.append(Qwen3)

    resoconto = []
    for cls in modelli:
        t = time.time()
        m = cls()
        punteggi = m.punteggi(coppie)
        dur = time.time() - t
        r = valuta(m.nome, glm, passati, punteggi)
        r["secondi"] = round(dur, 1)
        r["coppie_al_secondo"] = round(len(coppie) / dur, 1)
        resoconto.append(r)
        print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
