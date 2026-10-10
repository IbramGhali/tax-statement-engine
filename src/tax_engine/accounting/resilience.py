"""Client-side resilience: token-bucket rate limiting, retry with backoff, circuit breaker."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

T = TypeVar("T")


class TokenBucket:
    """Blocking rate limiter: `acquire` waits until a token is available."""

    def __init__(self, rate_per_second: float, burst: int = 5) -> None:
        self.rate, self.capacity = rate_per_second, float(burst)
        self.tokens, self.updated = float(burst), time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> float:
        waited = 0.0
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return waited
                delay = (1 - self.tokens) / self.rate
            time.sleep(delay)
            waited += delay


class CircuitOpenError(RuntimeError):
    """The provider is failing; calls are refused until the cool-down passes."""


@dataclass
class CircuitBreaker:
    """Opens after `threshold` consecutive failures; lets one trial call through after
    `cooldown` seconds (half-open); closes again on success."""

    threshold: int = 5
    cooldown: float = 1.0
    failures: int = 0
    opened_at: float | None = None
    trips: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def before_call(self) -> None:
        with self.lock:
            if self.opened_at is None:
                return
            if time.monotonic() - self.opened_at < self.cooldown:
                raise CircuitOpenError("provider circuit open")
            self.opened_at = None  # half-open: allow a trial call
            self.failures = self.threshold - 1

    def record(self, ok: bool) -> None:
        with self.lock:
            if ok:
                self.failures = 0
                return
            self.failures += 1
            if self.failures >= self.threshold and self.opened_at is None:
                self.opened_at = time.monotonic()
                self.trips += 1


class RetryableError(RuntimeError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class RetryStats:
    attempts: int = 0
    retries: int = 0


def with_retries(
    fn: Callable[[], T],
    stats: RetryStats,
    max_attempts: int = 4,
    base_delay: float = 0.1,
    max_delay: float = 2.0,
) -> T:
    """Call fn, retrying RetryableError with exponential backoff and full jitter.
    A server-provided Retry-After wins over the computed delay."""
    for attempt in range(1, max_attempts + 1):
        stats.attempts += 1
        try:
            return fn()
        except RetryableError as exc:
            if attempt == max_attempts:
                raise
            stats.retries += 1
            delay = (
                exc.retry_after
                if exc.retry_after is not None
                else random.uniform(0, min(max_delay, base_delay * 2 ** (attempt - 1)))
            )
            time.sleep(min(delay, max_delay))
    raise AssertionError("unreachable")
