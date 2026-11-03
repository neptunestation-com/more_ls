# MLS — "More LS": RLS more-or-less

MLS is a specification language and compiler for PostgreSQL access-control and data-integrity rules.
A spec (JSON) states who may see and change which rows and which invariants the data must satisfy.
The toolchain **proves properties** about the spec with an SMT solver, then **compiles** it to
Postgres primitives: RLS policies, CHECK constraints, foreign keys, triggers from fixed templates,
column privileges, indexes and grants. RLS is one compilation target, not the foundation.

Specs are written by LLMs from human business requirements. Humans approve **properties and
scenarios** (plain English, concrete examples), never MLS or DDL.

Read before writing code, in this order:
1. `docs/DECISIONS.md` — settled decisions. Do not reopen them; flag conflicts instead.
2. `docs/SEMANTICS.md` — the meaning of every construct. This is the source of truth.
3. `docs/ROADMAP.md` — milestones and acceptance criteria. **Start with Milestone 1.**
4. `docs/FINDINGS.md` — what experiments on real Postgres already established.
5. `prototype/` — a working throwaway prototype (Python + Z3). Reuse ideas, not code structure.
6. `examples/tenant_docs.mls.json` — an illustrative spec. The real schema is defined in M1.

## Non-negotiable invariants

1. **Three-valued logic everywhere.** Every predicate has SQL semantics (TRUE / FALSE / UNKNOWN).
   The SMT encoding uses (is-true, is-false) pairs. A two-valued shortcut is a bug.
2. **One meaning per predicate across targets.** RLS denies on UNKNOWN; CHECK passes on UNKNOWN.
   The compiler normalizes (`CHECK ((p) IS TRUE)`, trigger `IF NOT ((p) IS TRUE)`).
3. **The compiler emits only fixed templates.** Every template has a differential test against real
   Postgres (15, 16, 17, 18) and at least one mutation test proving the differential test can fail.
4. **Never silently weaken enforcement.** If a target cannot enforce a construct soundly, the
   compiler refuses with an actionable error. Out-of-fragment logic is only allowed as an explicit
   `opaque` block, and every proof report lists the opaque blocks it depends on.
5. **Proofs are about the model; tests tie the model to Postgres.** Every proof report states its
   assumptions (identity adapter, trusted roles, isolation level, opaque blocks).
6. **Decidability is a property of the spec language.** Do not add a construct whose proof
   obligations leave quantifier-free / EPR + linear integer arithmetic + uninterpreted functions
   without a declared bound (e.g. hierarchy depth).
7. **Error messages are written for an LLM to act on**: what is wrong, where (JSON path), and the
   allowed alternative.

## Stack and conventions

- Python 3.12+, `uv` for env and dependencies, `pydantic` v2 for the spec model and JSON Schema export.
- `z3-solver` primary; solver access goes through our own small term layer that can also print
  SMT-LIB2 so `cvc5` cross-checks results in CI.
- `psycopg` 3; `pytest`; `hypothesis` for differential fuzzing; Docker (or testcontainers) to run
  Postgres 15–18 in CI.
- Parse `EXPLAIN (FORMAT JSON)`, never text plans (InitPlan display differs across versions).
- License: Apache-2.0. Personal open-source project.
- Package name `mls`; CLI `mls` with subcommands (`check`, `prove`, `compile`, `test`, `diff`,
  `migrate`, `preflight`, `drift`).

## Running things

- `uv sync --all-extras`; `uv run mls <check|prove|compile|test|lint|schema|eval> ...`.
- `uv run pytest -m "not pg"` is fast and needs no Docker. `uv run pytest -m pg` starts
  `postgres:15..18` containers (set `MLS_PG_VERSIONS=16` for one, `MLS_PG_DSN=...` for an existing
  server). `-m slow` runs the EXPLAIN checks at 1M rows.
- Every compiler template has a `Mutation` (compile/mutations.py); `tests/templates` proves each
  mutation is caught by the differential test. Add a mutation when you add a template.
- Implementation defaults decided during M1 are D21–D27, during M2 D28–D33, during M3 D34–D36, in
  `docs/DECISIONS.md`.
- M4: `uv run mls generate` / `mls eval --llm` (src/mls/llm, docs/LLM.md, D37–D39). Tests never call the
  API: `MLS_LLM_SCRIPT=<json>` or `ScriptedClient` replays answers. `evals/make_cases.py` regenerates
  the seed corpus (the solver must confirm every intended verdict); `mls eval evals/cases --mutate
  evals/cases/synthetic` regenerates the mutants. Keep both committed and in sync (tests check).
  Grading is by net effect (`props/diff.py:behaviour_diff`), not layer by layer; when a live case
  fails, read its transcript first (`--transcripts DIR`): the model's notes usually name the real
  cause, and so far that was an ambiguous requirement sentence or a reference bug as often as the
  model. Live runs need `ANTHROPIC_API_KEY`; decoding is unconstrained (the API rejects recursive
  schemas), answers are validated locally.
- M3: hierarchies and cardinality limits compile to SECURITY DEFINER helpers owned by `mls_internal`
  (compile/internal.py, hierarchy.py, cardinality.py). The parent-row lock is only testable with
  parallel writers (`tests/templates/test_cardinality.py`); `-m cvc5` cross-checks the org_tree
  example too (integer-quantified obligations need cvc5 without finite-model-find).
- `uv run mls explain|diff|migrate|preflight|drift ...` (M2). Migrations are phased; every statement
  kind has a lock mode in `compile/migrate.py:LOCKS` that `tests/templates/test_locks.py` observes
  on every PG version — add the kind and its mode when you add a migration statement.
- `tests/templates/test_migrate.py` is the migration acceptance test: deploy v1 + data, migrate,
  `drift` against v2 must be clean, then the v2 differential test on the migrated database.

## Suggested layout

```
src/mls/
  spec/        pydantic models, JSON Schema export, typechecker, fragment checker
  logic/       term AST, 3VL encoding, Z3 backend, SMT-LIB2 printer
  props/       property templates + custom properties, proof runner, reports
  compile/     one module per template: policy, check, fk, transition, time, column, index, hardening
  identity/    adapters: supabase, pg_roles
  pg/          catalog introspection, EXPLAIN checks, differential test harness
  cli.py
tests/
  templates/   per-template differential + mutation tests (parametrized over PG versions)
  props/       proof tests with expected PROVED / REFUTED + counterexample shape
examples/
docs/
```
