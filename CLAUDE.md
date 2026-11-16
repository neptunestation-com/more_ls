# MLS — "More LS": RLS more-or-less

MLS is a specification language and compiler for PostgreSQL access-control and data-integrity rules.
A spec (JSON) states who may see and change which rows and which invariants the data must satisfy.
The toolchain **proves properties** about the spec with an SMT solver, then **compiles** it to
Postgres primitives: RLS policies, CHECK constraints, foreign keys, triggers from fixed templates,
column privileges, indexes and grants. RLS is one compilation target, not the foundation.

Specs are written by LLMs from human business requirements. Humans approve **properties and
scenarios** (plain English, concrete examples), never MLS or DDL.

Read before writing code, in this order:
1. `docs/design/DECISIONS.md` — settled decisions. Do not reopen them; flag conflicts instead.
2. `docs/SEMANTICS.md` — the meaning of every construct. This is the source of truth.
3. `docs/design/FINDINGS.md` — what experiments on real Postgres already established.
4. `docs/design/BACKGROUND.md` — what `pg_more_ls` established before this project: the undecidability
   theorem and the survey of authorization languages, cited, not restated.
5. `prototype/` — a working throwaway prototype (Python + Z3). Reuse ideas, not code structure.
6. `examples/` — the example specs, each as `.mls` and `.mls.json`.

The milestones that built all of this are in `docs/design/archive/ROADMAP.md` (archived; every one is implemented).

