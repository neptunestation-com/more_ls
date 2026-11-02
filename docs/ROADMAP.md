# Roadmap

## Milestone 1 — core loop, no LLM

Pipeline: JSON spec → validate (schema) → typecheck → fragment check → prove → compile DDL →
differential test against PG 15–18.

Scope:
- Spec model (pydantic) and exported JSON Schema for: tables/columns, relations, identity adapter,
  `policy`, `check`, `inclusion`, `transition`, `column_rule`, `now_between` atom, `opaque` (parsed,
  modeled, compiled; full reporting in M3).
- Identity adapters `supabase` and `pg_roles`.
- Logic layer: term AST, 3VL (T, F) encoding, Z3 backend, SMT-LIB2 printer.
- Property templates from D9 + custom properties; proof reports (Markdown + JSON) listing
  assumptions; counterexamples as structured scenarios.
- Compiler templates for every M1 construct, plus generated indexes (D14) and hardening (D13).
- Test harness: Postgres 15–18 in Docker; per-template differential tests (SELECT, INSERT, UPDATE,
  DELETE outcomes: filtered / ok / error) with hypothesis-generated data; at least one mutation test
  per template; EXPLAIN (FORMAT JSON) checks asserting index conditions; cvc5 cross-check job.
- Leakproof lint.
- Eval case format (requirements text, expected properties, reference spec, expected verdicts),
  with the 3 corpora from `prototype/demo.py` converted as the first cases.
- CLI: `mls check | prove | compile | test`.

Acceptance:
- `examples/tenant_docs.mls.json` (finalized to the real schema) reproduces the prototype's results:
  isolation PROVED; UPDATE tenant move REFUTED without the transition and PROVED with it; naive
  CHECK NULL issue caught; membership-delete induction needs both FKs.
- 0 differential mismatches on all four PG versions; every mutation test fails as expected.
- Every generated policy passes its EXPLAIN check at ≥1M rows.
- A spec using a construct outside the fragment gets an error naming the JSON path and the fix.

## Milestone 2 — evolution

`mls diff | migrate | preflight | drift`.
- Access diff with scenarios (§7 of SEMANTICS).
- Migration generation: ordering, `NOT VALID` / `VALIDATE`, documented and tested lock levels per
  DDL statement, transactional policy swaps.
- Pre-flight: compiled `IS TRUE` predicates as violation queries, with counts and samples.
- Drift: compile the expected objects into a shadow schema on the same server and compare
  normalized catalog output (`pg_get_expr`, `pg_get_constraintdef`, `pg_get_triggerdef`, ACLs)
  rather than comparing strings we generated.
- English rendering of rules, properties and scenarios (the approval artifact, D10).

## Milestone 3 — harder constructs

- Hierarchies (D5) with depth bound, immutable parent, recursive-CTE InitPlan; EXPLAIN-checked.
- Cardinality (D4) with parent-row lock; concurrency test with parallel writers proving the bound
  holds under READ COMMITTED (and fails without the lock — mutation test).
- Opaque blocks: full reporting (conditional proofs, diff flags).

## Milestone 4 — LLM loop

- Evaluation set (D6): 30–50 seed cases from David plus synthetic mutations.
- Separate-pass generation (D16) with constrained decoding against the JSON Schema; bounded repair
  using counterexamples; LLM-oriented error messages; metrics (spec correctness, repair rounds,
  human-escalation rate).

## Parallel track — Lean 4

Formalize: 3VL, predicate semantics, policy combination, NULL normalization lemma, and correctness
of the CHECK and FK templates against a small model of Postgres. No milestone depends on it.

## Open details (decide during implementation, record in DECISIONS.md)

- Exact JSON shape (keep it explicit: no defaults for permissive/restrictive, command, NULL handling).
- How the Supabase adapter detects or documents direct-connection exposure.
- Whether column rules need security-invoker views for roles that also need row filtering.
- Lock levels of each DDL statement on PG 15–18 (test, don't assume).
