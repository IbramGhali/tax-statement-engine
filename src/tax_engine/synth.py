"""Synthetic ledgers for 20 fictional businesses over 4 quarters.

Ground truth is accumulated while transactions are generated, independently of the
calculation code, so the evaluation measures the engine rather than agreeing with itself.
Problems are planted on purpose and recorded with the validation rule that must catch them.

Accounting policy (shared by the generator's truth and the engine, documented in the README):
* uncoded and out-of-period transactions are excluded from every figure and flagged;
* recorded tax amounts are used as booked, and flagged when inconsistent with the rate;
* tax recorded on an exempt purchase is not claimable; the purchase still counts at cost;
* duplicates are kept and flagged for a person to decide.
"""

from __future__ import annotations

import json
import random
import secrets
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tax_engine.money import ZERO, q

RATE = Decimal("0.10")
QUARTERS = ["2025Q3", "2025Q4", "2026Q1", "2026Q2"]
BUSINESSES = [
    "Northwind Bakery",
    "Acme Plumbing",
    "Bluegum Cafe",
    "Harbour Signs",
    "Kestrel Joinery",
    "Lantern Books",
    "Marlin Fitness",
    "Oakridge Florist",
    "Pebble Dental Lab",
    "Quill Print Co",
    "Redfern Bikes",
    "Saltbush Catering",
    "Tidewater Tiles",
    "Umbra Lighting",
    "Vale Vet Supplies",
    "Wattle Web Studio",
    "Yarrow Landscaping",
    "Zephyr Cleaning",
    "Ironbark Electrical",
    "Juniper Pet Grooming",
]
COUNTERPARTIES = [
    "Cedar Wholesale",
    "Delta Freight",
    "Echo Office Supply",
    "Fern Packaging",
    "Granite Hire",
    "Helix Software",
    "Ibis Energy",
    "Jade Insurance",
    "walk-in customer",
    "online order",
    "trade account",
    "market stall",
]
PLANTS = {  # problem -> (probability per tenant-quarter, rule that must catch it)
    "uncoded": (0.12, "V01"),
    "misrated": (0.12, "V02"),
    "withholding": (0.08, "V03"),
    "divergence": (0.10, "V04"),
    "duplicate": (0.12, "V05"),
    "exempt_claim": (0.10, "V08"),
    "out_of_period": (0.10, "V09"),
}


def quarter_bounds(quarter: str) -> tuple[date, date]:
    year, qn = int(quarter[:4]), int(quarter[-1])
    start = date(year, 3 * (qn - 1) + 1, 1)
    end_month = 3 * qn
    end = (date(year + (end_month == 12), end_month % 12 + 1, 1)) - timedelta(days=1)
    return start, end


@dataclass
class Truth:
    tenant_id: str
    quarter: str
    figures: dict[str, Decimal]
    expected_rules: set[str] = field(default_factory=set)
    expected_blocked: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "quarter": self.quarter,
            "figures": {k: str(v) for k, v in self.figures.items()},
            "expected_rules": sorted(self.expected_rules),
            "expected_blocked": self.expected_blocked,
        }


def _cents(rng: random.Random, low: int, high: int) -> Decimal:
    return Decimal(rng.randint(low * 100, high * 100)) / 100


