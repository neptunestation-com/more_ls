# MLS — "More LS": RLS more-or-less

A specification language and verified compiler for PostgreSQL access-control and data-integrity
rules. A spec (JSON) says who may see and change which rows and which invariants the data must
satisfy. `mls` proves properties about the spec with an SMT solver, then compiles it to Postgres
primitives (RLS policies, `CHECK ((p) IS TRUE)`, composite foreign keys, trigger templates, column
privileges, indexes, grants, hardening), and differential-tests the result against real Postgres.

**Start with `docs/GUIDE.md`** (the user guide: install, a walkthrough, deploying, evolving a
spec, the LLM loop) and `docs/REFERENCE.md` (every construct, property template, CLI flag and
error).  For the design: `CLAUDE.md`, then `docs/DECISIONS.md`, `docs/SEMANTICS.md`,
`docs/ROADMAP.md`, `docs/FINDINGS.md`, and `docs/BACKGROUND.md` for what the project grew out of.

## Install

    uv sync --all-extras

## Use

    uv run mls check   examples/tenant_docs.mls.json        # schema + types + fragment (+ soundness obligations)
    uv run mls prove   examples/tenant_docs.mls.json        # proof report (Markdown; --format json)
    uv run mls compile examples/tenant_docs.mls.json        # DDL
    uv run mls test    examples/tenant_docs.mls.json        # differential test on Postgres 15–18 (Docker)
    uv run mls explain examples/org_tree.mls.json           # group hierarchy + per-tenant limit, in English
    uv run mls generate req.txt --skeleton skel.json        # LLM loop: requirements -> proved spec (docs/LLM.md)
    uv run mls eval evals/cases [--llm] [--mutate DIR]      # grade the corpus; run the loop on it; make mutants
    uv run mls test    examples/tenant_docs.mls.json --versions 16 --explain   # + EXPLAIN checks at 1M rows
    uv run mls lint    examples/tenant_docs.mls.json        # leakproof warnings and policy-set findings (hygiene, exit 0)
    uv run mls explain examples/tenant_docs.mls.json        # the rules and properties in plain English
    uv run mls explain examples/tenant_docs.mls.json --terse   # the whole spec in the terse syntax (D50), one line per item
    uv run mls fmt     examples/tenant_docs.mls.json        # convert to the terse syntax (and back from a .mls file)
    uv run mls check   examples/tenant_docs.mls             # every command reads .mls (terse) or .mls.json by extension
    uv run mls diff    examples/tenant_docs.mls.json examples/tenant_docs_v2.mls.json     # access diff with scenarios
    uv run mls migrate examples/tenant_docs.mls.json examples/tenant_docs_v2.mls.json     # phased DDL with lock levels
    uv run mls preflight examples/tenant_docs_v2.mls.json --old examples/tenant_docs.mls.json --dsn $DSN
    uv run mls drift   examples/tenant_docs_v2.mls.json --dsn $DSN   # live catalog vs. a shadow deploy
    uv run mls import  --dsn $DSN --out imported.mls.json   # a spec from a live schema: tables adopted, policies lifted
    uv run mls status  examples/tenant_docs.mls.json [--dsn $DSN]   # per table: ungoverned, refuted, opaque, unproved, proved
    uv run mls schema                                       # JSON Schema of the spec language
    uv run mls eval    evals/cases                          # expected verdicts of the eval corpus

