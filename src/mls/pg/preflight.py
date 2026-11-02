"""Pre-flight (D7): before a migration, count and sample the existing rows that the new spec's
constraints would reject.  The queries are the compiled `IS TRUE` predicates, negated, so the
report means exactly what VALIDATE CONSTRAINT will enforce (a NULL in a nullable column fails a
CHECK the same way it does in the compiled constraint).

Columns the new spec adds do not exist yet; they are substituted by NULL, which is what every
existing row will hold after ADD COLUMN.  Relation tables that do not exist yet are empty.
Runs in a READ ONLY transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql as psql

from mls.compile.check import check_def
from mls.compile.fk import fk_def
from mls.compile.objects import objects
from mls.compile.sql import SqlCompiler, q
from mls.identity import get_adapter
from mls.identity.base import SQL_TYPE
from mls.spec.model import Spec


@dataclass
class Violation:
    rule_id: str
    table: str
    what: str
    count: int
    samples: list[dict[str, Any]] = field(default_factory=list)
    sql: str = ""
    cond: str = ""   # the WHERE condition over the table aliased `t` (empty for schema violations)

    def __str__(self) -> str:
        s = f"{self.rule_id} on {self.table}: {self.count} row(s) {self.what}"
        if self.samples:
            s += "\n  e.g. " + "; ".join(str(r) for r in self.samples)
        return s


@dataclass
class PreflightResult:
    violations: list[Violation]
    notes: list[str]

    @property
    def ok(self) -> bool:
        return not self.violations


def _live_columns(cur, schema: str, table: str) -> dict[str, str] | None:
    cur.execute("""select column_name, data_type from information_schema.columns
                   where table_schema = %s and table_name = %s order by ordinal_position""", (schema, table))
    rows = cur.fetchall()
    return {r[0]: r[1] for r in rows} if rows else None


def _source(spec: Spec, schema: str, table: str, live: dict[str, str]) -> str:
    """`table` as it will look after ADD COLUMN: live columns plus NULL for the new ones."""
    extra = [f"null::{SQL_TYPE[col.type]} as {q(c)}" for c, col in spec.tables[table].columns.items()
             if c not in live]
    sel = "t.*" + ("".join(", " + e for e in extra))
    return f"(select {sel} from {q(schema)}.{q(table)} t)"


def _count_and_sample(cur, src: str, cond: str, limit: int) -> tuple[int, list[dict], str]:
    count_sql = f"select count(*) from {src} t where {cond}"
    cur.execute(count_sql)
    n = cur.fetchone()[0]
    samples: list[dict] = []
    if n and limit:
        cur.execute(f"select t.* from {src} t where {cond} limit {int(limit)}")
        cols = [d.name for d in cur.description]
        samples = [dict(zip(cols, r)) for r in cur.fetchall()]
    return n, samples, count_sql


def preflight(conn: psycopg.Connection, new: Spec, old: Spec | None = None, schema: str = "public",
              samples: int = 5) -> PreflightResult:
    sc = SqlCompiler(new, get_adapter(new))
    n_obj = objects(new)
    o_obj = objects(old) if old is not None else None
    out: list[Violation] = []
    notes: list[str] = []
    with conn.transaction(force_rollback=True):  # nests as a savepoint inside a caller's transaction
        with conn.cursor() as cur:
            cur.execute("set transaction read only")
            cur.execute(psql.SQL("set local search_path to {}, pg_catalog").format(psql.Identifier(schema)))
            live_cols: dict[str, dict[str, str] | None] = {t: _live_columns(cur, schema, t) for t in new.tables}
            for t, cols in live_cols.items():
                if cols is None:
                    notes.append(f"table '{t}' does not exist yet: nothing to check")
            # NOT NULL and UNIQUE changes
            for t, tobj in n_obj.tables.items():
                live = live_cols[t]
                if live is None:
                    continue
                prev = o_obj.tables.get(t) if o_obj else None
                for c, col in tobj.columns.items():
                    was_notnull = prev.columns[c].notnull if prev and c in prev.columns else None
                    if not col.notnull or was_notnull:
                        continue
                    if c not in live:
                        n, s, sql = _count_and_sample(cur, f"{q(schema)}.{q(t)}", "true", samples)
                        if n:
                            out.append(Violation("schema", t, f"exist, but new column '{c}' is NOT NULL and "
                                                 f"has no default (ADD COLUMN fails)", n, s, sql))
                    else:
                        n, s, sql = _count_and_sample(cur, f"{q(schema)}.{q(t)}", f"{q(c)} is null", samples)
                        if n:
                            out.append(Violation("schema", t, f"have NULL in '{c}', which becomes NOT NULL", n, s, sql))
                for uname, ucols in tobj.uniques.items():
                    if prev is not None and uname in prev.uniques:
                        continue
                    if any(c not in live for c in ucols):
                        continue
                    cols_sql = ", ".join(q(c) for c in ucols)
                    cur.execute(f"select count(*) from (select 1 from {q(schema)}.{q(t)} group by {cols_sql} "
                                f"having count(*) > 1) d")
                    n = cur.fetchone()[0]
                    if n:
                        cur.execute(f"select {cols_sql}, count(*) as n from {q(schema)}.{q(t)} group by {cols_sql} "
                                    f"having count(*) > 1 limit {int(samples)}")
                        names = [d.name for d in cur.description]
                        out.append(Violation("schema", t, f"groups of duplicates for new unique ({', '.join(ucols)})",
                                             n, [dict(zip(names, r)) for r in cur.fetchall()]))
            # CHECKs and FKs that are new or changed
            for key, c in n_obj.constraints.items():
                prev = o_obj.constraints.get(key) if o_obj else None
                if prev is not None and prev.definition == c.definition:
                    continue
                live = live_cols[c.table]
                if live is None:
                    continue
                src = _source(new, schema, c.table, live)
                rule = new.rule(c.rule_id)
                if c.kind == "check":
                    cond = f"not ({sc.pred(rule.predicate, c.table)} is true)"
                    what = f"violate {c.name}"
                else:
                    rel = new.relations[rule.relation]
                    c1, c2 = rule.columns
                    if live_cols.get(rel.table) is None and _live_columns(cur, schema, rel.table) is None:
                        exists = "false"
                    else:
                        exists = (f"exists (select 1 from {q(schema)}.{q(rel.table)} x where x.{q(rel.key)} = t.{q(c1)} "
                                  f"and x.{q(rel.value)} = t.{q(c2)})")
                    cond = f"t.{q(c1)} is not null and t.{q(c2)} is not null and not {exists}"
                    what = f"have ({c1}, {c2}) not in {rule.relation}"
                n, s, sql = _count_and_sample(cur, src, cond, samples)
                if n:
                    out.append(Violation(c.rule_id, c.table, what, n, s, sql, cond))
    return PreflightResult(out, notes)
