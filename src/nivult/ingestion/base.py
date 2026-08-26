"""Infrastruttura comune ai client di fonte: rate limit, retry, contabilità."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from nivult.ingestion.models import FetchResult

log = logging.getLogger("nivult.ingestion")


class TokenBucket:
    """Limitatore di frequenza.

    Le fonti pubbliche sono gratuite, quindi il circuit breaker sui crediti non
    scatta mai per loro. La risorsa scarsa è un'altra: superare il rate limit
    non costa denaro, costa un ban. Questo è ciò che protegge da quello.
    """

    def __init__(self, rate_per_second: float, burst: int) -> None:
        self.rate = rate_per_second
        self.burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()

    def take(self, n: int = 1) -> None:
        while True:
            now = time.monotonic()
            self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= n:
                self._tokens -= n
                return
            time.sleep((n - self._tokens) / self.rate)


@dataclass(slots=True)
class Attempt:
    status: int
    latency_ms: int


class SourceClient(Protocol):
    """Il contratto che ogni fonte deve rispettare, e nient'altro."""

    source: str
    countries: frozenset[str]
    credits_per_request: int

    def fetch(self, *, query: str, country: str, since, limit: int) -> FetchResult: ...


class CreditoEsaurito(RuntimeError):
    """Il fornitore risponde 429 ma il problema è il credito, non il ritmo."""


_SEGNALI_SALDO = (
    "insufficient balance",
    "no resource package",
    "please recharge",
    "quota exceeded",
    "billing",
)


def _saldo_esaurito(corpo: str) -> bool:
    c = (corpo or "").lower()
    return any(seg in c for seg in _SEGNALI_SALDO)


class HttpSource:
    """Base per i client HTTP: timeout, retry con backoff, rispetto di 429."""

    source: str = "?"
    countries: frozenset[str] = frozenset()
    credits_per_request: int = 0

    def __init__(self, *, rate_per_second: float = 2.0, burst: int = 5,
                 timeout: float = 20.0, max_retries: int = 4) -> None:
        self.bucket = TokenBucket(rate_per_second, burst)
        self.timeout = timeout
        self.max_retries = max_retries
        self.attempts: list[Attempt] = []
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "nivult-engine/0.1 (+https://nivult.com)"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.max_retries):
            self.bucket.take()
            started = time.monotonic()
            try:
                r = self._client.request(method, url, **kw)
            except httpx.HTTPError as exc:
                last = exc
                log.warning("%s: errore di rete (tentativo %d): %s",
                            self.source, attempt + 1, exc)
                time.sleep(self._backoff(attempt))
                continue

            self.attempts.append(Attempt(r.status_code, int((time.monotonic() - started) * 1000)))

            if r.status_code == 429 and _saldo_esaurito(r.text):
                # Non tutti i 429 vogliono dire "rallenta". Z.ai usa lo stesso
                # codice per "credito finito" (1113), e quello aspettando non
                # diventa vero: quattro tentativi con backoff sono venti
                # secondi buttati che finiscono in un timeout, nascondendo la
                # causa vera dietro un errore che non le somiglia.
                raise CreditoEsaurito(f"{self.source}: {r.text[:200]}")

            if r.status_code == 429:
                # Retry-After è il numero che la fonte ci sta dando: ignorarlo
                # e riprovare col nostro backoff è il modo di farsi bandire.
                # Lo standard ammette anche una DATA HTTP al posto dei secondi:
                # float() su quella crashava l'intera fetch per un header
                # scritto nell'altro formato lecito.
                try:
                    wait = float(r.headers.get("Retry-After", ""))
                except ValueError:
                    wait = self._backoff(attempt)
                log.warning("%s: 429, attendo %.1fs come richiesto", self.source, wait)
                time.sleep(min(wait, 120))
                continue

            if r.status_code >= 500:
                log.warning("%s: %d dalla fonte (tentativo %d)",
                            self.source, r.status_code, attempt + 1)
                time.sleep(self._backoff(attempt))
                continue

            return r

        raise RuntimeError(
            f"{self.source}: {self.max_retries} tentativi falliti"
            + (f" — ultimo errore: {last}" if last else "")
        )

    @staticmethod
    def _backoff(attempt: int) -> float:
        # Jitter, se no dopo un disservizio tutti i worker ripartono insieme.
        return min(2 ** attempt, 30) * (0.5 + random.random())
