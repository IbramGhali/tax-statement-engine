from __future__ import annotations

import re
from decimal import Decimal

import openpyxl
from fastapi.testclient import TestClient

from tax_engine import queue, synth
from tax_engine.api import create_app
from tax_engine.app import build_app
from tax_engine.config import Settings
from tax_engine.mock_api import Faults, create_mock_api
from tax_engine.workbook import SUMMARY_ROWS
from tax_engine.worker import run_batch


def _tokens(settings: Settings) -> dict[str, str]:
    return {t["id"]: t["api_token"] for t in synth.load_tenants(settings.data_dir)}


def test_mock_api_auth_and_pagination(settings: Settings) -> None:
    client = TestClient(create_mock_api(settings.data_dir))
    tok = _tokens(settings)
    params = {"quarter": "2026Q2"}
    assert client.get("/tenants/t01/transactions", params=params).status_code == 401
    assert (
        client.get(
            "/tenants/t01/transactions",
            params=params,
            headers={"Authorization": f"Bearer {tok['t02']}"},
        ).status_code
        == 401
    )
    page = client.get(
        "/tenants/t01/transactions",
        params={**params, "page": 1},
        headers={"Authorization": f"Bearer {tok['t01']}"},
    ).json()
    assert len(page["items"]) == 50 and page["has_more"]


def test_mock_faults_are_deterministic_and_transient(settings: Settings) -> None:
    client = TestClient(create_mock_api(settings.data_dir, Faults(error_rate=0.5)))
    headers = {"Authorization": f"Bearer {_tokens(settings)['t01']}"}
    codes = [
        client.get(
            "/tenants/t01/payroll", params={"quarter": "2026Q2"}, headers=headers
        ).status_code
        for _ in range(10)
    ]
    assert 200 in codes and 503 in codes


def _run_one(settings: Settings) -> str:
    with build_app(settings) as a:
        batch = queue.create_batch(a.engine, "t", a.config.version, [("t01", "2026Q2")])
        run_batch(a.pipeline, batch, workers=1, max_attempts=2)
    return batch


def test_api_requires_the_tenants_own_token(settings: Settings) -> None:
    _run_one(settings)
    client = TestClient(create_app(settings))
    tok = _tokens(settings)
    assert client.get("/tenants/t01/runs").status_code == 401
    assert (
        client.get("/tenants/t01/runs", headers={"X-Tenant-Token": tok["t02"]}).status_code == 401
    )
    assert client.get("/tenants/nope/runs", headers={"X-Tenant-Token": "x"}).status_code == 401
    runs = client.get("/tenants/t01/runs", headers={"X-Tenant-Token": tok["t01"]}).json()
    assert len(runs) == 1
    detail = client.get(
        f"/tenants/t01/runs/{runs[0]['id']}", headers={"X-Tenant-Token": tok["t01"]}
    ).json()
    assert len(detail["findings"]) == 9
    assert (
        client.get(
            f"/tenants/t02/runs/{runs[0]['id']}", headers={"X-Tenant-Token": tok["t02"]}
        ).status_code
        == 404
    )
    assert "Quarterly statements" in client.get("/").text


def _sumifs(formula: str, rows: list[tuple]) -> Decimal:  # type: ignore[type-arg]
    """Evaluate the SUMIFS patterns the workbook writes, over the Transactions rows."""
    cols = {"C": 2, "D": 3, "E": 4, "F": 5, "G": 6}
    total = Decimal("0")
    for term in re.findall(r"SUMIFS\(([^)]*)\)", formula):
        parts = term.split(",")
        target = cols[parts[0].split("!")[1][0]]
        crit = [
            (cols[parts[i].split("!")[1][0]], parts[i + 1].strip('"'))
            for i in range(1, len(parts), 2)
        ]
        total += sum(
            (Decimal(str(r[target])) for r in rows if all(r[c] == v for c, v in crit)), Decimal("0")
        )
    return total


def test_workbook_formulas_reproduce_engine_figures(settings: Settings) -> None:
    batch = _run_one(settings)
    from tax_engine.db import make_engine
    from tax_engine.repository import TenantRepository

    run = TenantRepository(make_engine(settings.db_url), "t01").runs(batch)[0]
    wb = openpyxl.load_workbook(run["workbook_path"])
    rows = [tuple(r) for r in wb["Transactions"].iter_rows(min_row=2, values_only=True)]
    values: dict[str, Decimal] = {}
    for i, name in enumerate(SUMMARY_ROWS, start=2):
        formula = wb["Summary"][f"B{i}"].value
        assert formula.startswith("="), name
        if "SUMIFS" in formula:
            values[name] = _sumifs(formula, rows)
    for name, value in values.items():
        assert value.quantize(Decimal("0.01")) == Decimal(run["figures"][name]), name
