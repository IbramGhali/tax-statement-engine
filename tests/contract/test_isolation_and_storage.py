"""Tenant isolation is enforced in one place and checked two ways: statically (no query
outside the data-access modules) and at runtime (one tenant cannot read another's rows)."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

from tax_engine import queue
from tax_engine.config import Settings
from tax_engine.db import make_engine
from tax_engine.models import Figures, RunStatus
from tests.conftest import snapshot, txn

SRC = Path(__file__).resolve().parents[2] / "src" / "tax_engine"
ALLOWED = {"repository.py", "queue.py", "db.py"}


def test_queries_only_in_data_access_modules() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        sql_builders = re.search(
            r"from sqlalchemy[\w.]* import[^\n]*\b(select|insert|update|delete|text)\b", text
        )
        if ".execute(" in text or sql_builders:
            offenders.append(path.name)
    assert offenders == [], f"queries outside the repository layer: {offenders}"


def _figures() -> Figures:
    z = Decimal("0.00")
    return Figures(
        sales_total=z,
        sales_taxable=z,
        sales_exempt=z,
        output_tax=z,
        purchases_total=z,
        input_tax=z,
        wages_gross=z,
        wages_withheld=z,
        net_payable=z,
    )


def test_one_tenant_cannot_read_another(settings: Settings) -> None:
    from tax_engine.repository import TenantRepository

    engine = make_engine(settings.db_url)
    a, b = TenantRepository(engine, "t01"), TenantRepository(engine, "t02")
    sha = a.save_snapshot(snapshot([txn(1, "sale", "10.00", "1.00")], "1.00", "0.00"))
    a.save_run(
        run_id="run-a",
        batch_id=None,
        quarter="2026Q2",
        status=RunStatus.COMPLETE,
        config_version="v",
        snapshot_sha=sha,
        figures=_figures(),
        findings=[],
        commentary=None,
        commentary_status=None,
        model=None,
        prompt_version=None,
        api_requests=0,
        api_retries=0,
        seconds=0.0,
        workbook_path=None,
    )
    assert a.run("run-a") is not None
    assert b.run("run-a") is None
    assert b.snapshot(sha) is None
    assert b.runs() == []
    with pytest.raises(PermissionError):
        b.save_snapshot(snapshot([], "0.00", "0.00"))  # snapshot tenant is t01


def test_snapshots_are_content_addressed(settings: Settings) -> None:
    from tax_engine.repository import TenantRepository

    repo = TenantRepository(make_engine(settings.db_url), "t01")
    snap = snapshot([txn(1, "sale", "10.00", "1.00")], "1.00", "0.00")
    assert repo.save_snapshot(snap) == repo.save_snapshot(snap)


def test_queue_runs_quarters_in_order_and_retries_then_fails(settings: Settings) -> None:
    engine = make_engine(settings.db_url)
    batch = queue.create_batch(engine, "t", "v", [("t01", "2026Q1"), ("t01", "2026Q2")])
    first = queue.claim(engine, batch)
    assert first is not None and first.quarter == "2026Q1"
    assert queue.claim(engine, batch) is None  # Q2 waits for Q1
    assert queue.retry_later(engine, first, "boom", max_attempts=2)
    assert queue.claim(engine, batch) is None  # Q1 back-off pending, Q2 still blocked
    for _ in range(100):
        again = queue.claim(engine, batch)
        if again:
            break
        import time

        time.sleep(0.02)
    assert again is not None and again.attempts == 2
    assert not queue.retry_later(engine, again, "boom", max_attempts=2)  # now failed
    second = queue.claim(engine, batch)
    assert second is not None and second.quarter == "2026Q2"
