"""Database-backed job queue.

Claiming is atomic: `FOR UPDATE SKIP LOCKED` on Postgres, a single guarded UPDATE on SQLite.
A tenant's quarters run in order (a job waits until earlier quarters in its batch are no
longer queued or running), because each quarter compares itself with the one before.
Transient failures are re-queued with a delay; permanent ones fail the job.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, and_, exists, func, insert, select, update
from sqlalchemy.orm import aliased

from tax_engine import db
from tax_engine.repository import now

_sqlite_claim_lock = threading.Lock()


@dataclass(frozen=True)
class Job:
    id: int
    batch_id: str
    tenant_id: str
    quarter: str
    attempts: int


def create_batch(
    engine: Engine, label: str, config_version: str, work: list[tuple[str, str]]
) -> str:
    batch_id = now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    with engine.begin() as c:
        c.execute(
            insert(db.batches).values(
                id=batch_id, created_at=now(), label=label, config_version=config_version
            )
        )
        for tenant_id, quarter in work:
            c.execute(
                insert(db.jobs).values(
                    batch_id=batch_id,
                    tenant_id=tenant_id,
                    quarter=quarter,
                    status="queued",
                    attempts=0,
                    not_before=0.0,
                )
            )
    return batch_id


def claim(engine: Engine, batch_id: str) -> Job | None:
    earlier = aliased(db.jobs)
    blocked_by_earlier = exists().where(
        and_(
            earlier.c.batch_id == db.jobs.c.batch_id,
            earlier.c.tenant_id == db.jobs.c.tenant_id,
            earlier.c.quarter < db.jobs.c.quarter,
            earlier.c.status.in_(("queued", "running")),
        )
    )
    candidate = (
        select(db.jobs)
        .where(
            and_(
                db.jobs.c.batch_id == batch_id,
                db.jobs.c.status == "queued",
                db.jobs.c.not_before <= time.time(),
                ~blocked_by_earlier,
            )
        )
        .order_by(db.jobs.c.quarter, db.jobs.c.id)
        .limit(1)
    )
    postgres = engine.dialect.name == "postgresql"
    if postgres:
        candidate = candidate.with_for_update(skip_locked=True, of=db.jobs)
    lock = threading.Lock() if postgres else _sqlite_claim_lock
    with lock, engine.begin() as c:
        row = c.execute(candidate).first()
        if row is None:
            return None
        c.execute(
            update(db.jobs)
            .where(db.jobs.c.id == row.id)
            .values(status="running", attempts=row.attempts + 1)
        )
    return Job(row.id, row.batch_id, row.tenant_id, row.quarter, row.attempts + 1)


def complete(engine: Engine, job: Job, run_id: str) -> None:
    with engine.begin() as c:
        c.execute(
            update(db.jobs)
            .where(db.jobs.c.id == job.id)
            .values(status="done", run_id=run_id, last_error=None)
        )


def retry_later(engine: Engine, job: Job, error: str, max_attempts: int) -> bool:
    """Re-queue with linear backoff, or fail after max_attempts. Returns True if re-queued."""
    requeue = job.attempts < max_attempts
    with engine.begin() as c:
        c.execute(
            update(db.jobs)
            .where(db.jobs.c.id == job.id)
            .values(
                status="queued" if requeue else "failed",
                last_error=error[:500],
                not_before=time.time() + 0.5 * job.attempts,
            )
        )
    return requeue


def fail(engine: Engine, job: Job, error: str) -> None:
    with engine.begin() as c:
        c.execute(
            update(db.jobs)
            .where(db.jobs.c.id == job.id)
            .values(status="failed", last_error=error[:500])
        )


def pending(engine: Engine, batch_id: str) -> int:
    with engine.connect() as c:
        return int(
            c.execute(
                select(func.count())
                .select_from(db.jobs)
                .where(
                    and_(
                        db.jobs.c.batch_id == batch_id, db.jobs.c.status.in_(("queued", "running"))
                    )
                )
            ).scalar_one()
        )


def finish_batch(engine: Engine, batch_id: str) -> None:
    with engine.begin() as c:
        c.execute(update(db.batches).where(db.batches.c.id == batch_id).values(finished_at=now()))


def batch_jobs(engine: Engine, batch_id: str) -> list[dict[str, Any]]:
    with engine.connect() as c:
        rows = c.execute(select(db.jobs).where(db.jobs.c.batch_id == batch_id)).all()
    return [dict(r._mapping) for r in rows]


def open_batches(engine: Engine) -> list[str]:
    """Batches that still have queued or running jobs (for example queued through the API)."""
    with engine.connect() as c:
        rows = c.execute(
            select(db.jobs.c.batch_id).where(db.jobs.c.status.in_(("queued", "running"))).distinct()
        ).all()
    return sorted(r.batch_id for r in rows)


def latest_batch(engine: Engine, label: str | None = None) -> dict[str, Any] | None:
    stmt = select(db.batches).where(db.batches.c.finished_at.is_not(None))
    if label:
        stmt = stmt.where(db.batches.c.label == label)
    with engine.connect() as c:
        row = c.execute(stmt.order_by(db.batches.c.created_at.desc())).first()
    return dict(row._mapping) if row else None
