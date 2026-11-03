# MLS — "More LS": RLS more-or-less

A specification language and verified compiler for PostgreSQL access-control and data-integrity
rules. A spec (JSON) says who may see and change which rows and which invariants the data must
satisfy. `mls` proves properties about the spec with an SMT solver, then compiles it to Postgres
primitives (RLS policies, `CHECK ((p) IS TRUE)`, composite foreign keys, trigger templates, column
privileges, indexes, grants, hardening), and differential-tests the result against real Postgres.

Read `CLAUDE.md`, then `docs/DECISIONS.md`, `docs/SEMANTICS.md`, `docs/ROADMAP.md`, `docs/FINDINGS.md`.

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
    uv run mls lint    examples/tenant_docs.mls.json        # leakproof lint
    uv run mls explain examples/tenant_docs.mls.json        # the rules and properties in plain English
    uv run mls diff    examples/tenant_docs.mls.json examples/tenant_docs_v2.mls.json     # access diff with scenarios
    uv run mls migrate examples/tenant_docs.mls.json examples/tenant_docs_v2.mls.json     # phased DDL with lock levels
    uv run mls preflight examples/tenant_docs_v2.mls.json --old examples/tenant_docs.mls.json --dsn $DSN
    uv run mls drift   examples/tenant_docs_v2.mls.json --dsn $DSN   # live catalog vs. a shadow deploy
    uv run mls schema                                       # JSON Schema of the spec language
    uv run mls eval    evals/cases                          # expected verdicts of the eval corpus

Exit codes: `check` 1 on errors, `prove` 2 if any property is refuted, `test` 1 on mismatches,
`diff` 2 if anything changed, `preflight` and `drift` 1 when they find something.
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
    src/mls/identity  supabase, pg_roles adapters
    src/mls/pg        concrete oracle, differential harness, EXPLAIN checks, leakproof lint,
                      pre-flight, catalog snapshots, drift, lock observation
    src/mls/english.py  plain-English rendering of rules, properties, scenarios (the approval artifact)
    tests/            spec, logic, props, cli (no Postgres); templates (Postgres 15–18, Docker)
    evals/cases       eval case format and the first cases (the prototype's three corpora)
    examples/         tenant_docs (supabase), tenant_docs_v2 (its next version), pg_roles_docs (pg_roles),
                      org_tree (hierarchy with in_group policies and a cardinality limit)
    prototype/        the throwaway Python + Z3 prototype this grew from

## Status

Milestones 1 (core loop, no LLM), 2 (`diff | migrate | preflight | drift`, English rendering),
3 (group hierarchies with `in_group`, cardinality limits with a parent-row lock, full opaque-block
reporting) and 4 (the LLM loop with bounded repair and human escalation, the evaluation corpus with
solver-graded mutants; `docs/LLM.md`) are implemented.  Measured live with `claude-opus-5-5`
on the 30 generation cases of the seed corpus: every case ends proved, net-equivalent to its
reference and with the reference properties' expected verdicts (3 cases needed one repair round,
none was escalated).  The seed cases are the author's rendering of common patterns, and that
number is as much a statement about their requirement texts as about the model: see
`docs/FINDINGS.md` for what the first run found and `docs/LLM.md` for how grading works.
