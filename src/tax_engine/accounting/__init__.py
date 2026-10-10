"""Accounting provider adapters. The pipeline depends only on `AccountingAdapter`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from tax_engine.accounting.resilience import RetryStats
from tax_engine.models import Payroll, TaxReport, Transaction


@dataclass
class FetchStats:
    requests: int = 0
    retries: int = 0
    rate_limit_wait_s: float = 0.0
    retry_stats: RetryStats = field(default_factory=RetryStats)


class ProviderError(RuntimeError):
    """A permanent provider failure (bad credentials, missing data). Not retried."""


class TransientProviderError(RuntimeError):
    """The provider is temporarily unavailable. The job is re-queued and retried later."""


class AccountingAdapter(Protocol):
    def transactions(
        self, tenant_id: str, token: str, quarter: str, stats: FetchStats
    ) -> list[Transaction]: ...

    def tax_report(
        self, tenant_id: str, token: str, quarter: str, stats: FetchStats
    ) -> TaxReport: ...

    def payroll(self, tenant_id: str, token: str, quarter: str, stats: FetchStats) -> Payroll: ...
