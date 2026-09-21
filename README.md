# AI Agent for HR Data Migration

An agent that migrates a customer's messy HR export (CSV / XLSX) into a target HR system — mapping
inconsistent columns and values to a target contract, cleaning and validating every record, reconciling
data spread across multiple files, comparing against what already exists in the target, and syncing the
approved changes. It asks a human to decide **only** where the evidence is genuinely ambiguous.

> **Deterministic where provable, AI where semantic, human where ambiguous.**

## The problem

Onboarding a customer means moving their employees out of whatever they use today into the target
system, and real exports are messy:

- column names don't match the target (`Worker Type`, `Office Location`, `Department Name`, …);
- the same employee is split across several files/sheets (core data, contact/HR supplements, addresses,
  dependents, vehicles);
- values, dates and names are written each customer's own way;
- some employees already exist in the target and must be **updated**, not duplicated.

Letting an LLM freely rewrite employee data is unsafe; asking a human to confirm every field defeats the
point of automation. This agent draws a clear line between the two.

## Quick start

**Prerequisites:** Python 3.12, Node 18+ (a Groq API key is needed only for live model runs).

```bash
# 1) Backend — the deterministic flow needs no key; add GROQ_API_KEY to backend/.env for the live model.
cd backend && python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
LLM_PROVIDER=fake DATA_DIR=./data AUTO_CONTINUE=true .venv/bin/python -m uvicorn app.main:app --port 8000

# 2) Frontend (separate terminal)
npm --prefix frontend install && npm --prefix frontend run dev      # http://localhost:5173
```

Open **http://localhost:5173**, start a new migration and upload the four
[`sample-data/naive-solutions/`](sample-data/naive-solutions/) files to watch the whole flow end to end.

```bash
cd backend && .venv/bin/python -m pytest          # tests (offline, no key)
cd backend && python -m app.eval_harness          # decision-quality evaluation (offline)
cd frontend && npm run build                       # frontend production build
```

Env vars and key handling are documented under **Configuration** near the end. The rest of this README
explains what the agent does and why it is safe.

## What the agent does

```
Upload CSV/XLSX
  → understand the source (profile every column)
  → map source columns to the target contract
  → normalize values and dates
  → reconcile one employee across files + attach one-to-many collections
  → stop for a human ONLY on genuine ambiguity
  → compare against the existing target
  → sync (create / update)
  → audit + immutable versions + rollback
```

Every field mapping, value translation and record decision is explained and traceable back to its
source cell. Nothing is silently guessed and nothing is silently dropped.

## Concrete example: Naive Solutions

The four files under [`sample-data/naive-solutions/`](sample-data/naive-solutions/) are a realistic,
fully synthetic migration for a fictitious customer, "Naive Solutions". They tell the whole story.

### 1. Core employee file — `naive_solutions_01_core_200x20.csv`

200 employees with canonical core data (`employee_id`, `full_name`, `hire_date`, `department`,
`designation`, `work_location`, `work_email`, …). For example `NS-0001` is **Avni Nair**, a
**Software Engineer** in **Engineering** at **Bengaluru** (grade `G4`). These headers are canonical or
declared aliases, so they map with **zero model calls**.

### 2. Structured collections — `naive_solutions_02_structured_collections.xlsx`

Four sheets — **Addresses**, **Emergency Contacts**, **Vehicles**, **Dependents** — each joined to an
employee by `employee_id` and kept as **one-to-many records**, not flattened into the employee row.
Exact-duplicate items collapse (keeping all source references), complementary rows merge, conflicting
variants of the same item become a scoped review, and a child row whose key matches no employee becomes
one `orphan_child_row` review rather than being dropped.

### 3. Supplemental file — `naive_solutions_03_supplemental_200x10.csv`

Same employees under **different headers**, which is where the autonomy boundary shows:

