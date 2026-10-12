"""Banco del maestro via HTTP: il path di PRODUZIONE, non il laboratorio.

Il giudice misurato ieri girava via transformers (bench_giudice_locale.py).
In produzione il digest chiama llama.cpp sull'N5 via Tailscale: modello
GGUF quantizzato, prefix cache, rete di mezzo. Qualita' e velocita si
misurano QUI, o non contano.

Uso (sul server): MAESTRO_URL=http://100.119.200.7:8090/v1 \
  python scripts/bench_maestro_http.py /tmp/coppie_match.jsonl.gz --max 200
"""
from __future__ import annotations

import argparse
import gzip
import json
import time

from nivult.matching.llm import MaestroLocale, valuta_offerta


def ranghi(v):
    o = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    for pos, i in enumerate(o):
        r[i] = pos
    return r


def spearman(a, b):
    ra, rb, n = ranghi(a), ranghi(b), len(a)
    m = (n - 1) / 2
    num = sum((x - m) * (y - m) for x, y in zip(ra, rb))
    den = (sum((x - m) ** 2 for x in ra) * sum((y - m) ** 2 for y in rb)) ** 0.5
    return num / den if den else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("coppie")
    ap.add_argument("--max", type=int, default=200)
    args = ap.parse_args()

    with gzip.open(args.coppie, "rt", encoding="utf-8") as f:
        righe = [json.loads(l) for l in f][: args.max]

    m = MaestroLocale(rate_per_second=100)   # niente freno: misuriamo il vero
    glm, pred, tempi = [], [], []
    errori = 0
    t0 = time.time()
    for i, r in enumerate(righe):
        t = time.time()
        try:
            score, reason, _ = valuta_offerta(m, r["profilo"], r["job"],
                                              lingua="Italiano")
        except Exception as e:                              # noqa: BLE001
            errori += 1
            print(f"[{i}] ERRORE: {str(e)[:100]}", flush=True)
            continue
        glm.append(float(r["score"]))
        pred.append(float(score))
        tempi.append(time.time() - t)
        if (i + 1) % 25 == 0:
            print(f"[{i+1}/{len(righe)}] rho {spearman(glm, pred):.3f} — "
                  f"mediana {sorted(tempi)[len(tempi)//2]:.1f}s/coppia",
                  flush=True)

    tempi.sort()
    print(json.dumps({
        "coppie": len(pred), "errori": errori,
        "spearman": round(spearman(glm, pred), 3),
        "mediana_s": tempi[len(tempi) // 2],
        "p90_s": tempi[int(len(tempi) * 0.9)],
        "totale_min": round((time.time() - t0) / 60, 1),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
