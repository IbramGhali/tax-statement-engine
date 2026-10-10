"""Settings from environment variables (and an optional .env). Defaults run offline."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class TaxConfig(BaseModel):
    version: str
    jurisdiction: str
    sales_tax_rate: Decimal
    divergence_tolerance: Decimal
    strict_mode: bool
    rate_tolerance: Decimal
    swing_threshold: Decimal


def load_tax_config(path: Path) -> TaxConfig:
    with path.open(encoding="utf-8") as fh:
        return TaxConfig.model_validate(yaml.safe_load(fh))


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default) or default


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    llm_provider: str = "fake"
    commentary_model: str = "claude-opus-5-5"
    accounting_url: str = ""  # empty: start the mock accounting API in-process
    workers: int = 4
    client_rate_per_second: float = 20.0
    client_concurrency: int = 8
    request_timeout: float = 2.0
    max_job_attempts: int = 4
    fake_commentary_drift: float = 0.05
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "outputs")
    state_dir: Path = field(default_factory=lambda: PROJECT_ROOT / ".tse")
    tax_config_path: Path = field(default_factory=lambda: PROJECT_ROOT / "config" / "tax.yaml")
    prompts_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "prompts")
    baseline_path: Path = field(default_factory=lambda: PROJECT_ROOT / "eval" / "baseline.json")

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.state_dir / 'tse.db').as_posix()}"

    @classmethod
    def from_env(cls) -> Settings:
        _load_dotenv(PROJECT_ROOT / ".env")
        d = cls()
        return cls(
            database_url=_env("TSE_DATABASE_URL", d.database_url),
            llm_provider=_env("TSE_LLM_PROVIDER", d.llm_provider).lower(),
            commentary_model=_env("TSE_COMMENTARY_MODEL", d.commentary_model),
            accounting_url=_env("TSE_ACCOUNTING_URL", d.accounting_url),
            workers=int(_env("TSE_WORKERS", str(d.workers))),
            client_rate_per_second=float(_env("TSE_CLIENT_RATE", str(d.client_rate_per_second))),
            client_concurrency=int(_env("TSE_CLIENT_CONCURRENCY", str(d.client_concurrency))),
            request_timeout=float(_env("TSE_REQUEST_TIMEOUT", str(d.request_timeout))),
            max_job_attempts=int(_env("TSE_MAX_JOB_ATTEMPTS", str(d.max_job_attempts))),
            fake_commentary_drift=float(
                _env("TSE_FAKE_COMMENTARY_DRIFT", str(d.fake_commentary_drift))
            ),
            data_dir=Path(_env("TSE_DATA_DIR", str(d.data_dir))),
            output_dir=Path(_env("TSE_OUTPUT_DIR", str(d.output_dir))),
            state_dir=Path(_env("TSE_STATE_DIR", str(d.state_dir))),
        )
