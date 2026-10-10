"""HTTP adapter for the accounting provider (the bundled mock API, or a real one with the
same shape). Every request goes through: concurrency limit, token bucket, circuit breaker,
timeout, and retry with backoff that honours Retry-After."""

from __future__ import annotations

import threading
from typing import Any

import httpx

from tax_engine.accounting import FetchStats, ProviderError, TransientProviderError
from tax_engine.accounting.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    RetryableError,
    TokenBucket,
    with_retries,
)
from tax_engine.models import Payroll, TaxReport, Transaction


class HttpAccountingAdapter:
    def __init__(
        self,
        base_url: str,
        rate_per_second: float = 20.0,
        concurrency: int = 8,
        timeout: float = 2.0,
        max_attempts: int = 4,
    ) -> None:
        self.client = httpx.Client(base_url=base_url, timeout=timeout)
        self.semaphore = threading.BoundedSemaphore(concurrency)
        self.buckets: dict[str, TokenBucket] = {}
        self.buckets_lock = threading.Lock()
        self.rate = rate_per_second
        self.breaker = CircuitBreaker(threshold=5, cooldown=1.0)
        self.max_attempts = max_attempts

    def close(self) -> None:
        self.client.close()

    def _bucket(self, tenant_id: str) -> TokenBucket:
        with self.buckets_lock:
            return self.buckets.setdefault(tenant_id, TokenBucket(self.rate))

    def _get(
        self, tenant_id: str, token: str, path: str, params: dict[str, Any], stats: FetchStats
    ) -> Any:
        def once() -> Any:
            self.breaker.before_call()
            stats.rate_limit_wait_s += self._bucket(tenant_id).acquire()
            stats.requests += 1
            try:
                with self.semaphore:
                    response = self.client.get(
                        path, params=params, headers={"Authorization": f"Bearer {token}"}
                    )
            except httpx.TimeoutException as exc:
                self.breaker.record(False)
                raise RetryableError("timeout") from exc
            except httpx.TransportError as exc:
                self.breaker.record(False)
                raise RetryableError(f"transport error: {exc}") from exc
            if response.status_code == 429:
                # Rate limiting is the provider protecting itself, not a failure.
                retry_after = float(response.headers.get("Retry-After", "0.5"))
                raise RetryableError("rate limited", retry_after=retry_after)
            if response.status_code >= 500:
                self.breaker.record(False)
                raise RetryableError(f"server error {response.status_code}")
            self.breaker.record(True)
            if response.status_code >= 400:
                raise ProviderError(f"{response.status_code} on {path}: {response.text[:200]}")
            return response.json()

        try:
            result = with_retries(once, stats.retry_stats, max_attempts=self.max_attempts)
        except (RetryableError, CircuitOpenError) as exc:
            raise TransientProviderError(str(exc)) from exc
        finally:
            stats.retries = stats.retry_stats.retries
        return result

    def transactions(
        self, tenant_id: str, token: str, quarter: str, stats: FetchStats
    ) -> list[Transaction]:
        items: list[Transaction] = []
        page = 1
        while True:
            body = self._get(
                tenant_id,
                token,
                f"/tenants/{tenant_id}/transactions",
                {"quarter": quarter, "page": page},
                stats,
            )
            items.extend(Transaction.model_validate(t) for t in body["items"])
            if not body["has_more"]:
                return items
            page += 1

    def tax_report(self, tenant_id: str, token: str, quarter: str, stats: FetchStats) -> TaxReport:
        return TaxReport.model_validate(
            self._get(
                tenant_id, token, f"/tenants/{tenant_id}/reports/tax", {"quarter": quarter}, stats
            )
        )

    def payroll(self, tenant_id: str, token: str, quarter: str, stats: FetchStats) -> Payroll:
        return Payroll.model_validate(
            self._get(
                tenant_id, token, f"/tenants/{tenant_id}/payroll", {"quarter": quarter}, stats
            )
        )