| Source column | Outcome | How |
|---|---|---|
| `Department Name` | → `department` | **deterministic** — declared alias |
| `Employee Status` | → `employment_status` | **deterministic** — declared alias |
| `Worker Type` | → `employment_type` | **deterministic** — declared alias |
| `Office Location` | → `work_location` | **deterministic** — declared alias |
| `personal_email` | → `personal_email` | **deterministic** — canonical field |
| `Separation Effective Date` | → `termination_date` | **AI** — the model proposes it; policy validates the "separation" role before applying |
| `Parking Access Zone` | → *(no target field)* | **custom field** — proposed for a human to approve/map/ignore, never silently discarded |

`Office Telephone Number` and `Personal Contact Number` follow the same AI path to `work_phone` /
`mobile_phone` (validated by the office / personal role qualifiers). The point: the deterministic layer
handles everything it can prove, the model is used only for the genuinely unresolved headers, and a
column with no home in the contract is surfaced as a custom-field decision.

### 4. Delta file — `naive_solutions_04_delta_update_existing_employee.csv`

One employee, `NS-0001` **Avni Nair**, whose `designation` has changed from **Software Engineer** to
**Senior Software Engineer**. Because the employee already exists in the target, reconciliation produces
an **UPDATE** (changed field only) — not a duplicate create — and records an immutable version so the
before/after is auditable. See **Delta / post-sync update handling**.

## Autonomy boundary

The agent is **deterministic-first**: it acts on its own wherever a decision has independent
corroboration, and it escalates only where meaning is genuinely undecidable. Anything the model touches
follows one pattern:

```
PROPOSE  (model suggests, for unresolved headers/values only)
  → PROVE   (re-validate against schema, type, allowed values, sibling/date policy)
     → APPLY      (evidence holds)
     → ESCALATE   (a human decides)
```

The line is drawn at **evidence, not model confidence** — a confident proposal is still escalated when
the evidence can't back it. From the Naive Solutions files:

- **Deterministic (no model):** `Department Name → department`, `Worker Type → employment_type` — declared
  aliases; the values (`Engineering`, `full_time`) are already valid, so they normalize cleanly.
- **AI, then proven:** `Separation Effective Date → termination_date` — not an alias, so the model
  proposes it and the deterministic policy accepts it only because the header carries a recognized
  "separation" role qualifier for that target.
- **Escalated to a human:** a bare `Email` when both a work and a personal email field are plausible (a
  sibling-field guardrail); a date column whose role can't be decided from the header; an organization
  taxonomy such as `R&D → Engineering` whose business meaning isn't provable. Record-level contradictions
  — for example two employees resolving to the same required work email — are escalated as a single scoped
  review with both employees and the conflicting value.
- **No target field:** `Parking Access Zone → custom-field proposal`.

## Where AI is used

Groq (`openai/gpt-oss-20b`) has exactly **two constrained responsibilities**:

1. propose a **target field** for a source column that deterministic rules could not resolve;
2. propose a **value map** for categorical/enum values that deterministic rules could not resolve.

**The model interprets semantics; it does not migrate employee rows.** It returns constrained structured
JSON (a per-call response schema built from the real target paths, re-validated with Pydantic). It never
writes to the target, never runs arbitrary tools, never invents a target field, and never bypasses
validation — valid JSON is necessary, never sufficient.

Value maps operate on **distinct unresolved values in bounded batches**, so model cost tracks *semantic
uncertainty, not row count* — illustratively, a column with 150 unresolved distinct labels costs a
handful of batched calls whether there are 200 rows or 100,000. A clean, already-aligned workbook makes
**zero** model calls. The model only ever sees a redacted projection of the source (headers, inferred
types, redaction classes, bounded non-PII shapes) — never raw names, emails, phones or ids — and source
text is treated as untrusted data, never as instructions.

## Deterministic guardrails

Everything except those two responsibilities is deterministic: parsing, profiling, alias resolution,
date-format inference, enum normalization, referential integrity, validation, reconciliation,
sync/retry/rollback and audit/versioning. Highlights of the never-guess discipline:

- a bare, generic column (e.g. `Email` with no work/personal qualifier) is escalated, never guessed;
- a date role is escalated when the header can't decide it — **even at high model confidence**; mixed
  date formats are flagged, never silently coerced;
- an enum/value-map target is checked against the **actual allowed values**; an out-of-enum answer is
  rejected;
