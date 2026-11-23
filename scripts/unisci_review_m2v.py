#!/usr/bin/env python3
"""Unisce e valida i blocchi di revisione automatica Model2Vec."""
from __future__ import annotations

import argparse
import glob
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=Path, default=ROOT / ".release-staging/m2v-pool.jsonl")
    ap.add_argument("--out", type=Path, default=ROOT / ".release-staging/m2v-silver.jsonl")
    ap.add_argument("--require-complete", action="store_true")
    args = ap.parse_args()
    pool = {r["case_number"]: r for r in map(json.loads, args.pool.read_text().splitlines())}
    revisioni = {}
    incompleti = []
    for path in sorted(glob.glob(str(ROOT / ".release-staging/m2v-review-output-*.jsonl"))):
        start = int(path.rsplit("-", 1)[1].split(".")[0])
        expected = set(range(start, start + 50))
        rows = []
        for linea in Path(path).read_text().splitlines():
            try:
                rows.append(json.loads(linea))
            except json.JSONDecodeError:
                incompleti.append({"file": path, "errore": "JSON non valido"})
        ids = {r.get("case_number") for r in rows}
        valid = len(rows) == 50 and ids == expected
        for r in rows:
            n = r.get("case_number")
            score = r.get("score")
            if pool.get(n, {}).get("pair_id") != r.get("pair_id"):
                valid = False
            if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
                valid = False
        if not valid:
            incompleti.append({"file": path, "righe": len(rows), "presenti": len(ids)})
            continue
        for r in rows:
            if r["case_number"] in revisioni:
                raise RuntimeError(f"caso duplicato: {r['case_number']}")
            revisioni[r["case_number"]] = r
    if incompleti:
        print("blocchi_saltati", json.dumps(incompleti, ensure_ascii=False), flush=True)
    mancanti = sorted(set(pool) - set(revisioni))
    if args.require_complete and mancanti:
        raise RuntimeError(f"revisione incompleta: mancano {len(mancanti)} casi")
    out = []
    for n in sorted(revisioni):
        r = dict(pool[n])
        score = revisioni[n]["score"]
        r.update({"score": score,
                  "label": "buona" if score >= 70 else "borderline" if score >= 50 else "scartata",
                  "reason": revisioni[n]["reason"], "teacher": "agent-review-v1"})
        out.append(r)
    args.out.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in out))
    meta = {"casi": len(out), "mancanti": len(mancanti),
            "split": dict(Counter(r["split"] for r in out)),
            "label": dict(Counter(r["label"] for r in out)),
            "selezione": dict(Counter(r["selezione"] for r in out)),
            "profili": dict(Counter(r["profile_key"] for r in out)),
            "case_mancanti": mancanti}
    args.out.with_name("m2v-silver-meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in meta.items() if k != "case_mancanti"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
