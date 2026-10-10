from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tax_engine.calc import calculate, reconcile
from tax_engine.config import TaxConfig
from tax_engine.models import Severity
from tax_engine.money import fmt, parse, q
from tax_engine.pipeline import previous_quarter
from tax_engine.validation import RULES, Context, is_blocked, validate
from tests.conftest import snapshot, txn


def test_money_rejects_floats_and_rounds_half_up() -> None:
    with pytest.raises(TypeError):
        parse(0.1)
    assert parse("0.10") == Decimal("0.10")
    assert q(Decimal("2.345")) == Decimal("2.35")
    assert q(Decimal("-2.345")) == Decimal("-2.35")
    assert fmt(Decimal("-1234.5")) == "-$1,234.50"


def test_previous_quarter_wraps_years() -> None:
    assert previous_quarter("2026Q1") == "2025Q4"
    assert previous_quarter("2026Q3") == "2026Q2"


def _clean() -> list:  # type: ignore[type-arg]
    return [
        txn(1, "sale", "1000.00", "100.00"),
        txn(2, "sale", "500.00", "0.00", "EXEMPT"),
        txn(3, "purchase", "300.00", "30.00"),
        txn(4, "purchase", "80.00", "0.00", "EXEMPT"),
    ]


def test_calculation_on_known_ledger() -> None:
    snap = snapshot(_clean(), "100.00", "30.00")
    f = calculate(snap, reconcile(snap))
    assert (f.sales_total, f.sales_taxable, f.sales_exempt, f.output_tax) == (
        Decimal("1600.00"),
        Decimal("1000.00"),
        Decimal("500.00"),
        Decimal("100.00"),
    )
    assert (f.purchases_total, f.input_tax) == (Decimal("410.00"), Decimal("30.00"))
    assert f.net_payable == Decimal("100.00") - Decimal("30.00") + Decimal("2000.00")


def test_uncoded_and_out_of_period_are_excluded() -> None:
    txns = [
        *_clean(),
        txn(5, "sale", "999.00", "99.90", None),
        txn(6, "sale", "777.00", "77.70", when=date(2026, 3, 20)),
    ]
    snap = snapshot(txns, "100.00", "30.00")
    rec = reconcile(snap)
    assert [t.id for t in rec.uncoded] == ["x5"] and [t.id for t in rec.out_of_period] == ["x6"]
    assert calculate(snap, rec).output_tax == Decimal("100.00")


def _fired(snap, config: TaxConfig) -> dict[str, Severity]:  # type: ignore[no-untyped-def]
    rec = reconcile(snap)
    findings = validate(Context(snap, rec, calculate(snap, rec), config))
    return {f.rule_id: f.severity for f in findings if f.severity is not Severity.PASS}


def test_clean_ledger_raises_nothing(config: TaxConfig) -> None:
    assert _fired(snapshot(_clean(), "100.00", "30.00"), config) == {}


@pytest.mark.parametrize(
    ("extra", "kwargs", "rule", "severity"),
    [
        ([txn(9, "sale", "100.00", "10.00", None)], {}, "V01", Severity.WARN),
        ([txn(9, "sale", "100.00", "15.00")], {"output_tax": "115.00"}, "V02", Severity.WARN),
        ([], {"withheld": "10000.01"}, "V03", Severity.FAIL),
        ([], {"output_tax": "101.01"}, "V04", Severity.FAIL),
        (
            [txn(1, "sale", "1000.00", "100.00", reference="REF-1")],
            {"output_tax": "200.00"},
            "V05",
            Severity.WARN,
        ),
        ([], {"prior": "500.00"}, "V06", Severity.INFORM),
        (
            [txn(9, "purchase", "50000.00", "5000.00")],
            {"input_tax": "5030.00"},
            "V07",
            Severity.INFORM,
        ),
        ([txn(9, "purchase", "100.00", "10.00", "EXEMPT")], {}, "V08", Severity.WARN),
        ([txn(9, "sale", "100.00", "10.00", when=date(2026, 7, 2))], {}, "V09", Severity.WARN),
    ],
)
def test_each_rule_fires_on_its_problem(
    config: TaxConfig,
    extra: list,
    kwargs: dict,  # type: ignore[type-arg]
    rule: str,
    severity: Severity,
) -> None:
    args = {"output_tax": "100.00", "input_tax": "30.00", **kwargs}
    snap = snapshot([*_clean(), *extra], args.pop("output_tax"), args.pop("input_tax"), **args)
    fired = _fired(snap, config)
    assert fired.get(rule) is severity, fired


def test_divergence_within_tolerance_passes(config: TaxConfig) -> None:
    assert "V04" not in _fired(snapshot(_clean(), "101.00", "30.00"), config)


def test_gate_blocks_only_in_strict_mode(config: TaxConfig) -> None:
    snap = snapshot(_clean(), "250.00", "30.00")
    rec = reconcile(snap)
    findings = validate(Context(snap, rec, calculate(snap, rec), config))
    assert is_blocked(findings, config)
    assert not is_blocked(findings, config.model_copy(update={"strict_mode": False}))


def test_every_rule_is_registered() -> None:
    assert sorted(RULES) == [f"V0{i}" for i in range(1, 10)]
