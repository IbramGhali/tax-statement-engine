"""Reviewer commentary: the only place an LLM is used. It never computes or changes a figure.

Every number in the note must match a number we supplied. If one does not (a rounded or
invented figure), or the call fails, the note is replaced with "Commentary unavailable" and
the run still completes: the statement does not depend on the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from string import Template

import yaml

from tax_engine.llm import LLMClient, LLMError, LLMRequest
from tax_engine.models import FIGURE_LABELS, Figures, Finding, Severity
from tax_engine.money import fmt
from tax_engine.validation import RULES

UNAVAILABLE = "Commentary unavailable."
NUMBER = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?%?")
SEVERITY_ORDER = {Severity.FAIL: 0, Severity.WARN: 1, Severity.INFORM: 2, Severity.PASS: 3}


@dataclass(frozen=True)
class Commentary:
    text: str
    status: str  # ok | ungrounded | llm_error
    model: str
    prompt_version: str
    input_tokens: int = 0
    output_tokens: int = 0


def load_prompt(path: Path) -> tuple[str, Template, Template]:
    _, front, body = path.read_text(encoding="utf-8").split("---", 2)
    meta = yaml.safe_load(front)
    system, user = body.split("=== USER ===", 1)
    return str(meta["version"]), Template(system.strip()), Template(user.strip())


def _change(figures: Figures, prior: Decimal | None) -> str:
    if prior is None or prior == 0:
        return "not available"
    return f"{(figures.net_payable - prior) / abs(prior):+.0%}"


def grounded(text: str, allowed: set[str]) -> bool:
    """Every number in the text must appear verbatim in the supplied facts."""

    def norm(token: str) -> str:
        return token.replace("$", "").replace(",", "").lstrip("+-").rstrip(".")

    allowed_norm = {norm(a) for a in allowed}
    return all(norm(tok) in allowed_norm for tok in NUMBER.findall(text))


def write_commentary(
    llm: LLMClient,
    model: str,
    prompt_path: Path,
    tenant_id: str,
    business: str,
    quarter: str,
    figures: Figures,
    prior: Decimal | None,
    findings: list[Finding],
) -> Commentary:
    version, system_t, user_t = load_prompt(prompt_path)
    shown = sorted(
        (f for f in findings if f.severity is not Severity.PASS),
        key=lambda f: (SEVERITY_ORDER[f.severity], f.rule_id),
    )
    fig_text = {k: fmt(v) for k, v in figures.as_dict().items()}
    change = _change(figures, prior)
    payload = {
        "tenant_id": tenant_id,
        "quarter": quarter,
        "figures": fig_text,
        "change": change,
        "findings": [
            {
                "rule_id": f.rule_id,
                "severity": f.severity.value,
                "title": RULES[f.rule_id][0],
                "message": f.message,
            }
            for f in shown
        ],
    }
    user = user_t.substitute(
        business=business,
        quarter=quarter,
        change=change,
        figures="\n".join(f"- {FIGURE_LABELS[k]}: {v}" for k, v in fig_text.items()),
        findings="\n".join(
            f"- [{f.severity.value}] {RULES[f.rule_id][0]}: {f.message}" for f in shown
        )
        or "- none",
    )
    try:
        response = llm.complete(
            LLMRequest(model=model, system=system_t.substitute(), user=user, payload=payload)
        )
    except LLMError:
        return Commentary(UNAVAILABLE, "llm_error", model, version)
    # Numbers the note may use: the prompt's own numbers (figures, change, findings, quarter).
    allowed = set(NUMBER.findall(user))
    if not grounded(response.text, allowed):
        return Commentary(
            UNAVAILABLE,
            "ungrounded",
            response.model,
            version,
            response.input_tokens,
            response.output_tokens,
        )
    return Commentary(
        response.text, "ok", response.model, version, response.input_tokens, response.output_tokens
    )
