#!/usr/bin/env python3
"""Addestra e valuta il regressore match-micro senza dipendenze runtime."""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from metriche_match_v2 import carica_banco, carica_banco_v3, metriche, metriche_ranking
from nivult.matching.micro import N_FEATURES, VERSIONE, MicroScorer, feature_vet, guardrail
from nivult.matching.micro_ruoli import RuoliMicro
from nivult.matching.retrieval import lingue_mancanti

ALPHA = (0.01, 0.1, 1.0, 10.0, 100.0, 1000.0)
SCALA = (0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)


def _chiave_offerta(riga: dict) -> tuple[str, str]:
    def pulisci(valore) -> str:
        return " ".join(str(valore or "").lower().split())
    return pulisci(riga.get("title")), pulisci(riga.get("organization") or riga.get("company"))


def _training_senza_perdite(v2: list[dict], v3: list[dict]) -> tuple[list[dict], int]:
    congelate = {_chiave_offerta(r) for r in v3 if r["split"] in {"validation", "test"}}
    train = [r for r in v2 if r["split"] == "train"]
    puliti = [r for r in train if _chiave_offerta(r) not in congelate]
    return puliti, len(train) - len(puliti)


def _matrice(righe: list[dict], media=None, scala=None, ruoli=None):
    denso = np.array([feature_vet(r["profilo"], r, ruoli)[0] for r in righe], dtype=np.float64)
    if media is None:
        media = denso.mean(axis=0)
        scala = denso.std(axis=0)
        scala = np.where(scala > 1e-8, scala, 1.0)
    x = np.zeros((len(righe), denso.shape[1] + N_FEATURES), dtype=np.float64)
    x[:, :denso.shape[1]] = (denso - media) / scala
    for i, riga in enumerate(righe):
        for indice, valore in feature_vet(riga["profilo"], riga, ruoli)[1]:
            x[i, denso.shape[1] + indice] = valore
    return x, media, scala


def _predizioni(righe: list[dict], grezzi: np.ndarray, bias: float) -> dict[str, float]:
    return {r["pair_id"]: guardrail(float(p) + bias, r["profilo"], r)
            for r, p in zip(righe, grezzi)}


def _a_soglia(righe: list[dict], punteggi: dict[str, float], soglia: float = 70) -> dict:
    tp = sum(r["label"] == "buona" and punteggi[r["pair_id"]] >= soglia for r in righe)
    fp = sum(r["label"] != "buona" and punteggi[r["pair_id"]] >= soglia for r in righe)
    fn = sum(r["label"] == "buona" and punteggi[r["pair_id"]] < soglia for r in righe)
    precisione = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precisione * recall / (precisione + recall) if precisione and recall else 0.0
    return {"soglia": soglia, "tp": tp, "fp": fp, "fn": fn,
            "precisione": precisione, "recall": recall, "f1": f1}


def _testo_offerta(riga: dict) -> str:
    return " ".join(str(riga.get(k) or "") for k in
                    ("title", "ai_requirements_summary", "ai_core_responsibilities"))


def _lingue_profilo(profilo: str) -> set[str]:
    return {riga.split("Lingua:", 1)[1].strip() for riga in profilo.splitlines()
            if "Lingua:" in riga}


def _vincoli(righe: list[dict], punteggi: dict[str, float]) -> dict:
    obbligatorie = [r for r in righe
                    if lingue_mancanti(_testo_offerta(r), _lingue_profilo(r["profilo"]))]
    stage = [r for r in righe if r.get("ai_experience_level") == "intern"]
    def misura(subset, limite):
        return {"n": len(subset),
                "entro_limite": sum(punteggi[r["pair_id"]] <= limite for r in subset),
                "mae": round(sum(abs(punteggi[r["pair_id"]] - r["score"]) for r in subset)
                             / len(subset), 4) if subset else None,
                "massimo": max((punteggi[r["pair_id"]] for r in subset), default=None)}
    return {"lingua_mancante": misura(obbligatorie, 45), "internship": misura(stage, 45)}


def _report(righe: list[dict], punteggi: dict[str, float]) -> dict:
    return {"gate": metriche(righe, punteggi),
            "ranking": metriche_ranking(righe, punteggi),
            "buone_70": _a_soglia(righe, punteggi),
            "vincoli": _vincoli(righe, punteggi)}


