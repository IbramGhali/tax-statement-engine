# ADR 0003: Snapshot raw inputs by hash so every run can be replayed

Status: accepted

## Context

Accounting data changes after the fact: transactions are recoded, late bills are entered, the provider recalculates its report. When a reviewer or auditor asks "why did the statement say this?" three months later, re-fetching from the provider gives today's data, not the data the statement was built from.

## Options

1. Store only the output figures.
2. Store outputs and log the API responses.
3. Store the complete fetched input (transactions, provider report, payroll, prior quarter figure) as a snapshot keyed by its SHA-256, and link every run to its snapshot.

## Decision

Option 3. The pipeline fetches, validates the payloads into typed models, stores the snapshot by content hash, then computes only from the snapshot. `tse replay RUN_ID --tenant ID` recomputes a stored run from its snapshot and confirms the figures match exactly. The snapshot hash, tax-config version and commentary prompt version are written into the workbook's Provenance sheet.

## Consequences

- Any statement can be explained and reproduced without the provider being available.
- Identical inputs produce the same hash, so re-running unchanged data does not store duplicates.
- A change to the calculation code can be tested against historical snapshots before release.
- Accepted cost: storage grows with every distinct input. At production volume, snapshots move to object storage with a retention policy matched to the record-keeping requirement.
