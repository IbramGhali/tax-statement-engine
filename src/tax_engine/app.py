"""Wiring. With no TSE_ACCOUNTING_URL, the mock provider is started in-process on a free
local port, so the client still talks real HTTP (timeouts, status codes, retries)."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import uvicorn
from sqlalchemy import Engine

from tax_engine.accounting.http_adapter import HttpAccountingAdapter
from tax_engine.config import Settings, TaxConfig, load_tax_config
from tax_engine.db import make_engine
from tax_engine.llm import make_client
from tax_engine.mock_api import Faults, create_mock_api
from tax_engine.pipeline import StatementPipeline


@dataclass
class App:
    settings: Settings
    config: TaxConfig
    engine: Engine
    pipeline: StatementPipeline
    adapter: HttpAccountingAdapter
    provider_stats: dict[str, int] | None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def mock_provider(settings: Settings, faults: Faults | None) -> Iterator[tuple[str, Any]]:
    api = create_mock_api(settings.data_dir, faults)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("mock provider did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}", api.state.stats
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@contextmanager
def build_app(
    settings: Settings | None = None,
    faults: Faults | None = None,
    request_timeout: float | None = None,
) -> Iterator[App]:
    settings = settings or Settings.from_env()
    config = load_tax_config(settings.tax_config_path)
    engine = make_engine(settings.db_url)
    llm = make_client(settings.llm_provider, settings.fake_commentary_drift)

    def assemble(url: str, stats: dict[str, int] | None) -> App:
        adapter = HttpAccountingAdapter(
            url,
            rate_per_second=settings.client_rate_per_second,
            concurrency=settings.client_concurrency,
            timeout=request_timeout or settings.request_timeout,
        )
        return App(
            settings,
            config,
            engine,
            StatementPipeline(settings, config, engine, adapter, llm),
            adapter,
            stats,
        )

    if settings.accounting_url:
        app = assemble(settings.accounting_url, None)
        try:
            yield app
        finally:
            app.adapter.close()
            engine.dispose()
        return
    with mock_provider(settings, faults) as (url, stats):
        app = assemble(url, stats)
        try:
            yield app
        finally:
            app.adapter.close()
            engine.dispose()
