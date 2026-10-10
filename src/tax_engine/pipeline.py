"""One statement run: fetch, snapshot, reconcile, calculate, validate, gate, comment, output.

Steps are idempotent: the raw inputs are stored by content hash, so a re-run of the same
inputs reuses the snapshot, and `replay` recomputes any run from its snapshot alone.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine

from tax_engine.accounting import AccountingAdapter, FetchStats
from tax_engine.calc import calculate, reconcile
from tax_engine.commentary import UNAVAILABLE, write_commentary
from tax_engine.config import Settings, TaxConfig
from tax_engine.llm import LLMClient
from tax_engine.models import Figures, Finding, RunStatus, Snapshot
from tax_engine.repository import TenantRepository
from tax_engine.validation import Context, is_blocked, validate
from tax_engine.workbook import build_workbook


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: RunStatus
    figures: Figures
    findings: list[Finding]
    commentary_status: str
    requests: int
    retries: int


def previous_quarter(quarter: str) -> str:
    year, qn = int(quarter[:4]), int(quarter[-1])
    return f"{year - 1}Q4" if qn == 1 else f"{year}Q{qn - 1}"


class StatementPipeline:
    def __init__(
        self,
        settings: Settings,
        config: TaxConfig,
        engine: Engine,
        accounting: AccountingAdapter,
        llm: LLMClient,
    ) -> None:
        self.settings = settings
        self.config = config
        self.engine = engine
        self.accounting = accounting
        self.llm = llm
        self.prompt_path = settings.prompts_dir / "commentary.v1.md"

    def fetch(
        self, repo: TenantRepository, quarter: str, batch_id: str | None, stats: FetchStats
    ) -> Snapshot:
        tenant = repo.tenant()
        if tenant is None:
            raise LookupError(f"unknown tenant {repo.tenant_id}")
        token = tenant["api_token"]
        prior: Decimal | None = None
        prev = previous_quarter(quarter)
        prior_run = repo.latest_run(prev, batch_id) or repo.latest_run(prev)
        if prior_run and prior_run["figures"]:
            prior = Decimal(prior_run["figures"]["net_payable"])
        return Snapshot(
            tenant_id=repo.tenant_id,
            quarter=quarter,
            transactions=self.accounting.transactions(repo.tenant_id, token, quarter, stats),
            tax_report=self.accounting.tax_report(repo.tenant_id, token, quarter, stats),
            payroll=self.accounting.payroll(repo.tenant_id, token, quarter, stats),
            prior_net_payable=prior,
        )

    def compute(self, snapshot: Snapshot) -> tuple[Figures, list[Finding], RunStatus, Context]:
        rec = reconcile(snapshot)
        figures = calculate(snapshot, rec)
        ctx = Context(snapshot, rec, figures, self.config)
        findings = validate(ctx)
        status = RunStatus.BLOCKED if is_blocked(findings, self.config) else RunStatus.COMPLETE
        return figures, findings, status, ctx

    def run(self, tenant_id: str, quarter: str, batch_id: str | None = None) -> RunResult:
        started = time.perf_counter()
        repo = TenantRepository(self.engine, tenant_id)
        stats = FetchStats()
        snapshot = self.fetch(repo, quarter, batch_id, stats)  # may raise; caller re-queues
        sha = repo.save_snapshot(snapshot)
        figures, findings, status, ctx = self.compute(snapshot)
        tenant = repo.tenant() or {"name": tenant_id}

        if status is RunStatus.BLOCKED:
            # A blocked statement must not look signable; no commentary is generated.
            text, cstatus, model, pversion = UNAVAILABLE, "skipped_blocked", None, None
        else:
            note = write_commentary(
                self.llm,
                self.settings.commentary_model,
                self.prompt_path,
                tenant_id,
                tenant["name"],
                quarter,
                figures,
                snapshot.prior_net_payable,
                findings,
            )
            text, cstatus, model, pversion = (
                note.text,
                note.status,
                note.model,
                note.prompt_version,
            )

        run_id = f"run-{uuid.uuid4().hex[:12]}"
        out = self.settings.output_dir / tenant_id / quarter / f"{run_id}.xlsx"
        build_workbook(
            out,
            tenant["name"],
            snapshot,
            ctx.rec,
            figures,
            findings,
            status.value,
            text,
            {
                "run_id": run_id,
                "snapshot_sha256": sha,
                "tax_config": self.config.version,
                "commentary_prompt": pversion or "-",
                "commentary_model": model or "-",
            },
        )
        repo.save_run(
            run_id=run_id,
            batch_id=batch_id,
            quarter=quarter,
            status=status,
            config_version=self.config.version,
            snapshot_sha=sha,
            figures=figures,
            findings=findings,
            commentary=text,
            commentary_status=cstatus,
            model=model,
            prompt_version=pversion,
            api_requests=stats.requests,
            api_retries=stats.retries,
            seconds=time.perf_counter() - started,
            workbook_path=str(Path(out)),
        )
        return RunResult(run_id, status, figures, findings, cstatus, stats.requests, stats.retries)

    def replay(self, tenant_id: str, run_id: str) -> tuple[bool, Figures]:
        """Recompute a stored run from its snapshot. True if the figures match exactly."""
        repo = TenantRepository(self.engine, tenant_id)
        run = repo.run(run_id)
        if run is None or not run["snapshot_sha"]:
            raise LookupError(f"run {run_id} not found for tenant {tenant_id}")
        snapshot = repo.snapshot(run["snapshot_sha"])
        if snapshot is None:
            raise LookupError("snapshot missing")
        figures, _, _, _ = self.compute(snapshot)
        stored = Figures.model_validate(run["figures"])
        return figures == stored, figures
