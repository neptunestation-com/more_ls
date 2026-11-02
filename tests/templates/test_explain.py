"""D14: every generated policy is index-driven at 1M rows (EXPLAIN FORMAT JSON), and dropping
the generated indexes is detected."""
from __future__ import annotations

import random

import pytest

from mls.compile import Mutation, Script, compile_spec
from mls.pg.explain import explain_checks, scale_load_sql
from mls.pg.harness import Harness, ValueGen

pytestmark = [pytest.mark.pg, pytest.mark.slow]

SCALE = 1_000_000


def _skinny(script: Script) -> Script:
    return Script([s for s in script.statements if s.section not in ("checks", "fks", "transitions")],
                  script.assumptions)


def _run(pg_dsn, spec, mutations):
    h = Harness(spec, _skinny(compile_spec(spec, mutations)), pg_dsn, "mls_explain")
    try:
        h.deploy()
        with h.conn.cursor() as cur:
            for s in scale_load_sql(spec, h.adapter, SCALE):
                cur.execute(s)
            cur.execute('select count(*) from "docs"')
            assert cur.fetchone()[0] >= SCALE
        gen = ValueGen(spec, h.adapter, random.Random(0), n_users=100)
        return explain_checks(h.conn, spec, h.adapter, lambda role: gen.context(role))
    finally:
        h.close()


def test_policies_are_index_driven_at_scale(pg_dsn, example_spec):
    results = _run(pg_dsn, example_spec, set())
    assert results, "no plans checked"
    bad = [f"{r.table}/{r.role}: {r.problems}" for r in results if not r.ok]
    assert not bad, bad


def test_missing_indexes_are_detected(pg_dsn, example_spec):
    results = _run(pg_dsn, example_spec, {Mutation.INDEX_NONE})
    assert any(not r.ok for r in results)
