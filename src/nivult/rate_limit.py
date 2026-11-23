"""Token bucket in-process per le rotte servite da un solo worker."""
from __future__ import annotations

import math
import threading
import time


class RateLimiter:
    def __init__(self, clock=time.monotonic, max_keys: int = 10_000) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, rate: float, burst: int) -> tuple[bool, int]:
        if rate <= 0 or burst <= 0:
            raise ValueError("rate and burst must be positive")
        now = self._clock()
        with self._lock:
            tokens, updated = self._buckets.get(key, (float(burst), now))
            tokens = min(float(burst), tokens + max(0.0, now - updated) * rate)
            if tokens >= 1.0:
                tokens -= 1.0
                allowed, retry_after = True, 0
            else:
                allowed = False
                retry_after = max(1, math.ceil((1.0 - tokens) / rate))
            self._buckets[key] = (tokens, now)
            self._prune(now)
            return allowed, retry_after

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()

    def _prune(self, now: float) -> None:
        if len(self._buckets) <= self._max_keys:
            return
        stale = [key for key, (_, updated) in self._buckets.items()
                 if now - updated >= 300]
        for key in stale:
            del self._buckets[key]
        while len(self._buckets) > self._max_keys:
            oldest = min(self._buckets, key=lambda key: self._buckets[key][1])
            del self._buckets[oldest]
