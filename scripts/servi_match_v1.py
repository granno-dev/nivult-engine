#!/usr/bin/env python3
"""Il microservizio del pre-scorer: match-v1 in casa, via Tailscale.

Gira sull'N5, dove torch e la GPU ci sono gia' (il training e' nato li') e
dove il server di produzione arriva solo dalla rete interna — stesso
disegno del maestro (llama.cpp, porta 8090): l'autenticazione e' la rete.

    python scripts/servi_match_v1.py --host 100.119.200.7 --port 8092

Il modello e' la directory scritta da allena_match_v1.py (tokenizer +
testa di classificazione a un'uscita, regressione sullo score del maestro
0-100). Il batch si ferma a 8 coppie per passo, come in fase di esame: la
VRAM dell'N5 divide con il maestro e con lo scrittore.

Contratto (lo consuma nivult.matching.prescore):

    POST /prescore  {"pairs": [["testo profilo", "testo offerta"], ...]}
                    -> {"scores": [0-100, ...]}   (stesso ordine)
    POST /famiglia  {"jobs": [{"title": "...", "location": "...",
                               "description": "..."}, ...]}
                    -> {"predictions": [{"scores": {famiglia: 0-1, ...}}, ...]}
    GET  /salute    -> {"ok": true, "modello": "..."}
"""

from __future__ import annotations

import argparse
import math
import os

MODELLO_DEFAULT = "/opt/nivult/modelli/match-v1"
FAMIGLIA_DEFAULT = "/opt/nivult/modelli/nivult-v1"
MAX_LEN = 512        # come in training e in esame
BATCH = 8            # come esame_match_v1.py: la VRAM non e' solo nostra
MAX_FAMIGLIA = 30    # il guardrail gira solo dopo il cap del pre-scorer


def _punteggio_contratto(valore: float) -> float:
    if isinstance(valore, bool) or not math.isfinite(valore):
        raise ValueError("match-v1 ha prodotto un punteggio non finito")
    return round(max(0.0, min(100.0, valore * 100)), 1)


def crea_app(percorso_modello: str, percorso_famiglia: str = FAMIGLIA_DEFAULT):
    import torch
    from fastapi import FastAPI
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(percorso_modello)
    mod = AutoModelForSequenceClassification.from_pretrained(percorso_modello)
    mod.eval()
    modello_famiglia = None

    app = FastAPI(title="match-v1 prescore", docs_url=None, redoc_url=None)

    @app.get("/salute")
    def salute():
        return {"ok": True, "modello": percorso_modello,
                "device": str(next(mod.parameters()).device)}

    @app.post("/prescore")
    def prescore(corpo: dict):
        coppie = corpo.get("pairs") or []
        if not coppie:
            return {"scores": []}
        punteggi: list[float] = []
        with torch.no_grad():
            for i in range(0, len(coppie), BATCH):
                lotto = coppie[i:i + BATCH]
                enc = tok([c[0] for c in lotto], [c[1] for c in lotto],
                          padding=True, truncation=True, max_length=MAX_LEN,
                          return_tensors="pt")
                punteggi.extend(mod(**enc).logits[:, 0].tolist())
        # La testa impara la scala 0-1: qui si torna ai punti del maestro.
        return {"scores": [_punteggio_contratto(p) for p in punteggi]}

    @app.post("/famiglia")
    def famiglia(corpo: dict):
        nonlocal modello_famiglia
        jobs = corpo.get("jobs") or []
        if (not isinstance(jobs, list) or len(jobs) > MAX_FAMIGLIA
                or any(not isinstance(j, dict) for j in jobs)):
            raise ValueError("famiglia: attesi al massimo 30 oggetti offerta")
        if not jobs:
            return {"predictions": []}
        if modello_famiglia is None:
            from nivult.ats.modello_v1 import ModelloV1
            modello_famiglia = ModelloV1(percorso_famiglia, device="cpu")
        from nivult.ats.modello_v1 import testo
        testi = [testo(j.get("title"), j.get("location"), j.get("description"))
                 for j in jobs]
        predetti = []
        for i in range(0, len(testi), BATCH):
            predetti.extend(modello_famiglia.predici(
                testi[i:i + BATCH], distribuzione=True))
        return {"predictions": [
            {"scores": p["family_distribution"]} for p in predetti]}

    return app


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.environ.get("PRESCORE_BIND",
                                                     "100.119.200.7"))
    ap.add_argument("--port", type=int, default=8092)
    ap.add_argument("--modello", default=os.environ.get("MATCH_V1_PATH",
                                                        MODELLO_DEFAULT))
    ap.add_argument("--modello-famiglia", default=os.environ.get(
        "NIVULT_V1_PATH", FAMIGLIA_DEFAULT))
    args = ap.parse_args()

    import uvicorn
    uvicorn.run(crea_app(args.modello, args.modello_famiglia),
                host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
