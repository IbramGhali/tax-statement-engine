"""Worker pool: N threads claim jobs from the queue until the batch is drained."""

from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass, field

from tax_engine import queue
from tax_engine.accounting import ProviderError, TransientProviderError
from tax_engine.pipeline import StatementPipeline


@dataclass
class BatchSummary:
    batch_id: str
    seconds: float = 0.0
    statuses: Counter[str] = field(default_factory=Counter)
    requeues: int = 0
    failed_jobs: int = 0
    requests: int = 0
    retries: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


def run_batch(
    pipeline: StatementPipeline, batch_id: str, workers: int, max_attempts: int
) -> BatchSummary:
    summary = BatchSummary(batch_id)
    engine = pipeline.engine
    started = time.perf_counter()

    def work() -> None:
        while True:
            job = queue.claim(engine, batch_id)
            if job is None:
                if queue.pending(engine, batch_id) == 0:
                    return
                time.sleep(0.05)  # waiting on backoff or an earlier quarter
                continue
            try:
                result = pipeline.run(job.tenant_id, job.quarter, batch_id)
            except TransientProviderError as exc:
                requeued = queue.retry_later(engine, job, f"transient: {exc}", max_attempts)
                with summary.lock:
                    summary.requeues += requeued
                    summary.failed_jobs += not requeued
                continue
            except (ProviderError, LookupError, ValueError) as exc:
                queue.fail(engine, job, f"permanent: {exc}")
                with summary.lock:
                    summary.failed_jobs += 1
                continue
            except Exception as exc:  # a bug must fail one job, not hang the batch
                queue.fail(engine, job, f"error: {type(exc).__name__}: {exc}")
                with summary.lock:
                    summary.failed_jobs += 1
                continue
            queue.complete(engine, job, result.run_id)
            with summary.lock:
                summary.statuses[result.status.value] += 1
                summary.requests += result.requests
                summary.retries += result.retries

    threads = [threading.Thread(target=work, name=f"worker-{i}") for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    queue.finish_batch(engine, batch_id)
    summary.seconds = time.perf_counter() - started
    return summary
