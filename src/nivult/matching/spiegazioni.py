"""Evidenza factual per il dettaglio match; nessuna deduzione dal testo libero."""
from __future__ import annotations

import re

from nivult.matching.retrieval import lingue_mancanti


def _norm(testo: str) -> str:
    return re.sub(r"[^a-z0-9à-ÿ]+", " ", testo.casefold()).strip()


def _skill(valori) -> set[str]:
    return {_norm(v) for v in valori or [] if isinstance(v, str) and _norm(v)}


def evidenze(profilo: dict, offerta: dict) -> dict:
    profilo_skill = _skill(profilo.get("competenze"))
    offerta_skill = _skill(offerta.get("ai_key_skills"))
    lingue = set(profilo.get("lingue") or [])
    return {
        "ruolo": {
            "cercato": profilo.get("ruolo"),
            "offerta": offerta.get("title"),
        },
        "seniority": {
            "profilo": profilo.get("seniority"),
            "offerta": offerta.get("ai_experience_level"),
        },
        "competenze_comuni": sorted(profilo_skill & offerta_skill),
        "requisiti_non_nel_profilo": sorted(offerta_skill - profilo_skill),
        "lingue_mancanti": sorted(lingue_mancanti(
            offerta.get("ai_requirements_summary") or "", lingue)),
    }
