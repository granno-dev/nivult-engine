#!/usr/bin/env python3
"""Combina risultati zero-shot match-v3 con RRF e valuta senza training."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from metriche_match_v2 import carica_banco, carica_banco_v3, metriche_ranking
from nivult.matching.retrieval import lingue_mancanti, ordina, rrf

TAGLI = (5, 10, 20, 30, 60, 100, 300, 500)


def _misure(righe, scores):
    return {split: metriche_ranking([r for r in righe if r["split"] == split],
                                    scores, tagli=TAGLI)
            for split in ("validation", "test")}


def _fondi(righe, componenti, viste):
    out = {}
    for profilo in sorted({r["profile_key"] for r in righe}):
        ids = [r["pair_id"] for r in righe if r["profile_key"] == profilo]
        rankings = {}
        for nome, modello, vista in componenti:
            if vista not in viste:
                continue
            scores = modello["scores"][vista]
            rankings[f"{nome}:{vista}"] = sorted(
                ids, key=lambda i: (-scores[i], i))
        out.update(rrf(rankings))
    return out


def _filtro_lingue(righe, scores):
    import re
    out = dict(scores)
    for r in righe:
        disponibili = set(re.findall(r"^\[\d+\] Lingua: (.+)$", r["profilo"], re.MULTILINE))
        if lingue_mancanti(r.get("ai_requirements_summary") or "", disponibili):
            out[r["pair_id"]] = -1e9
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--risultati", nargs="+", required=True)
    ap.add_argument("--dataset", choices=("v2", "v3"), default="v2")
    ap.add_argument("--filtro-lingue", action="store_true")
    ap.add_argument("--out", required=True)
    ap.add_argument("--predizioni-migliori")
    args = ap.parse_args()
    righe = carica_banco_v3() if args.dataset == "v3" else carica_banco()
    modelli = {}
    for path in args.risultati:
        with open(path, encoding="utf-8") as f:
            risultato = json.load(f)
        nome = risultato["model"]
        if nome in modelli:
            raise SystemExit(f"modello duplicato: {nome}")
        for vista, scores in risultato["scores"].items():
            if set(scores) != {r["pair_id"] for r in righe}:
                raise SystemExit(f"predizioni incomplete: {nome}/{vista}")
        modelli[nome] = risultato

    componenti = [(nome, modello, vista)
                  for nome, modello in modelli.items() for vista in modello["scores"]]
    candidati = {}
    for nome, modello, vista in componenti:
        candidati[f"{nome}:{vista}"] = modello["scores"][vista]
    for nome in modelli:
        propri = [(n, m, v) for n, m, v in componenti if n == nome]
        candidati[f"{nome}+viste"] = _fondi(righe, propri, {"full", "role", "skills"})
        candidati[f"{nome}+bm25-full"] = _fondi(
            righe, [(n, m, v) for n, m, v in componenti
                    if (n == nome and v == "full") or (n == "bm25" and v == "full")],
            {"full"})
    candidati["tutti+full"] = _fondi(righe, componenti, {"full"})
    candidati["tutti+role"] = _fondi(righe, componenti, {"role"})
    candidati["tutti+skills"] = _fondi(righe, componenti, {"skills"})
    candidati["tutti+viste"] = _fondi(righe, componenti, {"full", "role", "skills"})
    if args.filtro_lingue:
        candidati = {nome: _filtro_lingue(righe, scores)
                     for nome, scores in candidati.items()}

    rapporto = {"candidati": {}}
    for nome, scores in candidati.items():
        misure = _misure(righe, scores)
        rapporto["candidati"][nome] = misure
        v = misure["validation"]["complessivo"]
        t = misure["test"]["complessivo"]
        print(f"{nome:34s} val ndcg@10={v['ndcg@10']:.4f} "
              f"r@10={v['recall@10']:.4f} test ndcg@30={t['ndcg@30']:.4f} "
              f"r@30={t['recall@30']:.4f} p@30={t['precision@30']:.4f}")

    def chiave(item):
        nome, misure = item
        v = misure["validation"]["complessivo"]
        return (v["ndcg@10"], v["recall@10"], v["precision@10"], nome)

    classifica = sorted(rapporto["candidati"].items(), key=chiave, reverse=True)
    rapporto["classifica_validation"] = [nome for nome, _ in classifica]
    migliore = classifica[0][0]
    rapporto["migliore_validation"] = migliore
    Path(args.out).write_text(json.dumps(rapporto, ensure_ascii=False, indent=2,
                                         allow_nan=False) + "\n")
    if args.predizioni_migliori:
        scores = candidati[migliore]
        Path(args.predizioni_migliori).write_text("".join(
            json.dumps({"pair_id": i, "score": scores[i]}) + "\n"
            for i in ordina(scores)))
    print("migliore_validation", migliore)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
