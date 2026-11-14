"""Three-valued encoding of spec predicates (SEMANTICS §1).

Every predicate becomes a pair (T, F) of boolean terms: T = "is TRUE", F = "is FALSE";
UNKNOWN is ¬T ∧ ¬F.  Column values are integers (`row.col`), with a null flag (`row.col.null`)
for nullable columns.  Context values are `ctx.name` and never NULL.  Text/uuid constants are
interned to distinct integers.  Relations are uninterpreted predicates `rel.<name>` (optionally
tagged, e.g. `rel.<name>@before`, to talk about two states of one relation).  A composed relation
(D56) is its base relation read through one lookup per hop: `hop.<table>.<value>` (a row with that
value exists) and `hop.<table>.<value>.<key>` (its key), functions because the hop's value is a key
of its table; `(k, v) ∈ base . T(key, value)` is `hop.T.value(v) ∧ base(k, hop.T.value.key(v))`,
quantifier-free like `in_group`'s unrolled parent.  Opaque blocks are
pairs of uninterpreted predicates over their declared inputs, with ¬(T ∧ F) as a side condition.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from itertools import count
from typing import Iterator

from mls.logic.term import (
    FALSE, TRUE, FunDecl, Sort, Term, Var, and_, bool_var, eq, fun, int_var, le, lt, not_, or_, IntVal,
)
from mls.spec.chain import base_name, levels
from mls.spec.model import (
    CONTEXT_NOW, And, ColRef, Const, CtxRef, Cmp, InGroup, IsNull, Lit, Member, NewRef, Not,
    NowBetween, OldRef, OpaqueRef, Or, Pred, Spec,
)


# The opaque blocks encoded as *free* (#27): each occurrence an independent three-valued value
# instead of an uninterpreted function of the block's declared inputs.  A proof that still holds
# does not depend on the block (not even on its reading only its signature), so it is not
# conditional on it.  Set by the runner's re-check, never for a verdict.
_FREE_OPAQUES: ContextVar[frozenset[str]] = ContextVar("free_opaques", default=frozenset())
_FREE_IDS = count()


@contextmanager
def free_opaques(names: set[str] | frozenset[str]) -> Iterator[None]:
    token = _FREE_OPAQUES.set(frozenset(names))
    try:
        yield
    finally:
        _FREE_OPAQUES.reset(token)


def col_value_var(row: str, col: str) -> Var:
    return int_var(f"{row}.{col}")


def col_null_var(row: str, col: str) -> Var:
    return bool_var(f"{row}.{col}.null")


def ctx_var(name: str) -> Var:
    return int_var(f"ctx.{name}")


def rel_fun(name: str, tag: str | None = None) -> FunDecl:
    return fun(f"rel.{name}" + (f"@{tag}" if tag else ""), Sort.INT, Sort.INT)


def hop_exists_fun(table: str, value: str) -> FunDecl:
    """hop.T.v(x): a row of T has v = x (D56)."""
    return fun(f"hop.{table}.{value}", Sort.INT)


def hop_key_fun(table: str, key: str, value: str) -> FunDecl:
    """hop.T.v.k(x): the k of the row of T whose v = x (meaningful where hop.T.v(x))."""
    return fun(f"hop.{table}.{value}.{key}", Sort.INT, ret=Sort.INT)


def parent_fun(hierarchy: str, tag: str | None = None) -> FunDecl:
    """parent(x): the parent node of x (meaningful where has_parent(x))."""
    return fun(f"hier.{hierarchy}" + (f"@{tag}" if tag else "") + ".parent", Sort.INT, ret=Sort.INT)


def has_parent_fun(hierarchy: str, tag: str | None = None) -> FunDecl:
    return fun(f"hier.{hierarchy}" + (f"@{tag}" if tag else "") + ".has_parent", Sort.INT)


@dataclass
class Encoder:
    spec: Spec
    rel_tags: dict[str, str] = field(default_factory=dict)  # relation -> tag (state copy)
    opaque_tags: dict[str, str] = field(default_factory=dict)  # opaque -> tag (a different definition)
    hier_tags: dict[str, str] = field(default_factory=dict)  # hierarchy -> tag (a different tree)
    hierarchies_used: set[str] = field(default_factory=set)
    consts: dict[str, int] = field(default_factory=dict)    # interned text/uuid constants
    const_types: dict[str, set[str]] = field(default_factory=dict)  # constant -> column types it met
    side_conditions: list[Term] = field(default_factory=list)
    opaque_deps: set[str] = field(default_factory=set)
    opaque_axioms: set[Term] = field(default_factory=set)  # `not (T and F)` per block use: no dependency
    hops_used: set[tuple[str, str, str]] = field(default_factory=set)  # (table, key, value) read by a chain
    rels_used: set[str] = field(default_factory=set)  # base relations whose symbol was emitted

    # ------------------------------------------------------------------ symbols
    def rel(self, name: str) -> FunDecl:
        self.rels_used.add(name)
        return rel_fun(name, self.rel_tags.get(name))

    def const_id(self, value: str) -> int:
        if value not in self.consts:
            self.consts[value] = 1000 + len(self.consts)
        return self.consts[value]

    def term(self, t, row: str, table: str) -> tuple[Term, Term]:
        """(value, is_null)"""
        if isinstance(t, ColRef):
            return self._col(row, table, t.col)
        if isinstance(t, OldRef):
            return self._col("old", table, t.old)
        if isinstance(t, NewRef):
            return self._col("new", table, t.new)
        if isinstance(t, CtxRef):
            return ctx_var(t.ctx), FALSE
        if isinstance(t, Const):
            v = t.const
            if isinstance(v, bool):
                return IntVal(1 if v else 0), FALSE
            if isinstance(v, int):
                return IntVal(v), FALSE
            return IntVal(self.const_id(v)), FALSE
        raise TypeError(t)

    def _type_of(self, t, table: str) -> str | None:
        cols = self.spec.tables[table].columns
        if isinstance(t, ColRef):
            return cols[t.col].type
        if isinstance(t, OldRef):
            return cols[t.old].type
        if isinstance(t, NewRef):
            return cols[t.new].type
        if isinstance(t, CtxRef):
            return self.spec.context_type(t.ctx)
        return None

    def _note_const_types(self, l, r, table: str) -> None:
        for a, b in ((l, r), (r, l)):
            if isinstance(a, Const) and isinstance(a.const, str):
                ty = self._type_of(b, table)
                if ty:
                    self.const_types.setdefault(a.const, set()).add(ty)

    def _col(self, row: str, table: str, col: str) -> tuple[Term, Term]:
        nullable = self.spec.tables[table].columns[col].nullable
        return col_value_var(row, col), (col_null_var(row, col) if nullable else FALSE)

    # ------------------------------------------------------------------ predicates
    def tf(self, p: Pred, row: str, table: str) -> tuple[Term, Term]:
        if isinstance(p, Lit):
            return (TRUE, FALSE) if p.lit else (FALSE, TRUE)
        if isinstance(p, And):
            pairs = [self.tf(a, row, table) for a in p.and_]
            return and_(*[t for t, _ in pairs]), or_(*[f for _, f in pairs])
        if isinstance(p, Or):
            pairs = [self.tf(a, row, table) for a in p.or_]
            return or_(*[t for t, _ in pairs]), and_(*[f for _, f in pairs])
        if isinstance(p, Not):
            t, f = self.tf(p.not_, row, table)
            return f, t
        if isinstance(p, IsNull):
            _, n = self.term(p.is_null, row, table)
            return n, not_(n)
        if isinstance(p, Cmp):
            l, op, r = p.cmp
            self._note_const_types(l, r, table)
            (lv, ln), (rv, rn) = self.term(l, row, table), self.term(r, row, table)
            core = {"=": eq(lv, rv), "<>": not_(eq(lv, rv)), "<": lt(lv, rv), "<=": le(lv, rv),
                    ">": lt(rv, lv), ">=": le(rv, lv)}[op]
            ok = not_(or_(ln, rn))
            return and_(ok, core), and_(ok, not_(core))
        if isinstance(p, Member):
            m = p.member
            lv = levels(self.spec, m.relation)
            for t_, (tb, col) in ((m.key, (lv[0].table, lv[0].key)), (m.value, (lv[-1].table, lv[-1].value))):
                if isinstance(t_, Const) and isinstance(t_.const, str):
                    self.const_types.setdefault(t_.const, set()).add(self.spec.tables[tb].columns[col].type)
            kv, kn = self.term(m.key, row, table)
            vv, vn = self.term(m.value, row, table)
            ok = not_(or_(kn, vn))
            node: Term = vv
            guards: list[Term] = []
            for h in reversed(lv[1:]):  # D56: from the value back to the base relation's value
                self.hops_used.add((h.table, h.key, h.value))
                guards.append(hop_exists_fun(h.table, h.value)(node))
                node = hop_key_fun(h.table, h.key, h.value)(node)
            fact = and_(*guards, self.rel(base_name(self.spec, m.relation))(kv, node))
            return and_(ok, fact), and_(ok, not_(fact))
        if isinstance(p, InGroup):
            return self._in_group(p, row, table)
        if isinstance(p, NowBetween):
            now = ctx_var(CONTEXT_NOW)
            parts = []
            if p.now_between.from_ is not None:
                fv, fn = self.term(p.now_between.from_, row, table)
                parts.append(or_(fn, le(fv, now)))
            if p.now_between.until is not None:
                uv, un = self.term(p.now_between.until, row, table)
                parts.append(or_(un, lt(now, uv)))
            t = and_(*parts)
            return t, not_(t)
        if isinstance(p, OpaqueRef):
            return self._opaque(p.opaque, row, table)
        raise TypeError(p)

    def _in_group(self, p: InGroup, row: str, table: str) -> tuple[Term, Term]:
        """key ∈ members(ancestor_i(value)) for some i ≤ max_depth, where ancestor_0 = value and
        ancestor_{i+1} = parent(ancestor_i) exists.  Unrolled to the declared depth (D5): with the
        depth CHECK no chain is longer, so the unrolling is exact (SEMANTICS §7)."""
        g = p.in_group
        h = self.spec.hierarchies[g.hierarchy]
        self.hierarchies_used.add(g.hierarchy)
        kv, kn = self.term(g.key, row, table)
        vv, vn = self.term(g.value, row, table)
        ok = not_(or_(kn, vn))
        rel = self.rel(h.members)
        tag = self.hier_tags.get(g.hierarchy)
        par, has = parent_fun(g.hierarchy, tag), has_parent_fun(g.hierarchy, tag)
        node: Term = vv
        chain: Term = TRUE
        alts = [and_(chain, rel(kv, node))]
        for _ in range(h.max_depth):
            chain = and_(chain, has(node))
            node = par(node)
            alts.append(and_(chain, rel(kv, node)))
        hit = or_(*alts)
        return and_(ok, hit), and_(ok, not_(hit))

    def _opaque(self, name: str, row: str, table: str) -> tuple[Term, Term]:
        o = self.spec.opaques[name]
        self.opaque_deps.add(name)
        if name in _FREE_OPAQUES.get():
            k = next(_FREE_IDS)
            t, f = bool_var(f"opaque.{name}.T#{k}"), bool_var(f"opaque.{name}.F#{k}")
            self.side_conditions.append(not_(and_(t, f)))
            self.opaque_axioms.add(self.side_conditions[-1])
            return t, f
        args: list[Term] = []
        sorts: list[Sort] = []
        for c in o.columns:
            v, n = self._col(row, table, c)
            args.append(v)
            sorts.append(Sort.INT)
            if self.spec.tables[table].columns[c].nullable:
                args.append(n)
                sorts.append(Sort.BOOL)
        for c in o.context:
            args.append(ctx_var(c))
            sorts.append(Sort.INT)
        for r in o.relations:  # the block reads this relation: its value depends on the state copy
            args.append(int_var(f"state.{r}" + (f"@{self.rel_tags[r]}" if r in self.rel_tags else "")))
            sorts.append(Sort.INT)
        sym = name + (f"@{self.opaque_tags[name]}" if name in self.opaque_tags else "")
        tf = fun(f"opaque.{sym}.T", *sorts)
        ff = fun(f"opaque.{sym}.F", *sorts)
        t, f = tf(*args), ff(*args)
        self.side_conditions.append(not_(and_(t, f)))
        self.opaque_axioms.add(self.side_conditions[-1])
        return t, f

    def T(self, p: Pred, row: str, table: str) -> Term:
        return self.tf(p, row, table)[0]

    def F(self, p: Pred, row: str, table: str) -> Term:
        return self.tf(p, row, table)[1]
