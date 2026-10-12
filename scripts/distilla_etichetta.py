"""Il maestro locale etichetta il pool della distillazione (12/10/2026).

Legge il pool di distilla_dataset.py, chiede a Qwen3-4B-in-casa il
punteggio con la rubrica vera di produzione (valuta_offerta), scrive
riga per riga: se muore a meta' si riparte da dove era (le righe gia'
scritte si saltano per user_id+testo offerta).

Gira sull'N5 di notte. Lento e' ammesso: 12 s a coppia fanno ~300 coppie
all'ora, e il pool v1 si chiude in una notte.

  python scripts/distilla_etichetta.py pool.jsonl.gz etichettate.jsonl
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import time

from bench_giudice_locale import GiudiceLocale  # stesso dir (scripts/)
from nivult.matching.llm import valuta_offerta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pool")
    ap.add_argument("destinazione")
    ap.add_argument("--modello", default="Qwen/Qwen3-4B-Instruct-2507")
    args = ap.parse_args()

    fatte = set()
    if os.path.exists(args.destinazione):
        with open(args.destinazione, encoding="utf-8") as f:
            for linea in f:
                try:
                    r = json.loads(linea)
                    fatte.add(r["user_id"] + "|" + r["offerta"][:160])
                except (KeyError, json.JSONDecodeError):
                    continue
        print(f"ripresa: {len(fatte)} coppie gia' etichettate")

    with gzip.open(args.pool, "rt", encoding="utf-8") as f:
        pool = [json.loads(l) for l in f]
    da_fare = [r for r in pool
               if r["user_id"] + "|" + r["offerta"][:160] not in fatte]
    print(f"pool: {len(pool)} | da etichettare: {len(da_fare)}", flush=True)
    if not da_fare:
        return 0

    giudice = GiudiceLocale(args.modello)
    t = time.time()
    with open(args.destinazione, "a", encoding="utf-8") as out:
        for i, r in enumerate(da_fare):
            try:
                score, reason, _ = valuta_offerta(
                    giudice, r["profilo"], r["job"], lingua="Italiano")
            except Exception as e:                      # noqa: BLE001
                print(f"[{i}] ERRORE: {str(e)[:120]}", flush=True)
                continue
            out.write(json.dumps({
                "user_id": r["user_id"], "profilo": r["profilo"],
                "offerta": r["offerta"], "score_maestro": score,
                "reason": reason,
            }, ensure_ascii=False) + "\n")
            out.flush()
            if (i + 1) % 25 == 0:
                v = (i + 1) / (time.time() - t)
                resta = (len(da_fare) - i - 1) / v / 60
                print(f"[{i+1}/{len(da_fare)}] {v:.2f} coppie/s — "
                      f"restano ~{resta:.0f} min", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