- a non-empty target value is **never** overwritten by a different non-empty incoming value;
- identity is matched on the employee key only — **never** by fuzzy name matching;
- a column with no destination becomes a custom-field proposal (approve / map / ignore), never a silent
  drop.

## Human-in-the-loop

Reviews are **scoped and column-level, not per-row fan-out**: one value-map decision applies to every
matching row; one field-mapping confirmation settles a column. Each review states what happened, why the
agent stopped, the affected employee(s)/field, the evidence and a deep-link to the exact source cell —
and what happens next. The reviewer resolves the contradiction once and the migration resumes from its
persisted state; corrections must themselves pass validation, and every human decision is audited.

## Multi-file reconciliation & structured collections

`naive_solutions_01` (core) and `naive_solutions_03` (supplemental) describe the **same employees under
different headers** and are reconciled into one prepared employee by `employee_id`. The
`naive_solutions_02` workbook contributes **Addresses, Emergency Contacts, Vehicles and Dependents** as
one-to-many collections attached by `employee_id`, with declared item-identity keys used to collapse
exact duplicates, merge complementary rows, and turn conflicting variants or unmatched child rows into
scoped reviews. Before any target write, a consultant can also remove uploaded files — the migration is
rebuilt deterministically (mapping → preparation → reconciliation) from the remaining files, keeping at
least one file and never touching organization custom-field definitions.

## Target reconciliation and delivery

Prepared employees are compared against the existing organization-scoped target and classified into
create / update / no-change / excluded / review-required (incoming-vs-existing-target, kept separate from
the incoming-vs-incoming reconciliation done during preparation).

The migration reaches the target **only** through `TargetEmployeeGateway`, never its tables. Delivery is
safe by construction:

- CREATE and clearly-safe UPDATE operations execute through the gateway with **bounded exponential
  backoff + jitter**, honouring `Retry-After`;
- **optimistic concurrency** via the target's per-row revision: a revision conflict re-fetches and
  re-reconciles rather than blindly overwriting, and never re-sends the stale revision-bound write;
- **idempotency keys** make writes crash-safe — a restart mid-delivery replays the first response instead
  of double-writing;
- **per-attempt delivery history** (HTTP status, error category, request id, timing) is auditable;
- **compensating rollback** reverses eligible writes exactly (CREATE → delete; UPDATE → replace to the
  stored `before_snapshot`);
- a **no-stuck invariant**: a job never stays active with no claimable work and no open review — retry
  exhaustion terminalizes the operation and the job, and a delivery failure can never falsely mark a
  migration complete.

## Delta / post-sync update handling

The `naive_solutions_04` delta is the canonical example: `NS-0001` Avni Nair's designation changes from
`Software Engineer` to `Senior Software Engineer`. Reconciliation matches the existing target record by
identity and produces an **UPDATE** (changed field only), not a duplicate create; an immutable version
records the before/after; and the write still goes through the same durable path (organization scope,
revision check, idempotency, retry, rollback). The workspace also supports validated post-sync scalar
edits and deletion of a synced employee, which reuse that same durable delivery path and are blocked
while a delivery for that employee is in flight.

## Architecture

Reviewer-first: the app is a small, deliberate single-node stack.

```
Frontend (React + TypeScript + Vite)
   |
FastAPI API            upload enqueues work and returns; no work runs in the request
   |
Durable work_items queue (SQLite)   atomic claim + persisted status drive correctness
   |
Bounded WorkerPool
   |
LangGraph
   |-- Mapping workflow        profile -> map -> analyze -> assess -> (review loop) -> finalize
   |-- Preparation workflow    prepare -> (record-review loop) -> finalize
   |
Deterministic services
   |-- ingest / profile        streaming CSV + XLSX; row-limit enforced, no partial ingest
   |-- mapping guardrails      alias rules + acceptance/escalation policy (confidence is not proof)
   |-- normalization           dates, enums, references, transform plans
   |-- reconciliation          incoming-vs-existing-target, schema-driven
   |-- validation / versions   required fields, immutable snapshots + diffs
   |
Groq (openai/gpt-oss-20b)     two jobs only:
   |-- mapping proposal        unresolved source column -> target field
   |-- value proposal          unresolved source value  -> target value
   |
TargetEmployeeGateway         the ONLY target-write path (organization-scoped HTTP boundary)
   |
Mock target HR system         its own SQLite store, per-row revision, org-scoped

Storage:  app.db (application state) · checkpoints.db (LangGraph) · LocalBlobStore (raw uploads,
          SHA-256) · a separate mock-target DB.
```

