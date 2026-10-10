"""All 80 statements against the generator's independent ground truth, with the same hard
gates as CI: exact figures, no lost rule recall, no divergence-gate errors."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tax_engine import queue, synth
from tax_engine.app import build_app
from tax_engine.config import Settings
from tax_engine.evaluation import EvalReport, check_gate, evaluate
from tax_engine.repository import TenantRepository, tenant_ids
from tax_engine.worker import run_batch


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> EvalReport:
    from tax_engine.db import make_engine
    from tax_engine.repository import upsert_tenants

    tmp = tmp_path_factory.mktemp("eval")
    settings = replace(
        Settings(), data_dir=tmp / "data", output_dir=tmp / "out", state_dir=tmp / "state"
    )
    synth.write_dataset(settings.data_dir)
    upsert_tenants(make_engine(settings.db_url), synth.load_tenants(settings.data_dir))
    with build_app(settings) as a:
        work = [(t, q) for t in tenant_ids(a.engine) for q in synth.QUARTERS]
        batch = queue.create_batch(a.engine, "eval", a.config.version, work)
        run_batch(a.pipeline, batch, workers=4, max_attempts=4)
        runs, findings = [], {}
        for tid in tenant_ids(a.engine):
            repo = TenantRepository(a.engine, tid)
            for r in repo.runs(batch):
                runs.append(r)
                findings[r["id"]] = repo.findings(r["id"])
    return evaluate(runs, findings, synth.load_truth(settings.data_dir))


def test_every_figure_is_exact(report: EvalReport) -> None:
    assert report.missing_runs == 0
    assert report.line_accuracy == 1.0, report.mismatches[:5]


def test_hard_gates_pass_against_committed_baseline(report: EvalReport) -> None:
    ok, problems = check_gate(report, Settings().baseline_path)
    assert ok, problems


def test_every_planted_problem_is_caught(report: EvalReport) -> None:
    assert all(score.fn == 0 for score in report.rules.values())


def test_divergence_gate_is_exact(report: EvalReport) -> None:
    assert report.expected_blocks > 0
    assert report.blocked_correct == report.expected_blocks
    assert report.blocked_wrong == 0


def test_ungrounded_commentary_is_caught(report: EvalReport) -> None:
    assert report.commentary.get("ungrounded", 0) > 0  # the fake drifts ~5% of notes
    assert report.commentary.get("ok", 0) > 60