Exit codes: `check` 1 on errors, `prove` 2 if any property is refuted, `test` 1 on mismatches,
`diff` 2 if anything changed, `preflight` and `drift` 1 when they find something (`drift` lists an
adopted table's foreign objects and assumed hardening without failing), `status` 2 when a table is
refuted or, with `--dsn`, a governed table is missing or has lost row-level security.
`docs/MIGRATION.md` describes the migration phases and the observed lock levels.

## Test

    uv run pytest -m "not pg"                 # unit: spec, logic, proofs, CLI (seconds)
    uv run pytest -m "pg and not slow"        # differential, mutation, migration, lock, pre-flight, drift tests (Docker)
    uv run pytest -m slow                     # EXPLAIN checks at 1M rows
    MLS_PG_VERSIONS=16 uv run pytest -m pg    # one version
    MLS_PG_DSN=postgresql://postgres:pw@host:port/postgres uv run pytest -m pg   # an existing superuser server

## Layout

    src/mls/spec      pydantic models, JSON Schema, typechecker + fragment checker
    src/mls/logic     term AST, 3VL encoding, Z3 backend, SMT-LIB2 printer, cvc5 cross-check
    src/mls/props     policy combination, property templates as obligations, scenarios, reports, access diff
    src/mls/compile   one module per template + indexes, grants, hardening, plan, objects, migrate
    src/mls/identity  supabase, pg_roles, session_setting adapters
    src/mls/pg        concrete oracle, differential harness, EXPLAIN checks, leakproof lint,
                      pre-flight, catalog snapshots, drift, lock observation, import (adopt.py)
    src/mls/english.py  plain-English rendering of rules, properties, scenarios (the approval artifact)
    tests/            spec, logic, props, cli (no Postgres); templates (Postgres 15–18, Docker);
                      brownfield/ a hand-written Supabase-style schema `mls import` adopts
    evals/cases       eval case format and the first cases (the prototype's three corpora)
    examples/         tenant_docs (supabase), tenant_docs_v2 (its next version), pg_roles_docs (pg_roles),
                      session_docs (session_setting), org_tree (hierarchy with in_group policies and a
                      cardinality limit)
    prototype/        the throwaway Python + Z3 prototype this grew from
    docs/             GUIDE (user guide), REFERENCE (language and CLI), SEMANTICS, DECISIONS,
                      ROADMAP, FINDINGS, BACKGROUND (what pg_more_ls established), MIGRATION, LLM

## Status

Alpha (0.1).  All four milestones of `docs/ROADMAP.md` are implemented:

1. the core loop: check, prove, compile, differential test, with no LLM;
2. evolution: `diff`, `migrate`, `preflight`, `drift`, and the English rendering;
3. the harder constructs: group hierarchies with `in_group`, cardinality limits with a
   parent-row lock, full opaque-block reporting;
4. the LLM loop with bounded repair and human escalation, and the evaluation corpus with
   solver-graded mutants (`docs/LLM.md`).

Milestone 5 (brownfield, the `pg_more_ls` graft) is in progress: `in` with a constant list, the
`session_setting` adapter, ownership modes per table (`owned` / `adopted` / `observed`) with
stated hardening assumptions, `mls import` of a live schema, and the lifter (D46: the policies
MLS itself emits and the usual hand-written idioms read back into the verified fragment, the
rest opaque blocks with their catalog signature; importing a deployed spec gives it back with
no opaque block and an empty behaviour diff), and selectors (`applies_to`, D48: one rule for
every table a selector matches, expanded by `check`), `mls status` (per table: ungoverned, refuted,
opaque, unproved, proved) and the lint pack (D49: coverage, contradiction, subsumption and
redundancy as findings, never verdicts, plus the `role_separation` template) are done, which
closes Milestone 5; the terse syntax (D50) is next (`docs/ROADMAP.md`).

### Measured with `claude-opus-5-5`

- **Seed corpus, 30 generation cases:** 28 end proved, net-equivalent to their reference and with
  the reference properties' expected verdicts.  4 cases needed one repair round; none was
  escalated.
- **Repair benchmark, 194 synthetic cases** (the loop starts from mutated rules): 187 end proved
  and net-equivalent after about one repair round.
- **Mutation score of the corpus's property sets: 75 %.**  Properties state both bounds: the
  safety templates say what a role may never do, `must_allow` says what it must be able to do
  (D40).

The seed cases are the author's rendering of common patterns, so these numbers are as much a
statement about their requirement texts as about the model.  Every failure so far was read
before it was counted, and most were a sentence with two readings or a defect in the author's
reference.  `docs/FINDINGS.md` records what the runs found; `docs/LLM.md` explains the grading.

### The Lean 4 track

`lean/` (core Lean, no Mathlib) formalizes the semantics and proves, among other things:

- the three-valued logic, the pair encoding and the predicate encoder are sound against the SQL
  meaning of every construct of the verified fragment, `in_group` included;
- Postgres policy combination: a row is admitted iff some permissive policy is TRUE on it and
  every restrictive one is;
- the statement outcomes: every executed statement satisfies what the proofs assume, and
  without column reads it is executed exactly when the net-effect predicate admits it;
- the NULL normalization: with `IS TRUE`, RLS, CHECK and triggers admit the same rows, and
  without it they disagree exactly on UNKNOWN;
- against a small relational model, the CHECK, FK and transition templates keep their invariants
  under every statement, the RESTRICT guard the proofs assume is the one the FK enforces, and
  the induction step the solver discharges for the written row is enough for the state;
- the compiled hierarchy helper computes `in_group` (descending from the user's groups meets the
  walk up from the row's group), and the cardinality trigger keeps its bound over a bag of rows
  when the writers of a group are serialized, which is what the parent-row lock provides.

The transcription is checked against the Python by generated test vectors: the evaluator, the
encoder and the oracle on the same data, as `#guard`s in the Lean build.