The target contract lives in [`schemas/employee.v2.yaml`](schemas/employee.v2.yaml) — a **representative**
employee-migration schema defined by this prototype, read by mapping, validation, preparation,
reconciliation, versioning and the UI (it is not a reproduction of any vendor's proprietary schema). A
short source layout: `backend/app/` (ingest, profiling, mapping_rules, policy, source_intelligence,
prepare, reconcile_target, versions, delivery, target_gateway, worker, db, observability,
`workflows/{mapping,preparation}`, `llm/`, `model_projection`), `backend/mock_target/`, `frontend/src/`.

## LangGraph

One migration agent orchestrated as two small LangGraph workflows — no provider function-calling/tools.

- **Mapping:** `profile → map_columns → analyze_source → assess → [prepare_review → await_review →
  apply_decisions]* → finalize_mapping`, with a blocked path `analyze_source → mapping_blocked → END`.
- **Preparation:** `prep_start → prepare_records → [prepare_record_review → await_record_review →
  prepare_records]* → finalize_preparation`.

Human review uses **real** `interrupt()` / `Command(resume=...)`. Interrupt nodes are side-effect-free;
checkpoints use `AsyncSqliteSaver` in a **separate** checkpoint database keyed by a stable job thread id;
a human decision and the continuation work item are persisted in one transaction before the graph
resumes; and graphs run only inside the bounded worker pool, never inside an HTTP request — so a paused
job survives a page refresh or a backend restart.

## Storage and durability

- **Raw uploads:** `LocalBlobStore` with server-generated keys and SHA-256 integrity re-verified at
  ingest.
- **Application state:** SQLite with WAL, busy timeout and short transactions — no transaction is ever
  held across a model call, file parse, target HTTP call, or human wait.
- **Work execution:** a persistent `work_items` table drained by a bounded worker pool; correctness comes
  from persisted status + an atomic conditional-UPDATE claim + idempotent stages, with lease reclaim and
  a bounded attempt budget for crash/restart recovery.
- **LangGraph checkpoints:** a separate SQLite checkpoint database.
- **Target:** a separate mock-target SQLite store, reached only through `TargetEmployeeGateway`.

## LangSmith / observability

Tracing is **optional** — the app behaves identically with it off (no key or tracing disabled makes the
tracer a no-op that never imports LangSmith). When LangSmith tracing is enabled, each migration emits one
top-level trace with stage spans, and the two direct Groq calls are recorded as sanitized child spans
named exactly `model.mapping_proposal` and `model.transform_proposal`, carrying model id, attempts,
latency and token usage — never the raw prompt, raw values, or the API key.

## Metrics + estimated cost

Migration-level AI metrics are **real persisted data** (a `model_calls` table read via
`GET /api/jobs/{id}/metrics`), not UI placeholders: provider/model, AI call count, input/output/total
tokens, and total/average model latency. Estimated AI cost is computed **server-side** from configurable
per-million-token pricing (never hard-coded in React):

```
estimated_cost = input_tokens / 1_000_000 * LLM_PRICE_INPUT_PER_1M
              +  output_tokens / 1_000_000 * LLM_PRICE_OUTPUT_PER_1M
```

`LLM_PRICE_INPUT_PER_1M` / `LLM_PRICE_OUTPUT_PER_1M` default to the published Groq `openai/gpt-oss-20b`
rate. A deterministic migration that makes no model calls shows an explicit **0 calls / 0 tokens / $0.00**
state rather than blank data.

## Organization isolation

Every target call carries an explicit `X-Organization-ID` header (bound per gateway view; there is no
process-global organization state). Identity is `(organization_id, employee_id)` and work-email
uniqueness is `(organization_id, normalized_work_email)`, so the same id or email may exist in two
organizations, but a duplicate inside one organization is rejected. Lookup, create, update,
replace/rollback, delete, idempotency and the write log are all organization-scoped, so reconciliation,
delivery and rollback can never cross a boundary.

## Tests

All automated tests run **offline** (a deterministic fake adapter or a mocked Groq client — no API key,
no charges):

```bash
cd backend && .venv/bin/python -m pytest
# 547 collected: 533 passed, 14 skipped (gated live smokes), 0 failed
```

The 14 skips are live Groq / LangSmith smoke tests that opt in explicitly (`RUN_LIVE_GROQ=1`,
`RUN_LIVE_LANGSMITH=1`); they are not failures. Frontend:

```bash
cd frontend && npm run build      # tsc typecheck + vite production build
```

## Evaluation

Generic "the model was usually right" is not a safety statement for a migration agent — silently doing
the *wrong* thing is worse than asking. So the repository ships a deterministic **decision-quality**
evaluation (`eval.v2`, `backend/app/eval_harness.py`) that scores four buckets:

- `correct_auto` / `correct_escalate` — resolved, or escalated, correctly;
- `unnecessary_escalate` — escalated something it could have resolved (an autonomy cost — is the boundary
  drawn tightly enough?);
- `unsafe_auto` — **any unsafe autonomous action: either acting where a human was required, or producing
  an incorrect automatic result.** **This is the primary safety metric and must be 0.** If a golden case
  requires a human and the agent acts anyway, that is `unsafe_auto` even if it happened to guess the right
  value.

Current result: **32 golden cases, accuracy 100%, unsafe_auto 0, unnecessary_escalate 0** (15
`correct_auto`, 17 `correct_escalate`). The `unsafe_auto == 0` gate is enforced in the test suite **and**
by the CLI exit code, so a regression fails CI.

```bash
cd backend
python -m app.eval_harness                                       # offline, deterministic — the gate (no key)
python -m app.eval_harness --output ../reports/evaluation.json   # + machine-readable JSON
python -m app.eval_harness --mode live                           # + bounded real-Groq probes (RUN_LIVE_GROQ=1 + key)
```

Full detail and the per-dimension table: [`reports/evaluation.md`](reports/evaluation.md).

## Configuration

Setup commands are in **Quick start** at the top. The Groq key is read by the **backend only**, from
`backend/.env` — never sent to the frontend. With no key, parsing, deterministic mapping, all of
preparation, and target comparison still work; only columns that genuinely need model interpretation are
marked blocked until a key is set. A fake-adapter run is always labelled `fake (test/offline)` in the UI.

| `backend/.env` var | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `groq` | `groq` (real) or `fake` (test/offline) |
| `GROQ_MODEL` | `openai/gpt-oss-20b` | model id |
| `GROQ_API_KEY` | — | backend-only secret |
| `LLM_MAX_CONCURRENCY` | `2` | bounded concurrent model calls |
| `LLM_PRICE_INPUT_PER_1M` / `LLM_PRICE_OUTPUT_PER_1M` | `0.10` / `0.50` | per-1M-token pricing for estimated cost |
| `LANGSMITH_TRACING` | `false` | optional engineering tracing |
| `AUTO_CONTINUE` | `true` | safe stages chain automatically (prepare → reconcile → deliver) |

## Limitations

- The target contract (`schemas/employee.v2.yaml`) is a **representative** schema defined by this
  prototype — not a reproduction of any vendor's proprietary production schema.
- Writes reach only a **mock** target HR system (in-process by default, or a separate HTTP process); no
  real external HR API is called.
- Single-node runtime: SQLite + a local durable queue + a local blob store — appropriate for a prototype,
  **not** a production scale-out (no Postgres/broker/object storage is built).
- Ingestion is CSV/XLSX only; PDF/scanned/JSON are rejected with a clear message.
- Selected non-PII sample shapes are sent to hosted Groq inference; do not send real employee data
  without a separate data-handling review.

The production scale-out path — the same autonomy boundary with Postgres, object storage, a real broker
and a real target adapter swapped in behind existing seams — is covered in
[`WRITEUP.md`](WRITEUP.md).
