# ADR 0002: Independent cross-source validation rules, and a divergence gate that can block a run

Status: accepted

## Context

Accounting data arrives from several sources that should agree and sometimes do not: the transaction ledger, the provider's own tax report, the payroll summary, and last quarter's statement. A single "looks fine" check hides which source is wrong, and a check that only warns can be ignored at quarter end when everyone is busy.

## Options

1. One combined sanity check producing a score.
2. A list of warnings for a reviewer to read.
3. Independent rules, each comparing specific sources and returning `pass | inform | warn | fail`, plus one gate rule that blocks the run outright when the provider's report and the transaction-level calculation disagree beyond a tolerance.

## Decision

Option 3. Nine rules (V01 to V09) cover uncoded transactions, tax amounts inconsistent with the rate, withholding above gross wages, report vs transaction divergence, duplicates, quarter-on-quarter movement, refund positions, tax claimed on exempt purchases, and transactions outside the period. Rules are independent functions registered by id, so adding one does not change the others.

V04 is the gate: in strict mode (the default) a divergence above $1.00 marks the statement `blocked`. Figures are still computed and stored so the reviewer can see the gap, but no commentary is generated and the status makes it impossible to mistake for a finished statement.

## Consequences

- Every planted problem in the synthetic data is caught (9 of 9 rules at recall 1.00), and the gate made 7 of 7 expected blocks with no wrong blocks.
- A failure points at its cause: the rule id says which two sources disagree, and the message gives the amount.
- Precision of 1.00 is a property of synthetic data. Real ledgers have legitimate duplicates and genuine large swings, so V05 and V06 are `warn` and `inform`, never blocking.
- Accepted cost: strict mode will block statements a person might accept with an explanation. The tolerance and strict mode are configuration, versioned with every run.
