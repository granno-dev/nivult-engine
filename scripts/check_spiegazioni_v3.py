#!/usr/bin/env python3
"""Contratti delle spiegazioni factual match-v3, senza rete."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nivult.matching.spiegazioni import evidenze

PASSATI = 0
FALLITI = 0


def check(nome, condizione):
    global PASSATI, FALLITI
    if condizione:
        PASSATI += 1
        print("  ok   ", nome)
    else:
        FALLITI += 1
        print("  FAIL ", nome)


def main() -> int:
    profilo = {"ruolo": "Senior Backend Software Engineer", "seniority": "5-10",
               "competenze": ["Python", "SQL", "HRIS"], "lingue": ["Italian", "English"]}
    offerta = {"title": "Senior Backend Developer", "ai_experience_level": "senior",
               "ai_key_skills": ["python", "Docker"],
               "ai_requirements_summary": "Language Requirement: Swedish (Mandatory)"}
    e = evidenze(profilo, offerta)
    check("ruolo riportato, non dedotto", e["ruolo"]["cercato"] == profilo["ruolo"])
    check("titolo offerta riportato", e["ruolo"]["offerta"] == offerta["title"])
    check("seniority riportata", e["seniority"]["offerta"] == "senior")
    check("solo competenze esplicite in comune", e["competenze_comuni"] == ["python"])
    check("requisito assente riportato", e["requisiti_non_nel_profilo"] == ["docker"])
    check("lingua mancante riportata", e["lingue_mancanti"] == ["Swedish"])
    check("nessuna competenza inventata dal testo libero", "fastapi" not in e["competenze_comuni"])

    vuoto = evidenze({}, {"title": "Offerta"})
    check("campi nulli gestiti", vuoto["competenze_comuni"] == []
          and vuoto["requisiti_non_nel_profilo"] == []
          and vuoto["lingue_mancanti"] == [])
    check("nessuna lingua mancante senza requisiti", vuoto["lingue_mancanti"] == [])
    print(f"\n{PASSATI} ok, {FALLITI} falliti")
    return 1 if FALLITI else 0


if __name__ == "__main__":
    raise SystemExit(main())
