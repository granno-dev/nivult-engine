#!/usr/bin/env python3
"""Serva lo scorer Model2Vec su CPU, senza Torch e senza modelli esterni."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from nivult.matching.m2v import M2VScorer


def crea_app(percorso: str):
    from fastapi import FastAPI

    scorer = M2VScorer(percorso)
    app = FastAPI(title="m2v-match", docs_url=None, redoc_url=None)

    @app.get("/salute")
    def salute():
        return {"ok": True, "modello": percorso, "device": "cpu"}

    @app.post("/score")
    def score(corpo: dict):
        profilo = corpo.get("profile")
        jobs = corpo.get("jobs")
        if not isinstance(profilo, str) or not profilo.strip():
            raise ValueError("profilo vuoto")
        if not isinstance(jobs, list) or len(jobs) > 256:
            raise ValueError("jobs deve essere una lista di al massimo 256 offerte")
        if any(not isinstance(job, dict) for job in jobs):
            raise ValueError("ogni offerta deve essere un oggetto")
        return {"scores": scorer.score_batch(profilo, jobs)}

    return app


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("M2V_BIND", "100.119.200.7"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("M2V_PORT", "8094")))
    ap.add_argument("--modello", default=os.environ.get("M2V_PATH", "/opt/nivult/m2v-work/model"))
    args = ap.parse_args()
    if not Path(args.modello).is_dir():
        raise RuntimeError(f"modello non trovato: {args.modello}")
    import uvicorn
    uvicorn.run(crea_app(args.modello), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
