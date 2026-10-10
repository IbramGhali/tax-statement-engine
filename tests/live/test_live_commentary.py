"""One real commentary call. Excluded by default; costs real money.

Run with: TSE_LIVE_TESTS=1 uv run --extra anthropic pytest -m live
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

from tax_engine.commentary import write_commentary
from tax_engine.config import Settings
from tax_engine.llm import make_client
from tests.unit.test_resilience_commentary_synth import FIGS


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("TSE_LIVE_TESTS") != "1", reason="set TSE_LIVE_TESTS=1")
def test_live_commentary_is_grounded() -> None:
    settings = Settings.from_env()
    note = write_commentary(
        make_client("anthropic"),
        settings.commentary_model,
        settings.prompts_dir / "commentary.v1.md",
        "t01",
        "Northwind Bakery",
        "2026Q2",
        FIGS,
        Decimal("1800.00"),
        [],
    )
    assert note.status == "ok", note.text
