#!/usr/bin/env python3
"""Prompt, baseline e split decontaminati per il fine-tuning match-v2."""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
from collections import Counter
from pathlib import Path

from metriche_match_v2 import carica_banco, metriche
from nivult.matching import prescore


def scrivi_gzip(path: str, righe: list[dict]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in righe:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def chiave_testo(offerta: str) -> str:
    titolo = re.search(r"\btitolo:\s*([^|]+)", offerta)
    azienda = re.search(r"\bazienda:\s*([^|]+)", offerta)
    return "|".join(x.group(1).strip().lower() if x else "" for x in (titolo, azienda))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--silver", default="/opt/nivult/match-train.jsonl.gz")
    ap.add_argument("--out", required=True)
    ap.add_argument("--val", required=True)
    ap.add_argument("--test", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--prescore-url", default="http://100.119.200.7:8092")
    args = ap.parse_args()
    righe = carica_banco()
    if len(righe) != 240:
        raise RuntimeError(f"banco incompleto: {len(righe)}/240 etichette")
    aziende_test = {(r.get("organization") or "").strip().lower()
                    for r in righe if r["profile_key"] == "hrbp"}
    forzati = 0
    for r in righe:
        org = (r.get("organization") or "").strip().lower()
        if r["profile_key"] != "hrbp" and org in aziende_test and r["split"] != "test":
            r["split"] = "test"
            forzati += 1
    print("coppie_spostate_in_test_per_azienda_HRBP", forzati)
    os.environ["PRESCORE_URL"] = args.prescore_url
    baseline = {}
    for chiave in sorted({r["profile_key"] for r in righe}):
        per_profilo = [r for r in righe if r["profile_key"] == chiave]
        for i in range(0, len(per_profilo), 30):
            gruppo = per_profilo[i:i + 30]
            punteggi = prescore.prescore_batch(
                gruppo[0]["profilo"], [r["offerta"] for r in gruppo])
            baseline.update((r["pair_id"], p) for r, p in zip(gruppo, punteggi))
    with open(args.baseline, "w", encoding="utf-8") as f:
        for r in righe:
            f.write(json.dumps({"pair_id": r["pair_id"],
                                "score_v1": baseline[r["pair_id"]]},
                               ensure_ascii=False) + "\n")
    print("baseline_metriche", json.dumps(metriche(righe, baseline), ensure_ascii=False))

    trattenuti = [r for r in righe if r["split"] in {"validation", "test"}]
    id_vietati = {r["job_id"] for r in trattenuti}
    chiavi_vietate = {chiave_testo(r["offerta"]) for r in trattenuti}
    with gzip.open(args.silver, "rt", encoding="utf-8") as f:
        silver = [json.loads(x) for x in f]
    puliti = []
    for r in silver:
        m = re.search(r"\bid:\s*([^|]+)", r["offerta"])
        job_id = m.group(1).strip() if m else None
        if job_id in id_vietati or chiave_testo(r["offerta"]) in chiavi_vietate:
            continue
        puliti.append(r)
    manuali = [r for r in righe if r["split"] == "train"]
    training = puliti + manuali * 3
    validation = [r for r in righe if r["split"] == "validation"]
    test = [r for r in righe if r["split"] == "test"]
    scrivi_gzip(args.out, training)
    scrivi_gzip(args.val, validation)
    scrivi_gzip(args.test, test)
    print("silver_iniziali", len(silver), "rimossi_per_decontaminazione", len(silver) - len(puliti))
    print("manuali_train", len(manuali), "ripetuti_tre_volte", len(manuali) * 3)
    print("training", len(training), "validation", len(validation), "test", len(test))
    print("test_per_profilo", dict(Counter(r["profile_key"] for r in test)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
