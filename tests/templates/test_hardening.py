"""D13: the emitted hardening is verifiable in the catalog, and its mutation is detected."""
from __future__ import annotations

import psycopg
import pytest

from mls.compile import Mutation, compile_spec
from mls.pg.harness import Harness

pytestmark = pytest.mark.pg


def catalog_problems(conn: psycopg.Connection, spec) -> list[str]:
    problems = []
    with conn.cursor() as cur:
        for t in spec.tables:
            cur.execute("select relrowsecurity, relforcerowsecurity from pg_class where relname = %s", (t,))
            rls, force = cur.fetchone()
            if not rls:
                problems.append(f"{t}: RLS not enabled")
            if not force:
                problems.append(f"{t}: RLS not forced")
            cur.execute("select tableowner from pg_tables where tablename = %s", (t,))
            owner = cur.fetchone()[0]
            for r in spec.roles:
                if owner == r:
                    problems.append(f"{t}: owned by app role {r}")
                cur.execute("select has_table_privilege(%s, %s, 'TRUNCATE')", (r, t))
                if cur.fetchone()[0]:
                    problems.append(f"{t}: {r} may TRUNCATE")
        for r in spec.roles:
            cur.execute("select rolbypassrls, rolsuper from pg_roles where rolname = %s", (r,))
            bypass, su = cur.fetchone()
            if bypass or su:
                problems.append(f"{r}: BYPASSRLS/superuser")
            cur.execute("select has_parameter_privilege(%s, 'session_replication_role', 'SET')", (r,))
            if cur.fetchone()[0]:
                problems.append(f"{r}: may SET session_replication_role")
    return problems


@pytest.mark.parametrize("mutations,expect_problems", [(set(), False), ({Mutation.HARDENING_NO_FORCE}, True)])
def test_hardening_in_catalog(pg_dsn, example_spec, mutations, expect_problems):
    h = Harness(example_spec, compile_spec(example_spec, mutations), pg_dsn, "mls_hardening")
    try:
        h.deploy()
        problems = catalog_problems(h.conn, example_spec)
    finally:
        h.close()
    assert bool(problems) == expect_problems, problems
