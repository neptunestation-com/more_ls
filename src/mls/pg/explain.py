"""EXPLAIN (FORMAT JSON) checks (D14): every membership / comparison atom of a SELECT policy
appears as an index condition and the protected table is not sequentially scanned."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql as psql

from mls.compile.column import privileges
from mls.identity import Adapter
from mls.props.effective import effective
from mls.spec.expand import as_in_list
from mls.spec.model import And, ColRef, Cmp, CtxRef, InGroup, Member, Not, Or, Pred, Spec


@dataclass
class ExplainResult:
    table: str
    role: str
    ok: bool
    problems: list[str] = field(default_factory=list)
    plan: dict | None = None


def index_atoms(p: Pred, out: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(kind, column) for atoms that must be index-driven: member values, ctx comparisons, IN lists."""
    if isinstance(p, Member) and isinstance(p.member.value, ColRef):
        out.append(("member", p.member.value.col))
    elif isinstance(p, InGroup) and isinstance(p.in_group.value, ColRef):
        out.append(("in_group", p.in_group.value.col))
    elif isinstance(p, Cmp) and isinstance(p.cmp[0], ColRef) and isinstance(p.cmp[2], CtxRef):
        out.append(("cmp", p.cmp[0].col))
    elif isinstance(p, And):
        for a in p.and_:
            index_atoms(a, out)
    elif isinstance(p, Or):
        lst = as_in_list(p)
        if lst is not None and isinstance(lst[0], ColRef):
            out.append(("in", lst[0].col))  # compiled as one IN list (compile/sql.py)
        else:
            for a in p.or_:
                index_atoms(a, out)
    return out  # Not / IsNull / now_between / opaque: not required to be index conditions


def walk(node: dict, out: list[dict]) -> list[dict]:
    out.append(node)
    for k in ("Plans",):
        for child in node.get(k, []):
            walk(child, out)
    return out


def check_plan(spec: Spec, table: str, role: str, plan: dict) -> ExplainResult:
    nodes = walk(plan["Plan"], [])
    problems = []
    for n in nodes:
        if n.get("Node Type") == "Seq Scan" and n.get("Relation Name") == table:
            problems.append(f"sequential scan of {table}")
    conds = " ".join(str(n.get(k, "")) for n in nodes
                     for k in ("Index Cond", "Recheck Cond"))
    for kind, col in index_atoms(effective(spec, table, role, "select", "using"), []):
        if f'"{col}"' not in conds and f"{col} " not in conds and f"({col}" not in conds:
            problems.append(f"{kind} atom on {col} is not an index condition")
    return ExplainResult(table, role, not problems, problems, plan)


def explain_select(conn: psycopg.Connection, spec: Spec, adapter: Adapter, table: str, role: str,
                   ctx: dict[str, Any]) -> dict | None:
    cols = privileges(spec).get((role, table), {}).get("select")
    if cols is None:
        return None
    collist = list(spec.tables[table].columns) if cols == "*" else sorted(cols)
    with conn.cursor() as cur:
        cur.execute("begin")
        try:
            for s in adapter.harness_context_sql(role, ctx):
                cur.execute(s)
            cur.execute(psql.SQL("explain (format json) select {} from {}").format(
                psql.SQL(", ").join(map(psql.Identifier, collist)), psql.Identifier(table)))
            raw = cur.fetchone()[0]
        finally:
            cur.execute("rollback")
    plan = raw if isinstance(raw, list) else json.loads(raw)
    return plan[0]


def explain_checks(conn: psycopg.Connection, spec: Spec, adapter: Adapter, ctx_for_role) -> list[ExplainResult]:
    out = []
    for role in spec.roles:
        for table in spec.governed():
            if not any(p.table == table and p.role == role and p.command in ("select", "all")
                       for p in spec.policies()):
                continue
            plan = explain_select(conn, spec, adapter, table, role, ctx_for_role(role))
            if plan is None:
                continue
            out.append(check_plan(spec, table, role, plan))
    return out


def scale_load_sql(spec: Spec, adapter: Adapter, n_rows: int, n_users: int = 100,
                   n_groups: int = 1000) -> list[str]:
    """Synthetic data at scale for plan checks: relation tables get a sparse (user, group) grid,
    other tables n_rows rows with values spread over the same pools.  Constraints are not needed
    for plan shape, so callers deploy the script without checks/fks/transitions."""
    out = []
    relation_tables = {rel.table for rel in spec.relations.values()}
    hier_tables = {h.table: h for h in spec.hierarchies.values()}
    for h in hier_tables.values():
        # node i has parent i // 10 (none for 1..9) and depth floor(log10 i): the pool of
        # group ids must stay within the declared depth bound
        n_groups = min(n_groups, 10 ** (h.max_depth + 1) - 1)

    def expr(col, i: str) -> str:
        t = spec.tables  # noqa
        if col.type == "int":
            return f"(({i} % {n_groups}) + 1)"
        if col.type == "uuid":
            return f"(('00000000-0000-0000-0000-' || lpad((({i} % {n_users}) + 1)::text, 12, '0'))::uuid)"
        if col.type == "text":
            return f"('t' || ({i} % 50))"
        if col.type == "bool":
            return f"(({i} % 2) = 0)"
        return f"(now() + (({i} % 100) - 50) * interval '1 day')"

    for table, t in spec.governed().items():
        cols = list(t.columns)
        if table in hier_tables:
            h = hier_tables[table]
            n = min(n_groups, 10 ** (h.max_depth + 1) - 1)
            # level by level: the BEFORE trigger computing the depth and the parent FK need the
            # parent row to be already stored
            for level in range(h.max_depth + 1):
                lo, hi = 10 ** level, min(10 ** (level + 1) - 1, n)
                if lo > hi:
                    break
                parts = []
                for c in cols:
                    if c == h.id:
                        parts.append("i")
                    elif c == h.parent:
                        parts.append("nullif(i / 10, 0)")
                    elif c == h.depth:
                        parts.append(str(level))
                    else:
                        parts.append(expr(t.columns[c], "i"))
                out.append(f'insert into "{table}" ({", ".join(f"\"{c}\"" for c in cols)}) '
                           f'select {", ".join(parts)} from generate_series({lo}, {hi}) i on conflict do nothing')
        elif table in relation_tables:
            rel = next(r for r in spec.relations.values() if r.table == table)
            parts = []
            for c in cols:
                if c == rel.key:
                    parts.append(expr(t.columns[c], "k"))
                elif c == rel.value:
                    parts.append(expr(t.columns[c], "g"))
                else:
                    parts.append(expr(t.columns[c], "(k * 1000 + g)"))
            out.append(f'insert into "{table}" ({", ".join(f"\"{c}\"" for c in cols)}) '
                       f'select {", ".join(parts)} from generate_series(0, {n_users - 1}) k, '
                       f'generate_series(0, {n_groups - 1}) g where ((k * 7 + g) % 20) = 0 '
                       f'on conflict do nothing')
        else:
            parts = []
            pk = t.primary_key or []
            for c in cols:
                col = t.columns[c]
                if c in pk and col.type == "int" and len(pk) == 1:
                    parts.append("i")
                else:
                    parts.append(expr(col, "i"))
            out.append(f'insert into "{table}" ({", ".join(f"\"{c}\"" for c in cols)}) '
                       f'select {", ".join(parts)} from generate_series(1, {n_rows}) i on conflict do nothing')
    out.append("analyze")
    return out
