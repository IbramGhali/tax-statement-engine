# ADR 0001: The LLM writes commentary only; it never computes or changes a figure

Status: accepted

## Context

A tax statement is a regulated number. A wrong figure costs money and trust, and "the model said so" is not an acceptable explanation to a reviewer or an auditor. Language models are good at summarising and poor at guaranteed arithmetic, and their failures are silent: a plausible wrong number looks exactly like a right one.

The temptation is to let a model "prepare the statement": read the ledger, classify transactions, add things up and explain the result. Each of those steps except the explanation has a deterministic answer.

## Options

1. Model prepares the statement end to end; code checks the totals afterwards.
2. Model classifies transactions; code does the arithmetic.
3. Code does everything that has a right answer (reconciliation, classification by tax code, Decimal arithmetic, validation). The model writes a short reviewer's note from the finished figures and findings.

## Decision

Option 3. One model call per statement writes 3 to 4 sentences of commentary. It receives the computed figures and findings, takes no actions, and cannot change anything. Every number in its note is checked against the numbers it was given; if any number does not match (for example "about $12,000" for $12,345.67), or the call fails, the note becomes "Commentary unavailable" and the statement still completes. Blocked statements get no commentary, so they never look ready to sign.

## Consequences

- Figure accuracy is 720 of 720 statement lines against independent ground truth, and does not depend on which model is configured, or on any model being available.
- The commentary check is measurable: offline, 3 of 73 notes were rejected for a rounded figure, and the rest passed.
- Cost and latency are one short call per statement.
- Accepted cost: the model adds no judgement to classification. Where tax codes are missing, the engine excludes and flags rather than guessing. A model-suggested code, reviewed by a person, is on the roadmap as a separate, clearly labelled step.
