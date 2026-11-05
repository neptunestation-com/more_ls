"""Lock levels per migration statement, observed in pg_locks on every Postgres version
(ROADMAP: "test, don't assume")."""
from __future__ import annotations

import psycopg
import pytest

from mls.compile import compile_spec
from mls.compile.migrate import LOCKS, migrate
from mls.pg.harness import Harness
from mls.pg.locks import observe_migration
from tests.templates import specs

pytestmark = pytest.mark.pg

CHAIN = ["a", "b", "c_level", "d", "a"]  # c_level: v2 plus an index to create and then drop


def test_documented_locks_are_observed(pg_dsn):
    first = getattr(specs, f"v_{CHAIN[0]}")()
    h = Harness(first, compile_spec(first), pg_dsn, "mls_locks")
    h.deploy()
    db_dsn = psycopg.conninfo.make_conninfo(pg_dsn, dbname=h.dbname)
    seen: dict[str, set[str | None]] = {}
    bad = []
    try:
        for a, b in zip(CHAIN, CHAIN[1:]):
            old, new = getattr(specs, f"v_{a}")(), getattr(specs, f"v_{b}")()
            for ob in observe_migration(h.conn, db_dsn, migrate(old, new, allow_destructive=True)):
                st = ob.statement
                if st.table is None or LOCKS[st.kind] is None:
                    continue
                seen.setdefault(st.kind, set()).add(ob.mode)
                if not ob.ok:
                    bad.append(f"{st.kind}: expected {ob.expected}, observed {ob.mode}: {st.sql[:80]}")
    finally:
        h.close()
    assert not bad, "\n".join(bad)
    documented = {k for k, v in LOCKS.items() if v is not None}
    assert documented <= set(seen), f"never observed: {sorted(documented - set(seen))}"