`docs/` is for users: `docs/GUIDE.md` (user guide; §3 is the takeover, step by step),
`docs/REFERENCE.md` (language and CLI reference), `docs/SEMANTICS.md` and `docs/LLM.md`.
`docs/design/` is for contributors: DECISIONS, FINDINGS, EVALS (the LLM loop's corpus and
measured runs), BACKGROUND and `archive/`. The user docs state the current behaviour without its
history (invariant 8); the reasoning goes in DECISIONS. When you add or change a construct, a property template, a CLI flag, an exit
code or a generated object name, update them in the same change. A construct lives in both
syntaxes (D50): give it its terse form in `terse.py` and its parse in `spec/syntax.py`
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
8. **Development artefacts never reach users.**  Decision numbers (D45), issue numbers (#27),
   milestones and amendments are for contributors: they may appear in comments, docstrings, commit
   messages and `docs/design/`, never in anything the tool prints (output, errors, CLI help,
   generated SQL, the LLM's prompts) or in the user docs.  Say what the decision means instead.
   `tests/test_no_dev_artefacts.py` enforces it in the fast suite.

## Stack and conventions

- Python 3.12+, `uv` for env and dependencies, `pydantic` v2 for the spec model and JSON Schema export.
- `z3-solver` primary; solver access goes through our own small term layer that can also print
  SMT-LIB2 so `cvc5` cross-checks results in CI.
- `psycopg` 3; `pytest`; `hypothesis` for differential fuzzing; Docker (or testcontainers) to run
  Postgres 15–18 in CI.
- Parse `EXPLAIN (FORMAT JSON)`, never text plans (InitPlan display differs across versions).
- Package name `mls`; CLI `mls` with subcommands (`check`, `prove`, `compile`, `test`, `lint`,
  `explain`, `fmt`, `schema`, `diff`, `migrate`, `preflight`, `drift`, `import`, `status`,
  `generate`, `eval`).

## Running things

- `uv sync --all-extras`; `uv run mls <check|prove|compile|test|lint|fmt|schema|eval> ...` (a spec
  is `.mls` terse or JSON, by extension).
- `uv run pytest -m "not pg"` is fast and needs no Docker. `uv run pytest -m pg` starts
  `postgres:15..18` containers (set `MLS_PG_VERSIONS=16` for one, `MLS_PG_DSN=...` for an existing
  server). `-m slow` runs the EXPLAIN checks at 1M rows.
- D55: `-m supabase` (also selected by `-m pg`) runs `tests/platform` on the `supabase/postgres`
  image as its non-superuser `postgres` role (`pg/server.py:supabase_container`; each test database
  gets the image's auth schema, default privileges and GoTrue's auth functions via `prepare`).
  A template that works as a superuser can still fail there: FINDINGS "Supabase as a platform".
- D53: `int` and `bigint` are one SMT sort and compare with each other (`model.comparable`);
  only constants are bounded (`model.INT_RANGE`). The `bigint` fixture (`specs.bigint_spec`)
  draws values beyond 2³¹, so an `integer` left anywhere in a template fails its load. An
  adopted `varchar(n)` column is `text` (`lift.mls_type`); drift's stand-in takes the live type.
- D52: `mls import --policies discard` writes the takeover skeleton (`pg/adopt.py`; membership
  tables from FK-backed two-column keys). On an adopted table a check or inclusion rule compiles
  to `compile/adopted.py`'s compare-or-replace block (mutation `ADOPTED_KEEPS_CONSTRAINT`).
  `tests/brownfield/starter_schema.sql` is the Supabase starter; `tests/platform/test_takeover.py`.
- D54: bypass paths (`pg/catalog.py:bypass_paths`: non-invoker views, transitively, materialized
  views, SECURITY DEFINER functions whose owner bypasses the policies and whose body names the
  table) are drift `hardening` (`pg/drift.py:bypass_report`, also read by `mls status --dsn` and
  import's notes) unless the table assumes `{"bypass": "view v"}`; `tests/templates/test_bypass.py`
  has the positive and negative fixtures, the takeover test fixes the starter's two.
- #24: GUIDE §3 is the takeover, step by step, replayed by `tests/platform/test_guide_takeover.py`
  on the starter plus `tests/brownfield/starter_data.sql` (same block/command conventions as
  `test_guide.py`; `psql` and `pg_dump` lines are emulated); its spec is
  `examples/starter_takeover.mls`.  A change to import, diff, drift, status or deploy notices
  updates GUIDE §3 in the same change.  A policy on a relation's own table may not read the
  relation (Postgres: infinite recursion; `spec/check.py`).  `Harness(foreign=catalog.foreign_keys(..))`
  runs the differential on a deployed brownfield database.  #26: `mls generate --skeleton` keeps
  the skeleton's own rules as fixed rules (`Loop(fixed_rules=...)`), shown to both passes and
  appended after the model's; a clash of id or name is an error at the answer's path.
- #27: "conditional on an opaque block" means the proof needs it: `props/runner.py:needed_opaques`
  re-solves a PROVED obligation with the block freed (`logic/tvl.py:free_opaques`, each occurrence
  an independent (T, F) pair); the lint uses the same re-check, the diff only the occurrence filter.
- D56: a composed relation (`ComposedRelation`, `of` + `through` hops, `spec/chain.py:levels`;
  a prefix is named `R/i` internally) is a policy atom, refused in invariants, inclusions,
  hierarchies, opaque blocks and `monotone_in`.  Encoded with `hop.T.v` / `hop.T.v.k` functions
  plus `obligations.hop_row_axioms`; compiled to nested `= any(array(...))`, a hop visibility
  policy `mls_rel_<R>_h<i>_*` per hop (mutation `HOP_VISIBILITY_DROPPED`); fixture
  `specs.composed_spec` on `examples/team_tasks.mls`; Lean `Pred.memberVia`.
- Every compiler template has a `Mutation` (compile/mutations.py); `tests/templates` proves each
  mutation is caught by the differential test. Add a mutation when you add a template. The
  fixtures (`tests/templates/specs.py`) derive from the example specs; `test_specs.py` builds and
  checks every one without Docker, so an example edit that breaks a fixture fails in the fast
  suite, not only in the Postgres jobs.
- The guide's tour (GUIDE §4) is replayed by `tests/test_guide.py` on `examples/team_notes.mls`:
  every command it shows runs in its own process (in-process runs get other counterexamples)
  and must print the lines the guide shows. A change to report, lint, explain or compile output
  updates the tour in the same change.
- Implementation defaults decided during M1 are D21–D27, during M2 D28–D33, during M3 D34–D36,
  after M4 D40–D44, during M5 (brownfield) D45–D49, syntax D50, M6 (takeover) D51–D56, in `docs/design/DECISIONS.md`.  D42: row-dependent column visibility is normalization
  (the column is its own table with its own policies); there is no masking construct.
- M4: `uv run mls generate` / `mls eval --llm` (src/mls/llm, docs/LLM.md; the evals in docs/design/EVALS.md; D37–D39). Tests never call the
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
  in docs/design/EVALS.md); tests that script JSON answers pass `syntax="json"` / `--syntax json`.
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
  compiler never creates or forces an adopted table (`compile/hardening.py`) but replaces its whole policy set (D51, `compile/policy.py:replace_policies_ddl`; the harness seeds every adopted table with foreign policies, mutation `REPLACE_KEEPS_FOREIGN`); `drift` shadow-creates
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
- Lean track (D11, D41): `lean/` formalizes the core semantics; details in `lean/CLAUDE.md`. When you
  change `logic/tvl.py`, `props/effective.py`, `pg/model.py`, `props/obligations.py` or the CHECK, FK,
  transition, hierarchy or cardinality templates, change the `lean/MLS/*.lean` transcription and re-prove
  (or record the gap under "Known gaps" in `lean/CLAUDE.md`), and regenerate `lean/MLS/Vectors.lean` (`uv run python -m tests.lean.vectors`).
