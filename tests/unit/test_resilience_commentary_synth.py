from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

import pytest

from tax_engine import synth
from tax_engine.accounting.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    RetryableError,
    RetryStats,
    TokenBucket,
    with_retries,
)
from tax_engine.commentary import UNAVAILABLE, grounded, write_commentary
from tax_engine.config import Settings
from tax_engine.llm import LLMError, LLMRequest, LLMResponse
from tax_engine.llm.fake import FakeLLM
from tax_engine.models import Figures, Finding, Severity

PROMPT = Settings().prompts_dir / "commentary.v1.md"
FIGS = Figures(
    sales_total=Decimal("1100.00"),
    sales_taxable=Decimal("1000.00"),
    sales_exempt=Decimal("0.00"),
    output_tax=Decimal("100.00"),
    purchases_total=Decimal("330.00"),
    input_tax=Decimal("30.00"),
    wages_gross=Decimal("10000.00"),
    wages_withheld=Decimal("2000.00"),
    net_payable=Decimal("2070.00"),
)


def test_token_bucket_limits_rate() -> None:
    bucket = TokenBucket(rate_per_second=50, burst=1)
    start = time.monotonic()
    for _ in range(6):
        bucket.acquire()
    assert time.monotonic() - start >= 5 / 50 * 0.8


def test_circuit_breaker_opens_half_opens_and_closes() -> None:
    breaker = CircuitBreaker(threshold=3, cooldown=0.05)
    for _ in range(3):
        breaker.record(False)
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    assert breaker.trips == 1
    time.sleep(0.06)
    breaker.before_call()  # half-open trial allowed
    breaker.record(False)  # trial fails: open again
    with pytest.raises(CircuitOpenError):
        breaker.before_call()
    time.sleep(0.06)
    breaker.before_call()
    breaker.record(True)
    breaker.before_call()  # closed
    assert breaker.trips == 2


def test_retries_until_success_and_honour_retry_after() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableError("busy", retry_after=0.01)
        return "ok"

    stats = RetryStats()
    assert with_retries(flaky, stats, max_attempts=4) == "ok"
    assert (stats.attempts, stats.retries) == (3, 2)


def test_retries_give_up_after_max_attempts() -> None:
    def broken() -> str:
        raise RetryableError("down", retry_after=0.0)

    with pytest.raises(RetryableError):
        with_retries(broken, RetryStats(), max_attempts=3)


def test_grounding_check() -> None:
    allowed = {"$2,070.00", "$100.00", "+12%"}
    assert grounded("Net payable is $2,070.00, up +12%.", allowed)
    assert not grounded("Net payable is about $2,000.", allowed)


class _Broken:
    name = "broken"

    def complete(self, request: LLMRequest) -> LLMResponse:
        raise LLMError("timeout")


class _Inventive:
    name = "inventive"

    def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse("Net payable is $9,999.00.", "m", 1, 1)


def _note(llm: object, findings: list[Finding] | None = None) -> object:
    return write_commentary(
        llm,
        "m",
        PROMPT,
        "t01",
        "Northwind Bakery",
        "2026Q2",  # type: ignore[arg-type]
        FIGS,
        Decimal("1800.00"),
        findings or [],
    )


def test_commentary_fails_safe_on_error_and_on_invented_numbers() -> None:
    broken = _note(_Broken())
    assert broken.text == UNAVAILABLE and broken.status == "llm_error"  # type: ignore[attr-defined]
    inventive = _note(_Inventive())
    assert inventive.text == UNAVAILABLE and inventive.status == "ungrounded"  # type: ignore[attr-defined]


def test_fake_commentary_is_grounded_and_mentions_top_finding() -> None:
    finding = Finding(rule_id="V03", severity=Severity.FAIL, message="Tax withheld exceeds.")
    note = _note(FakeLLM(drift=0.0), [finding])
    assert note.status == "ok"  # type: ignore[attr-defined]
    assert "$2,070.00" in note.text and "+15%" in note.text  # type: ignore[attr-defined]
    assert "withholding" in note.text.lower()  # type: ignore[attr-defined]


def test_generator_is_deterministic(tmp_path: Path) -> None:
    _, ledgers_a, truth_a = synth.generate(seed=3)
    _, ledgers_b, truth_b = synth.generate(seed=3)
    assert ledgers_a == ledgers_b
    assert [t.to_json() for t in truth_a] == [t.to_json() for t in truth_b]
    assert len(ledgers_a) == 80


def test_quarter_bounds() -> None:
    start, end = synth.quarter_bounds("2025Q4")
    assert (start.isoformat(), end.isoformat()) == ("2025-10-01", "2025-12-31")
