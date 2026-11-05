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
5. `docs/BACKGROUND.md` — what `pg_more_ls` established before this project: the undecidability
   theorem and the survey of authorization languages, cited, not restated.
6. `prototype/` — a working throwaway prototype (Python + Z3). Reuse ideas, not code structure.
7. `examples/tenant_docs.mls.json` — an illustrative spec. The real schema is defined in M1.

User-facing documentation is `docs/GUIDE.md` (user guide) and `docs/REFERENCE.md` (language and
CLI reference). When you add or change a construct, a property template, a CLI flag, an exit
code or a generated object name, update them in the same change. A construct lives in both
syntaxes (D50): give it its terse form in `terse.py` (and, once the parser exists, its parse)
in the same change; `tests/test_terse.py` fails the fast suite when the model has a field or a
union member the renderer does not cover.

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
  `migrate`, `preflight`, `drift`, `import`, `status`).

## Running things

- `uv sync --all-extras`; `uv run mls <check|prove|compile|test|lint|schema|eval> ...`.
- `uv run pytest -m "not pg"` is fast and needs no Docker. `uv run pytest -m pg` starts
  `postgres:15..18` containers (set `MLS_PG_VERSIONS=16` for one, `MLS_PG_DSN=...` for an existing
  server). `-m slow` runs the EXPLAIN checks at 1M rows.
- Every compiler template has a `Mutation` (compile/mutations.py); `tests/templates` proves each
  mutation is caught by the differential test. Add a mutation when you add a template. The
  fixtures (`tests/templates/specs.py`) derive from the example specs; `test_specs.py` builds and
  checks every one without Docker, so an example edit that breaks a fixture fails in the fast
  suite, not only in the Postgres jobs.
- Implementation defaults decided during M1 are D21–D27, during M2 D28–D33, during M3 D34–D36,
  after M4 D40–D44, during M5 (brownfield) D45–D49, syntax D50, in `docs/DECISIONS.md`.  D42: row-dependent column visibility is normalization
  (the column is its own table with its own policies); there is no masking construct.
