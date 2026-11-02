"""Column rules and grants (propositional).

Privileges per (role, table) are derived from the spec:
  * each permissive policy for (role, table, command) grants that command (ALL grants all four);
  * a `read` column rule turns SELECT into a column list; a `write` rule turns INSERT and
    UPDATE into column lists;
  * every relation referenced by a policy of the role grants SELECT (key, value) on its table;
  * DELETE is always table-level; TRUNCATE is never granted (D13).
"""
from __future__ import annotations

from mls.compile.mutations import Mutation
from mls.compile.sql import q
from mls.spec.model import And, Member, Not, Or, Pred, Spec

Priv = dict[str, "str | set[str]"]  # command -> "*" | set of columns
COMMANDS = ("select", "insert", "update", "delete")


def relations_in(p: Pred | None, out: set[str]) -> set[str]:
    if p is None:
        return out
    if isinstance(p, Member):
        out.add(p.member.relation)
    elif isinstance(p, And):
        for a in p.and_:
            relations_in(a, out)
    elif isinstance(p, Or):
        for a in p.or_:
            relations_in(a, out)
    elif isinstance(p, Not):
        relations_in(p.not_, out)
    return out


def privileges(spec: Spec) -> dict[tuple[str, str], Priv]:
    privs: dict[tuple[str, str], Priv] = {}
    for p in spec.policies():
        if p.mode != "permissive":
            continue
        g = privs.setdefault((p.role, p.table), {})
        for cmd in (COMMANDS if p.command == "all" else (p.command,)):
            g[cmd] = "*"
    for cr in spec.column_rules():
        g = privs.setdefault((cr.role, cr.table), {})
        for cmd in (("select",) if cr.access == "read" else ("insert", "update")):
            if cmd in g:
                g[cmd] = set(cr.columns)
    for p in spec.policies():
        rels = relations_in(p.using, set())
        relations_in(p.check, rels)
        for rname in rels:
            rel = spec.relations[rname]
            g = privs.setdefault((p.role, rel.table), {})
            cur = g.get("select")
            if cur == "*":
                continue
            g["select"] = (cur or set()) | {rel.key, rel.value}
    return privs


def grant_ddl(spec: Spec, mutations: frozenset[Mutation] = frozenset()) -> list[str]:
    from mls.compile.internal import INTERNAL_ROLE, internal_grants

    out = []
    for cmd, table in internal_grants(spec):
        out.append(f"grant {cmd} on {q(table)} to {q(INTERNAL_ROLE)}")
    for (role, table), g in sorted(privileges(spec).items()):
        for cmd in COMMANDS:
            cols = g.get(cmd)
            if cols is None:
                continue
            if cols == "*" or Mutation.COLUMN_TABLE_GRANT in mutations:
                out.append(f"grant {cmd} on {q(table)} to {q(role)}")
            else:
                out.append(f"grant {cmd} ({', '.join(q(c) for c in sorted(cols))}) on {q(table)} to {q(role)}")
    return out
