"""Supabase as a target platform (D55): the Supabase examples deploy as Supabase's `postgres` role
(not a superuser) on the `supabase/postgres` image, the differential test passes on the deployed
database, and drift is clean.  `MLS_SUPABASE_DSN=<postgres dsn>|<supabase_admin dsn>` uses a
running image instead of starting one."""
from __future__ import annotations

import os

import pytest

from mls.compile import compile_spec
from mls.pg.drift import drift
from mls.pg.harness import Harness, run_differential
from mls.spec import load_checked
from tests.conftest import ROOT

pytestmark = [pytest.mark.pg, pytest.mark.supabase]

EXAMPLES = ["tenant_docs", "org_tree", "team_tasks"]


@pytest.fixture(scope="module")
def supabase():
    if os.environ.get("MLS_SKIP_PG"):
        pytest.skip("MLS_SKIP_PG set")
    from mls.pg.server import supabase_container

    with supabase_container() as server:
        yield server


@pytest.fixture(scope="module", params=EXAMPLES)
def deployed(request, supabase):
    spec = load_checked(ROOT / "examples" / f"{request.param}.mls")
    h = Harness(spec, compile_spec(spec), supabase.dsn, f"mls_supa_{request.param}", prepare=supabase.prepare)
    h.deploy()
    yield request.param, spec, h
    h.close()


def test_deploys_as_postgres_and_passes_the_differential_test(deployed):
    name, spec, h = deployed
    with h.conn.cursor() as cur:
        cur.execute("select current_user, rolsuper from pg_roles where rolname = current_user")
        assert cur.fetchone() == ("postgres", False)
    res = run_differential(spec, h.script, h.dsn, h.dbname, harness=h, seed=1,
                           rows_per_table=30, n_contexts=16, ops_per_context=25)
    assert res.ok, f"{name}: {res.summary()}\n" + "\n".join(str(m) for m in res.mismatches[:10])
    assert res.visible_rows > 0 and res.outcomes["ok"] > 0


def test_anon_loses_the_default_grants_and_service_role_keeps_them(deployed):
    """Supabase's default privileges grant every new table and function to its API roles; the
    deploy revokes them from the untrusted ones the spec does not use (anon) and leaves the
    trusted backend role, which the proof report names."""
    from mls.props.report import assumptions

    name, spec, h = deployed
    with h.conn.cursor() as cur:
        for t in spec.governed():
            cur.execute("select has_table_privilege('anon', %s, 'SELECT'), "
                        "has_table_privilege('service_role', %s, 'SELECT')", (f'"{t}"', f'"{t}"'))
            assert cur.fetchone() == (False, True), t
        cur.execute("select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
                    "where n.nspname = 'public' and has_function_privilege('anon', p.oid, 'EXECUTE') "
                    "and exists (select 1 from aclexplode(p.proacl) x where x.grantee = 'anon'::regrole)")
        assert cur.fetchall() == []
    assert any("service_role has BYPASSRLS" in a for a in assumptions(spec, []))


def test_drift_is_clean(deployed):
    name, spec, h = deployed
    res = drift(h.conn, spec, compile_spec(spec))
    assert res.ok, [str(i) for i in res.items]
