#!/usr/bin/env python3
"""Distilla BGE-M3 e addestra lo scorer di coppia Model2Vec su CPU."""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from nivult.matching.micro import feature_vet, guardrail
from nivult.matching.micro_ruoli import RuoliMicro
from nivult.matching.retrieval import documento_ruolo

VERSIONE = "m2v-match-1"


def _byte(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _job_text(riga: dict) -> str:
    return documento_ruolo(riga["job"])


def _vocabolario(tokenizer, silver: list[dict], massimo: int) -> list[str]:
    conteggi = Counter()
    for r in silver:
        conteggi.update(tokenizer.tokenize(r["profilo"]))
        conteggi.update(tokenizer.tokenize(_job_text(r)))
    return [token for token, _ in conteggi.most_common(massimo)]


def _frasi_esco(path: Path) -> list[str]:
    frasi = set()
    with path.open() as f:
        for linea in f:
            riga = json.loads(linea)
            for lingua in ("en", "it", "de", "fr", "sv"):
                valore = riga["preferred"].get(lingua)
                if valore:
                    frasi.add(valore)
    return sorted(frasi)


def _distilla(args, silver: list[dict], out: Path):
    from model2vec.distill.distillation import distill_from_model
    from skeletoken import TokenizerModel
    from transformers import AutoModel, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.base, use_fast=True)
    modello_token = TokenizerModel.from_transformers_tokenizer(tokenizer)
    mantieni = set(_vocabolario(tokenizer, silver, args.vocab))
    mantieni.update(tokenizer.all_special_tokens)
    mantieni.update({"<s>", "</s>", "<pad>", "<unk>", "<mask>", "<cls>", "<sep>"})
    rimuovi = sorted(set(modello_token.sorted_vocabulary) - mantieni)
    modello_token = modello_token.remove_tokens_from_vocabulary(rimuovi)
    tokenizer_compatto = modello_token.to_transformers()
    print(f"vocabolario tokenizer: {modello_token.vocabulary_size}; "
          f"rimossi {len(rimuovi)} token", flush=True)
    modello = AutoModel.from_pretrained(args.base)
    inizio = time.time()
    static = distill_from_model(
        model=modello,
        tokenizer=tokenizer_compatto,
        vocabulary=_frasi_esco(args.esco),
        device="cpu",
        pca_dims=args.dims,
        quantize_to="int8",
        vocabulary_quantization=args.clusters,
        pooling="mean",
    )
    static.save_pretrained(out)
    print(f"distillazione: {(time.time() - inizio) / 60:.1f} min; "
          f"artefatto {_byte(out) / 1e6:.3f} MB", flush=True)
    return static


def _base_features(static, ruoli: RuoliMicro, righe: list[dict]) -> np.ndarray:
    a = static.encode([r["profilo"] for r in righe])
    b = static.encode([_job_text(r) for r in righe])
    a = a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-12)
    b = b / np.maximum(np.linalg.norm(b, axis=1, keepdims=True), 1e-12)
    coseno = np.sum(a * b, axis=1, keepdims=True)
    denso = np.array([feature_vet(r["profilo"], r["job"], None)[0] for r in righe])
    return np.column_stack((coseno, a * b, np.abs(a - b), denso))


def _metriche(righe: list[dict], scores: np.ndarray) -> dict:
    errori = np.abs(np.array([r["score"] for r in righe]) - scores)
    tp = sum(r["label"] == "buona" and s >= 70 for r, s in zip(righe, scores))
    fp = sum(r["label"] != "buona" and s >= 70 for r, s in zip(righe, scores))
    fn = sum(r["label"] == "buona" and s < 70 for r, s in zip(righe, scores))
    precisione = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precisione * recall / (precisione + recall) if precisione + recall else 0.0
    return {"mae": float(errori.mean()), "precisione70": precisione,
            "recall70": recall, "f1_70": f1}


