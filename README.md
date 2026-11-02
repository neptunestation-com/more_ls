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
    uv run mls test    examples/tenant_docs.mls.json --versions 16 --explain   # + EXPLAIN checks at 1M rows
    uv run mls lint    examples/tenant_docs.mls.json        # leakproof lint
    uv run mls schema                                       # JSON Schema of the spec language
    uv run mls eval    evals/cases                          # expected verdicts of the eval corpus

Exit codes: `check` 1 on errors, `prove` 2 if any property is refuted, `test` 1 on mismatches.

## Test

    uv run pytest -m "not pg"                 # unit: spec, logic, proofs, CLI (seconds)
    uv run pytest -m "pg and not slow"        # differential + mutation tests, Postgres 15–18 in Docker
    uv run pytest -m slow                     # EXPLAIN checks at 1M rows
    MLS_PG_VERSIONS=16 uv run pytest -m pg    # one version
    MLS_PG_DSN=postgresql://postgres:pw@host:port/postgres uv run pytest -m pg   # an existing superuser server

## Layout

    src/mls/spec      pydantic models, JSON Schema, typechecker + fragment checker
    src/mls/logic     term AST, 3VL encoding, Z3 backend, SMT-LIB2 printer, cvc5 cross-check
    src/mls/props     policy combination, property templates as obligations, scenarios, reports
    src/mls/compile   one module per template + indexes, grants, hardening, plan
    src/mls/identity  supabase, pg_roles adapters
    src/mls/pg        concrete oracle, differential harness, EXPLAIN checks, leakproof lint
    tests/            spec, logic, props, cli (no Postgres); templates (Postgres 15–18, Docker)
    evals/cases       eval case format and the first cases (the prototype's three corpora)
    examples/         tenant_docs (supabase), pg_roles_docs (pg_roles)
    prototype/        the throwaway Python + Z3 prototype this grew from

## Status

Milestone 1 (core loop, no LLM) is implemented. Milestones 2–4 (`diff | migrate | preflight |
drift`, hierarchies and cardinality, the LLM loop) are in `docs/ROADMAP.md`.
