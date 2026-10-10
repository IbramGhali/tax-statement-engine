"""Schema and engine. SQLite by default, Postgres via TSE_DATABASE_URL (same schema)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)

metadata = MetaData()

tenants = Table(
    "tenants",
    metadata,
    Column("id", String(16), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("api_token", String(64), nullable=False),  # provider token (synthetic here)
)

batches = Table(
    "batches",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("label", String(60), nullable=False),
    Column("config_version", String(40), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("batch_id", String(40), ForeignKey("batches.id"), nullable=False),
    Column("tenant_id", String(16), ForeignKey("tenants.id"), nullable=False),
    Column("quarter", String(6), nullable=False),
    Column("status", String(12), nullable=False),  # queued | running | done | failed
    Column("attempts", Integer, nullable=False, default=0),
    Column("not_before", Float, nullable=False, default=0.0),  # epoch seconds
    Column("last_error", Text),
    Column("run_id", String(40)),
    UniqueConstraint("batch_id", "tenant_id", "quarter"),
    Index("ix_jobs_claim", "status", "not_before"),
)

snapshots = Table(
    "snapshots",
    metadata,
    Column("sha256", String(64), primary_key=True),
    Column("tenant_id", String(16), ForeignKey("tenants.id"), nullable=False),
    Column("body", JSON, nullable=False),
)

runs = Table(
    "runs",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("tenant_id", String(16), ForeignKey("tenants.id"), nullable=False),
    Column("batch_id", String(40), ForeignKey("batches.id")),
    Column("quarter", String(6), nullable=False),
    Column("status", String(12), nullable=False),  # complete | blocked | failed
    Column("config_version", String(40), nullable=False),
    Column("snapshot_sha", String(64)),
    Column("figures", JSON),
    Column("commentary", Text),
    Column("commentary_status", String(16)),
    Column("model", String(60)),
    Column("prompt_version", String(40)),
    Column("api_requests", Integer, nullable=False, default=0),
    Column("api_retries", Integer, nullable=False, default=0),
    Column("seconds", Float, nullable=False, default=0.0),
    Column("workbook_path", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("ix_runs_tenant_quarter", "tenant_id", "quarter"),
)

findings = Table(
    "findings",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(40), ForeignKey("runs.id"), nullable=False),
    Column("tenant_id", String(16), ForeignKey("tenants.id"), nullable=False),
    Column("rule_id", String(8), nullable=False),
    Column("severity", String(8), nullable=False),
    Column("message", Text, nullable=False),
    Column("value", String(32)),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant_id", String(16), ForeignKey("tenants.id"), nullable=False),
    Column("run_id", String(40)),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("event", String(40), nullable=False),
    Column("detail", Text),
)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _pragmas(conn, _record):  # type: ignore[no-untyped-def]
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    else:
        engine = create_engine(url, pool_size=10, max_overflow=10, pool_pre_ping=True)
    metadata.create_all(engine)
    return engine