- M4: `uv run mls generate` / `mls eval --llm` (src/mls/llm, docs/LLM.md, D37–D39). Tests never call the
  API: `MLS_LLM_SCRIPT=<json>` or `ScriptedClient` replays answers. `evals/make_cases.py` regenerates
  the seed corpus (the solver must confirm every intended verdict); `mls eval evals/cases --mutate
  evals/cases/synthetic` regenerates the mutants. Keep both committed and in sync (tests check).
  `must_allow` (D40) is the lower bound: without one per stated grant a reference's mutation score
  cannot see dropped grants, and `mls eval --mutate` is idempotent over its own output. Its written
  image is assumed valid (CHECKs and FKs are assumptions, not goals): a lower bound that had to
  restate the CHECKs made the model drop them.
  Grading is by net effect (`props/diff.py:behaviour_diff`), not layer by layer; when a live case
  fails, read its transcript first (`--transcripts DIR`): the model's notes usually name the real
  cause, and so far that was an ambiguous requirement sentence or a reference bug as often as the
  model. Live runs need `ANTHROPIC_API_KEY`; decoding is unconstrained (the API rejects recursive
  schemas), answers are validated locally. `--syntax terse` (D50, #16) swaps the JSON Schema
  for the grammar and the three proved examples of `llm/terse_prompt.py` (the fast suite keeps
  them parsing, canonical, proved and disjoint from the corpus's tables); the answer is a
  section read by `syntax.parse_section` (`loop._terse_answer` finds it in a fence or prose),
  repair quotes the model's own text with line:col feedback; the grader sees the JSON shape.
  Terse is the default (`loop.DEFAULT_SYNTAX`, D50 as amended after the 2026-11-05 corpus run
  in LLM.md); tests that script JSON answers pass `syntax="json"` / `--syntax json`.
- M3: hierarchies and cardinality limits compile to SECURITY DEFINER helpers owned by `mls_internal`
  (compile/internal.py, hierarchy.py, cardinality.py). The parent-row lock is only testable with
  parallel writers (`tests/templates/test_cardinality.py`); `-m cvc5` cross-checks the org_tree
  example too (integer-quantified obligations need cvc5 without finite-model-find).
- `uv run mls explain|diff|migrate|preflight|drift ...` (M2). `mls explain --terse`, the diff's
  `before`/`after` lines and the transcripts' `spec_terse` use `mls/terse.py` (D44, D50):
  `terse.render` is the whole spec in the terse syntax (sections `mls`, `comment`, `identity`,
  `roles`, `table` blocks, `relation`, `hierarchy`, `opaque` with `$$` SQL, `rules {}`,
  `properties {}`; the grammar is the module docstring and REFERENCE §13). `spec/syntax.py` is
  the parser (`parse`, `parse_data` with a span per JSON path); `load_spec` dispatches on the
  extension (`.mls` terse, else JSON), `mls fmt` converts, every example has a committed `.mls`
  twin that `tests/test_terse.py` checks is the renderer's output, and the round trip
  `parse(render(spec)) == spec` over every example and eval case (plus hypothesis specs in
  `tests/spec/test_syntax.py`) is the evidence; the JSON stays canonical (D15) and the English
  (`english.py`, D33) stays the approval artifact. A one-element `and`/`or` is the one shape
  the round trip cannot keep; a nested same-connective group keeps its parentheses. Errors on
  terse input carry a span (`MlsError.span`, `syntax.Source.locate`; the JSON path stays the
  key): the CLI loads through `spec.load_checked`, later errors go through `spec.locate(path,
  errors)`, and `tests/spec/test_check.py` runs every error fixture through both syntaxes. Migrations are phased; every statement
  kind has a lock mode in `compile/migrate.py:LOCKS` that `tests/templates/test_locks.py` observes
  on every PG version — add the kind and its mode when you add a migration statement.
- `tests/templates/test_migrate.py` is the migration acceptance test: deploy v1 + data, migrate,
  `drift` against v2 must be clean, then the v2 differential test on the migrated database.
- M5 (brownfield): `in` with a constant list is sugar. `check_spec` validates it natively (error
  paths point into the `in`) and returns the spec with it expanded to an `or` of `=`
  (`spec/expand.py`); every consumer must use the returned spec, and nothing downstream of the
  checker ever sees an `In` node. `compile/sql.py` renders that `or` shape as one IN list
  (mutation `IN_DROP_LAST`, fixture `in_spec`), and `pg/explain.py` requires it as an index condition.
  The `session_setting` adapter (D47) reads `current_setting('app.<name>', true)` through
  `nullif(.., '')`; its hardening is drift-only (`pg/catalog.py` collects per role the functions
  that set `app.*` and the `ALTER ROLE .. SET app.*` defaults; `pg/drift.py` reports them for this
  adapter). The harness user context is the membership key (`user_context_name`), not a source.
  Table modes (D45): `Table.mode` is `owned` | `adopted` | `observed`; `Spec.governed()` is what the
  compiler, harness, objects and drift iterate (observed tables are only checked to exist). The
  compiler never creates or forces an adopted table (`compile/hardening.py`); `drift` shadow-creates
  it from the spec, reports undeclared constraints/triggers/indexes/columns on it as `foreign`
  (non-failing) and the `assume` entries as `assumed` (non-failing; a stale one is a note). The
  assumptions are rendered once (`english.assumption_lines`) for the proof report, explain, diff
  and the script header. `pg/adopt.py` is `mls import` (policies through the lifter; what it
  cannot read an opaque block with its catalog signature; inclusion-shaped FKs as inclusion
  rules; column grants as column rules; readable CHECKs as check rules; the compiler's own
  transition triggers as transition rules; hardening state as `assume`; the compiler's
  `mls_rel_*` policies and relation grants recognized, not imported) and `verify_adopted`
  (`check --dsn`). `pg/lift.py` is the lifter (D46): a tokenizer and recursive-descent parser for
  the deparsed text, an `Env` (the spec so far) and a `Lifter` that reads the compiler's shapes
  and the hand-written idioms at the boolean connectives, validating every atom with
  `spec/check.py` before keeping it; `tests/pg/test_lift.py` pins every shape (fast, no Docker),
  `tests/templates/test_lift.py` is the per-template round trip (deploy, import, drift clean,
  `behaviour_diff` empty) on PG 15–18. The harness creates adopted tables itself before the
  script (`Harness.pre_existing_sql`). `tests/brownfield/supabase_schema.sql` is the hand-written
  acceptance fixture. Selectors (D48): `Rule.applies_to` in place of `table`; `check_spec` runs
  `spec/selectors.py:expand_selectors` first (one rule per governed table, id `<id>.<table>`,
  `from`, the selector kept on the expansion for `english.selector_line`), then the checker
  with the original rule paths; nothing downstream sees a selector. `in_schema` is not
  implemented (tables carry no schema). `mls status` (`status.py`) is the per-table overlay:
  `ungoverned` / `refuted` / `opaque` / `unproved` / `proved` from the proof results (global
  properties attributed per obligation through `Obligation.rows`) plus, with `--dsn`, the live
  RLS state and the tables outside the spec; `tests/props/test_status.py` pins the output.
  Lint pack (D49, `props/lint.py`): coverage, contradiction, subsumption, redundancy as
  `mls lint` findings (never verdicts, exit 0), solver questions on the (T, F) encoding
  (`tests/logic/test_tvl.py` keeps the case the two-valued check gets wrong), a positive and
  a negative fixture per lint in `tests/props/test_lint.py`; the LLM loop quotes them as hints
  in proof-driven repair prompts. `role_separation` is a property template (one obligation,
  both roles under the same context values).
- Lean track (D11, D41): `lean/` is a Lake project (core Lean 4, no Mathlib; pinned in
  `lean/lean-toolchain`). `cd lean && lake build` checks it; CI does, and `uv run pytest -m lean`
  does when `lake` is on PATH (skipped otherwise). `lean/MLS/Pred.lean` is `logic/tvl.py`
  transcribed clause for clause and proved sound against the SQL meaning; `lean/MLS/Policy.lean`
  transcribes `props/effective.py`, the statement outcomes of `pg/model.py` and the assumption
  builders of `props/obligations.py`; `lean/MLS/Normalize.lean` is the NULL normalization of the
  CHECK and transition templates (and of their `*_NAIVE` mutations); `lean/MLS/Relational.lean`
  is the CHECK and FK templates against a relational model, with the RESTRICT guard and
  relation-change axioms of `obligations.py` derived from it; `lean/MLS/Transition.lean` is the
  transition trigger against that model (the induction step of `invariant_preserved` lifted to
  the state), `lean/MLS/Reach.lean` the compiled hierarchy helper and depth trigger,
  `lean/MLS/Cardinality.lean` the cardinality trigger over a bag of rows. When you change any of
  them, change the transcription and re-prove, or record the gap in ROADMAP.
  `lean/MLS/Vectors.lean` is generated by `uv run python -m tests.lean.vectors` (D43): `#guard`s
  comparing `Pred.eval`, `Pred.tf` and the statement outcomes with the Python evaluator, encoder
  and oracle on the same data; the fast suite checks it is current, so regenerate it when you
  change `pg/model.py`, `logic/tvl.py` or the generator. No local toolchain is needed: a throwaway container
  works (`docs/ROADMAP.md`, CI uses `leanprover/lean-action`).

## Suggested layout

```
src/mls/
  spec/        pydantic models, JSON Schema export, typechecker, fragment checker
  logic/       term AST, 3VL encoding, Z3 backend, SMT-LIB2 printer
  props/       property templates + custom properties, proof runner, reports
  compile/     one module per template: policy, check, fk, transition, time, column, index, hardening
  identity/    adapters: supabase, pg_roles, session_setting (D47)
  pg/          catalog introspection, EXPLAIN checks, differential test harness, import (adopt.py), lifter (lift.py)
  cli.py
tests/
  templates/   per-template differential + mutation tests (parametrized over PG versions)
  props/       proof tests with expected PROVED / REFUTED + counterexample shape
lean/          Lean 4 formalization of the core semantics (parallel track, D11)
examples/
docs/
```
