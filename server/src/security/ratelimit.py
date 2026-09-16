"""A sliding-window rate limiter, in process. Phase 18.

One limiter per purpose — API requests per key, anonymous requests per
address, login attempts per address — each answering one question: may
this caller do this now, and if not, how long until they may?

**In-process on purpose**, like Phase 9's pacing limiter and Phase 11's
snapshot cache: one API process and one dashboard process are the shape of
this stage, and a shared limiter would need infrastructure the project has
none of. Two processes would each allow their own limit, which bounds the
rate at twice the figure rather than not at all. The limit is not what
stops an attacker with a botnet; it is what stops one script from guessing
keys at wire speed and one runaway workflow from turning the API into a
denial of service on the database the dialer is using.

A limit of zero or less disables the limiter: every call is allowed.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    """Whether a call is allowed, and what to tell the caller if not."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_secs: float

    @property
    def retry_after_header(self) -> str:
        """`Retry-After` in whole seconds, never zero."""
        return str(max(1, int(self.retry_after_secs + 0.999)))


class RateLimiter:
    """`limit` calls per `window_secs` per key, over a sliding window."""

    def __init__(
        self,
        limit: int,
        window_secs: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        """Create the limiter.

        Args:
            limit: Calls allowed per window per key. Zero or less: unlimited.
            window_secs: The window's length.
            clock: Where "now" comes from; the checks inject one.
            max_keys: How many keys are tracked before the oldest are dropped
                — a bound on memory when every request carries a new address.
        """
        self._limit = int(limit)
        self._window = float(window_secs)
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._last_prune = clock()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def window_secs(self) -> float:
        return self._window

    @property
    def enabled(self) -> bool:
        return self._limit > 0

    def check(self, key: str, *, consume: bool = True) -> Decision:
        """Whether `key` may make a call now; records it when `consume` is True."""
        if not self.enabled:
            return Decision(True, self._limit, self._limit, 0.0)
        now = self._clock()
        self._prune_if_due(now)
        hits = self._hits.get(key)
        if hits is None:
            if len(self._hits) >= self._max_keys:
                self._evict_oldest()
            hits = deque()
            self._hits[key] = hits
        cutoff = now - self._window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self._limit:
            return Decision(False, self._limit, 0, hits[0] + self._window - now)
        if consume:
            hits.append(now)
        return Decision(True, self._limit, self._limit - len(hits), 0.0)

    def reset(self, key: str) -> None:
        """Forget a key — after a successful login, say, so a slow typist is not punished."""
        self._hits.pop(key, None)

    def _prune_if_due(self, now: float) -> None:
        if now - self._last_prune < self._window:
            return
        self._last_prune = now
        cutoff = now - self._window
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]

    def _evict_oldest(self) -> None:
        oldest = min(self._hits, key=lambda k: self._hits[k][-1] if self._hits[k] else 0.0)
        del self._hits[oldest]


__all__ = ["Decision", "RateLimiter"]
