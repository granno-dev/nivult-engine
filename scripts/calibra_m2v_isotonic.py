#!/usr/bin/env python3
"""Ricalibra gli scorer M2V esistenti senza riaddestrarli."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LinearRegression

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from allena_match_micro import _carica_punteggi, _report
from metriche_match_v2 import carica_banco_v3
from nivult.matching.m2v import M2VScorer
from nivult.matching.micro import guardrail


def _grezzi(scorer, righe):
    scorer.bias = 0.0
    out = {}
    gruppi = defaultdict(list)
    for r in righe:
        gruppi[r["profile_key"]].append(r)
    for gruppo in gruppi.values():
        scores = scorer.score_batch(gruppo[0]["profilo"], gruppo)
        for r, score in zip(gruppo, scores):
            out[r["pair_id"]] = score
    return np.array([out[r["pair_id"]] for r in righe])


def _applica(raw, righe, metodo, calibratore):
    pred = np.clip(calibratore(raw), 0, 100)
    return {r["pair_id"]: guardrail(float(score), r["profilo"], r)
            for r, score in zip(righe, pred)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "recalibration-report.json")
    args = ap.parse_args()
    tutti = carica_banco_v3(ROOT)
    validation = [r for r in tutti if r["split"] == "validation"]
    test = [r for r in tutti if r["split"] == "test"]
    y = np.array([r["score"] for r in validation], dtype=float)
    candidati = []
    percorsi = {"pair": ROOT / "model-pair", "combinato": ROOT / "model"}
    for nome, percorso in percorsi.items():
        scorer = M2VScorer(percorso)
        raw = _grezzi(scorer, validation)
        mediana = float(np.median(y - raw))
        affine = LinearRegression().fit(raw.reshape(-1, 1), y)
        calibratori = {
            "mediana": lambda x, m=mediana: x + m,
            "affine": lambda x, m=affine: m.predict(x.reshape(-1, 1)),
        }
        isotonic = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=100).fit(raw, y)
        calibratori["isotonic"] = isotonic.predict
        for metodo, calibratore in calibratori.items():
            punteggi = _applica(raw, validation, metodo, calibratore)
            report = _report(validation, punteggi)
            gate = report["gate"]["complessivo"]
            f1 = report["buone_70"]["f1"]
            candidati.append({"modello": nome, "metodo": metodo, "calibratore": calibratore,
                              "raw_pair": raw, "mae": gate["mae"], "f1": f1,
                              "validation": report})
            print(f"{nome}/{metodo}: MAE={gate['mae']:.3f} F1={f1:.3f}", flush=True)
    migliore_mae = min(c["mae"] for c in candidati)
    scelto = min((c for c in candidati if c["mae"] <= migliore_mae + 1),
                 key=lambda c: (-c["f1"], c["mae"]))
    raw_test = _grezzi(M2VScorer(percorsi[scelto["modello"]]), test)
    punteggi_test = _applica(raw_test, test, scelto["metodo"], scelto["calibratore"])
    glm = _carica_punteggi(ROOT / "score-glm.jsonl")
    maestro = _carica_punteggi(ROOT / "score-maestro.jsonl")
    report = {"scelto": {"modello": scelto["modello"], "metodo": scelto["metodo"]},
              "validation": scelto["validation"], "test": _report(test, punteggi_test)}
    comuni_test = [r for r in test if r["pair_id"] in glm]
    if comuni_test:
        report["glm_test_subset"] = {"n": len(comuni_test), "glm": _report(comuni_test, glm),
                                     "m2v": _report(comuni_test, punteggi_test)}
    if maestro:
        report["self_hosted_test"] = _report(test, maestro)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    args.out.with_name("recalibrated-scores.jsonl").write_text(
        "".join(json.dumps({"pair_id": r["pair_id"], "score": punteggi_test[r["pair_id"]]},
                          ensure_ascii=False) + "\n" for r in test))
    print(json.dumps({"scelto": report["scelto"],
                      "test": report["test"]["gate"]["complessivo"],
                      "test_70": report["test"]["buone_70"]}, ensure_ascii=False, indent=2))
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
