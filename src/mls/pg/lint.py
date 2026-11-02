"""Leakproof lint (D14, FINDINGS): operators that are not leakproof cannot be pushed below a
policy, so a query filtering a protected table on such an operator fetches every visible row
first.  The static lint warns about the usual suspects per column type; with a connection it
reads pg_proc.proleakproof for the real list."""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from mls.spec.model import Spec

STATIC_NON_LEAKPROOF = {
    "text": ["~~ (LIKE)", "~~* (ILIKE)", "~ (regex)", "~* (regex, case-insensitive)", "<", "<=", ">", ">="],
    "timestamp": [],
    "int": [],
    "bool": [],
    "uuid": [],
}
PG_TYPE = {"int": "integer", "text": "text", "bool": "boolean", "timestamp": "timestamptz", "uuid": "uuid"}


@dataclass
class LintWarning:
    table: str
    column: str
    type: str
    operators: list[str]

    def __str__(self) -> str:
        return (f"{self.table}.{self.column} ({self.type}): queries filtering on "
                f"{', '.join(self.operators)} cannot use an index before the row policy "
                f"(operator is not leakproof); equality is leakproof")


def protected_tables(spec: Spec) -> set[str]:
    return {p.table for p in spec.policies()}


def lint_static(spec: Spec) -> list[LintWarning]:
    out = []
    for table in sorted(protected_tables(spec)):
        for c, col in spec.tables[table].columns.items():
            ops = STATIC_NON_LEAKPROOF.get(col.type, [])
            if ops:
                out.append(LintWarning(table, c, col.type, ops))
    return out


def lint_catalog(spec: Spec, conn: psycopg.Connection) -> list[LintWarning]:
    out = []
    with conn.cursor() as cur:
        for table in sorted(protected_tables(spec)):
            for c, col in spec.tables[table].columns.items():
                cur.execute(
                    """select o.oprname from pg_operator o join pg_proc p on p.oid = o.oprcode
                       where o.oprleft = %s::regtype and o.oprright = o.oprleft and not p.proleakproof
                         and o.oprname in ('=', '<>', '<', '<=', '>', '>=', '~~', '~~*', '~', '~*')
                       order by 1""", (PG_TYPE[col.type],))
                ops = [r[0] for r in cur.fetchall()]
                if ops:
                    out.append(LintWarning(table, c, col.type, ops))
    return out
