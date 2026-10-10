from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tax_engine import synth
from tax_engine.config import Settings, TaxConfig, load_tax_config
from tax_engine.db import make_engine
from tax_engine.models import Payroll, Snapshot, TaxReport, Transaction
from tax_engine.repository import upsert_tenants


@pytest.fixture
def config() -> TaxConfig:
    return load_tax_config(Settings().tax_config_path)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Offline settings with data, database and outputs inside tmp_path, tenants provisioned."""
    s = replace(
        Settings(),
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        baseline_path=tmp_path / "eval" / "baseline.json",
    )
    synth.write_dataset(s.data_dir)
    upsert_tenants(make_engine(s.db_url), synth.load_tenants(s.data_dir))
    return s


def txn(
    i: int,
    kind: str,
    net: str,
    tax: str,
    code: str | None = "STD",
    when: date = date(2026, 5, 10),
    counterparty: str = "Cedar Wholesale",
    reference: str | None = None,
) -> Transaction:
    return Transaction(
        id=f"x{i}",
        date=when,
        kind=kind,
        net=Decimal(net),  # type: ignore[arg-type]
        tax=Decimal(tax),
        tax_code=code,  # type: ignore[arg-type]
        counterparty=counterparty,
        reference=reference or f"REF-{i}",
    )


def snapshot(
    transactions: list[Transaction],
    output_tax: str,
    input_tax: str,
    gross: str = "10000.00",
    withheld: str = "2000.00",
    prior: str | None = None,
) -> Snapshot:
    return Snapshot(
        tenant_id="t01",
        quarter="2026Q2",
        transactions=transactions,
        tax_report=TaxReport(output_tax=Decimal(output_tax), input_tax=Decimal(input_tax)),
        payroll=Payroll(gross_wages=Decimal(gross), withheld=Decimal(withheld)),
        prior_net_payable=Decimal(prior) if prior else None,
    )
