#!/usr/bin/env python3
"""Servizio embedding per il retrieval ruolo match-v3, isolato dall'applicazione."""
from __future__ import annotations

import argparse
import math
import os

from nivult.matching.retrieval import documento_ruolo, fatti_profilo

MODELLO_DEFAULT = "BAAI/bge-m3"
MAX_JOBS = 512
BATCH = 8


def _input_jobs(corpo: dict) -> list[dict]:
    jobs = corpo.get("jobs")
    if not isinstance(jobs, list) or len(jobs) > MAX_JOBS:
        raise ValueError(f"jobs deve essere una lista di al massimo {MAX_JOBS} elementi")
    if any(not isinstance(j, dict) for j in jobs):
        raise ValueError("jobs malformati")
    return jobs


def _input_rank(corpo: dict) -> tuple[str, list[dict]]:
    profilo = corpo.get("profile")
    if not isinstance(profilo, str) or not profilo.strip():
        raise ValueError("profile mancante")
    return profilo, _input_jobs(corpo)


def crea_app(percorso_modello: str = MODELLO_DEFAULT, device: str = "cpu"):
    import torch
    from fastapi import FastAPI
    from sentence_transformers import SentenceTransformer

    if device != "cpu" and not torch.cuda.is_available():
        raise RuntimeError("device GPU richiesto ma non disponibile")
    modello = SentenceTransformer(percorso_modello, device=device,
                                  local_files_only=True)
    modello.max_seq_length = 512 if "e5" in percorso_modello.lower() else 1024
    modello.eval()
    app = FastAPI(title="match-v3 retrieval", docs_url=None, redoc_url=None)

    @app.get("/salute")
    def salute():
        return {"ok": True, "modello": percorso_modello,
                "device": str(next(modello.parameters()).device)}

    def encode(testi):
        out = []
        with torch.inference_mode():
            for i in range(0, len(testi), BATCH):
                out.extend(modello.encode(
                    testi[i:i + BATCH], batch_size=BATCH, convert_to_numpy=True,
                    device=device, normalize_embeddings=True, show_progress_bar=False))
        return out

    @app.post("/rank")
    def rank(corpo: dict):
        profilo, jobs = _input_rank(corpo)
        if not jobs:
            return {"scores": []}
        query = fatti_profilo(profilo, ("Ruolo cercato", "Seniority", "Note")) or profilo
        documenti = [documento_ruolo(j) for j in jobs]
        if "e5" in percorso_modello.lower():
            query = "query: " + query
            documenti = ["passage: " + d for d in documenti]
        q = encode([query])[0]
        out = encode(documenti) @ q
        if any(not math.isfinite(v) for v in out):
            raise RuntimeError("retrieval ha prodotto un punteggio non finito")
        return {"scores": [round(max(-1.0, min(1.0, float(v))), 6) for v in out]}

    @app.post("/embed")
    def embed(corpo: dict):
        jobs = _input_jobs(corpo)
        documenti = [documento_ruolo(j) for j in jobs]
        if "e5" in percorso_modello.lower():
            documenti = ["passage: " + d for d in documenti]
        vettori = encode(documenti)
        if any(not math.isfinite(v) for vettore in vettori for v in vettore):
            raise RuntimeError("embedding non finito")
        return {"embeddings": [v.tolist() for v in vettori]}

    @app.post("/embed_query")
    def embed_query(corpo: dict):
        profilo = corpo.get("profile")
        if not isinstance(profilo, str) or not profilo.strip():
            raise ValueError("profile mancante")
        query = fatti_profilo(profilo, ("Ruolo cercato", "Seniority", "Note")) or profilo
        if "e5" in percorso_modello.lower():
            query = "query: " + query
        vettore = encode([query])[0]
        if any(not math.isfinite(v) for v in vettore):
            raise RuntimeError("embedding non finito")
        return {"embedding": vettore.tolist()}

    return app


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.environ.get("MATCH_V3_BIND", "100.119.200.7"))
    ap.add_argument("--port", type=int, default=8093)
    ap.add_argument("--modello", default=os.environ.get("MATCH_V3_PATH", MODELLO_DEFAULT))
    ap.add_argument("--device", default=os.environ.get("MATCH_V3_DEVICE", "cpu"))
    args = ap.parse_args()
    import uvicorn
    uvicorn.run(crea_app(args.modello, args.device), host=args.host, port=args.port,
                log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
