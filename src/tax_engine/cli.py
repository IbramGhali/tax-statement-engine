"""Command line: tse generate | run | eval | bench | replay | serve."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from tax_engine import queue, synth
from tax_engine.app import build_app
from tax_engine.config import Settings
from tax_engine.db import make_engine
from tax_engine.evaluation import EvalReport, check_gate, evaluate, write_baseline
from tax_engine.mock_api import Faults
from tax_engine.repository import TenantRepository, tenant_ids, upsert_tenants
from tax_engine.validation import RULES
from tax_engine.worker import BatchSummary, run_batch

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Quarterly sales-tax statements with deterministic figures.",
)
console = Console()


@app.command()
def generate(seed: Annotated[int, typer.Option(help="Random seed")] = 7) -> None:
    """Generate synthetic ledgers and ground truth, and provision the tenants."""
    settings = Settings.from_env()
    ledgers, planted = synth.write_dataset(settings.data_dir, seed)
    upsert_tenants(make_engine(settings.db_url), synth.load_tenants(settings.data_dir))
    console.print(
        f"Wrote {ledgers} ledgers (20 tenants x 4 quarters) with {planted} expected "
        f"findings to {settings.data_dir}"
    )


def _work(
    tenant: str | None, quarter: str | None, all_: bool, tenants: list[str]
) -> list[tuple[str, str]]:
    if all_:
        return [(t, q) for t in tenants for q in synth.QUARTERS]
    if not tenant or not quarter:
        console.print("[red]Use --all, or --tenant and --quarter.[/red]")
        raise typer.Exit(2)
    return [(tenant, quarter)]


def _print_batch(s: BatchSummary, stats: dict[str, int] | None, trips: int) -> None:
    done = sum(s.statuses.values())
    console.print(
        f"Batch [bold]{s.batch_id}[/bold]: {done} statements in {s.seconds:.1f}s "
        f"({60 * done / s.seconds:.0f} per minute). Status: {dict(s.statuses)}; "
        f"failed jobs: {s.failed_jobs}"
    )
    console.print(
        f"Provider calls: {s.requests} requests, {s.retries} retries, "
        f"{s.requeues} job re-queues, {trips} circuit-breaker trips"
        + (f"; provider injected {dict(stats)}" if stats else "")
    )


@app.command()
def run(
    all_: Annotated[bool, typer.Option("--all", help="Every tenant and quarter")] = False,
    tenant: Annotated[str | None, typer.Option(help="Tenant id, e.g. t01")] = None,
    quarter: Annotated[str | None, typer.Option(help="Quarter, e.g. 2026Q2")] = None,
    label: Annotated[str, typer.Option(help="Batch label")] = "manual",
) -> None:
    """Queue statement runs and process them with the worker pool."""
    with build_app() as a:
        work = _work(tenant, quarter, all_, tenant_ids(a.engine))
        batch_id = queue.create_batch(a.engine, label, a.config.version, work)
        summary = run_batch(a.pipeline, batch_id, a.settings.workers, a.settings.max_job_attempts)
        _print_batch(summary, a.provider_stats, a.adapter.breaker.trips)


def _collect(
    settings: Settings, batch_id: str
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    engine = make_engine(settings.db_url)
    runs, findings = [], {}
    for tid in tenant_ids(engine):  # evaluation reads each tenant through its own repository
        repo = TenantRepository(engine, tid)
        for r in repo.runs(batch_id):
            runs.append(r)
            findings[r["id"]] = repo.findings(r["id"])
    return runs, findings


def _print_eval(report: EvalReport) -> None:
    console.print(
        f"Figure accuracy: {report.lines_exact}/{report.lines_total} statement lines "
        f"exact ({report.line_accuracy:.2%}); {report.statements_exact}/"
        f"{report.runs} statements fully exact; missing: {report.missing_runs}"
    )
    table = Table(title="Validation rules against planted problems")
    for col in ("Rule", "Title", "Precision", "Recall", "TP", "FP", "FN"):
        table.add_column(col)
    for rid, s in sorted(report.rules.items()):
        table.add_row(
            rid,
            RULES[rid][0],
            f"{s.precision:.2f}",
            f"{s.recall:.2f}",
            str(s.tp),
            str(s.fp),
            str(s.fn),
        )
    console.print(table)
    console.print(
        f"Divergence gate: {report.blocked_correct}/{report.expected_blocks} expected "
        f"blocks made, {report.blocked_wrong} wrong blocks, {report.block_missed} "
        f"missed"
    )
    console.print(f"Commentary: {report.commentary}")
    for line in report.mismatches[:5]:
        console.print(f"[red]Mismatch[/red] {line}")


@app.command("eval")
def eval_cmd(
    batch: Annotated[str | None, typer.Option(help="Batch id (default latest)")] = None,
    gate: Annotated[bool, typer.Option(help="Exit 1 on any hard-gate failure")] = False,
    update_baseline: Annotated[bool, typer.Option(help="Write eval/baseline.json")] = False,
) -> None:
    """Score the latest batch against ground truth."""
    settings = Settings.from_env()
    engine = make_engine(settings.db_url)
    latest = queue.latest_batch(engine) if batch is None else {"id": batch}
    if not latest:
        console.print("[red]No finished batch. Run `tse run --all` first.[/red]")
        raise typer.Exit(1)
    runs, findings = _collect(settings, latest["id"])
    report = evaluate(runs, findings, synth.load_truth(settings.data_dir))
    _print_eval(report)
    out = settings.baseline_path.parent / "results" / "latest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    if update_baseline:
        write_baseline(report, settings.baseline_path)
        console.print(f"Baseline written to {settings.baseline_path}")
    if gate:
        ok, problems = check_gate(report, settings.baseline_path)
        for p in problems:
            console.print(f"[red]Gate:[/red] {p}")
        console.print("[green]All hard gates passed[/green]" if ok else "[red]Gate failed[/red]")
        if not ok:
            raise typer.Exit(1)


SCENARIOS: dict[str, tuple[Faults, float]] = {  # name -> (provider faults, client timeout)
    "clean": (Faults(), 2.0),
    "flaky": (
        Faults(error_rate=0.15, slow_rate=0.05, slow_seconds=0.8, rate_per_second=15, burst=5),
        0.5,
    ),
    "outage": (Faults(outage_seconds=2.0), 2.0),
}


@app.command()
def bench(
    scenarios: Annotated[
        str, typer.Option(help="Comma-separated scenarios")
    ] = "clean,flaky,outage",
) -> None:
    """Run every statement under each fault scenario; report resilience and accuracy."""
    settings = Settings.from_env()
    if settings.accounting_url:
        console.print(
            "[red]bench needs the in-process mock provider; unset TSE_ACCOUNTING_URL[/red]"
        )
        raise typer.Exit(2)
    truth = synth.load_truth(settings.data_dir)
    results = {}
    for name in scenarios.split(","):
        faults, timeout = SCENARIOS[name]
        with build_app(settings, faults=faults, request_timeout=timeout) as a:
            work = [(t, q) for t in tenant_ids(a.engine) for q in synth.QUARTERS]
            batch_id = queue.create_batch(a.engine, f"bench-{name}", a.config.version, work)
            s = run_batch(a.pipeline, batch_id, a.settings.workers, a.settings.max_job_attempts)
            trips = a.adapter.breaker.trips
            stats = dict(a.provider_stats or {})
        runs, findings = _collect(settings, batch_id)
        report = evaluate(runs, findings, truth)
        done = sum(s.statuses.values())
        results[name] = {
            "statements": len(work),
            "completed_or_blocked": done,
            "success_rate": round(done / len(work), 4),
            "failed_jobs": s.failed_jobs,
            "seconds": round(s.seconds, 1),
            "per_minute": round(60 * done / s.seconds),
            "requests": s.requests,
            "retries": s.retries,
            "requeues": s.requeues,
            "breaker_trips": trips,
            "provider_faults": stats,
            "line_accuracy": round(report.line_accuracy, 6),
        }
        console.print(f"[bold]{name}[/bold]: {results[name]}")
    out = settings.baseline_path.parent / "results" / "bench.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    table = Table(title="Resilience under injected provider faults")
    for col in (
        "Scenario",
        "Success",
        "Per min",
        "Retries",
        "Re-queues",
        "Breaker trips",
        "Figures exact",
    ):
        table.add_column(col)
    for name, r in results.items():
        table.add_row(
            name,
            f"{r['success_rate']:.0%}",
            str(r["per_minute"]),
            str(r["retries"]),
            str(r["requeues"]),
            str(r["breaker_trips"]),
            f"{r['line_accuracy']:.0%}",
        )
    console.print(table)


@app.command()
def replay(run_id: str, tenant: Annotated[str, typer.Option(help="Tenant id")]) -> None:
    """Recompute a stored run from its snapshot and confirm the figures match."""
    settings = replace(Settings.from_env())
    with build_app(settings) as a:
        ok, figures = a.pipeline.replay(tenant, run_id)
    console.print(
        ("[green]Replay matches[/green]" if ok else "[red]Replay differs[/red]")
        + f": net payable {figures.net_payable}"
    )
    if not ok:
        raise typer.Exit(1)


@app.command()
def worker(
    once: Annotated[bool, typer.Option(help="Drain open batches, then exit")] = False,
) -> None:
    """Process batches queued by the API (or any unfinished batch)."""
    import time

    with build_app() as a:
        while True:
            for batch_id in queue.open_batches(a.engine):
                summary = run_batch(
                    a.pipeline, batch_id, a.settings.workers, a.settings.max_job_attempts
                )
                _print_batch(summary, a.provider_stats, a.adapter.breaker.trips)
            if once:
                return
            time.sleep(1.0)


@app.command()
def provider(host: str = "127.0.0.1", port: int = 9000) -> None:
    """Serve the mock accounting provider on its own (used by docker compose)."""
    import uvicorn

    from tax_engine.mock_api import create_mock_api

    uvicorn.run(create_mock_api(Settings.from_env().data_dir), host=host, port=port)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the API and the statements page."""
    import uvicorn

    uvicorn.run("tax_engine.api:create_app", factory=True, host=host, port=port)


if __name__ == "__main__":
    app()
