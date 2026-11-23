#!/usr/bin/env python3
"""Calibra una soglia sul validation e la valuta una sola volta sul test."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from metriche_match_v2 import carica_banco, carica_banco_v3, metriche_ranking


def _carica_scores(path: str, campo: str | None = None,
                   vista: str = "full") -> dict[str, float]:
    testo = Path(path).read_text()
    if testo.lstrip().startswith("{"):
        try:
            dati = json.loads(testo)
        except json.JSONDecodeError:
            pass
        else:
            scores = dati["scores"]
            return {i: float(v) for i, v in scores.get(vista, scores).items()}
    out = {}
    for r in map(json.loads, testo.splitlines()):
        chiave = campo or ("score" if "score" in r else "score_v1")
        out[r["pair_id"]] = float(r[chiave])
    return out


def _limita_preselettore(righe: list[dict], scores: dict[str, float],
                         preselettore: dict[str, float], top: int) -> dict[str, float]:
    ammessi = set()
    for profilo in {r["profile_key"] for r in righe}:
        gruppo = [r for r in righe if r["profile_key"] == profilo]
        ordinati = sorted(gruppo, key=lambda r: (-preselettore[r["pair_id"]], r["pair_id"]))
        ammessi.update(r["pair_id"] for r in ordinati[:top])
    return {i: v if i in ammessi else -1e9 for i, v in scores.items()}


def selezione(righe: list[dict], scores: dict[str, float], soglia: float,
              cap: int = 30) -> dict:
    per_profilo = {}
    for profilo in sorted({r["profile_key"] for r in righe}):
        gruppo = [r for r in righe if r["profile_key"] == profilo]
        ordinati = sorted(gruppo, key=lambda r: (-scores[r["pair_id"]], r["pair_id"]))
        scelti = [r for r in ordinati if scores[r["pair_id"]] >= soglia][:cap]
        conteggi = Counter(r["label"] for r in scelti)
        totali = sum(r["label"] == "buona" for r in gruppo)
        per_profilo[profilo] = {
            "selezionate": len(scelti), "buone": conteggi["buona"],
            "borderline": conteggi["borderline"], "scartate": conteggi["scartata"],
            "buone_totali": totali,
            "recall": conteggi["buona"] / totali if totali else None,
            "precision": conteggi["buona"] / len(scelti) if scelti else None,
        }
    gruppi = list(per_profilo.values())
    tot = {k: sum(g[k] for g in gruppi) for k in
           ("selezionate", "buone", "borderline", "scartate", "buone_totali")}
    tot["recall"] = tot["buone"] / tot["buone_totali"] if tot["buone_totali"] else None
    tot["precision"] = tot["buone"] / tot["selezionate"] if tot["selezionate"] else None
    return {"per_profilo": per_profilo, "complessivo": tot}


def _chiave(m: dict) -> tuple:
    profili = m["per_profilo"].values()
    recall_min = min((p["recall"] for p in profili if p["recall"] is not None),
                     default=1.0)
    c = m["complessivo"]
    return (recall_min, c["recall"], -c["scartate"], c["precision"])


def _chiave_robusta(righe: list[dict], scores: dict[str, float], soglia: float) -> tuple:
    gruppi = {i: [] for i in range(3)}
    for r in righe:
        azienda = r.get("organization") or r.get("company") or r["job_id"]
        digest = hashlib.md5(azienda.lower().encode()).hexdigest()
        gruppi[int(digest[:8], 16) % 3].append(r)
    per_fold = [selezione(gruppo, scores, soglia) for gruppo in gruppi.values()]
    recall_fold = [p["recall"] for f in per_fold for p in f["per_profilo"].values()
                   if p["recall"] is not None]
    complessiva = selezione(righe, scores, soglia)
    return (min(recall_fold, default=1.0), *_chiave(complessiva))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scores", required=True)
    ap.add_argument("--campo")
    ap.add_argument("--preselettore")
    ap.add_argument("--vista", default="full", choices=("full", "role", "skills"))
    ap.add_argument("--top-pre", type=int, default=30)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--dataset", choices=("v2", "v3"), default="v3")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    righe = carica_banco_v3() if args.dataset == "v3" else carica_banco()
    scores = _carica_scores(args.scores, args.campo, args.vista)
    baseline = _carica_scores(args.baseline)
    ids = {r["pair_id"] for r in righe}
    if set(scores) != ids or set(baseline) != ids:
        raise SystemExit("punteggi non allineati al banco")
    if args.preselettore:
        if args.top_pre <= 0:
            raise SystemExit("--top-pre deve essere positivo")
        preselettore = _carica_scores(args.preselettore, vista=args.vista)
        if set(preselettore) != ids:
            raise SystemExit("preselettore non allineato al banco")
        scores = _limita_preselettore(righe, scores, preselettore, args.top_pre)

    validation = [r for r in righe if r["split"] == "validation"]
    test = [r for r in righe if r["split"] == "test"]
    prove = []
    for soglia in range(-10, 101, 5):
        misure = selezione(validation, scores, soglia)
        prove.append((soglia, _chiave_robusta(validation, scores, soglia), misure))
    soglia, _, misure_validation = max(prove, key=lambda x: x[1])
    candidato = selezione(test, scores, soglia)
    base = selezione(test, baseline, 50)
    finale = selezione(test, scores, 70)
    finale_base = selezione(test, baseline, 70)
    errori = []
    for profilo, b in base["per_profilo"].items():
        c = candidato["per_profilo"][profilo]
        if c["buone"] < b["buone"]:
            errori.append(f"{profilo}: buone {c['buone']} < baseline {b['buone']}")
        if finale["per_profilo"][profilo]["buone"] < \
                finale_base["per_profilo"][profilo]["buone"]:
            errori.append(f"{profilo}: buone nel digest finale inferiori alla baseline")
    if candidato["complessivo"]["precision"] < base["complessivo"]["precision"]:
        errori.append("precision finale inferiore alla baseline")
    if args.dataset == "v2" and candidato["per_profilo"]["hrbp"]["buone"] < 12:
        errori.append("HRBP sotto 12/12")
    rapporto = {
        "soglia_scelta_validation": soglia,
        "validation": misure_validation,
        "test_candidato": candidato,
        "test_baseline": base,
        "digest_finale_70": finale,
        "digest_finale_baseline_70": finale_base,
        "ranking_candidato": metriche_ranking(test, scores),
        "errori": errori,
        "passa": not errori,
        "valutazioni_locali": candidato["complessivo"]["selezionate"],
        "valutazioni_baseline": base["complessivo"]["selezionate"],
        "costo_esterno": 0,
    }
    Path(args.out).write_text(json.dumps(rapporto, ensure_ascii=False, indent=2,
                                         allow_nan=False) + "\n")
    print(json.dumps(rapporto, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if not errori else 4


if __name__ == "__main__":
    raise SystemExit(main())
