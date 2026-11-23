#!/usr/bin/env python3
"""Confronta i reranker locali sul golden, con test solo sul modello scelto."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
from sklearn.isotonic import IsotonicRegression

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bench_reranker
from allena_match_micro import _carica_punteggi, _report
from bench_reranker import BgeM3, MiniLM, Qwen3
from metriche_match_v2 import carica_banco_v3
from nivult.matching.micro import guardrail

ISTRUZIONE_NIVULT = (
    "You are an expert recruiter judging whether THIS job deserves THIS candidate's time. "
    "Score high when role and seniority fit and there is no explicit blocker. "
    "Do not penalize anything the ad does not say, and never invent requirements. "
    "The desired role matters more than past history; a different technology stack is transferable "
    "when role and level match. A move inside the same professional family is plausible. "
    "If the candidate accepts Europe, every European country is acceptable. "
    "A consultancy posting for an internal client role is not automatically an agency job; "
    "preferences such as 'no agency' are strong but not absolute. "
    "If an explicitly mandatory language is absent from the profile, score low even if the role "
    "matches perfectly. Never infer a language requirement from the ad's language or location. "
    "For internships, stages, alternance, apprenticeships, praktikum or working-student roles "
    "targeting a senior candidate, score low. Consider contract duration only when explicit."
)


def _raw_path(modello: str) -> Path:
    return ROOT / f"reranker-{modello}-validation.jsonl"


def _inferenza(nome: str, righe: list[dict], batch: int) -> tuple[np.ndarray, float]:
    if nome in {"qwen", "qwen4b"}:
        bench_reranker.ISTRUZIONE = ISTRUZIONE_NIVULT
    classe = Qwen3 if nome in {"qwen", "qwen4b"} else MiniLM if nome == "minilm" else BgeM3
    opzioni = {"model_name": "Qwen/Qwen3-Reranker-4B", "dtype": torch.bfloat16} \
        if nome == "qwen4b" else {}
    modello = classe(max_length=1024, **opzioni) if classe is Qwen3 else classe()
    inizio = time.time()
    grezzi = modello.punteggi([(r["profilo"], r["offerta"]) for r in righe], batch=batch)
    durata = time.time() - inizio
    return np.array(grezzi, dtype=float), durata


def _punteggi(raw: np.ndarray, righe: list[dict], calibratore) -> dict[str, float]:
    pred = np.clip(calibratore.predict(raw), 0, 100)
    return {r["pair_id"]: guardrail(float(score), r["profilo"], r)
            for r, score in zip(righe, pred)}


def _salva_raw(path: Path, righe: list[dict], raw: np.ndarray) -> None:
    path.write_text("".join(json.dumps({"pair_id": r["pair_id"], "raw": float(score)})
                            + "\n" for r, score in zip(righe, raw)))


def _leggi_raw(path: Path, righe: list[dict]) -> np.ndarray:
    valori = {r["pair_id"]: r["raw"] for r in map(json.loads, path.read_text().splitlines())}
    return np.array([valori[r["pair_id"]] for r in righe], dtype=float)


def _fit(raw: np.ndarray, righe: list[dict]):
    y = np.array([r["score"] for r in righe], dtype=float)
    return IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=100).fit(raw, y)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=("qwen", "bge", "minilm", "qwen4b"))
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    tutti = carica_banco_v3(ROOT)
    validation = [r for r in tutti if r["split"] == "validation"]
    if args.test:
        report_val = json.loads((ROOT / "reranker-validation-report.json").read_text())
        nome = args.model or report_val["scelto"]
        raw_val = _leggi_raw(_raw_path(nome), validation)
        calibratore = _fit(raw_val, validation)
        test = [r for r in tutti if r["split"] == "test"]
        raw, secondi = _inferenza(nome, test, args.batch)
        punteggi = _punteggi(raw, test, calibratore)
        glm = _carica_punteggi(ROOT / "score-glm.jsonl")
        maestro = _carica_punteggi(ROOT / "score-maestro.jsonl")
        report = {"modello": nome, "test": _report(test, punteggi),
                  "secondi": secondi, "ms_coppia": secondi * 1000 / len(test)}
        comuni = [r for r in test if r["pair_id"] in glm]
        if comuni:
            report["glm_subset"] = {"n": len(comuni), "glm": _report(comuni, glm),
                                    nome: _report(comuni, punteggi)}
        if maestro:
            report["self_hosted_test"] = _report(test, maestro)
        (ROOT / "reranker-test-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        _salva_raw(ROOT / f"reranker-{nome}-test.jsonl", test, raw)
        print(json.dumps({"modello": nome, "test": report["test"]["gate"]["complessivo"],
                          "test_70": report["test"]["buone_70"],
                          "ms_coppia": report["ms_coppia"]}, ensure_ascii=False, indent=2))
        return 0

    candidati = []
    for nome in ("qwen", "bge", "minilm", "qwen4b"):
        if args.model and nome != args.model:
            continue
        if args.resume and _raw_path(nome).exists():
            raw = _leggi_raw(_raw_path(nome), validation)
            secondi = 0.0
        else:
            raw, secondi = _inferenza(nome, validation, args.batch)
        _salva_raw(_raw_path(nome), validation, raw)
        calibratore = _fit(raw, validation)
        punteggi = _punteggi(raw, validation, calibratore)
        report = _report(validation, punteggi)
        gate = report["gate"]["complessivo"]
        f1 = report["buone_70"]["f1"]
        candidati.append({"modello": nome, "mae": gate["mae"], "f1": f1, "report": report})
        print(f"{nome}: MAE={gate['mae']:.3f} F1={f1:.3f} "
              f"{secondi * 1000 / len(validation):.1f} ms/coppia", flush=True)
    migliore_mae = min(c["mae"] for c in candidati)
    scelto = min((c for c in candidati if c["mae"] <= migliore_mae + 1),
                 key=lambda c: (-c["f1"], c["mae"]))
    out = {"scelto": scelto["modello"],
           "modelli": {c["modello"]: c["report"] for c in candidati}}
    (ROOT / "reranker-validation-report.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    print("modello_scelto", scelto["modello"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
