"""Apply a generated migration over a connection, phase by phase, exactly as the SQL file says:
transactional phases in one transaction, the others statement by statement in autocommit."""
from __future__ import annotations

import psycopg

from mls.compile.migrate import Migration, Phase


def apply_phase(conn: psycopg.Connection, phase: Phase) -> None:
    if not phase.statements:
        return
    if phase.transactional:
        with conn.transaction():
            with conn.cursor() as cur:
                for st in phase.statements:
                    cur.execute(st.sql)
    else:
        assert conn.autocommit, "non-transactional phases need an autocommit connection"
        with conn.cursor() as cur:
            for st in phase.statements:
                cur.execute(st.sql)


def apply_migration(conn: psycopg.Connection, m: Migration, until: str | None = None) -> None:
    """Run every phase (or up to and including phase `until`)."""
    for ph in m.phases:
        apply_phase(conn, ph)
        if until is not None and ph.name == until:
            return
