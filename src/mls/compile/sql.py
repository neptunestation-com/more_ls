"""Predicate -> SQL.  One fixed rendering per atom (SEMANTICS §2)."""
from __future__ import annotations

from mls.compile.mutations import Mutation
from mls.compile.opaque import opaque_sql
from mls.compile.time import now_between_sql
from mls.spec.expand import as_in_list
from mls.identity.base import SQL_TYPE, Adapter
from mls.spec.model import (
    And, ColRef, ColType, Const, CtxRef, Cmp, InGroup, IsNull, Lit, Member, NewRef, Not, NowBetween,
    OldRef, OpaqueRef, Or, Pred, Spec,
)


def q(ident: str) -> str:
    """Quote an identifier (the checker already restricts names to [a-z0-9_])."""
    return '"' + ident.replace('"', '""') + '"'


def const_sql(value, type_: ColType | None) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    lit = "'" + value.replace("'", "''") + "'"
    if type_ == "uuid":
        return lit + "::uuid"
    return lit


class SqlCompiler:
    def __init__(self, spec: Spec, adapter: Adapter, mutations: frozenset[Mutation] = frozenset()):
        self.spec = spec
        self.adapter = adapter
        self.mutations = mutations

    def term_type(self, t, table: str) -> ColType | None:
        if isinstance(t, ColRef):
            return self.spec.tables[table].columns[t.col].type
        if isinstance(t, OldRef):
            return self.spec.tables[table].columns[t.old].type
        if isinstance(t, NewRef):
            return self.spec.tables[table].columns[t.new].type
        if isinstance(t, CtxRef):
            return self.spec.context_type(t.ctx)
        return None

    def term(self, t, table: str, type_hint: ColType | None = None) -> str:
        if isinstance(t, ColRef):
            return q(t.col)
        if isinstance(t, OldRef):
            return "old." + q(t.old)
        if isinstance(t, NewRef):
            return "new." + q(t.new)
        if isinstance(t, CtxRef):
            return self.adapter.ctx_sql(t.ctx)
        if isinstance(t, Const):
            return const_sql(t.const, type_hint)
        raise TypeError(t)

    def pred(self, p: Pred, table: str) -> str:
        if isinstance(p, Lit):
            return "true" if p.lit else "false"
        if isinstance(p, And):
            return "(" + " and ".join(self.pred(a, table) for a in p.and_) + ")"
        if isinstance(p, Or):
            lst = as_in_list(p)
            if lst is not None:
                # an expanded `in` (or a hand-written disjunction of the same shape): one IN list,
                # which the planner turns into a single `= ANY` index condition on every version
                t, consts = lst
                ty = self.term_type(t, table)
                if Mutation.IN_DROP_LAST in self.mutations:
                    consts = consts[:-1]
                return f"({self.term(t, table)} in ({', '.join(self.term(c, table, ty) for c in consts)}))"
            return "(" + " or ".join(self.pred(a, table) for a in p.or_) + ")"
        if isinstance(p, Not):
            return f"(not {self.pred(p.not_, table)})"
        if isinstance(p, IsNull):
            return f"({self.term(p.is_null, table)} is null)"
        if isinstance(p, Cmp):
            l, op, r = p.cmp
            ty = self.term_type(l, table) or self.term_type(r, table)
            return f"({self.term(l, table, ty)} {op} {self.term(r, table, ty)})"
        if isinstance(p, Member):
            m = p.member
            rel = self.spec.relations[m.relation]
            kt = self.spec.tables[rel.table].columns[rel.key].type
            return (f"({self.term(m.value, table)} = any (array(select {q(rel.value)} from {q(rel.table)} "
                    f"where {q(rel.key)} = {self.term(m.key, table, kt)})))")
        if isinstance(p, InGroup):
            from mls.compile.hierarchy import reach_fn_name

            g = p.in_group
            h = self.spec.hierarchies[g.hierarchy]
            rel = self.spec.relations[h.members]
            kt = self.spec.tables[rel.table].columns[rel.key].type
            it = SQL_TYPE[self.spec.tables[h.table].columns[h.id].type]
            # the cast makes the scalar subquery an array expression (ANY (subquery) would compare
            # the value with the whole array row); it stays an uncorrelated InitPlan
            return (f"({self.term(g.value, table)} = any ((select {q(reach_fn_name(g.hierarchy))}"
                    f"({self.term(g.key, table, kt)}))::{it}[]))")
        if isinstance(p, NowBetween):
            nb = p.now_between
            return now_between_sql(
                self.term(nb.from_, table) if nb.from_ is not None else None,
                self.term(nb.until, table) if nb.until is not None else None,
                self.adapter, closed_null=Mutation.TIME_NULL_CLOSED in self.mutations)
        if isinstance(p, OpaqueRef):
            return opaque_sql(self.spec.opaques[p.opaque], self.adapter,
                              invert=Mutation.OPAQUE_INVERT in self.mutations)
        raise TypeError(p)


def column_ddl(name: str, col) -> str:
    return f"{q(name)} {SQL_TYPE[col.type]}" + ("" if col.nullable else " not null")
