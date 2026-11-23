#!/usr/bin/env python3
"""Banco riproducibile match-v3: baseline, ranking, slice e cancello qualità."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path

from metriche_match_v2 import carica_banco, metriche, metriche_ranking, metriche_slice

ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _norm(testo: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", testo.lower()).strip()


def _chiavi(r: dict) -> tuple[str, str]:
    azienda = r.get("organization") or r.get("company") or ""
    return _norm(azienda), f"{_norm(azienda)}|{_norm(r.get('title') or '')}"


def errori_split(righe: list[dict]) -> list[str]:
    aziende, annunci = {}, {}
    for r in righe:
        azienda, annuncio = _chiavi(r)
        if azienda:
            aziende.setdefault(azienda, set()).add(r["split"])
        annunci.setdefault(annuncio, set()).add(r["split"])
    errori = [f"azienda in più split: {a}" for a, s in aziende.items() if len(s) > 1]
    errori += [f"annuncio normalizzato in più split: {a}"
               for a, s in annunci.items() if len(s) > 1]
    return sorted(errori)


def valuta(righe: list[dict], punteggi: dict[str, float]) -> dict:
    base = metriche(righe, punteggi)
    selezionate = base["complessivo"]["selezionate"]
    buone = base["complessivo"]["buone"]
    base["costo_per_buona"] = selezionate / buone if buone else None
    base["ranking"] = metriche_ranking(righe, punteggi)
    base["slice"] = metriche_slice(righe, punteggi)
    return base


def cancello_v3(baseline: dict, candidato: dict) -> list[str]:
    errori = []
    for profilo, b in baseline["per_profilo"].items():
        c = candidato["per_profilo"][profilo]
        if c["buone"] < b["buone"]:
            errori.append(f"{profilo}: buone {c['buone']} < baseline {b['buone']}")
    hrbp = candidato["per_profilo"].get("hrbp", {})
    if hrbp.get("buone", 0) < 12:
        errori.append(f"hrbp: buone {hrbp.get('buone', 0)} < 12")
    b = baseline["ranking"]["complessivo"]["precision@30"]
    c = candidato["ranking"]["complessivo"]["precision@30"]
    if c + 1e-12 < b:
        errori.append(f"precision@30 {c:.6f} < baseline {b:.6f}")
    return errori


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--predizioni", help="JSONL pair_id/score_v1 oppure pair_id/score")
    ap.add_argument("--out", help="rapporto JSON; senza questo argomento, solo stdout")
    args = ap.parse_args()

    righe = carica_banco(ROOT)
    ids = {r["pair_id"] for r in righe}
    baseline_path = ROOT / "dati/match-v2/baseline-v1.jsonl"
    baseline = {r["pair_id"]: float(r["score_v1"])
                for r in map(json.loads, baseline_path.read_text().splitlines())}
    if set(baseline) != ids or not all(math.isfinite(v) for v in baseline.values()):
        raise SystemExit("baseline v1 incompleta o non valida")

    rapporto = {
        "schema_version": "match-v3-bench-1",
        "dataset_sha256": {
            str(p.relative_to(ROOT)): _sha256(p) for p in (
                ROOT / "dati/match-v2/candidati.jsonl",
                ROOT / "dati/match-v2/profili.json",
                baseline_path,
                *sorted((ROOT / "dati/match-v2").glob("etichette-*.jsonl")),
                ROOT / "dati/match-benchmark/etichette-manuali.jsonl",
                ROOT / "profilo-esempio.json",
            )
        },
        "righe": len(righe),
        "errori_split": errori_split(righe),
        "baseline": {s: valuta([r for r in righe if r["split"] == s], baseline)
                     for s in ("train", "validation", "test")},
    }
    test = [r for r in righe if r["split"] == "test"]
    if rapporto["baseline"]["test"]["complessivo"]["buone"] != 21 or \
            rapporto["baseline"]["test"]["per_profilo"]["hrbp"]["buone"] != 12:
        raise SystemExit("baseline v1 non riprodotta: attese 21 buone, HRBP 12")

    if args.predizioni:
        predette = {}
        for r in map(json.loads, Path(args.predizioni).read_text().splitlines()):
            predette[r["pair_id"]] = float(r.get("score", r.get("score_v1")))
        if set(predette) != ids or not all(math.isfinite(v) for v in predette.values()):
            raise SystemExit("predizioni incomplete o non valide")
        candidato = valuta(test, predette)
        rapporto["candidato"] = candidato
        rapporto["errori_cancello"] = cancello_v3(rapporto["baseline"]["test"], candidato)
        rapporto["passa"] = not rapporto["errori_cancello"]
        rapporto["predizioni_per_coppia"] = [
            {"pair_id": r["pair_id"], "profile_key": r["profile_key"],
             "label": r["label"], "manuale": r["score"],
             "baseline": baseline[r["pair_id"]], "candidato": predette[r["pair_id"]]}
            for r in test]

    testo = json.dumps(rapporto, ensure_ascii=False, indent=2, allow_nan=False)
    if args.out:
        Path(args.out).write_text(testo + "\n")
    print(testo)
    return 0 if rapporto.get("passa", True) else 4


if __name__ == "__main__":
    raise SystemExit(main())
