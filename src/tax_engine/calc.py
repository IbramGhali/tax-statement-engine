"""Reconcile and calculate. Pure functions over a snapshot; Decimal only; no model involved.

Policy (see README "Accounting policy"): uncoded and out-of-period transactions are
excluded and reported; recorded tax amounts are used as booked; tax on an exempt purchase
is not claimable; duplicates are kept and reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tax_engine.models import Figures, Snapshot, Transaction
from tax_engine.money import ZERO, q
from tax_engine.synth import quarter_bounds


@dataclass
class Reconciliation:
    included: list[Transaction] = field(default_factory=list)
    uncoded: list[Transaction] = field(default_factory=list)
    out_of_period: list[Transaction] = field(default_factory=list)


def reconcile(snapshot: Snapshot) -> Reconciliation:
    start, end = quarter_bounds(snapshot.quarter)
    rec = Reconciliation()
    for txn in sorted(snapshot.transactions, key=lambda t: (t.date, t.id)):
        if not start <= txn.date <= end:
            rec.out_of_period.append(txn)
        elif txn.tax_code is None:
            rec.uncoded.append(txn)
        else:
            rec.included.append(txn)
    return rec


def calculate(snapshot: Snapshot, rec: Reconciliation) -> Figures:
    sales_total = sales_taxable = sales_exempt = output_tax = ZERO
    purchases_total = input_tax = ZERO
    for txn in rec.included:
        if txn.kind == "sale":
            sales_total += txn.net + txn.tax
            if txn.tax_code == "STD":
                sales_taxable += txn.net
                output_tax += txn.tax
            else:
                sales_exempt += txn.net
        else:
            purchases_total += txn.net + txn.tax
            if txn.tax_code == "STD":
                input_tax += txn.tax
    withheld = snapshot.payroll.withheld
    return Figures(
        sales_total=q(sales_total),
        sales_taxable=q(sales_taxable),
        sales_exempt=q(sales_exempt),
        output_tax=q(output_tax),
        purchases_total=q(purchases_total),
        input_tax=q(input_tax),
        wages_gross=q(snapshot.payroll.gross_wages),
        wages_withheld=q(withheld),
        net_payable=q(output_tax - input_tax + withheld),
    )
