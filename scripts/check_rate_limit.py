#!/usr/bin/env python3
"""Verifica token bucket e ordine 429/OpenSearch/metering, senza servizi esterni."""
from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["API_OPENSEARCH_JOBS"] = "1"

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nivult.api_clienti import v1
from nivult.rate_limit import RateLimiter

PASSED = []
FAILED = []


def check(label, got, expected=True):
    if got == expected:
        PASSED.append(label)
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}: {got!r} != {expected!r}")


def test_clock_eviction_e_thread() -> None:
    adesso = [100.0]
    limiter = RateLimiter(clock=lambda: adesso[0], max_keys=2)
    for _ in range(5):
        check("burst free ammesso", limiter.allow("free", 2, 5)[0], True)
    negato = limiter.allow("free", 2, 5)
    check("sesta richiesta free negata", negato[0], False)
    check("Retry-After intero >= 1", isinstance(negato[1], int) and negato[1] >= 1, True)
    adesso[0] += 0.5
    check("refill da clock monotonico", limiter.allow("free", 2, 5)[0], True)

    limiter.allow("vecchia-a", 1, 1)
    limiter.allow("vecchia-b", 1, 1)
    adesso[0] += 400
    limiter.allow("nuova", 1, 1)
    check("bucket inattivi eliminati", len(limiter._buckets) <= 2, True)
    adesso[0] += 1
    limiter.allow("altra", 1, 1)
    check("tetto RAM rispettato", len(limiter._buckets) <= 2, True)
    check("clock default monotonico", RateLimiter()._clock is time.monotonic, True)

    concorrente = RateLimiter()
    barriera = threading.Barrier(8)

    def richiesta(_):
        barriera.wait()
        return [concorrente.allow("stessa", 1, 25)[0] for _ in range(10)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        esiti = list(pool.map(richiesta, range(8)))
    ammessi = sum(esito for gruppo in esiti for esito in gruppo)
    check("80 richieste concorrenti: solo burst 25", ammessi, 25)


def test_http_429() -> None:
    app = FastAPI()
    app.include_router(v1.router)
    client = TestClient(app)
    originale_verifica = v1.chiavi.verifica
    originale_addebito = v1.chiavi.addebita_chiave
    originale_offerte = v1.opensearch.offerte
    chiamate = {"os": 0, "metering": 0, "limiti": []}

    def finto_addebito(*args, **kwargs):
        chiamate["metering"] += 1
        return {}

    def finte_offerte(filtri, cursore, limite):
        chiamate["os"] += 1
        chiamate["limiti"].append(limite)
        return [{"id": "job-test"}], None

    v1.chiavi.addebita_chiave = finto_addebito
    v1.opensearch.offerte = finte_offerte
    try:
        for tier, richieste, atteso_limite in ((1000, 6, 25), (10_000, 31, 100)):
            v1._rate_limiter.clear()
            chiamate.update(os=0, metering=0, limiti=[])
            v1.chiavi.verifica = lambda chiave, tier=tier: {
                "id": "test-key", "user_id": "test-user", "monthly_tier": tier,
                "crediti_mensili": tier, "usati_mese": 0, "mese_uso": None,
                "label": "test",
            }
            codici = []
            for _ in range(richieste):
                r = client.get("/v1/jobs?limit=100", headers={"X-Api-Key": "test"})
                codici.append(r.status_code)
            check(f"tier {tier}: risposte prima del 429", codici[:-1],
                  [200] * (richieste - 1))
            check(f"tier {tier}: ultima risposta 429", codici[-1], 429)
            check(f"tier {tier}: Retry-After valido", int(r.headers["Retry-After"]) >= 1,
                  True)
            check(f"tier {tier}: OpenSearch solo richieste ammesse", chiamate["os"],
                  richieste - 1)
            check(f"tier {tier}: zero crediti su 429", chiamate["metering"],
                  richieste - 1)
            check(f"tier {tier}: limite pagina corretto", set(chiamate["limiti"]),
                  {atteso_limite})
    finally:
        v1.chiavi.verifica = originale_verifica
        v1.chiavi.addebita_chiave = originale_addebito
        v1.opensearch.offerte = originale_offerte
        v1._rate_limiter.clear()


def main() -> int:
    test_clock_eviction_e_thread()
    test_http_429()
    print(f"{len(PASSED)} controlli passati, {len(FAILED)} falliti")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
