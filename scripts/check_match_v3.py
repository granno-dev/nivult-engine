#!/usr/bin/env python3
"""Contratti del retrieval ibrido e del banco match-v3, senza rete o ML."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from banco_match_v3 import errori_split, valuta
from metriche_match_v2 import carica_banco, metriche_ranking, spearman
from nivult.matching import prescore_v3
from nivult.matching.retrieval import (
    BM25,
    lingue_mancanti,
    lingue_obbligatorie,
    media_geometrica,
    min_max,
    ordina,
    prompt_reranker,
    rrf,
    tokenizza,
)

SPEC = importlib.util.spec_from_file_location(
    "servi_match_v3", ROOT / "scripts" / "servi_match_v3.py")
servizio = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(servizio)

PASSATI = 0
FALLITI = 0


def check(nome: str, condizione: bool) -> None:
    global PASSATI, FALLITI
    if condizione:
        PASSATI += 1
        print("  ok   ", nome)
    else:
        FALLITI += 1
        print("  FAIL ", nome)


def solleva(nome: str, fn, eccezione=ValueError) -> None:
    try:
        fn()
    except eccezione:
        check(nome, True)
    else:
        check(nome, False)


class Risposta:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def main() -> int:
    punteggi = rrf({"dense": ["a", "b", "c"], "sparse": ["b", "a", "d"]})
    check("RRF somma le prove indipendenti",
          abs(punteggi["a"] - (1 / 61 + 1 / 62)) < 1e-12)
    check("RRF tie-break deterministico", ordina(punteggi) == ["a", "b", "c", "d"])
    check("RRF limita senza riordinare in modo instabile", ordina(punteggi, limite=2) == ["a", "b"])
    check("RRF input vuoto", rrf({}) == {})
    check("RRF vista vuota", rrf({"dense": []}) == {})
    solleva("RRF rifiuta duplicati", lambda: rrf({"dense": ["a", "a"]}))
    solleva("RRF rifiuta peso NaN", lambda: rrf({"dense": ["a"]}, pesi={"dense": float("nan")}))
    solleva("RRF rifiuta ID vuoto", lambda: rrf({"dense": [""]}))
    solleva("ordinamento rifiuta NaN", lambda: ordina({"a": float("nan")}))
    solleva("cap negativo rifiutato", lambda: ordina({"a": 1.0}, limite=-1))

    check("min-max normalizza", min_max({"a": 10.0, "b": 20.0}) == {"a": 0.0, "b": 1.0})
    check("vista costante non aggiunge evidenza", min_max({"a": 4.0, "b": 4.0}) == {"a": 0.0, "b": 0.0})
    check("min-max input vuoto", min_max({}) == {})
    solleva("min-max rifiuta infinito", lambda: min_max({"a": float("inf")}))
    fusione = media_geometrica({"dense": {"a": 1.0, "b": 0.0},
                                "sparse": {"a": 1.0, "b": 0.0}})
    check("media geometrica premia accordo", fusione["a"] == 1.0 and fusione["b"] < 1e-5)
    check("media geometrica input vuoto", media_geometrica({}) == {})

    bm25 = BM25({"hr": "HR Business Partner, persone e organizzazione",
                 "tech": "Python backend API PostgreSQL"})
    risultato = bm25.punteggi("hr business")
    check("BM25 recupera il documento pertinente", risultato["hr"] > risultato["tech"] > 0 or
          (risultato["hr"] > 0 and risultato["tech"] == 0))
    check("BM25 query senza termini utili", bm25.punteggi("inexistente") == {"hr": 0.0, "tech": 0.0})
    check("BM25 input vuoto", BM25({}).punteggi("hr") == {})
    check("tokenizer mantiene tecnologie", tokenizza("C++ C# PostgreSQL") == ["c++", "c#", "postgresql"])
    check("lingua obbligatoria inglese",
          lingue_obbligatorie("Languages Required: Swedish (Mandatory) & English")
          == {"Swedish", "English"})
    check("lingua obbligatoria tedesca",
          lingue_obbligatorie("Sehr gute Deutsch- und Englischkenntnisse")
          == {"German", "English"})
    check("lingua comunicazione obbligatoria",
          lingue_obbligatorie("Kommunikationsstark auf Deutsch und Englisch")
          == {"German", "English"})
    check("lingua mancante nel profilo",
          lingue_mancanti("Language Requirement: Swedish (Mandatory)",
                          {"Italian", "English"}) == {"Swedish"})
    check("riferimenti geografici non sono requisiti",
          lingue_obbligatorie("German business, Swedish DeepTech, French Tech") == set())
    check("lingua solo preferita non è obbligatoria",
          lingue_obbligatorie("Preferred Language: English") == set())

    check("Spearman con parità", abs(spearman([1, 1, 2], [10, 10, 20]) - 1.0) < 1e-12)
    check("Spearman inverso", abs(spearman([1, 1, 2], [20, 20, 10]) + 1.0) < 1e-12)
    solleva("Spearman lunghezze diverse", lambda: spearman([1], [1, 2]))

    righe = [
        {"pair_id": "p:a", "profile_key": "p", "label": "buona"},
        {"pair_id": "p:b", "profile_key": "p", "label": "scartata"},
        {"pair_id": "q:a", "profile_key": "q", "label": "borderline"},
        {"pair_id": "q:b", "profile_key": "q", "label": "scartata"},
    ]
    punteggi = {"p:a": 2.0, "p:b": 1.0, "q:a": 2.0, "q:b": 1.0}
    ranking = metriche_ranking(righe, punteggi, tagli=(1,))
    check("recall@K calcolata per query", ranking["per_profilo"]["p"]["recall@1"] == 1.0)
    check("precision@K calcolata per query", ranking["complessivo"]["precision@1"] == 0.5)
    check("nDCG usa borderline come rilevanza parziale", ranking["per_profilo"]["q"]["ndcg@1"] == 1.0)

    golden = carica_banco(ROOT)
    check("banco completo", len(golden) == 240)
    check("split aziende e titoli normalizzati disgiunti", errori_split(golden) == [])
    test = [r for r in golden if r["split"] == "test"]
    baseline = {r["pair_id"]: float(r["score_studente"]) for r in test if r["profile_key"] == "hrbp"}
    import json
    for r in map(json.loads, (ROOT / "dati/match-v2/baseline-v1.jsonl").read_text().splitlines()):
        if r["pair_id"] not in baseline:
            baseline[r["pair_id"]] = float(r["score_v1"])
    misure = valuta(test, baseline)
    check("baseline HRBP 12/12", misure["per_profilo"]["hrbp"]["buone"] == 12)
    check("baseline complessiva 21/21", misure["complessivo"]["buone"] == 21)
    check("costo per offerta buona", abs(misure["costo_per_buona"] - 78 / 21) < 1e-12)
    check("slice paese presenti", "IT" in misure["slice"]["per_paese"])
    check("slice fonte presenti", "linkedin" in misure["slice"]["per_fonte"])
    from metriche_match_v2 import carica_banco_v3
    holdout = carica_banco_v3(ROOT)
    check("holdout v3 congelato e completo", len(holdout) == 256)
    check("holdout v3 contiene profilo unseen",
          any(r["profile_key"] == "senior-data-scientist" for r in holdout))
    from calibra_match_v3 import selezione
    calibrata = selezione(righe, punteggi, 1.5, cap=1)
    check("selezione calibrata rispetta cap e soglia",
          calibrata["complessivo"]["selezionate"] == 2
          and calibrata["complessivo"]["buone"] == 1
          and calibrata["complessivo"]["scartate"] == 0)
    prompt = prompt_reranker("system", "instruction", "HRBP", "HR Manager")
    check("prompt reranker contiene query e documento",
          "<Query>: HRBP\n<Document>: HR Manager<|im_end|>" in prompt)

    check("cap adattivo disattivato resta 150", prescore_v3.cap_adattivo("hrbp") == 150)
    with mock.patch.dict("os.environ", {"PRESCORE_V3_ADAPTIVE": "1"}):
        check("cap HRBP non perde recall rispetto a top 150",
              prescore_v3.cap_adattivo("HR Business Partner") == 150)
        check("cap backend validato senza perdita rispetto a top 150",
              prescore_v3.cap_adattivo("Senior Backend Software Engineer") == 100)
        check("cap backend alias", prescore_v3.cap_adattivo("Senior Backend Engineer") == 100)
        check("cap ruolo sconosciuto conservativo", prescore_v3.cap_adattivo("Nuovo ruolo") == 150)
        check("cap ruolo assente conservativo", prescore_v3.cap_adattivo(None) == 150)
    with mock.patch.dict("os.environ", {"PRESCORE_V3_ADAPTIVE": "0"}):
        check("cap HRBP disattivato resta 150",
              prescore_v3.cap_adattivo("HR Business Partner") == 150)

    offerte_v3 = [
        {"title": "a", "ai_requirements_summary": ""},
        {"title": "b", "ai_requirements_summary": "Language Requirement: Swedish (Mandatory)"},
        {"title": "c", "ai_requirements_summary": ""},
        {"title": "d", "ai_requirements_summary": ""},
    ]
    check("retrieval v3 esclude solo la lingua mancante",
          prescore_v3.seleziona_top([0.5, 0.9, 0.8, 0.7], offerte_v3,
                                    {"Italian", "English"}, max_items=2) == [2, 3])
    check("retrieval v3 non esclude se il profilo non dichiara lingue",
          prescore_v3.seleziona_top([0.5, 0.9, 0.8, 0.7], offerte_v3,
                                    set(), max_items=2) == [1, 2])
    check("retrieval v3 cap zero", prescore_v3.seleziona_top(
        [1.0], offerte_v3[:1], {"English"}, max_items=0) == [])
    check("servizio accetta lista vuota",
          servizio._input_rank({"profile": "profilo", "jobs": []}) == ("profilo", []))
    solleva("servizio rifiuta profilo vuoto", lambda: servizio._input_rank({"jobs": []}))
    solleva("servizio rifiuta troppi job",
            lambda: servizio._input_rank({"profile": "p", "jobs": [{}] * 513}))
    with mock.patch.dict("os.environ", {"PRESCORE_V3_URL": "http://retrieval.test"}):
        with mock.patch.object(prescore_v3.httpx, "post",
                               return_value=Risposta({"scores": [0.25]})):
            check("client retrieval accetta coseno valido",
                  prescore_v3.rank_batch("profilo", [{"title": "HR"}]) == [0.25])
        def due_lotti(*args, **kwargs):
            return Risposta({"scores": [0.1] * len(kwargs["json"]["jobs"])})
        with mock.patch.object(prescore_v3.httpx, "post", side_effect=due_lotti) as post:
            risultato = prescore_v3.rank_batch("profilo", [{"title": "HR"}] * 300)
            check("client retrieval divide oltre 256 offerte",
                  len(risultato) == 300 and post.call_count == 2)
        with mock.patch.object(prescore_v3.httpx, "post",
                               return_value=Risposta({"scores": [2.0]})):
            solleva("client retrieval rifiuta score fuori scala",
                    lambda: prescore_v3.rank_batch("profilo", [{"title": "HR"}]),
                    RuntimeError)

    print(f"\n{PASSATI} ok, {FALLITI} falliti")
    return 1 if FALLITI else 0


if __name__ == "__main__":
    raise SystemExit(main())