def _allena_testa(static, ruoli: RuoliMicro, train: list[dict], validation: list[dict],
                  out: Path) -> dict:
    from scipy import sparse
    from sklearn.linear_model import Ridge

    from nivult.matching.micro import N_FEATURES

    x_train = _base_features(static, ruoli, train)
    x_val = _base_features(static, ruoli, validation)
    media_feature = x_train.mean(axis=0)
    scala_feature = np.where(x_train.std(axis=0) > 1e-8, x_train.std(axis=0), 1.0)

    def matrice(righe, base):
        rows, cols, values = [], [], []
        for i, r in enumerate(righe):
            for indice, valore in feature_vet(r["profilo"], r["job"], None)[1]:
                rows.append(i)
                cols.append(indice)
                values.append(valore)
        hashed = sparse.csr_matrix((values, (rows, cols)), shape=(len(righe), N_FEATURES))
        denso = sparse.csr_matrix((base - media_feature) / scala_feature)
        return sparse.hstack((hashed, denso), format="csr")

    xt = matrice(train, x_train)
    xv = matrice(validation, x_val)
    y = np.array([r["score"] for r in train], dtype=float)
    y_val = np.array([r["score"] for r in validation], dtype=float)
    conteggi = Counter(r["label"] for r in train)
    regimi = {"uniforme": {k: 1 / len(conteggi) for k in conteggi},
              "positivo": {"buona": .65, "borderline": .20, "scartata": .15}}
    candidati = []
    for regime, quote in regimi.items():
        pesi = np.array([len(train) * quote[r["label"]] / conteggi[r["label"]] for r in train])
        for alpha in (0.1, 1.0, 10.0, 100.0, 1000.0):
            modello = Ridge(alpha=alpha, solver="lsqr", max_iter=5000, tol=1e-5)
            modello.fit(xt, y, sample_weight=pesi)
            base = modello.predict(xv)
            for scala in (1.0, 1.5, 2.0):
                grezzi = base * scala
                residui = y_val - grezzi
                bias_values = set(np.round(np.quantile(residui, np.linspace(0, 1, 11)), 3))
                bias_values.update(np.linspace(-40, 40, 17))
                for bias in bias_values:
                    scores = np.array([guardrail(float(s) + bias, r["profilo"], r["job"])
                                       for r, s in zip(validation, grezzi)])
                    misure = _metriche(validation, scores)
                    candidati.append({"regime": regime, "alpha": alpha, "scala": scala,
                                      "bias": float(bias), "modello": modello, **misure})
        migliore = min((c for c in candidati if c["regime"] == regime),
                       key=lambda c: (c["mae"], -c["f1_70"]))
        print(f"regime={regime} MAE={migliore['mae']:.3f} "
              f"F1={migliore['f1_70']:.3f}", flush=True)
    migliore_mae = min(c["mae"] for c in candidati)
    scelto = min((c for c in candidati if c["mae"] <= migliore_mae + 1.0),
                 key=lambda c: (-c["f1_70"], c["mae"]))
    modello = scelto["modello"]
    config = {"versione": VERSIONE, "modello_testa": "combinato", "n_sparse": N_FEATURES,
              "weights": [round(float(v), 7) for v in modello.coef_],
              "media": scelto["scala"] * float(modello.intercept_), "bias": scelto["bias"],
              "feature_mean": [round(float(v), 7) for v in media_feature],
              "feature_std": [round(float(v), 7) for v in scala_feature],
              "regime": scelto["regime"], "alpha": scelto["alpha"], "scala": scelto["scala"]}
    (out / "scoring.json").write_text(json.dumps(config, separators=(",", ":")) + "\n")
    for nome in ("regressor.pkl", "classifier.pkl"):
        (out / nome).unlink(missing_ok=True)
    return {"scelto": {k: v for k, v in scelto.items() if k != "modello"},
            "byte": _byte(out)}


def _quantizza(static):
    from model2vec.quantization import DType, quantize_embeddings
    static.embedding = quantize_embeddings(static.embedding, DType.Int8)
    return static


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--silver", type=Path, required=True)
    ap.add_argument("--esco", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--base", default="BAAI/bge-m3")
    ap.add_argument("--vocab", type=int, default=8000)
    ap.add_argument("--clusters", type=int, default=4096)
    ap.add_argument("--dims", type=int, default=64)
    ap.add_argument("--distill-only", action="store_true")
    ap.add_argument("--skip-distill", action="store_true")
    ap.add_argument("--skip-pair", action="store_true")
    ap.add_argument("--extra-train", type=Path)
    args = ap.parse_args()
    silver = [json.loads(x) for x in args.silver.read_text().splitlines() if x.strip()]
    train = [r for r in silver if r["split"] == "train"]
    if args.extra_train:
        extra = [json.loads(x) for x in args.extra_train.read_text().splitlines() if x.strip()]
        train.extend(extra)
        print(f"aggiunte {len(extra)} coppie umane di train", flush=True)
    validation = [r for r in silver if r["split"] == "validation"]
    if not train or not validation:
        raise RuntimeError("train o validation vuoti")
    args.out.mkdir(parents=True, exist_ok=True)
    if args.skip_distill:
        from model2vec import StaticModel
        static = StaticModel.from_pretrained(args.out)
    else:
        static = _distilla(args, silver, args.out)
    if args.distill_only:
        if _byte(args.out) > 5_000_000:
            raise RuntimeError(f"artefatto oltre 5 MB: {_byte(args.out)}")
        return 0
    if not args.skip_pair:
        from model2vec.train import StaticModelForPairSimilarity
        positivi = [r for r in train if r["label"] == "buona"]
        positivi_val = [r for r in validation if r["label"] == "buona"]
        if len(positivi) < 2 or len(positivi_val) < 2:
            raise RuntimeError("servono almeno due match buoni per train e validation")
        pair = StaticModelForPairSimilarity.from_static_model(
            model=static, max_length=256, n_layers=1, hidden_dim=128)
        pair.fit(text_a=[r["profilo"] for r in positivi],
                 text_b=[_job_text(r) for r in positivi],
                 text_a_val=[r["profilo"] for r in positivi_val],
                 text_b_val=[_job_text(r) for r in positivi_val],
                 batch_size=32, max_epochs=15, early_stopping_patience=3,
                 learning_rate=.001, device="cpu", temperature=.07, token_dropout=.05)
        static = pair.to_static_model()
    static = _quantizza(static)
    static.save_pretrained(args.out)
    if _byte(args.out) > 5_000_000:
        raise RuntimeError(f"artefatto oltre 5 MB: {_byte(args.out)}")
    ruoli = RuoliMicro.da_esco(args.esco)
    report = _allena_testa(static, ruoli, train, validation, args.out)
    (args.out / "training-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    if _byte(args.out) > 5_000_000:
        raise RuntimeError(f"artefatto finale oltre 5 MB: {_byte(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
