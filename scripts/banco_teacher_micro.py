#!/usr/bin/env python3
"""Misura il teacher locale 35B su un piccolo campione prima del training."""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from metriche_match_v2 import carica_banco_v3, metriche
from nivult.matching.llm import (
    RUBRICA,
    Scrittore,
    _estrai_json,
    _score_valido,
    _testa,
    _uso_modello,
    offerta_per_prompt,
)
from nivult.matching.micro import _campo, _lista, guardrail
from nivult.matching.retrieval import lingue_mancanti

VINCOLI = """

CHECKLIST PRIORITARIA, da applicare prima del punteggio:
1. Se l'annuncio richiede esplicitamente una lingua che il profilo non dichiara,
   il punteggio deve essere AL MASSIMO 45, anche se ruolo e seniority sono perfetti.
   Riconoscere il requisito nella motivazione senza abbassare il voto è un errore.
   Non dedurre MAI un requisito linguistico dalla lingua dell'annuncio o dalla sede.
2. Se l'offerta è internship, stage, tirocinio, alternance, apprenticeship o
   praktikum e il candidato è senior (5+ anni), il punteggio deve essere AL MASSIMO 45.
   Usa tutta la fascia 0-45 in base alla coerenza del ruolo: non assegnare 45 automaticamente.
3. Questi limiti prevalgono su ogni altra valutazione positiva.
4. Se il profilo accetta l'Europa, ogni paese europeo è una sede accettata: non penalizzarla.
5. Un annuncio pubblicato da una società di consulenza per un ruolo interno presso
   un suo cliente non è automaticamente un lavoro in agenzia. Valuta il lavoro reale.
6. Considera contratto e durata quando sono espliciti, senza inventare penalità.
7. Se ruolo e livello coincidono, una tecnologia, un framework o un linguaggio di
   programmazione diversi sono competenze trasferibili, NON un gap. Non penalizzare
   Python rispetto a Go, Java o altri stack equivalenti senza un motivo di ruolo reale.
"""


def valuta_teacher(modello, profilo: str, offerta: dict, desiderio: str | None = None):
    testo = " ".join(str(offerta.get(k) or "") for k in
                     ("title", "ai_requirements_summary", "ai_core_responsibilities"))
    lingue = set(_lista(profilo, "Lingua"))
    mancanti = lingue_mancanti(testo, lingue)
    paesi = offerta.get("countries") or ([offerta["country"]] if offerta.get("country") else [])
    controllo = f"""

VERIFICHE MECCANICHE GIÀ ESEGUITE, non reinterpretarle:
- Lingue dichiarate nel profilo: {', '.join(sorted(lingue)) or 'non disponibili'}.
- Lingue obbligatorie mancanti rilevate: {', '.join(sorted(mancanti)) or 'nessuna'}.
  Se è scritto 'nessuna', puoi citare un requisito solo se riporti una frase testuale
  esplicita dell'annuncio; non dedurlo da lingua del testo, paese o nazionalità dell'azienda.
- Sedi accettate dal profilo: {_campo(profilo, ('Sedi accettate',)) or 'non disponibili'}.
- Paesi dell'offerta: {', '.join(map(str, paesi)) or 'non disponibili'}.
- Seniority dichiarata dall'offerta: {offerta.get('ai_experience_level') or 'non disponibile'}.
"""
    corpo = _testa(profilo, RUBRICA.format(lingua="English") + VINCOLI + controllo) + [{
        "role": "user", "content": offerta_per_prompt(offerta, desiderio)}]
    risposta = modello.chat(corpo, max_tokens=120)
    uso = _uso_modello(modello)
    payload = _estrai_json(risposta)
    raw = _score_valido(payload, modello)
    score = guardrail(raw, profilo, offerta)
    reason = str(payload.get("reason") or "")[:400].strip() or "—"
    return score, reason, uso, raw


def _carica(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    out = {}
    for linea in path.read_text().splitlines():
        riga = json.loads(linea)
        if "score" in riga:
            out[riga["pair_id"]] = riga
    return out


def _seleziona(righe: list[dict], glm: dict[str, dict], limite: int) -> list[dict]:
    per_id = {r["pair_id"]: r for r in righe}
    scelti = [per_id[pair_id] for pair_id in sorted(glm) if pair_id in per_id]
    scelti = scelti[:limite]
    ids = {r["pair_id"] for r in scelti}
    restanti = [r for r in righe if r["pair_id"] not in ids]
    random.Random(42).shuffle(restanti)
    for r in restanti:
        if len(scelti) >= limite:
            break
        if r.get("ai_experience_level") == "intern" or "German required" in (
                r.get("ai_requirements_summary") or ""):
            scelti.append(r)
            ids.add(r["pair_id"])
    gruppi = defaultdict(list)
    for r in restanti:
        if r["pair_id"] not in ids:
            gruppi[(r["profile_key"], r["label"])].append(r)
    while len(scelti) < limite and any(gruppi.values()):
        for chiave in sorted(gruppi):
            if gruppi[chiave] and len(scelti) < limite:
                r = gruppi[chiave].pop()
                scelti.append(r)
                ids.add(r["pair_id"])
    return scelti


def _salva(path: Path, predette: dict[str, dict], errori: list[dict]) -> None:
    with path.open("w") as f:
        for pair_id in sorted(predette):
            f.write(json.dumps(predette[pair_id], ensure_ascii=False) + "\n")
        for errore in errori:
            f.write(json.dumps(errore, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path,
                    default=ROOT / ".release-staging/score-teacher35-v3.jsonl")
    ap.add_argument("--url", default="http://100.119.200.7:8091/v1")
    ap.add_argument("--limit", type=int, default=64)
    args = ap.parse_args()
    righe = carica_banco_v3(ROOT)
    glm = _carica(ROOT / ".release-staging/score-glm.jsonl")
    maestro = _carica(ROOT / ".release-staging/score-maestro.jsonl")
    scelti = _seleziona(righe, glm, args.limit)
    predette = _carica(args.out)
    errori = []
    import os
    os.environ["SCRITTORE_URL"] = args.url
    profili = json.loads((ROOT / "dati/match-v3/profili.json").read_text())
    with Scrittore(rate_per_second=2.0) as modello:
        for n, riga in enumerate(scelti, 1):
            if riga["pair_id"] in predette:
                continue
            try:
                desiderio = profili[riga["profile_key"]]["wants"]
                score, reason, _, raw = valuta_teacher(
                    modello, riga["profilo"], dict(riga, _wants=desiderio), desiderio)
                predette[riga["pair_id"]] = {
                    "pair_id": riga["pair_id"], "score": score, "score_raw": raw,
                    "reason": reason, "model": modello.model,
                }
            except Exception as exc:  # noqa: BLE001
                errori.append({"pair_id": riga["pair_id"], "error": str(exc)[:300]})
            _salva(args.out, predette, errori)
            print(f"teacher35 {n}/{len(scelti)} errori={len(errori)}", flush=True)
    print("ERRORI", json.dumps(errori, ensure_ascii=False))
    for nome, punteggi in (("teacher35", predette), ("glm", glm), ("self-hosted", maestro)):
        disponibili = [r for r in scelti if r["pair_id"] in punteggi]
        scores = {r["pair_id"]: float(punteggi[r["pair_id"]]["score"]) for r in disponibili}
        if disponibili:
            print(nome, "n", len(disponibili),
                  json.dumps(metriche(disponibili, scores)["complessivo"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
