"""The session_setting adapter (D47) on Postgres 15-18: a missing setting denies everything, and
the adapter's hardening (no app role may execute a function that sets app.*, no ALTER ROLE ...
SET app.* default) is observed by drift, with a tampering for each."""
from __future__ import annotations

import psycopg
import pytest

from mls.compile import compile_spec
from mls.pg.drift import drift
from mls.pg.harness import Harness, run_differential
from tests.templates import specs

pytestmark = pytest.mark.pg


@pytest.fixture(scope="module")
def deployed(pg_dsn):
    spec = specs.session_spec()
    h = Harness(spec, compile_spec(spec), pg_dsn, "mls_session")
    h.deploy()
    res = run_differential(spec, h.script, h.dsn, h.dbname, harness=h, seed=1, rows_per_table=30,
                           n_contexts=8, ops_per_context=10)
    assert res.ok and res.visible_rows > 0, res.summary()
    yield spec, h
    h.close()


def _denied(cur, sql: str) -> bool:
    """Run `sql` in a savepoint: denied means no row was touched or Postgres refused it (a policy
    violation and a missing privilege are both InsufficientPrivilege)."""
    cur.execute("savepoint s")
    try:
        cur.execute(sql)
        n = cur.rowcount
        if cur.description:
            n = len(cur.fetchall())
        return n == 0
    except psycopg.errors.InsufficientPrivilege:
        return True
    finally:
        cur.execute("rollback to savepoint s")


def test_missing_setting_denies_everything(deployed):
    """No set_config in the transaction: current_setting is NULL, every comparison UNKNOWN, so
    every row is filtered and every write is denied (3VL, no special case in the compiler)."""
    from mls.compile.column import privileges

    spec, h = deployed
    priv = privileges(spec)
    with h.conn.cursor() as cur:
        cur.execute("select count(*) from docs")
        assert cur.fetchone()[0] > 0  # as the owner: rows exist
        cur.execute("begin")
        try:
            cur.execute("set local role authenticated")
            checked = 0
            for t in spec.tables:
                cols = priv.get(("authenticated", t), {})
                if "select" in cols:
                    sel = list(spec.tables[t].columns) if cols["select"] == "*" else sorted(cols["select"])
                    assert _denied(cur, f'select {", ".join(f"\"{c}\"" for c in sel)} from "{t}"'), t
                    checked += 1
                assert _denied(cur, f'update "{t}" set "{next(iter(spec.tables[t].columns))}" = null'), t
                assert _denied(cur, f'delete from "{t}"'), t
            assert checked > 0
            assert _denied(cur, "insert into docs (id, tenant_id, owner_id, level, archived) values (999, 1, null, 0, 0)")
        finally:
            cur.execute("rollback")


def test_empty_setting_is_null_too(deployed):
    """A setting that was set and reset reads as '' (not NULL); the nullif in the adapter's SQL
    makes it NULL, so it denies like a missing one."""
    spec, h = deployed
    with h.conn.cursor() as cur:
        cur.execute("begin")
        try:
            cur.execute("select set_config('app.uid', '', true)")
            cur.execute("select set_config('app.clearance', '', true)")
            cur.execute("set local role authenticated")
            cur.execute("select count(*) from docs")
            assert cur.fetchone()[0] == 0
        finally:
            cur.execute("rollback")


TAMPER = {
    "setter_function": ("create function set_app_uid(u text) returns void language sql security definer "
                        "as $$ select set_config('app.uid', u, false) $$; "
                        "grant execute on function set_app_uid(text) to authenticated",
                        "role authenticated may execute public.set_app_uid(u text), which sets app.*"),
    "setter_function_public": ("create function become(u text) returns void language plpgsql as "
                               "$$ begin perform set_config('app.uid', u, true); end $$",
                               "role authenticated may execute public.become(u text), which sets app.*"),
    "set_local_in_body": ("create function enter() returns void language plpgsql as "
                          "$$ begin set local app.clearance = '3'; end $$",
                          "role authenticated may execute public.enter(), which sets app.*"),
    "role_default": ("alter role authenticated set app.clearance = '3'",
                     "role authenticated has a default for app.clearance (ALTER ROLE ... SET)"),
}


def test_fresh_deploy_has_no_drift(deployed):
    spec, h = deployed
    res = drift(h.conn, spec, compile_spec(spec))
    assert res.ok, [str(i) for i in res.items]


@pytest.mark.parametrize("name", list(TAMPER))
def test_hardening_tampering_is_reported(deployed, name):
    spec, h = deployed
    sql, expected = TAMPER[name]
    try:
        with h.conn.cursor() as cur:
            cur.execute(sql)
        res = drift(h.conn, spec, compile_spec(spec))
        assert any(str(i) == f"hardening: {expected}" for i in res.items), [str(i) for i in res.items]
    finally:
        with h.conn.cursor() as cur:  # ALTER ROLE and functions are not transactional across drift's own tx
            cur.execute("drop function if exists set_app_uid(text), become(text), enter()")
            cur.execute("alter role authenticated reset all")
    assert drift(h.conn, spec, compile_spec(spec)).ok
