"""Evaluation against ground truth: figure accuracy, validation precision/recall per rule,
divergence-gate decisions, commentary grounding, and resilience under injected faults."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from tax_engine.models import Severity
from tax_engine.validation import RULES


@dataclass
class RuleScore:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0


@dataclass
class EvalReport:
    runs: int
    missing_runs: int
    lines_total: int
    lines_exact: int
    statements_exact: int
    rules: dict[str, RuleScore] = field(default_factory=dict)
    blocked_correct: int = 0
    blocked_wrong: int = 0
    block_missed: int = 0
    expected_blocks: int = 0
    commentary: dict[str, int] = field(default_factory=dict)
    mismatches: list[str] = field(default_factory=list)

    @property
    def line_accuracy(self) -> float:
        return self.lines_exact / self.lines_total if self.lines_total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "missing_runs": self.missing_runs,
            "line_accuracy": round(self.line_accuracy, 6),
            "lines_exact": self.lines_exact,
            "lines_total": self.lines_total,
            "statements_exact": self.statements_exact,
            "rules": {
                k: {
                    "precision": round(v.precision, 3),
                    "recall": round(v.recall, 3),
                    "tp": v.tp,
                    "fp": v.fp,
                    "fn": v.fn,
                }
                for k, v in sorted(self.rules.items())
            },
            "gate": {
                "expected": self.expected_blocks,
                "blocked_correctly": self.blocked_correct,
                "blocked_wrongly": self.blocked_wrong,
                "missed": self.block_missed,
            },
            "commentary": self.commentary,
            "mismatches": self.mismatches[:20],
        }


def evaluate(
    runs: list[dict[str, Any]],
    findings: dict[str, list[dict[str, Any]]],
    truth: dict[tuple[str, str], dict[str, Any]],
) -> EvalReport:
    by_key = {(r["tenant_id"], r["quarter"]): r for r in runs}
    report = EvalReport(
        runs=len(runs),
        missing_runs=0,
        lines_total=0,
        lines_exact=0,
        statements_exact=0,
        rules={rid: RuleScore() for rid in RULES},
    )
    for key, t in sorted(truth.items()):
        run = by_key.get(key)
        if run is None or not run["figures"]:
            report.missing_runs += 1
            continue
        exact = True
        for name, expected in t["figures"].items():
            report.lines_total += 1
            if Decimal(run["figures"][name]) == Decimal(expected):
                report.lines_exact += 1
            else:
                exact = False
                report.mismatches.append(
                    f"{key[0]} {key[1]} {name}: engine {run['figures'][name]} vs truth {expected}"
                )
        report.statements_exact += exact

        fired = {
            f["rule_id"]
            for f in findings.get(run["id"], [])
            if f["severity"] != Severity.PASS.value
        }
        expected_rules = set(t["expected_rules"])
        for rid, score in report.rules.items():
            if rid in fired and rid in expected_rules:
                score.tp += 1
            elif rid in fired:
                score.fp += 1
            elif rid in expected_rules:
                score.fn += 1

        blocked = run["status"] == "blocked"
        report.expected_blocks += t["expected_blocked"]
        if blocked and t["expected_blocked"]:
            report.blocked_correct += 1
        elif blocked:
            report.blocked_wrong += 1
        elif t["expected_blocked"]:
            report.block_missed += 1

        status = run["commentary_status"] or "none"
        report.commentary[status] = report.commentary.get(status, 0) + 1
    return report


def check_gate(report: EvalReport, baseline_path: Path) -> tuple[bool, list[str]]:
    """Hard gates: figures must be exact, no rule may lose recall, the gate may not err."""
    problems = []
    if report.line_accuracy < 1.0:
        problems.append(f"figure accuracy {report.line_accuracy:.4%} (must be 100%)")
    if report.missing_runs:
        problems.append(f"{report.missing_runs} statements missing")
    if report.blocked_wrong or report.block_missed:
        problems.append(
            f"divergence gate errors: {report.blocked_wrong} wrong, {report.block_missed} missed"
        )
    if baseline_path.is_file():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        for rid, score in report.rules.items():
            floor = baseline.get("rule_recall", {}).get(rid)
            if floor is not None and score.recall < floor:
                problems.append(f"{rid} recall {score.recall:.2f} below baseline {floor:.2f}")
    return not problems, problems


def write_baseline(report: EvalReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "line_accuracy": report.line_accuracy,
                "rule_recall": {rid: round(s.recall, 3) for rid, s in sorted(report.rules.items())},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
