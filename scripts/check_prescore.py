#!/usr/bin/env python3
"""Contratti di pre-scorer e guardrail famiglia, senza rete reale."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from nivult.matching import prescore as client

SPEC = importlib.util.spec_from_file_location(
    "servi_match_v1", ROOT / "scripts" / "servi_match_v1.py")
servizio = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(servizio)

PASSATI: list[str] = []
FALLITI: list[str] = []


def check(nome: str, condizione: bool) -> None:
    (PASSATI if condizione else FALLITI).append(nome)
    print(("  ok    " if condizione else "  FAIL  ") + nome)


class Risposta:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def main() -> int:
    for valore, atteso in ((-0.1, 0.0), (0.0, 0.0), (0.5, 50.0),
                           (1.0, 100.0), (1.476, 100.0)):
        check(f"servizio limita {valore} a {atteso}",
              servizio._punteggio_contratto(valore) == atteso)
    for valore in (float("nan"), float("inf"), float("-inf")):
        try:
            servizio._punteggio_contratto(valore)
        except ValueError:
            check(f"servizio rifiuta {valore}", True)
        else:
            check(f"servizio rifiuta {valore}", False)

    with mock.patch.dict(os.environ, {"PRESCORE_URL": "http://prescore.test"}):
        with mock.patch.object(client.httpx, "post",
                               return_value=Risposta({"scores": [50.0]})):
            check("client accetta un punteggio valido",
                  client.prescore_batch("profilo", ["offerta"]) == [50.0])
        for valore in ([147.6], [-1.0], [float("nan")], ["non-numero"], [True]):
            with mock.patch.object(client.httpx, "post",
                                   return_value=Risposta({"scores": valore})):
                try:
                    client.prescore_batch("profilo", ["offerta"])
                except (RuntimeError, TypeError):
                    check(f"client rifiuta {valore}", True)
                else:
                    check(f"client rifiuta {valore}", False)
        with mock.patch.object(client.httpx, "post",
                               return_value=Risposta({"scores": []})):
            try:
                client.prescore_batch("profilo", ["offerta"])
            except RuntimeError:
                check("client verifica il numero di risultati", True)
            else:
                check("client verifica il numero di risultati", False)
    seleziona = client.seleziona_per_glm
    check("oltre 30 plausibili: solo i migliori 30",
          seleziona(list(range(50, 90)), [70] * 40, max_items=30)
          == list(range(10, 40)))
    check("i candidati fuori fascia non fanno backfill",
          seleziona([90, 20, 80, 10, 70, 0], [70] * 6, max_items=4)
          == [0, 2, 4])
    check("meno di 30 totali non riempie con offerte deboli",
          seleziona([95, 10, 85], [70, 70, 70], max_items=30) == [0, 2])
    check("tutti fuori fascia: nessuna valutazione",
          seleziona([49, 0, 20], [70, 70, 70], max_items=30) == [])
    check("le soglie restano per candidato",
          seleziona([49, 50, 49], [70, 70, 70], max_items=1) == [1])
    check("i pareggi sono deterministici",
          seleziona([95] * 35, [70] * 35, max_items=30) == list(range(30)))
    check("input vuoto", seleziona([], [], max_items=30) == [])
    check("cap zero non seleziona niente",
          seleziona([100, 100], [70, 70], max_items=0) == [])
    check("cap negativo non seleziona niente",
          seleziona([100], [70], max_items=-1) == [])

    scores = {f"famiglia-{i}": 0.0 for i in range(30)}
    scores.update({"Human Resources": 0.1, "Security & Safety": 0.8,
                   "Environmental & Sustainability": 0.1})
    assert len(scores) == 33 and abs(sum(scores.values()) - 1.0) < 1e-9
    offerta = {"title": "HR Manager", "cities": ["Milano", "Roma"],
               "ai_requirements_summary": "Gestione HR"}
    with mock.patch.dict(os.environ, {"PRESCORE_URL": "http://prescore.test",
                                      "FAMILY_GUARD": "1"}):
        check("il guardrail si attiva solo esplicitamente",
              client.family_guard_disponibile() is True)
        with mock.patch.object(client.httpx, "post",
                               return_value=Risposta({"predictions": [{"scores": scores}]})) as post:
            check("client accetta una distribuzione di famiglia valida",
                  client.famiglia_batch([offerta]) == [scores])
            check("il formatter famiglia resta sul servizio",
                  post.call_args.kwargs["json"]["jobs"] == [{
                      "title": "HR Manager", "location": "Milano, Roma",
                      "description": "Gestione HR"}])
        for valore in ({}, {**scores, "Security & Safety": True},
                       {**scores, "Security & Safety": float("nan")},
                       {**scores, "Security & Safety": 0.1}):
            with mock.patch.object(client.httpx, "post",
                                   return_value=Risposta({"predictions": [{"scores": valore}]})):
                try:
                    client.famiglia_batch([offerta])
                except (RuntimeError, TypeError):
                    check("client rifiuta una distribuzione malformata", True)
                else:
                    check("client rifiuta una distribuzione malformata", False)
        with mock.patch.object(client.httpx, "post",
                               return_value=Risposta({"predictions": []})):
            try:
                client.famiglia_batch([offerta])
            except RuntimeError:
                check("client verifica il numero delle distribuzioni", True)
            else:
                check("client verifica il numero delle distribuzioni", False)
    with mock.patch.dict(os.environ, {"PRESCORE_URL": "http://prescore.test",
                                      "FAMILY_GUARD": "0"}):
        check("guardrail spento di default",
              client.family_guard_disponibile() is False)
    check("HR scarta solo il gruppo incompatibile sopra soglia",
          client.incompatibile_hr(["Human Resources"], {
              "Security & Safety": 0.7, "Environmental & Sustainability": 0.25}) is True)
    check("HR adiacente a Legal non viene scartata",
          client.incompatibile_hr(["Human Resources"], {"Legal": 0.99}) is False)
    check("una seconda famiglia eleggibile impedisce il guardrail HR",
          client.incompatibile_hr(["Human Resources", "Engineering"],
                                  {"Security & Safety": 1.0}) is False)
    check("gruppo incompatibile ambiguo conservato",
          client.incompatibile_hr(["Human Resources"], {
              "Security & Safety": 0.7, "Environmental & Sustainability": 0.19}) is False)

    print(f"\n{len(PASSATI)} ok, {len(FALLITI)} falliti")
    return 1 if FALLITI else 0


if __name__ == "__main__":
    raise SystemExit(main())
