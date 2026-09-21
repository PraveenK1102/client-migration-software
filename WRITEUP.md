# AI Agent for HR Data Migration — Problem, Solution & Decisions

> **Deterministic where provable, AI where semantic, human where ambiguous.**

## Problem

Onboarding a customer means moving their employees into the target HR system, and real exports are
messy: column names don't match the target, the same employee is spread across several files and sheets,
values/dates/names follow each customer's own conventions, and some employees already exist and must be
**updated**, not duplicated. Doing it by hand is slow and error-prone, and both obvious shortcuts fail —
letting an LLM freely rewrite employee data is unsafe, opaque and expensive, while confirming every field
by hand defeats the automation.

## Solution

One LangGraph-orchestrated agent over deterministic application services. It ingests multiple CSV/XLSX
files, maps each source column to a versioned target contract, normalizes values and dates, **reconciles
the same employee across files** (keeping addresses, dependents, emergency contacts and vehicles as
one-to-many records), compares each candidate against the existing target organization, and syncs
creates/updates through a mock target API — asking a human
only on genuine ambiguity. Groq's open-weight **`openai/gpt-oss-20b`** is used only for the two things
rules cannot resolve. The "Naive Solutions" demo is one flow: a core file and a supplemental file (with
different headers) reconcile into one `NS-0001` / Avni Nair, the workbook attaches his collections, and a
later delta turns his new designation into an **UPDATE**, not a duplicate.

## Approach — what it decides alone vs. escalates

The design is **deterministic-first**, and everything the model touches follows **PROPOSE → PROVE →
APPLY / ESCALATE**: the model proposes only for unresolved cases, deterministic code re-validates each
proposal against the target schema, types, allowed values and policy, and a human is asked only when
meaning is genuinely undecidable. Model confidence is never treated as evidence, and the model only ever
sees a PII-safe projection (redacted headers and shapes, never raw names, emails, phones or ids).

The line, concretely:

- **Handled alone:** canonical or declared-alias headers (`Department Name → department`), safe
  value/case normalization, dates decided by full-column evidence, values that match a target enum,
  clearly-new employees, and deterministic multi-file / collection reconciliation.
- **Escalated to a human:** a generic `Email` when both work and personal fields are plausible; a date
  whose role can't be decided from the header (even at high model confidence); business taxonomy such as
  `R&D → Engineering`; a value with no safe target enum; two employees resolving to the same required
  work email; and any non-empty target value that a different non-empty incoming value would overwrite. A
  column with no target field (`Parking Access Zone`) becomes a custom-field proposal, never a silent drop.

## Trade-offs (what I chose, and against what)

- **Deterministic-first with a narrow LLM — not an LLM that migrates the data.** An end-to-end LLM is
  simpler to wire but unsafe, opaque and costly. Two constrained proposal jobs keep every action
  auditable and tie model cost to *semantic uncertainty, not row count* — a clean workbook makes zero
  model calls.
- **Escalate on ambiguity — and measure over-escalation too.** A silent wrong write corrupts customer
  data, so ambiguity escalates even at high confidence; but needless reviews defeat automation, so the
  eval also tracks *unnecessary* escalation.
- **`unsafe_auto` as the primary metric — not generic accuracy.** "Usually right" hides the failure that
  matters: a confident correct-by-luck guess on a must-escalate case still counts as unsafe.
- **Distinct-value batching — not per-row model calls,** so 200 rows and 100,000 rows cost the same
  interpretation.
- **A representative, schema-driven contract — not a claim to the vendor's proprietary schema;** the whole
  app reads `schemas/employee.v2.yaml`, so the contract is configuration, not hardcoding.

## Architecture & key decisions

- **LangGraph with real `interrupt()` / resume, checkpointed to a separate SQLite database
  (`AsyncSqliteSaver`).** Graphs run inside a bounded worker pool, never in the HTTP request, and a human
  decision plus its continuation work item are written in one transaction — so a paused review survives a
  page refresh or a backend restart. Durable, checkpointed workflows over ad-hoc in-request state.
- **A durable SQLite `work_items` queue with an atomic claim — over in-memory task ownership,** so a crash
  or restart never double-writes or loses work. SQLite with WAL suits a single-node prototype; a
  `Database` seam keeps Postgres a later swap, not a rewrite.
- **`TargetEmployeeGateway` as the only write path, in front of a mock target** — organization-scoped,
  with idempotency keys, optimistic revision checks, bounded retry/backoff, immutable versions and
  compensating rollback, so a real adapter swaps in without touching the workflow and a delivery failure
  can never falsely mark a migration complete.
- **Deterministic guardrails re-validate every model proposal,** and a repository-owned golden evaluation
  (`eval.v2`) keeps it honest: 32 decision-quality cases, **unsafe_auto = 0** and **unnecessary_escalate
  = 0**, gated in the test suite and by the evaluator's exit code.

## What's next (production)

The autonomy boundary would not change; the infrastructure would, behind seams that already exist:
Postgres for shared state (the `Database` seam), object storage for raw blobs, a real broker behind the
work-item model, and a real target adapter behind `TargetEmployeeGateway`. The measurement is already in
place — every migration persists AI token usage (input/output/total), model latency and a server-side
estimated cost, and the `eval.v2` golden suite already scores decision accuracy (100% on 32 cases,
`unsafe_auto = 0`). What's left is to run that evaluation **at scale** on a larger, customer-derived
corpus and report accuracy, review rate, latency and cost **by migration type** — today's figures are from
a small synthetic set, not production-scale numbers. None of the
production infrastructure is built yet, and the principle stays the same: **automate what can be proved,
use AI only for semantic uncertainty, and stop for a human only when guessing could corrupt customer
data.**
