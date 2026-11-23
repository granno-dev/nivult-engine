#!/usr/bin/env python3
"""Recall del servizio v3 su golden arricchito con concorrenti reali."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from metriche_match_v2 import carica_banco_v3
from nivult.matching import prescore_v3
from nivult.matching.retrieval import lingue_mancanti

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--distrattori", default=str(ROOT / "dati/match-v3/distrattori.jsonl"))
    ap.add_argument("--motore", choices=("v1", "v3"), default="v3")
    ap.add_argument("--out", required=True)
    ap.add_argument("--scores", required=True)
    args = ap.parse_args()
    golden = carica_banco_v3(ROOT)
    distrattori = [json.loads(x) for x in Path(args.distrattori).read_text().split("\n")
                   if x.strip()]
    jobs = golden + distrattori
    per_profilo = {}
    for r in golden:
        per_profilo[r["profile_key"]] = r["profilo"]
    scores_path = Path(args.scores)
    scores = {}
    if scores_path.exists():
        scores = {r["pair_id"]: r["score"]
                  for r in map(json.loads, scores_path.read_text().splitlines())}
    config = json.loads((ROOT / "dati/match-v3/profili.json").read_text())
    for profilo, testo in per_profilo.items():
        gruppo = [r for r in jobs if r["profile_key"] == profilo]
        for i in range(0, len(gruppo), 16):
            lotto = gruppo[i:i + 16]
            nuovi = [r for r in lotto if r["pair_id"] not in scores]
            if nuovi:
                if args.motore == "v1":
                    from nivult.matching import prescore
                    from nivult.matching.llm import offerta_per_prompt
                    prompts = [offerta_per_prompt(dict(r, _wants=config[profilo]["wants"]))
                               for r in nuovi]
                    valori = prescore.prescore_batch(testo, prompts)
                else:
                    valori = prescore_v3.rank_batch(testo, nuovi)
                scores.update({r["pair_id"]: v for r, v in zip(nuovi, valori)})
            print(profilo, min(i + 16, len(gruppo)), "/", len(gruppo), flush=True)
        scores_path.write_text("".join(json.dumps({"pair_id": i, "score": scores[i]}) + "\n"
                                       for i in sorted(scores)))

    rapporto = {"jobs": len(jobs), "tagli": {}}
    for k in (30, 60, 70, 80, 100, 110, 120, 130, 140, 150, 300, 500):
        per = {}
        for profilo, testo in per_profilo.items():
            gruppo = [r for r in jobs if r["profile_key"] == profilo]
            lingue = set(re.findall(r"^\[\d+\] Lingua: (.+)$", testo, re.MULTILINE))
            eleggibili = []
            for r in gruppo:
                if lingue and lingue_mancanti(r.get("ai_requirements_summary") or "", lingue):
                    continue
                eleggibili.append(r)
            top = sorted(eleggibili, key=lambda r: (-scores[r["pair_id"]], r["pair_id"]))[:k]
            buone = sum(r.get("label") == "buona" for r in top)
            totali = sum(r.get("label") == "buona" for r in gruppo)
            per[profilo] = {"buone": buone, "totali": totali,
                            "recall": buone / totali if totali else None}
        totali = sum(p["totali"] for p in per.values())
        buone = sum(p["buone"] for p in per.values())
        rapporto["tagli"][str(k)] = {"per_profilo": per, "buone": buone,
                                     "totali": totali, "recall": buone / totali}
    gate = {}
    for profilo in per_profilo:
        ruolo = config[profilo]["profilo"]["ruolo"].strip().lower()
        cap = prescore_v3.CAP_PER_RUOLO.get(ruolo, prescore_v3.MAX_CANDIDATI)
        attese = rapporto["tagli"]["150"]["per_profilo"][profilo]["buone"]
        trovate = rapporto["tagli"][str(cap)]["per_profilo"][profilo]["buone"]
        gate[profilo] = {"cap": cap, "buone_top150": attese,
                         "buone_cap": trovate, "passa": trovate >= attese}
    rapporto["cap_gate"] = gate
    Path(args.out).write_text(json.dumps(rapporto, ensure_ascii=False, indent=2,
                                         allow_nan=False) + "\n")
    print(json.dumps(rapporto, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if all(g["passa"] for g in gate.values()) else 4


if __name__ == "__main__":
    raise SystemExit(main())
