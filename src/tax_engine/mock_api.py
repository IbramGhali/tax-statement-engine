"""Mock accounting provider API serving synthetic ledgers, with injectable faults.

Faults are what real accounting APIs do to you: transient 5xx errors, slow responses,
per-tenant rate limits with Retry-After, and outages. Error and slow-response decisions are
deterministic per (request, attempt), so a retried request eventually succeeds.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

PAGE_SIZE = 50


@dataclass(frozen=True)
class Faults:
    error_rate: float = 0.0  # share of requests answered with 503
    slow_rate: float = 0.0  # share of requests delayed by slow_seconds
    slow_seconds: float = 0.0
    rate_per_second: float = 0.0  # per-tenant limit; 0 disables
    burst: int = 10
    outage_seconds: float = 0.0  # every request fails for this long after the first one


class _Bucket:
    def __init__(self, rate: float, burst: int) -> None:
        self.rate, self.capacity = rate, float(burst)
        self.tokens, self.updated = float(burst), time.monotonic()
        self.lock = threading.Lock()

    def take(self) -> float:
        """Take a token. Returns 0 if allowed, else seconds until one is available."""
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return 0.0
            return (1 - self.tokens) / self.rate


def _fraction(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32


def create_mock_api(data_dir: Path, faults: Faults | None = None) -> FastAPI:
    faults = faults or Faults()

    def tenants() -> dict[str, dict[str, str]]:
        # Read per request so data generated after start-up (docker compose) is picked up.
        path = data_dir / "tenants.json"
        rows = json.loads(path.read_text("utf-8")) if path.is_file() else []
        return {t["id"]: t for t in rows}

    started: list[float] = []  # the outage clock starts at the first tenant request
    attempts: dict[str, int] = defaultdict(int)
    attempts_lock = threading.Lock()
    buckets: dict[str, _Bucket] = {}
    stats: dict[str, int] = defaultdict(int)
    api = FastAPI(title="Mock accounting API")
    api.state.stats = stats

    @api.middleware("http")
    async def inject_faults(request: Request, call_next: Any) -> Response:
        if not request.url.path.startswith("/tenants/"):
            return await call_next(request)  # type: ignore[no-any-return]
        stats["requests"] += 1
        tenant = request.url.path.split("/")[2]
        if not started:
            started.append(time.monotonic())
        if faults.outage_seconds and time.monotonic() - started[0] < faults.outage_seconds:
            stats["outage"] += 1
            return JSONResponse({"error": "service unavailable"}, status_code=503)
        if faults.rate_per_second:
            bucket = buckets.setdefault(tenant, _Bucket(faults.rate_per_second, faults.burst))
            wait = bucket.take()
            if wait:
                stats["rate_limited"] += 1
                return JSONResponse(
                    {"error": "rate limited"},
                    status_code=429,
                    headers={"Retry-After": f"{wait:.3f}"},
                )
        key = f"{request.url.path}?{request.url.query}"
        with attempts_lock:
            attempts[key] += 1
            attempt = attempts[key]
        if _fraction(key, str(attempt), "error") < faults.error_rate:
            stats["errors"] += 1
            return JSONResponse({"error": "temporary failure"}, status_code=503)
        if _fraction(key, str(attempt), "slow") < faults.slow_rate:
            stats["slow"] += 1
            await asyncio.sleep(faults.slow_seconds)  # slow upstream; other requests continue
        return await call_next(request)  # type: ignore[no-any-return]

    def ledger(tenant_id: str, quarter: str, authorization: str | None) -> dict[str, Any]:
        tenant = tenants().get(tenant_id)
        if tenant is None:
            raise HTTPException(404, "unknown tenant")
        if authorization != f"Bearer {tenant['api_token']}":
            raise HTTPException(401, "invalid token for tenant")
        path = data_dir / "ledgers" / tenant_id / f"{quarter}.json"
        if not path.is_file():
            raise HTTPException(404, "no data for quarter")
        data: dict[str, Any] = json.loads(path.read_text("utf-8"))
        return data

    @api.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/tenants/{tenant_id}/transactions")
    def transactions(
        tenant_id: str,
        quarter: str,
        page: int = 1,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        txns = ledger(tenant_id, quarter, authorization)["transactions"]
        start = (page - 1) * PAGE_SIZE
        return {
            "items": txns[start : start + PAGE_SIZE],
            "page": page,
            "has_more": start + PAGE_SIZE < len(txns),
        }

    @api.get("/tenants/{tenant_id}/reports/tax")
    def tax_report(
        tenant_id: str, quarter: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        report: dict[str, Any] = ledger(tenant_id, quarter, authorization)["tax_report"]
        return report

    @api.get("/tenants/{tenant_id}/payroll")
    def payroll(
        tenant_id: str, quarter: str, authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        result: dict[str, Any] = ledger(tenant_id, quarter, authorization)["payroll"]
        return result

    return api
