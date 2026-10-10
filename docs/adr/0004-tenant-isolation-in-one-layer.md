# ADR 0004: Tenant isolation enforced in one data-access layer and checked in CI

Status: accepted

## Context

Each tenant is a separate business. Showing one business's figures to another is the worst failure this system can have, and in multi-tenant code it usually happens through one forgotten `WHERE tenant_id = ?` in a query written months later.

## Options

1. Trust every query author to add the tenant filter.
2. Separate database per tenant.
3. One `TenantRepository` bound to a tenant at construction that adds the tenant filter to every statement it runs; queries are forbidden anywhere else, and a CI test enforces that.

## Decision

Option 3. Code outside `repository.py` and `queue.py` cannot run SQL; a contract test scans the package and fails if `.execute(` or SQLAlchemy query builders appear elsewhere. A second test proves at runtime that a repository for tenant B cannot read tenant A's runs or snapshots, and that saving another tenant's snapshot raises. The API resolves the tenant from the path, checks that tenant's token with a constant-time comparison, and returns the same 401 for an unknown tenant and a wrong token. Output files live under a per-tenant directory.

## Consequences

- A new query cannot skip the tenant filter without failing CI.
- Evaluation and the operator page read every tenant through its own repository, so even internal tools use the same path.
- Accepted cost: cross-tenant reporting needs an explicit, separately reviewed admin path. Database row-level security in Postgres is the production backstop and is on the roadmap.