def _ledger(
    rng: random.Random, tenant_id: str, quarter: str, scale: float, prior_net: Decimal | None
) -> tuple[dict[str, Any], Truth]:
    start, end = quarter_bounds(quarter)
    days = (end - start).days
    planted = {p for p, (prob, _) in PLANTS.items() if rng.random() < prob}
    txns: list[dict[str, Any]] = []
    acc = {
        k: ZERO
        for k in (
            "sales_total",
            "sales_taxable",
            "sales_exempt",
            "output_tax",
            "purchases_total",
            "input_tax",
        )
    }

    def add(
        kind: str,
        net: Decimal,
        code: str | None,
        tax: Decimal,
        when: date,
        counted: bool = True,
        claimable: bool = True,
    ) -> dict[str, Any]:
        txn = {
            "id": f"{tenant_id}-{quarter}-{len(txns) + 1:04d}",
            "date": when.isoformat(),
            "kind": kind,
            "net": str(net),
            "tax": str(tax),
            "tax_code": code,
            "counterparty": rng.choice(COUNTERPARTIES),
            "reference": f"{'INV' if kind == 'sale' else 'BILL'}-{rng.randint(10000, 99999)}",
        }
        txns.append(txn)
        if counted:
            if kind == "sale":
                acc["sales_total"] += net + tax
                if code == "STD":
                    acc["sales_taxable"] += net
                    acc["output_tax"] += tax
                else:
                    acc["sales_exempt"] += net
            else:
                acc["purchases_total"] += net + tax
                if code == "STD" and claimable:
                    acc["input_tax"] += tax
        return txn

    def day() -> date:
        return start + timedelta(days=rng.randint(0, days))

    for _ in range(rng.randint(30, 80)):
        code = "STD" if rng.random() < 0.85 else "EXEMPT"
        net = _cents(rng, 40, int(2500 * scale))
        add("sale", net, code, q(net * RATE) if code == "STD" else ZERO, day())
    big_purchase = rng.random() < 0.10  # capital purchase, often a refund position
    for i in range(rng.randint(15, 45)):
        code = "STD" if rng.random() < 0.8 else "EXEMPT"
        net = _cents(rng, 20, int(1800 * scale))
        if big_purchase and i == 0:
            net = _cents(rng, int(60000 * scale), int(90000 * scale))
            code = "STD"
        add("purchase", net, code, q(net * RATE) if code == "STD" else ZERO, day())

    truth = Truth(tenant_id, quarter, {})
    if "uncoded" in planted:
        for _ in range(rng.randint(1, 3)):
            net = _cents(rng, 50, 900)
            add(rng.choice(["sale", "purchase"]), net, None, q(net * RATE), day(), counted=False)
    if "misrated" in planted:
        net = _cents(rng, 200, 1500)
        add("sale", net, "STD", q(net * Decimal("0.15")), day())
    if "exempt_claim" in planted:
        net = _cents(rng, 100, 800)
        add("purchase", net, "EXEMPT", q(net * RATE), day(), claimable=False)
    if "duplicate" in planted:
        original = rng.choice([t for t in txns if t["tax_code"] is not None])
        dup = add(
            original["kind"],
            Decimal(original["net"]),
            original["tax_code"],
            Decimal(original["tax"]),
            date.fromisoformat(original["date"]),
        )
        dup.update(counterparty=original["counterparty"], reference=original["reference"])
    if "out_of_period" in planted:
        net = _cents(rng, 100, 1200)
        add(
            "sale",
            net,
            "STD",
            q(net * RATE),
            start - timedelta(days=rng.randint(3, 20)),
            counted=False,
        )
    rng.shuffle(txns)

    gross = q(Decimal(rng.randint(int(15000 * scale), int(45000 * scale))))
    withheld = q(gross * Decimal(rng.randint(15, 24)) / 100)
    if "withholding" in planted:
        withheld = q(gross + _cents(rng, 100, 2000))

    report = {"output_tax": str(acc["output_tax"]), "input_tax": str(acc["input_tax"])}
    if "divergence" in planted:
        report["output_tax"] = str(acc["output_tax"] + _cents(rng, 25, 400))

    net_payable = acc["output_tax"] - acc["input_tax"] + withheld
    truth.figures = {
        **acc,
        "wages_gross": gross,
        "wages_withheld": withheld,
        "net_payable": net_payable,
    }
    truth.expected_rules = {PLANTS[p][1] for p in planted}
    truth.expected_blocked = "divergence" in planted
    if net_payable < 0:
        truth.expected_rules.add("V07")
    if (
        prior_net is not None
        and prior_net != 0
        and abs(net_payable - prior_net) / abs(prior_net) > Decimal("0.50")
    ):
        truth.expected_rules.add("V06")

    ledger = {
        "tenant_id": tenant_id,
        "quarter": quarter,
        "transactions": txns,
        "tax_report": report,
        "payroll": {"gross_wages": str(gross), "withheld": str(withheld)},
    }
    return ledger, truth


def generate(seed: int = 7) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[Truth]]:
    rng = random.Random(seed)
    tenants, ledgers, truths = [], [], []
    for i, name in enumerate(BUSINESSES, start=1):
        tenant_id = f"t{i:02d}"
        # Tokens are random per generation; they only guard the local mock API.
        tenants.append({"id": tenant_id, "name": name, "api_token": secrets.token_hex(16)})
        scale = rng.choice([0.5, 1.0, 1.0, 1.5, 2.5])
        prior: Decimal | None = None
        for quarter in QUARTERS:
            ledger, truth = _ledger(rng, tenant_id, quarter, scale, prior)
            ledgers.append(ledger)
            truths.append(truth)
            prior = truth.figures["net_payable"]
    return tenants, ledgers, truths


def write_dataset(data_dir: Path, seed: int = 7) -> tuple[int, int]:
    tenants, ledgers, truths = generate(seed)
    (data_dir / "ledgers").mkdir(parents=True, exist_ok=True)
    (data_dir / "tenants.json").write_text(json.dumps(tenants, indent=2), encoding="utf-8")
    for ledger in ledgers:
        path = data_dir / "ledgers" / ledger["tenant_id"] / f"{ledger['quarter']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ledger, indent=1), encoding="utf-8")
    with (data_dir / "truth.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for truth in truths:
            fh.write(json.dumps(truth.to_json(), sort_keys=True) + "\n")
    planted = sum(len(t.expected_rules) for t in truths)
    return len(ledgers), planted


def load_tenants(data_dir: Path) -> list[dict[str, str]]:
    data: list[dict[str, str]] = json.loads((data_dir / "tenants.json").read_text("utf-8"))
    return data


def load_truth(data_dir: Path) -> dict[tuple[str, str], dict[str, Any]]:
    with (data_dir / "truth.jsonl").open(encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    return {(r["tenant_id"], r["quarter"]): r for r in rows}
