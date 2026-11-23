#!/usr/bin/env python3
"""Valuta lo scorer M2V sul golden congelato e contro GLM/self-hosted."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from allena_match_micro import _carica_punteggi, _latenza, _report
from metriche_match_v2 import carica_banco_v3
from nivult.matching.m2v import M2VScorer


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=Path, default=ROOT / "model")
    ap.add_argument("--out", type=Path, default=ROOT / "evaluation.json")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    righe = carica_banco_v3(ROOT)
    if not args.test:
        righe = [r for r in righe if r["split"] == "validation"]
    scorer = M2VScorer(args.model)
    punteggi = {}
    gruppi = defaultdict(list)
    for r in righe:
        gruppi[r["profile_key"]].append(r)
    for gruppo in gruppi.values():
        profilo = gruppo[0]["profilo"]
        scores = scorer.score_batch(profilo, gruppo)
        for r, score in zip(gruppo, scores):
            punteggi[r["pair_id"]] = score
    report = {"byte": sum(p.stat().st_size for p in args.model.rglob("*") if p.is_file()),
              "latenza": _latenza(scorer, righe)}
    for split in sorted({r["split"] for r in righe}):
        subset = [r for r in righe if r["split"] == split]
        report[split] = _report(subset, punteggi)
        maestro = _carica_punteggi(ROOT / "score-maestro.jsonl")
        if maestro:
            report[f"self_hosted_{split}"] = _report(subset, maestro)
    glm = _carica_punteggi(ROOT / "score-glm.jsonl")
    comuni = [r for r in righe if r["pair_id"] in glm]
    if comuni:
        report["glm_subset"] = {"n": len(comuni), "glm": _report(comuni, glm),
                                "m2v": _report(comuni, punteggi)}
        maestro = _carica_punteggi(ROOT / "score-maestro.jsonl")
        if maestro:
            report["glm_subset"]["self_hosted"] = _report(comuni, maestro)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    args.out.with_name("evaluation-scores.jsonl").write_text(
        "".join(json.dumps({"pair_id": r["pair_id"], "split": r["split"],
                            "score": punteggi[r["pair_id"]]}, ensure_ascii=False) + "\n"
                for r in righe))
    sintesi = {"byte": report["byte"], "latenza": report["latenza"]}
    for split in sorted({r["split"] for r in righe}):
        sintesi[split] = report[split]["gate"]["complessivo"]
    if comuni:
        sintesi["glm"] = report["glm_subset"]["glm"]["gate"]["complessivo"]
        sintesi["m2v_su_glm"] = report["glm_subset"]["m2v"]["gate"]["complessivo"]
    print(json.dumps(sintesi, ensure_ascii=False, indent=2))
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
