"""Validation engine: independent rules, each returning pass | inform | warn | fail.

Rules are deliberately independent and mostly cross-source (transactions vs the provider's
report vs payroll vs last quarter), so one bad input cannot hide behind another. The
divergence gate (V04) is the only rule that can block a run.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from tax_engine.calc import Reconciliation
from tax_engine.config import TaxConfig
from tax_engine.models import Figures, Finding, Severity, Snapshot
from tax_engine.money import ZERO, fmt, q


@dataclass(frozen=True)
class Context:
    snapshot: Snapshot
    rec: Reconciliation
    figures: Figures
    config: TaxConfig


Rule = Callable[[Context], Finding]
RULES: dict[str, tuple[str, Rule]] = {}


def rule(rule_id: str, title: str) -> Callable[[Rule], Rule]:
    def register(fn: Rule) -> Rule:
        RULES[rule_id] = (title, fn)
        return fn

    return register


def _ok(rule_id: str, message: str = "No issues.") -> Finding:
    return Finding(rule_id=rule_id, severity=Severity.PASS, message=message)


@rule("V01", "Uncoded transactions")
def uncoded(c: Context) -> Finding:
    if not c.rec.uncoded:
        return _ok("V01")
    total = q(sum((t.net for t in c.rec.uncoded), ZERO))
    return Finding(
        rule_id="V01",
        severity=Severity.WARN,
        value=total,
        message=f"{len(c.rec.uncoded)} transaction(s) without a tax code, "
        f"{fmt(total)} net, excluded from the statement.",
    )


@rule("V02", "Tax amount consistent with rate")
def rate_consistency(c: Context) -> Finding:
    bad = [
        t
        for t in c.rec.included
        if t.tax_code == "STD"
        and abs(t.tax - q(t.net * c.config.sales_tax_rate)) > c.config.rate_tolerance
    ]
    if not bad:
        return _ok("V02")
    diff = q(sum((t.tax - q(t.net * c.config.sales_tax_rate) for t in bad), ZERO))
    return Finding(
        rule_id="V02",
        severity=Severity.WARN,
        value=diff,
        message=f"{len(bad)} standard-rated transaction(s) where recorded tax differs "
        f"from {c.config.sales_tax_rate:%} of net, total difference "
        f"{fmt(diff)}. Recorded amounts used.",
    )


@rule("V03", "Withholding within gross wages")
def withholding(c: Context) -> Finding:
    p = c.snapshot.payroll
    if p.withheld <= p.gross_wages:
        return _ok("V03")
    return Finding(
        rule_id="V03",
        severity=Severity.FAIL,
        value=q(p.withheld - p.gross_wages),
        message=f"Tax withheld {fmt(p.withheld)} exceeds gross wages {fmt(p.gross_wages)}.",
    )


@rule("V04", "Provider report agrees with transactions")
def divergence(c: Context) -> Finding:
    report = c.snapshot.tax_report
    gaps = {
        "output tax": report.output_tax - c.figures.output_tax,
        "input tax": report.input_tax - c.figures.input_tax,
    }
    worst_name, worst = max(gaps.items(), key=lambda kv: abs(kv[1]))
    if abs(worst) <= c.config.divergence_tolerance:
        return _ok("V04", "Provider report matches transaction totals within tolerance.")
    return Finding(
        rule_id="V04",
        severity=Severity.FAIL,
        value=q(worst),
        message=f"Provider report {worst_name} differs from transactions by "
        f"{fmt(worst)} (tolerance {fmt(c.config.divergence_tolerance)}).",
    )


@rule("V05", "Possible duplicate transactions")
def duplicates(c: Context) -> Finding:
    keys = Counter((t.kind, t.date, t.counterparty, t.net, t.reference) for t in c.rec.included)
    dups = sum(n - 1 for n in keys.values() if n > 1)
    if not dups:
        return _ok("V05")
    return Finding(
        rule_id="V05",
        severity=Severity.WARN,
        value=Decimal(dups),
        message=f"{dups} possible duplicate(s): same date, counterparty, amount and "
        f"reference. Kept in the statement for review.",
    )


@rule("V06", "Movement against prior quarter")
def swing(c: Context) -> Finding:
    prior = c.snapshot.prior_net_payable
    if prior is None or prior == 0:
        return _ok("V06", "No prior quarter to compare.")
    change = (c.figures.net_payable - prior) / abs(prior)
    if abs(change) <= c.config.swing_threshold:
        return _ok("V06")
    return Finding(
        rule_id="V06",
        severity=Severity.INFORM,
        value=q(change * 100),
        message=f"Net payable moved {change:+.0%} against the prior quarter "
        f"({fmt(prior)} to {fmt(c.figures.net_payable)}).",
    )


@rule("V07", "Refund position")
def refund(c: Context) -> Finding:
    if c.figures.net_payable >= 0:
        return _ok("V07")
    return Finding(
        rule_id="V07",
        severity=Severity.INFORM,
        value=c.figures.net_payable,
        message=f"Refund position of {fmt(-c.figures.net_payable)}; check large "
        f"purchases are correctly coded.",
    )


@rule("V08", "Tax claimed on exempt purchases")
def exempt_claim(c: Context) -> Finding:
    bad = [
        t for t in c.rec.included if t.kind == "purchase" and t.tax_code == "EXEMPT" and t.tax > 0
    ]
    if not bad:
        return _ok("V08")
    total = q(sum((t.tax for t in bad), ZERO))
    return Finding(
        rule_id="V08",
        severity=Severity.WARN,
        value=total,
        message=f"{len(bad)} exempt purchase(s) carry {fmt(total)} of tax; not claimed.",
    )


@rule("V09", "Transactions outside the period")
def out_of_period(c: Context) -> Finding:
    if not c.rec.out_of_period:
        return _ok("V09")
    total = q(sum((t.net for t in c.rec.out_of_period), ZERO))
    return Finding(
        rule_id="V09",
        severity=Severity.WARN,
        value=total,
        message=f"{len(c.rec.out_of_period)} transaction(s) dated outside "
        f"{c.snapshot.quarter}, {fmt(total)} net, excluded.",
    )


def validate(ctx: Context) -> list[Finding]:
    return [fn(ctx) for _, (_, fn) in sorted(RULES.items())]


def is_blocked(findings: list[Finding], config: TaxConfig) -> bool:
    """The divergence gate: in strict mode a V04 failure blocks the run."""
    return config.strict_mode and any(
        f.rule_id == "V04" and f.severity is Severity.FAIL for f in findings
    )
