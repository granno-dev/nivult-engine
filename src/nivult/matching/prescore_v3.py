"""Client del retrieval E5 v3; fallback lasciato al chiamante."""
from __future__ import annotations

import math
import os

import httpx

from nivult.matching.retrieval import lingue_mancanti

MAX_CANDIDATI = 150
TIMEOUT = 120.0
CAP_PER_RUOLO = {
    "senior backend software engineer": 100,
    "senior backend engineer": 100,
}


def cap_adattivo(ruolo: str | None) -> int:
    """Riduce solo ruoli con cap validato; default conservativo a 150."""
    if os.environ.get("PRESCORE_V3_ADAPTIVE") != "1":
        return MAX_CANDIDATI
    return CAP_PER_RUOLO.get((ruolo or "").strip().lower(), MAX_CANDIDATI)


def disponibile() -> bool:
    return bool(os.environ.get("PRESCORE_V3_URL"))


def rank_batch(profilo: str, offerte: list[dict]) -> list[float]:
    url = os.environ["PRESCORE_V3_URL"].rstrip("/")
    out = []
    for inizio in range(0, len(offerte), 256):
        jobs = [{"title": o.get("title"),
                 "ai_core_responsibilities": o.get("ai_core_responsibilities"),
                 "ai_requirements_summary": o.get("ai_requirements_summary")}
                for o in offerte[inizio:inizio + 256]]
        r = httpx.post(f"{url}/rank", timeout=TIMEOUT,
                       json={"profile": profilo, "jobs": jobs})
        if r.status_code != 200:
            raise RuntimeError(f"retrieval v3 ({r.status_code}): {r.text[:200]}")
        scores = r.json().get("scores")
        if not isinstance(scores, list) or len(scores) != len(jobs):
            raise RuntimeError("retrieval v3: numero o forma dei punteggi non valida")
        for valore in scores:
            if isinstance(valore, bool):
                raise TypeError("retrieval v3: punteggio booleano non ammesso")
            try:
                score = float(valore)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("retrieval v3: punteggio non numerico") from exc
            if not math.isfinite(score) or not -1 <= score <= 1:
                raise RuntimeError("retrieval v3: punteggio fuori dalla scala coseno")
            out.append(score)
    return out


def embed_batch(offerte: list[dict]) -> list[list[float]]:
    url = os.environ["PRESCORE_V3_URL"].rstrip("/")
    out = []
    for inizio in range(0, len(offerte), 256):
        jobs = [{"title": o.get("title"),
                 "ai_core_responsibilities": o.get("ai_core_responsibilities"),
                 "ai_requirements_summary": o.get("ai_requirements_summary")}
                for o in offerte[inizio:inizio + 256]]
        r = httpx.post(f"{url}/embed", timeout=TIMEOUT,
                       json={"jobs": jobs})
        if r.status_code != 200:
            raise RuntimeError(f"embedding v3 ({r.status_code}): {r.text[:200]}")
        vettori = r.json().get("embeddings")
        if not isinstance(vettori, list) or len(vettori) != len(jobs):
            raise RuntimeError("embedding v3: numero o forma dei vettori non valida")
        for vettore in vettori:
            if not isinstance(vettore, list) or len(vettore) != 1024:
                raise RuntimeError("embedding v3: dimensione non valida")
            if any(isinstance(v, bool) or not math.isfinite(v) for v in vettore):
                raise RuntimeError("embedding v3: valore non finito")
            out.append([float(v) for v in vettore])
    return out


def query_embedding(profilo: str) -> list[float]:
    if not profilo.strip():
        raise ValueError("profilo vuoto")
    url = os.environ["PRESCORE_V3_URL"].rstrip("/")
    r = httpx.post(f"{url}/embed_query", timeout=TIMEOUT, json={"profile": profilo})
    if r.status_code != 200:
        raise RuntimeError(f"embedding profilo ({r.status_code}): {r.text[:200]}")
    vettore = r.json().get("embedding")
    if not isinstance(vettore, list) or len(vettore) != 1024:
        raise RuntimeError("embedding profilo: dimensione non valida")
    if any(isinstance(v, bool) or not math.isfinite(v) for v in vettore):
        raise RuntimeError("embedding profilo: valore non finito")
    return [float(v) for v in vettore]


def seleziona_top(punteggi: list[float], offerte: list[dict],
                  lingue_profilo: set[str], *, max_items: int = MAX_CANDIDATI) -> list[int]:
    if max_items <= 0:
        return []
    eleggibili = []
    for i, (score, offerta) in enumerate(zip(punteggi, offerte)):
        if lingue_profilo and lingue_mancanti(
                offerta.get("ai_requirements_summary") or "", lingue_profilo):
            continue
        eleggibili.append(i)
    eleggibili.sort(key=lambda i: (-punteggi[i], i))
    return sorted(eleggibili[:max_items])
