"""Domain types. Amounts are Decimal end to end and serialise as strings."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

Money = Annotated[Decimal, PlainSerializer(lambda d: str(d), return_type=str)]
TaxCode = Literal["STD", "EXEMPT"]


class Transaction(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    date: date
    kind: Literal["sale", "purchase"]
    net: Money
    tax: Money
    tax_code: TaxCode | None = Field(description="None means the bookkeeper left it uncoded")
    counterparty: str
    reference: str


class TaxReport(BaseModel):
    """The accounting provider's own computed totals for the quarter."""

    output_tax: Money
    input_tax: Money


class Payroll(BaseModel):
    gross_wages: Money
    withheld: Money


class Snapshot(BaseModel):
    """Everything fetched for one run. Stored by hash so any run can be replayed."""

    tenant_id: str
    quarter: str
    transactions: list[Transaction]
    tax_report: TaxReport
    payroll: Payroll
    prior_net_payable: Money | None = None


class Figures(BaseModel):
    sales_total: Money
    sales_taxable: Money
    sales_exempt: Money
    output_tax: Money
    purchases_total: Money
    input_tax: Money
    wages_gross: Money
    wages_withheld: Money
    net_payable: Money

    def as_dict(self) -> dict[str, Decimal]:
        return {name: getattr(self, name) for name in type(self).model_fields}


FIGURE_LABELS: dict[str, str] = {
    "sales_total": "Total sales (incl. tax)",
    "sales_taxable": "Taxable sales (excl. tax)",
    "sales_exempt": "Exempt sales",
    "output_tax": "Tax on sales",
    "purchases_total": "Total purchases (incl. tax)",
    "input_tax": "Tax credits on purchases",
    "wages_gross": "Gross wages",
    "wages_withheld": "Tax withheld from wages",
    "net_payable": "Net amount payable",
}


class Severity(StrEnum):
    PASS = "pass"
    INFORM = "inform"
    WARN = "warn"
    FAIL = "fail"


class Finding(BaseModel):
    rule_id: str
    severity: Severity
    message: str
    value: Money | None = None


class RunStatus(StrEnum):
    COMPLETE = "complete"
    BLOCKED = "blocked"
    FAILED = "failed"