def _carica_punteggi(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    out = {}
    for linea in path.read_text().splitlines():
        riga = json.loads(linea)
        if "score" in riga:
            out[riga["pair_id"]] = float(riga["score"])
    return out


def _latenza(modello: MicroScorer, righe: list[dict]) -> dict:
    for r in righe[:10]:
        modello.score(r["profilo"], r)
    misure = []
    for _ in range(3):
        for r in righe:
            inizio = time.perf_counter_ns()
            modello.score(r["profilo"], r)
            misure.append((time.perf_counter_ns() - inizio) / 1_000_000)
    misure.sort()
    return {"n": len(misure), "p50_ms": round(misure[len(misure) // 2], 4),
            "p95_ms": round(misure[min(len(misure) - 1, math.ceil(len(misure) * .95) - 1)], 4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / ".release-staging/match-micro.json")
    ap.add_argument("--alpha", type=float, choices=ALPHA)
    ap.add_argument("--test", action="store_true", help="Valuta anche il test congelato, una sola volta")
    args = ap.parse_args()

    v2 = carica_banco(ROOT)
    v3 = carica_banco_v3(ROOT)
    train, esclusi = _training_senza_perdite(v2, v3)
    validation = [r for r in v3 if r["split"] == "validation"]
    test = [r for r in v3 if r["split"] == "test"]
    if not train or not validation:
        raise RuntimeError("training o validation vuoti")

    ruoli = RuoliMicro.da_esco(ROOT / "dati/esco/occupazioni.jsonl")
    x_train, media_denso, scala_denso = _matrice(train, ruoli=ruoli)
    x_val, _, _ = _matrice(validation, media_denso, scala_denso, ruoli)
    y = np.array([r["score"] for r in train], dtype=np.float64)
    media = float(y.mean())
    kernel = x_train @ x_train.T
    kernel_val = x_val @ x_train.T
    identita = np.eye(len(train))
    candidati = []
    for alpha in (args.alpha,) if args.alpha is not None else ALPHA:
        dual = np.linalg.solve(kernel + alpha * identita, y - media)
        grezzi_base = media + kernel_val @ dual
        for scala in SCALA:
            grezzi = scala * grezzi_base
            residui = np.array([r["score"] for r in validation]) - grezzi
            bias = float(np.median(residui))
            punteggi = _predizioni(validation, grezzi, bias)
            gate = metriche(validation, punteggi)["complessivo"]
            buone = _a_soglia(validation, punteggi)
            candidati.append({"alpha": alpha, "scala": scala, "bias": bias, "mae": gate["mae"],
                              "scartate_selezionate": gate["scartate"], "f1": buone["f1"],
                              "spearman": gate["spearman"], "dual": dual})
            print(f"alpha={alpha:g} scala={scala:g} bias={bias:.3f} MAE={gate['mae']:.3f} "
                  f"rho={gate['spearman']:.3f} F1={buone['f1']:.3f} "
                  f"scartate={gate['scartate']}", flush=True)
    migliore_mae = min(c["mae"] for c in candidati)
    scelto = min((c for c in candidati if c["mae"] <= migliore_mae + 0.1),
                 key=lambda c: (-c["f1"], -c["spearman"], c["mae"]))
    pesi = scelto["scala"] * (x_train.T @ scelto["dual"])
    modello_json = {
        "versione": VERSIONE, "n_features": N_FEATURES,
        "weights": [round(float(v), 8) for v in pesi],
        "media": round(scelto["scala"] * media, 8), "bias": round(scelto["bias"], 8),
        "dense_mean": [round(float(v), 8) for v in media_denso],
        "dense_std": [round(float(v), 8) for v in scala_denso],
        "alpha": scelto["alpha"], "scala": scelto["scala"],
        "train_n": len(train), "duplicati_esclusi": esclusi,
        "ruoli_z": ruoli.comprimi(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(modello_json, separators=(",", ":")) + "\n")
    scorer = MicroScorer(args.out)
    tutti = validation + (test if args.test else [])
    punteggi = {r["pair_id"]: scorer.score(r["profilo"], r) for r in tutti}
    score_path = args.out.with_name("score-micro.jsonl")
    score_path.write_text("".join(json.dumps({"pair_id": r["pair_id"], "split": r["split"],
                                               "score": punteggi[r["pair_id"]]},
                                              ensure_ascii=False) + "\n" for r in tutti))

    report = {"modello": str(args.out), "byte": args.out.stat().st_size,
              "alpha": scelto["alpha"], "bias": scelto["bias"],
              "train_n": len(train), "duplicati_esclusi": esclusi,
              "validation": _report(validation, punteggi),
              "latenza": _latenza(scorer, validation)}
    maestro = _carica_punteggi(ROOT / ".release-staging/score-maestro.jsonl")
    glm = _carica_punteggi(ROOT / ".release-staging/score-glm.jsonl")
    if maestro:
        report["self_hosted_validation"] = _report(validation, maestro)
    comuni = [r for r in tutti if r["pair_id"] in glm]
    if comuni:
        report["glm_subset"] = {"n": len(comuni), "glm": _report(comuni, glm),
                                "micro": _report(comuni, punteggi)}
        if maestro:
            report["glm_subset"]["self_hosted"] = _report(comuni, maestro)
    if args.test:
        report["test"] = _report(test, punteggi)
        if maestro:
            report["self_hosted_test"] = _report(test, maestro)
    report_path = args.out.with_name("match-micro-test-report.json" if args.test
                                     else "match-micro-validation-report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    sintesi = {"byte": report["byte"], "alpha": scelto["alpha"], "bias": scelto["bias"],
               "latenza": report["latenza"],
               "validation": report["validation"]["gate"]["complessivo"],
               "validation_buone_70": report["validation"]["buone_70"],
               "validation_vincoli": report["validation"]["vincoli"]}
    if args.test:
        sintesi["test"] = report["test"]["gate"]["complessivo"]
        sintesi["test_buone_70"] = report["test"]["buone_70"]
        sintesi["test_vincoli"] = report["test"]["vincoli"]
    print(json.dumps(sintesi, ensure_ascii=False, indent=2))
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
