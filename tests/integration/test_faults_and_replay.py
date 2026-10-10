"""End to end against the mock provider over real HTTP, with injected faults."""

from __future__ import annotations

from decimal import Decimal

from tax_engine import queue, synth
from tax_engine.app import build_app
from tax_engine.config import Settings
from tax_engine.mock_api import Faults
from tax_engine.repository import TenantRepository
from tax_engine.worker import run_batch

WORK = [(t, q) for t in ("t01", "t02", "t03") for q in synth.QUARTERS]


def _exact(settings: Settings, engine: object, batch: str) -> bool:
    truth = synth.load_truth(settings.data_dir)
    for tenant in ("t01", "t02", "t03"):
        for run in TenantRepository(engine, tenant).runs(batch):  # type: ignore[arg-type]
            expected = truth[(tenant, run["quarter"])]["figures"]
            if any(Decimal(run["figures"][k]) != Decimal(v) for k, v in expected.items()):
                return False
    return True


def test_flaky_provider_still_produces_exact_statements(settings: Settings) -> None:
    faults = Faults(error_rate=0.2, slow_rate=0.05, slow_seconds=0.6, rate_per_second=15, burst=3)
    with build_app(settings, faults=faults, request_timeout=0.4) as a:
        batch = queue.create_batch(a.engine, "flaky", a.config.version, WORK)
        summary = run_batch(a.pipeline, batch, workers=4, max_attempts=4)
        assert sum(summary.statuses.values()) == len(WORK) and summary.failed_jobs == 0
        assert summary.retries > 0
        assert _exact(settings, a.engine, batch)


def test_outage_trips_breaker_and_jobs_recover(settings: Settings) -> None:
    with build_app(settings, faults=Faults(outage_seconds=1.0)) as a:
        batch = queue.create_batch(a.engine, "outage", a.config.version, WORK)
        summary = run_batch(a.pipeline, batch, workers=4, max_attempts=6)
        assert a.adapter.breaker.trips >= 1
        assert summary.requeues > 0
        assert sum(summary.statuses.values()) == len(WORK)
        assert (a.provider_stats or {}).get("outage", 0) < 20  # breaker stopped the hammering


def test_replay_reproduces_stored_figures(settings: Settings) -> None:
    with build_app(settings) as a:
        batch = queue.create_batch(a.engine, "r", a.config.version, [("t04", "2026Q1")])
        run_batch(a.pipeline, batch, workers=1, max_attempts=2)
        run = TenantRepository(a.engine, "t04").runs(batch)[0]
        ok, figures = a.pipeline.replay("t04", run["id"])
        assert ok and str(figures.net_payable) == run["figures"]["net_payable"]
