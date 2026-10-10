"""HTTP API (tenant-scoped, token per tenant) and an operator page listing statements."""

from __future__ import annotations

import hmac
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from tax_engine import queue
from tax_engine.config import Settings
from tax_engine.db import make_engine
from tax_engine.repository import TenantRepository, tenant_ids

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class RunRequest(BaseModel):
    quarter: str


def tenant_repo(
    request: Request, tenant_id: str, x_tenant_token: Annotated[str | None, Header()] = None
) -> TenantRepository:
    """Bind the request to one tenant after checking that tenant's token."""
    repo = TenantRepository(request.app.state.engine, tenant_id)
    tenant = repo.tenant()
    if (
        tenant is None
        or x_tenant_token is None
        or not hmac.compare_digest(x_tenant_token, tenant["api_token"])
    ):
        raise HTTPException(401, "invalid tenant token")  # same answer for unknown tenant
    return repo


Repo = Annotated[TenantRepository, Depends(tenant_repo)]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.db_url)
    api = FastAPI(title="tax-statement-engine", version="0.1.0")
    api.state.engine = engine

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/tenants/{tenant_id}/runs")
    def runs(repo: Repo) -> list[dict[str, Any]]:
        return [
            {
                k: r[k]
                for k in ("id", "quarter", "status", "figures", "commentary_status", "created_at")
            }
            for r in repo.runs()
        ]

    @api.get("/tenants/{tenant_id}/runs/{run_id}")
    def run(run_id: str, repo: Repo) -> dict[str, Any]:
        found = repo.run(run_id)
        if found is None:
            raise HTTPException(404, "run not found")
        return {**found, "findings": repo.findings(run_id)}

    @api.get("/tenants/{tenant_id}/runs/{run_id}/workbook")
    def workbook(run_id: str, repo: Repo) -> FileResponse:
        found = repo.run(run_id)
        if found is None or not found["workbook_path"]:
            raise HTTPException(404, "run not found")
        return FileResponse(found["workbook_path"], filename=f"{run_id}.xlsx")

    @api.post("/tenants/{tenant_id}/runs", status_code=202)
    def start(body: RunRequest, repo: Repo) -> dict[str, str]:
        batch_id = queue.create_batch(
            engine, "api", "queued-by-api", [(repo.tenant_id, body.quarter)]
        )
        return {"batch_id": batch_id, "status": "queued"}

    @api.get("/", response_class=HTMLResponse)
    def page(request: Request) -> HTMLResponse:
        latest = queue.latest_batch(engine)
        rows = []
        if latest:
            for tid in tenant_ids(engine):
                repo = TenantRepository(engine, tid)
                name = (repo.tenant() or {}).get("name", tid)
                for r in repo.runs(latest["id"]):
                    counts: dict[str, int] = {}
                    for f in repo.findings(r["id"]):
                        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
                    rows.append(
                        {
                            "tenant": name,
                            "quarter": r["quarter"],
                            "status": r["status"],
                            "net": (r["figures"] or {}).get("net_payable", "-"),
                            "fail": counts.get("fail", 0),
                            "warn": counts.get("warn", 0),
                            "commentary": r["commentary_status"],
                        }
                    )
        return TEMPLATES.TemplateResponse(
            request, "statements.html", {"batch": latest, "rows": rows}
        )

    return api
