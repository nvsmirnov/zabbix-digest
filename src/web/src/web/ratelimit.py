"""In-memory sliding window rate limiter (single process)."""

import time
from collections import deque


class RateLimiter:
    MAX_KEYS = 100_000

    def __init__(self, clock=time.monotonic) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._clock = clock

    def hit(self, key: str, limit: int, period: float) -> bool:
        """Register a hit. Returns False when the limit is exceeded."""
        now = self._clock()
        window = self._hits.get(key)
        if window is None:
            if len(self._hits) >= self.MAX_KEYS:
                self._prune(now, period)
            window = self._hits[key] = deque()
        while window and window[0] <= now - period:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True

    def _prune(self, now: float, period: float) -> None:
        for key in [k for k, w in self._hits.items() if not w or w[-1] <= now - period]:
            del self._hits[key]
