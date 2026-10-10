# tax-statement-engine

A multi-tenant service that prepares quarterly sales-tax statements from accounting data. Deterministic code owns every number, a validation engine can block a run, and an LLM does exactly one job: the reviewer's commentary. **720 of 720 statement lines match independent ground truth, including under injected provider failures.**

[![CI](https://github.com/IbramGhali/tax-statement-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/IbramGhali/tax-statement-engine/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

The problem: at quarter end an accounting team prepares statements for dozens of client businesses against a flaky upstream API, and every figure must be right. The design question this repository answers is where an LLM belongs in that work, and the answer is "almost nowhere". Code reconciles, classifies and adds up. Independent rules check the result against other sources. A gate stops a statement when its inputs disagree. A model writes a short note for the person signing off, every number in that note is checked, and the statement does not depend on the model at all.

> **Synthetic data, fictional rules.** Twenty fictional businesses, generated ledgers and a made-up flat 10% sales tax. This is not real tax law and not advice.

## At a glance

| | |
| --- | --- |
| Figure accuracy | **720 / 720** statement lines exact against ground truth the generator computes independently (80 statements, 9 lines each) |
| Validation engine | 9 rules, every planted problem caught (108 of 108), no false alarms on synthetic data |
| Divergence gate | 7 of 7 expected blocks, 0 wrong blocks |
| Commentary check | 3 of 73 notes rejected for a rounded figure and replaced with "Commentary unavailable"; 70 passed |
| Resilience | 100% of statements completed, figures still exact, under a flaky provider (15% errors, slow responses) and a 2-second outage |
| Throughput | roughly 300 to 1,000 statements per minute with 4 workers on a laptop (varies with machine load) |
| Tests | 42: 26 unit, 8 contract, 3 integration, 5 evaluation, plus 1 live-model test excluded by default |
| Runs on | Windows, macOS, Linux; Python 3.11+; SQLite by default, Postgres via Docker; offline by default |

Every number comes from `uv run tse eval` and `uv run tse bench` on a fresh clone.

## Demo

Two real statements from one run. First, a completed statement for Northwind Bakery, 2025Q3:

| Line | Amount |
| --- | --- |
| Total sales (incl. tax) | $49,914.04 |
| Tax on sales | $3,635.42 |
| Tax credits on purchases | $2,647.13 |
| Tax withheld from wages | $37,092.20 |
| **Net amount payable** | **$38,080.49** |

| Rule | Result | Message |
| --- | --- | --- |
| V03 | fail | Tax withheld $37,092.20 exceeds gross wages $35,600.00. |
| V05 | warn | 1 possible duplicate(s): same date, counterparty, amount and reference. Kept in the statement for review. |

> **Reviewer note (generated, every number checked):** The net amount payable for 2025Q3 is $38,080.49, from tax on sales of $3,635.42 less credits of $2,647.13 plus $37,092.20 withheld from wages. The main item to check is withholding within gross wages: Tax withheld $37,092.20 exceeds gross wages $35,600.00.

Second, Bluegum Cafe, 2026Q2, which was **blocked**. The provider's tax report showed output tax $70.63 higher than the transactions add up to (V04, tolerance $1.00). The figures are stored so the gap is visible, but no commentary is written and the status cannot be mistaken for a finished statement.

Each statement also produces an Excel workbook. Its Summary cells are `SUMIFS` formulas over a Transactions sheet that marks every excluded row and why, and a Provenance sheet records the snapshot hash, config version, prompt version and model. `uv run tse serve` shows all statements at http://127.0.0.1:8000.

## How it works

```mermaid
flowchart LR
    T[tse run --all] --> Q[(job queue<br/>quarters in order per tenant)]
    Q --> W[worker pool]
    W --> F[fetch via HTTP<br/>rate limit, retries,<br/>circuit breaker, timeout]
    P[accounting provider<br/>mock with fault injection] --> F
    F --> S[(snapshot<br/>stored by SHA-256)]
    S --> R[reconcile + calculate<br/>Decimal only]
    R --> V[9 validation rules<br/>pass / inform / warn / fail]
    V --> G{divergence gate<br/>report vs transactions}
    G -- agree --> C[commentary<br/>one LLM call,<br/>numbers checked]
    G -- disagree --> B[blocked<br/>no commentary]
    C --> O[workbook + JSON]
    B --> O
    O --> DB[(runs, findings, audit<br/>tenant-scoped)]
    W -. transient failure .-> Q
```

1. **Queue.** `tse run --all` creates a batch of 80 jobs (20 tenants x 4 quarters). Workers claim jobs atomically: `FOR UPDATE SKIP LOCKED` on Postgres, a guarded update on SQLite. A tenant's quarters run in order because each one compares itself with the quarter before.
2. **Fetch.** The adapter calls the provider over real HTTP. Every request goes through:
   - a per-tenant token bucket (rate limit) and a cap on concurrent requests
   - a circuit breaker and a request timeout
   - retries with backoff that honour `Retry-After`

   If the provider is still down, the job goes back on the queue with a delay. Bad credentials or missing data fail the job permanently.
3. **Snapshot.** Everything fetched is validated into typed models and stored by content hash. From here on the run never touches the provider, so it can be replayed exactly.
4. **Calculate.** Reconciliation drops uncoded and out-of-period transactions and records each exclusion. Calculation sums `Decimal` amounts and rounds half-up to cents. No floats, no model.
5. **Validate and gate.** Nine independent rules compare the transactions, the provider's report, payroll and last quarter. If the report and the transactions disagree by more than $1.00, the statement is blocked.
6. **Comment.** One call writes 3 to 4 sentences from the computed figures and findings. Any number in the note that was not in the input, or any failed call, replaces the note with "Commentary unavailable".
7. **Store and output.** Run, findings, snapshot link and an audit event are written through a tenant-scoped repository, and the workbook goes to a per-tenant folder.

## Key decisions

| Decision | Why | ADR |
| --- | --- | --- |
| The LLM writes commentary only; it never computes or changes a figure | Statement figures have right answers; a silent model error in a regulated number is unacceptable | [0001](docs/adr/0001-llm-writes-commentary-only.md) |
| Independent cross-source rules, plus a gate that blocks on divergence | Each failure names the two sources that disagree; a warning alone gets ignored at quarter end | [0002](docs/adr/0002-independent-rules-and-divergence-gate.md) |
| Raw inputs are snapshotted by hash; every run can be replayed | Accounting data changes after the fact; a statement must be explainable months later | [0003](docs/adr/0003-snapshot-inputs-for-replay.md) |
| Tenant isolation lives in one repository layer and CI enforces it | Cross-tenant leaks come from one forgotten filter; make forgetting impossible | [0004](docs/adr/0004-tenant-isolation-in-one-layer.md) |

## Quickstart

Needs [uv](https://docs.astral.sh/uv/). No API key, no Docker and no network by default: the mock provider starts in-process on a local port.

```bash
git clone https://github.com/IbramGhali/tax-statement-engine && cd tax-statement-engine
uv sync
uv run tse generate
uv run tse run --all
uv run tse eval
```

Then try `uv run tse bench` (fault scenarios), `uv run tse serve`, or `uv run tse replay <run-id> --tenant t01`.

**With Postgres and the provider as separate services:**

```bash
docker compose up -d --build
uv sync --extra postgres
export TSE_DATABASE_URL=postgresql+psycopg://tse:tse@localhost:5433/tse TSE_ACCOUNTING_URL=http://localhost:9000
uv run tse generate && uv run tse run --all && uv run tse eval
```

PowerShell sets the two variables with `$env:NAME="..."`. The statements page is then on http://localhost:8000.

**With Claude writing the commentary:** `uv sync --extra anthropic`, set `ANTHROPIC_API_KEY`, then run with `TSE_LLM_PROVIDER=anthropic`.

## Configuration

Environment variables or a local `.env` (see [.env.example](.env.example)). Tax rules live in [config/tax.yaml](config/tax.yaml), whose version is recorded on every run.

| Variable | Default | Effect |
| --- | --- | --- |
| `TSE_DATABASE_URL` | SQLite in `.tse/` | Any SQLAlchemy URL; Postgres needs the `postgres` extra |
| `TSE_ACCOUNTING_URL` | empty | Empty starts the mock provider in-process; otherwise the provider's base URL |
| `TSE_LLM_PROVIDER` | `fake` | `fake` (offline, deterministic) or `anthropic` |
| `TSE_COMMENTARY_MODEL` | `claude-opus-5-5` | Model for the commentary call |
| `TSE_WORKERS` | `4` | Worker threads |
| `TSE_CLIENT_RATE`, `TSE_CLIENT_CONCURRENCY` | `20`, `8` | Requests per second per tenant, and concurrent requests overall |
| `TSE_REQUEST_TIMEOUT` | `2.0` | Seconds per provider request |
| `TSE_MAX_JOB_ATTEMPTS` | `4` | Re-queues before a job fails |
| `TSE_FAKE_COMMENTARY_DRIFT` | `0.05` | Share of fake notes that round a figure, to exercise the check |

| `config/tax.yaml` key | Default | Effect |
| --- | --- | --- |
| `sales_tax_rate` | `0.10` | Rate used to check recorded tax amounts |
| `divergence_tolerance` | `1.00` | Largest allowed gap between the provider report and transactions |
| `strict_mode` | `true` | Whether a divergence blocks the statement |
| `swing_threshold` | `0.50` | Quarter-on-quarter movement that raises an inform finding |

## Accounting policy

These choices are applied the same way by the engine and by the generator's ground truth, and the README states them so a reviewer can disagree with them explicitly:
- Uncoded transactions and transactions dated outside the quarter are excluded from every figure and reported (V01, V09).
- Recorded tax amounts are used as booked, and flagged when they are inconsistent with the rate (V02).
- Tax recorded on an exempt purchase is not claimable; the purchase still counts at cost (V08).
- Possible duplicates are kept and flagged for a person to decide (V05).
- Only the divergence gate (V04) blocks automatically. Other `fail` findings, such as V03, must be cleared by the reviewer before sign-off.

## Evaluation

**What is measured, and against what.** `tse generate` writes 80 ledgers and, separately, the correct figures for each one. Those figures are accumulated while the generator builds the transactions, not by calling the engine, so the evaluation cannot simply agree with itself. Problems are planted at known rates, and each is recorded with the rule that must catch it.

| Measure | Result |
| --- | --- |
| Statement lines exact | 720 / 720 (100%) |
| Statements fully exact | 80 / 80 |
| Rule recall (planted problems caught) | 1.00 for all 9 rules; 108 true positives |
| Rule precision | 1.00 for all 9 rules |
| Divergence gate | 7 / 7 correct blocks, 0 wrong, 0 missed |
| Commentary | 70 grounded, 3 rejected as ungrounded, 7 skipped (blocked) |

**Resilience (`tse bench`).** All 80 statements run under each provider scenario:

| Scenario | What the provider does | Completed | Figures exact | Client behaviour |
| --- | --- | --- | --- | --- |
| clean | nothing unusual | 100% | 100% | 334 requests, no retries |
| flaky | 15% of requests return 503; 5% take 0.8 s against a 0.5 s timeout | 100% | 100% | 62 errors and 22 timeouts absorbed by 84 retries |
| outage | every request fails for the first 2 seconds | 100% | 100% | Only 6 requests reached the failing provider. The circuit breaker refused the rest locally, and jobs were re-queued (60 re-queues, 2 breaker trips) until it recovered. |

**What the numbers mean.** 100% figure accuracy is the bar, not a boast. Every figure has a right answer, so any miss is a bug, and CI fails on one. Perfect precision is a property of synthetic data. Real ledgers contain legitimate duplicates and genuine large swings, which is why those rules only warn or inform. The commentary numbers use a deterministic stand-in that rounds 5% of headline figures on purpose; they show the check works, not how often Claude would err.

**Hard gates (`tse eval --gate`, run in CI).** Figures must be 100% exact. No statement may be missing. The divergence gate may make no errors. No rule's recall may fall below `eval/baseline.json`.

## Testing

| Tier | Count | What it covers |
| --- | --- | --- |
| Unit | 26 | Money rules (no floats, half-up rounding), calculation on a known ledger, each of the 9 rules firing on its problem, strict and non-strict gate, token bucket, circuit breaker states, retry with `Retry-After`, commentary grounding and fail-safe, generator determinism |
| Contract | 8 | Static check that no query exists outside the repository layer; runtime proof one tenant cannot read another; content-addressed snapshots; queue ordering, back-off and failure; mock API auth, pagination and deterministic faults; API token checks; workbook formulas re-evaluated against engine figures |
| Integration | 3 | Flaky provider over real HTTP with exact figures; outage trips the breaker and jobs recover; replay reproduces stored figures |
| Evaluation | 5 | All 80 statements against ground truth with the CI hard gates |
| Live | 1 | One real commentary call, grounded; runs only with `TSE_LIVE_TESTS=1` |

```bash
uv run pytest
uv run ruff check . && uv run mypy
```

CI runs the test suite on Windows, macOS and Linux. A separate job runs the full flow and the fault benchmark on a real Postgres service, and a third runs a secret scan over the full history.

## Safety and governance

| Concern | Control | Where |
| --- | --- | --- |
| Wrong figures | Decimal-only arithmetic; floats rejected; ground-truth gate in CI | `money.py`, `calc.py`, `evaluation.py` |
| Inconsistent inputs | Divergence gate blocks the statement; nine cross-source rules | `validation.py` |
| Model error | One read-only call; every number checked; fails safe to "Commentary unavailable" | `commentary.py` |
| Cross-tenant leaks | Tenant-bound repository; CI forbids queries elsewhere; per-tenant API token, constant-time check | `repository.py`, `api.py` |
| Unexplainable results | Inputs snapshotted by hash; replay; provenance sheet; audit event per run | `pipeline.py`, `workbook.py` |
| Upstream instability | Rate limit, timeout, retries, circuit breaker, re-queue with back-off | `accounting/`, `queue.py`, `worker.py` |

## What changes at scale

- **Queue.** The database queue suits hundreds of jobs. Thousands per hour move to a managed queue (SQS, Pub/Sub or Azure Service Bus) with the same claim, retry and dead-letter semantics, and workers become separate containers.
- **Provider limits.** Real accounting APIs enforce daily as well as per-minute limits. The token bucket moves to Redis so every worker shares it, and a daily-budget breaker stops a batch cleanly instead of failing it mid-way.
- **Isolation.** Add Postgres row-level security as a second line behind the repository, plus per-tenant encryption keys for stored provider credentials.
- **Snapshots** move to object storage with retention set by record-keeping rules, and the database keeps the hash.
- **Commentary** for a bulk quarter-end run goes through the provider's batch API (about half the cost), because nobody needs the notes within seconds.

## Known limitations and roadmap

- Fictional tax rules, a single rate and a single jurisdiction. Real regimes add multiple rates, adjustments and special schemes.
- The provider adapter targets the bundled mock; a real provider needs an OAuth adapter behind the same interface.
- Commentary numbers above come from a stand-in; live accuracy needs a Claude run with an API key.
- Retry counts in `tse bench` include only statements that completed; retries inside jobs that were later re-queued are not counted.
- No web authentication on the operator page; the JSON API uses per-tenant tokens only.
- Possible next step: model-suggested tax codes for uncoded transactions, shown to the reviewer as suggestions and never applied automatically.

## Layout

```text
config/tax.yaml        tax rules and gate settings (versioned)
prompts/               commentary.v1.md
eval/baseline.json     recall floors for the CI gate
src/tax_engine/
  synth.py             synthetic ledgers and independent ground truth
  mock_api.py          mock accounting provider with fault injection
  accounting/          HTTP adapter, rate limiter, retries, circuit breaker
  calc.py              reconciliation and Decimal calculation
  validation.py        9 rules and the divergence gate
  commentary.py        single LLM call with number grounding
  llm/                 fake and Claude adapters
  repository.py        tenant-scoped data access (the only place with queries)
  queue.py, worker.py  job queue and worker pool
  pipeline.py          one statement run, and replay
  workbook.py          formula-driven Excel output
  api.py, cli.py       HTTP API, statements page, `tse` command
tests/                 unit, contract, integration, evaluation, live
docs/adr/              architecture decision records
```

## AI assistance disclosure

Built with Claude Code as a pair programmer. I set the design, the decisions recorded in the ADRs and the evaluation approach, and I reviewed the code and checked every result.
