"""Observe the lock mode a DDL statement takes on a table (pg_locks), so the lock levels the
migration generator documents are tested on every Postgres version rather than assumed.

Statements that can run inside a transaction are executed and the locks held by the backend are
read before commit.  CONCURRENTLY statements cannot run in a transaction: another session holds
a conflicting SHARE lock on the table, the statement blocks, and the waiting request's mode is
read from pg_locks before the blocker rolls back.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import psycopg
from psycopg import sql as psql

from mls.compile.migrate import LOCKS, Migration, MStatement

STRENGTH = ["AccessShareLock", "RowShareLock", "RowExclusiveLock", "ShareUpdateExclusiveLock", "ShareLock",
            "ShareRowExclusiveLock", "ExclusiveLock", "AccessExclusiveLock"]


@dataclass
class Observed:
    statement: MStatement
    mode: str | None
    expected: str | None

    @property
    def ok(self) -> bool:
        return self.mode == self.expected


def _strongest(modes: list[str]) -> str | None:
    return max(modes, key=STRENGTH.index) if modes else None


def _oid(cur, table: str) -> int:
    cur.execute("select %s::regclass::oid", (f'"{table}"',))
    return cur.fetchone()[0]


def _held(cur, oid: int, pid_clause: str) -> list[str]:
    cur.execute(f"""select l.mode from pg_locks l where l.locktype = 'relation' and l.relation = %s
                    and {pid_clause}""", (oid,))
    return [r[0] for r in cur.fetchall()]


def observe(conn: psycopg.Connection, dsn: str, st: MStatement) -> str | None:
    """Run `st` (committing it) and return the strongest lock mode it took on its table.
    `dsn` must reach the same database as `conn` (a second session is needed for CONCURRENTLY)."""
    assert conn.autocommit
    table = st.table
    if table is None:
        with conn.cursor() as cur:
            cur.execute(st.sql)
        return None
    if "concurrently" in st.kind:
        return _observe_concurrent(conn, dsn, st)
    with conn.transaction():
        with conn.cursor() as cur:
            oid = _oid(cur, table) if st.kind != "create table" else None
            cur.execute(st.sql)
            if oid is None:
                oid = _oid(cur, table)
            modes = _held(cur, oid, "l.pid = pg_backend_pid()")
    return _strongest(modes)


def _observe_concurrent(conn: psycopg.Connection, dsn: str, st: MStatement) -> str | None:
    table = st.table
    err: list[Exception] = []

    def run():
        try:
            with psycopg.connect(dsn, autocommit=True) as c2:
                c2.execute(st.sql)
        except Exception as e:  # pragma: no cover
            err.append(e)

    with conn.transaction():
        with conn.cursor() as cur:
            oid = _oid(cur, table)
            cur.execute(psql.SQL("lock table {} in share mode").format(psql.Identifier(table)))
            t = threading.Thread(target=run)
            t.start()
            modes: list[str] = []
            for _ in range(200):  # up to ~10 s for the request to show up as waiting
                modes = _held(cur, oid, "not l.granted")
                if modes:
                    break
                time.sleep(0.05)
    t.join()
    if err:
        raise err[0]
    return _strongest(modes)


def observe_migration(conn: psycopg.Connection, dsn: str, m: Migration) -> list[Observed]:
    """Apply the migration statement by statement, recording the lock mode of each one."""
    out = []
    for ph in m.phases:
        for st in ph.statements:
            mode = observe(conn, dsn, st)
            out.append(Observed(st, mode, LOCKS[st.kind] if st.table else None))
    return out
