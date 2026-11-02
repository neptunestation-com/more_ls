"""D4: the bound holds under READ COMMITTED with parallel writers because the trigger locks the
parent row first; without the lock (mutation) the writers overshoot."""
from __future__ import annotations

import threading
import time

import psycopg
import pytest

from mls.compile import Mutation, compile_spec
from mls.pg.harness import Harness
from tests.templates import specs

pytestmark = pytest.mark.pg

WRITERS, ATTEMPTS, MAX = 8, 6, 5


def hammer(dsn: str, dbname: str, adapter, n_writers: int, attempts: int, hold: float) -> int:
    """Each writer, as the app role, tries `attempts` inserts into tenant 1 (group 1), holding
    every successful insert open for `hold` seconds before commit so the writers overlap."""
    db_dsn = psycopg.conninfo.make_conninfo(dsn, dbname=dbname)
    ctx = {"uid": adapter.harness_user_values(1)[0]}
    next_id = [1000]
    lock = threading.Lock()

    def writer():
        with psycopg.connect(db_dsn, autocommit=True) as conn:
            for _ in range(attempts):
                with lock:
                    next_id[0] += 1
                    fid = next_id[0]
                try:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            for s in adapter.harness_context_sql("authenticated", ctx):
                                cur.execute(s)
                            cur.execute('insert into "files" ("id", "group_id", "tenant_id", "size") '
                                        'values (%s, 1, 1, 1)', (fid,))
                            time.sleep(hold)
                except psycopg.errors.CheckViolation:
                    pass

    threads = [threading.Thread(target=writer) for _ in range(n_writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with psycopg.connect(db_dsn) as conn:
        return conn.execute('select count(*) from "files" where "tenant_id" = 1').fetchone()[0]


@pytest.mark.parametrize("mutations,within_bound", [(set(), True), ({Mutation.CARDINALITY_NO_LOCK}, False)])
def test_parallel_writers(pg_dsn, mutations, within_bound):
    spec = specs.org_base_spec()
    h = Harness(spec, compile_spec(spec, mutations), pg_dsn, "mls_card")
    h.deploy()
    try:
        with h.conn.cursor() as cur:
            cur.execute('insert into "tenants" ("id") values (1)')
            cur.execute('insert into "groups" ("id", "parent_id", "depth", "tenant_id") values (1, null, 0, 1)')
            cur.execute('insert into "group_members" ("user_id", "group_id") values (%s, 1)',
                        (h.adapter.harness_user_values(1)[0],))
        n = hammer(pg_dsn, h.dbname, h.adapter, WRITERS, ATTEMPTS, hold=0.05)
    finally:
        h.close()
    if within_bound:
        assert n == MAX, n
    else:
        assert n > MAX, f"the unlocked trigger did not overshoot ({n} rows); contention too low?"
