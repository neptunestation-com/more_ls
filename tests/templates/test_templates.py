"""Per-template differential tests against Postgres 15-18 (0 mismatches) and mutation tests
(each deliberately wrong compilation must produce mismatches).  Invariant 3."""
from __future__ import annotations

import random

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mls.compile import Mutation, compile_spec
from mls.pg.harness import Harness, run_differential
from tests.templates import specs

pytestmark = pytest.mark.pg

CASES = {
    "policy": (specs.policy_spec, Mutation.POLICY_ALL_RESTRICTIVE, None),
    "policy_restrictive": (specs.policy_spec, Mutation.POLICY_DROP_RESTRICTIVE, None),
    "in": (specs.in_spec, Mutation.IN_DROP_LAST, None),
    "check": (specs.check_nullable_spec, Mutation.CHECK_NAIVE, None),
    "transition": (specs.transition_nullable_spec, Mutation.TRANSITION_NAIVE, None),
    "fk": (specs.fk_spec, Mutation.FK_MATCH_FULL, None),
    "column": (specs.column_rule_spec, Mutation.COLUMN_TABLE_GRANT, None),
    "time": (specs.time_spec, Mutation.TIME_NULL_CLOSED, None),
    "opaque": (specs.opaque_spec, Mutation.OPAQUE_INVERT, specs.opaque_impl),
    "pg_roles": (specs.pg_roles_spec, Mutation.POLICY_ALL_RESTRICTIVE, None),
    "session_setting": (specs.session_spec, Mutation.POLICY_ALL_RESTRICTIVE, None),
    "hierarchy": (specs.hierarchy_spec, Mutation.HIERARCHY_SHALLOW, None),
    "hierarchy_depth": (specs.hierarchy_spec, Mutation.HIERARCHY_NO_DEPTH_CHECK, None),
    "cardinality": (specs.cardinality_spec, Mutation.CARDINALITY_OFF_BY_ONE, None),
}


@pytest.fixture(scope="module", params=list(CASES), ids=list(CASES))
def deployed(request, pg_dsn):
    name = request.param
    build, _, impl = CASES[name]
    spec = build()
    h = Harness(spec, compile_spec(spec), pg_dsn, f"mls_t_{name}", impl() if impl else None)
    h.deploy()
    yield name, spec, h
    h.close()


@settings(max_examples=6, deadline=None, database=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(seed=st.integers(0, 10_000), rows=st.integers(2, 30), null_p=st.sampled_from([0.1, 0.3, 0.6]))
def test_differential(deployed, seed, rows, null_p):
    """Hypothesis chooses the seed and the data shape (row count, NULL rate); the harness fills in
    rows, contexts and statements from that seed, so a failure shrinks to a small shape."""
    name, spec, h = deployed
    res = run_differential(spec, h.script, h.dsn, h.dbname, harness=h, seed=seed, null_p=null_p,
                           rows_per_table=rows, n_contexts=6, ops_per_context=15)
    assert res.ok, f"{name}: {res.summary()}\n" + "\n".join(str(m) for m in res.mismatches[:10])
    assert res.outcomes["ok"] > 0 and res.outcomes["error"] > 0


def test_differential_seeded(deployed):
    """A larger seeded run (the hypothesis run above is small so it can shrink)."""
    name, spec, h = deployed
    res = run_differential(spec, h.script, h.dsn, h.dbname, harness=h, seed=1,
                           rows_per_table=30, n_contexts=16, ops_per_context=25)
    assert res.ok, f"{name}: {res.summary()}\n" + "\n".join(str(m) for m in res.mismatches[:10])
    assert res.visible_rows > 0 and res.outcomes["ok"] > 0 and res.outcomes["filtered"] > 0, res.summary()


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_is_detected(name, pg_dsn):
    build, mutation, impl = CASES[name]
    spec = build()
    script = compile_spec(spec, {mutation})
    res = run_differential(spec, script, pg_dsn, f"mls_m_{name}", seed=1, rows_per_table=30,
                           n_contexts=16, ops_per_context=25, opaque_impl=impl() if impl else None)
    assert not res.ok, f"{name}: mutation {mutation.value} went undetected ({res.summary()})"
