"""Tenant-scoped data access. The only module (with queue.py) allowed to run queries.

`TenantRepository` is bound to one tenant at construction and adds `tenant_id = :tenant`
to every statement it runs, so a caller cannot read or write another tenant's data by
passing the wrong id. A CI test fails if queries appear anywhere else in the codebase.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, and_, insert, select

from tax_engine import db
from tax_engine.models import Figures, Finding, RunStatus, Snapshot


def now() -> datetime:
    return datetime.now(UTC)


class TenantRepository:
    def __init__(self, engine: Engine, tenant_id: str) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self.engine = engine
        self.tenant_id = tenant_id

    # tenant -----------------------------------------------------------------------------
    def tenant(self) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            row = c.execute(select(db.tenants).where(db.tenants.c.id == self.tenant_id)).first()
        return dict(row._mapping) if row else None

    # snapshots --------------------------------------------------------------------------
    def save_snapshot(self, snapshot: Snapshot) -> str:
        if snapshot.tenant_id != self.tenant_id:
            raise PermissionError("snapshot belongs to another tenant")
        body = json.loads(snapshot.model_dump_json())
        sha = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with self.engine.begin() as c:
            exists = c.execute(
                select(db.snapshots.c.sha256).where(db.snapshots.c.sha256 == sha)
            ).first()
            if not exists:
                c.execute(
                    insert(db.snapshots).values(sha256=sha, tenant_id=self.tenant_id, body=body)
                )
        return sha

    def snapshot(self, sha: str) -> Snapshot | None:
        with self.engine.connect() as c:
            row = c.execute(
                select(db.snapshots.c.body).where(
                    and_(db.snapshots.c.sha256 == sha, db.snapshots.c.tenant_id == self.tenant_id)
                )
            ).first()
        return Snapshot.model_validate(row.body) if row else None

    # runs -------------------------------------------------------------------------------
    def save_run(
        self,
        *,
        run_id: str,
        batch_id: str | None,
        quarter: str,
        status: RunStatus,
        config_version: str,
        snapshot_sha: str | None,
        figures: Figures | None,
        findings: list[Finding],
        commentary: str | None,
        commentary_status: str | None,
        model: str | None,
        prompt_version: str | None,
        api_requests: int,
        api_retries: int,
        seconds: float,
        workbook_path: str | None,
    ) -> None:
        with self.engine.begin() as c:
            c.execute(
                insert(db.runs).values(
                    id=run_id,
                    tenant_id=self.tenant_id,
                    batch_id=batch_id,
                    quarter=quarter,
                    status=status.value,
                    config_version=config_version,
                    snapshot_sha=snapshot_sha,
                    figures=json.loads(figures.model_dump_json()) if figures else None,
                    commentary=commentary,
                    commentary_status=commentary_status,
                    model=model,
                    prompt_version=prompt_version,
                    api_requests=api_requests,
                    api_retries=api_retries,
                    seconds=seconds,
                    workbook_path=workbook_path,
                    created_at=now(),
                )
            )
            for f in findings:
                c.execute(
                    insert(db.findings).values(
                        run_id=run_id,
                        tenant_id=self.tenant_id,
                        rule_id=f.rule_id,
                        severity=f.severity.value,
                        message=f.message,
                        value=str(f.value) if f.value is not None else None,
                    )
                )
            c.execute(
                insert(db.audit_events).values(
                    tenant_id=self.tenant_id,
                    run_id=run_id,
                    at=now(),
                    event=f"run_{status.value}",
                    detail=json.dumps(
                        {"quarter": quarter, "snapshot": snapshot_sha, "config": config_version}
                    ),
                )
            )

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            row = c.execute(
                select(db.runs).where(
                    and_(db.runs.c.id == run_id, db.runs.c.tenant_id == self.tenant_id)
                )
            ).first()
        return dict(row._mapping) if row else None

    def runs(self, batch_id: str | None = None) -> list[dict[str, Any]]:
        stmt = select(db.runs).where(db.runs.c.tenant_id == self.tenant_id)
        if batch_id:
            stmt = stmt.where(db.runs.c.batch_id == batch_id)
        with self.engine.connect() as c:
            rows = c.execute(stmt.order_by(db.runs.c.quarter, db.runs.c.created_at)).all()
        return [dict(r._mapping) for r in rows]

    def latest_run(self, quarter: str, batch_id: str | None = None) -> dict[str, Any] | None:
        stmt = select(db.runs).where(
            and_(
                db.runs.c.tenant_id == self.tenant_id,
                db.runs.c.quarter == quarter,
                db.runs.c.status != RunStatus.FAILED.value,
            )
        )
        if batch_id:
            stmt = stmt.where(db.runs.c.batch_id == batch_id)
        with self.engine.connect() as c:
            row = c.execute(stmt.order_by(db.runs.c.created_at.desc())).first()
        return dict(row._mapping) if row else None

    def findings(self, run_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(db.findings)
                .where(
                    and_(db.findings.c.run_id == run_id, db.findings.c.tenant_id == self.tenant_id)
                )
                .order_by(db.findings.c.rule_id)
            ).all()
        return [dict(r._mapping) for r in rows]

    def audit(self, event: str, detail: str, run_id: str | None = None) -> None:
        with self.engine.begin() as c:
            c.execute(
                insert(db.audit_events).values(
                    tenant_id=self.tenant_id, run_id=run_id, at=now(), event=event, detail=detail
                )
            )


def upsert_tenants(engine: Engine, rows: list[dict[str, str]]) -> None:
    """Provisioning: the one place tenant rows are created (admin operation)."""
    with engine.begin() as c:
        existing = {r.id for r in c.execute(select(db.tenants.c.id)).all()}
        for row in rows:
            if row["id"] in existing:
                c.execute(
                    db.tenants.update()
                    .where(db.tenants.c.id == row["id"])
                    .values(name=row["name"], api_token=row["api_token"])
                )
            else:
                c.execute(insert(db.tenants).values(**row))


def tenant_ids(engine: Engine) -> list[str]:
    with engine.connect() as c:
        return [r.id for r in c.execute(select(db.tenants.c.id).order_by(db.tenants.c.id))]
