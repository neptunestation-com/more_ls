"""Terse rendering of predicates, rules, properties and scenarios (D44).

A compact, fixed, one-line-per-item rendering of the JSON syntax for reading transcripts and
diffs quickly: `member_of(@uid, tenant_id) and now_between(-, expires_at)` instead of the nested
single-key objects.  Rendering only — the JSON stays the only input (D15) and the English of
`mls/english.py` stays the approval artifact (D10, D33).  The grammar:

    term   := column | old.column | new.column | @context | 'text' | 123 | true | false
    atom   := term op term | term is null | relation(key, value) | in_group(h, key, value)
            | now_between(from | -, until | -) | opaque(name) | true | false
    pred   := atom | not pred | pred and pred | pred or pred     (not > and > or; minimal parens)
    rule   := ID kind table: ...        property := ID template [table role command which]: ...
"""
from __future__ import annotations

from typing import Any

from mls.spec.model import (
    And, CardinalityRule, CheckRule, ColRef, ColumnImmutable, ColumnRule, Cmp, Const, CtxRef, Custom,
    InclusionRule, InGroup, InvariantPreserved, IsNull, LeastPrivilege, Lit, Member, MonotoneIn, MustAllow, NewRef,
    Not, NowBetween, OldRef, OpaqueRef, Or, PolicyRule, Pred, Property, Rule, Spec, TenantIsolation,
    TransitionRule, WriteSubsetRead,
)

OR, AND, NOT, ATOM = 1, 2, 3, 4


def value(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return str(v)


def term(t) -> str:
    if isinstance(t, ColRef):
        return t.col
    if isinstance(t, OldRef):
        return f"old.{t.old}"
    if isinstance(t, NewRef):
        return f"new.{t.new}"
    if isinstance(t, CtxRef):
        return f"@{t.ctx}"
    if isinstance(t, Const):
        return value(t.const)
    raise TypeError(t)


def _pred(p: Pred) -> tuple[str, int]:
    """The rendering and its precedence level."""
    if isinstance(p, Lit):
        return ("true" if p.lit else "false"), ATOM
    if isinstance(p, And):
        return " and ".join(_sub(a, AND) for a in p.and_), (AND if len(p.and_) > 1 else _pred(p.and_[0])[1])
    if isinstance(p, Or):
        return " or ".join(_sub(a, OR) for a in p.or_), (OR if len(p.or_) > 1 else _pred(p.or_[0])[1])
    if isinstance(p, Not):
        return f"not {_sub(p.not_, NOT)}", NOT
    if isinstance(p, IsNull):
        return f"{term(p.is_null)} is null", ATOM
    if isinstance(p, Cmp):
        l, op, r = p.cmp
        return f"{term(l)} {op} {term(r)}", ATOM
    if isinstance(p, Member):
        m = p.member
        return f"{m.relation}({term(m.key)}, {term(m.value)})", ATOM
    if isinstance(p, InGroup):
        g = p.in_group
        return f"in_group({g.hierarchy}, {term(g.key)}, {term(g.value)})", ATOM
    if isinstance(p, NowBetween):
        nb = p.now_between
        lo = "-" if nb.from_ is None else term(nb.from_)
        hi = "-" if nb.until is None else term(nb.until)
        return f"now_between({lo}, {hi})", ATOM
    if isinstance(p, OpaqueRef):
        return f"opaque({p.opaque})", ATOM
    raise TypeError(p)


def _sub(p: Pred, context: int) -> str:
    s, prec = _pred(p)
    return f"({s})" if prec < context else s


def pred(p: Pred) -> str:
    return _pred(p)[0]


def rule(r: Rule) -> str:
    if isinstance(r, PolicyRule):
        s = f"{r.id} policy {r.table}: {r.role} {r.command} {r.mode}"
        if r.using is not None:
            s += f" using {pred(r.using)}"
        if r.check is not None:
            s += f" check {pred(r.check)}"
        return s
    if isinstance(r, CheckRule):
        return f"{r.id} check {r.table}: {pred(r.predicate)}"
    if isinstance(r, InclusionRule):
        c1, c2 = r.columns
        return f"{r.id} inclusion {r.table}: ({c1}, {c2}) in {r.relation} on_delete {r.on_delete}"
    if isinstance(r, TransitionRule):
        return f"{r.id} transition {r.table}: {pred(r.predicate)}"
    if isinstance(r, CardinalityRule):
        return f"{r.id} cardinality {r.table}: {r.group} -> {r.parent} max {r.max}"
    if isinstance(r, ColumnRule):
        return f"{r.id} column_rule {r.table}: {r.role} {r.access} {', '.join(r.columns)}"
    raise TypeError(r)


def prop(p: Property) -> str:
    if isinstance(p, TenantIsolation):
        return (f"{p.id} tenant_isolation {p.table}: {p.column} in {p.relation}({term(p.key)}) "
                f"for {', '.join(p.commands)}")
    if isinstance(p, ColumnImmutable):
        return f"{p.id} column_immutable {p.table}: {p.column}"
    if isinstance(p, MonotoneIn):
        return f"{p.id} monotone_in: {p.relation}"
    if isinstance(p, WriteSubsetRead):
        return f"{p.id} write_subset_read {p.table}"
    if isinstance(p, LeastPrivilege):
        parts = [f"{role} {table}[{', '.join(cmds)}]"
                 for role, tables in p.allowed.items() for table, cmds in tables.items()]
        return f"{p.id} least_privilege: " + "; ".join(parts)
    if isinstance(p, InvariantPreserved):
        parts = [inv.rule if inv.rule is not None else f"{inv.table}: {pred(inv.predicate)}"
                 for inv in p.invariants]
        return f"{p.id} invariant_preserved: " + "; ".join(parts)
    if isinstance(p, Custom):
        return f"{p.id} custom {p.table} {p.role} {p.command} {p.which}: {pred(p.goal)}"
    if isinstance(p, MustAllow):
        return f"{p.id} must_allow {p.table} {p.role} {p.command}: {pred(p.when)}"
    raise TypeError(p)


def scenario(sc) -> str:
    """`@uid = uuid#1; old: docs {tenant_id = 1}; member_of = {(uuid#1, 1)}`"""
    parts = [f"@{k} = {value(v)}" for k, v in sc.context.items()]
    for name, r in sc.rows.items():
        vals = ", ".join(f"{k} = {value(v)}" for k, v in r["values"].items())
        parts.append(f"{name}: {r['table']} {{{vals}}}")
    for name, pairs in sc.relations.items():
        shown = ", ".join(f"({value(a)}, {value(b)})" for a, b in pairs)
        parts.append(f"{name} = {{{shown}}}")
    return "; ".join(parts) if parts else "(no particular data)"


def spec_listing(spec: Spec) -> str:
    lines = ["# rules"] + [rule(r) for r in spec.rules]
    lines += ["", "# properties"] + [prop(p) for p in spec.properties]
    if spec.hierarchies:
        lines += ["", "# hierarchies"]
        for name, h in spec.hierarchies.items():
            lines.append(f"{name}: {h.table} id {h.id} parent {h.parent} depth {h.depth} max_depth {h.max_depth} "
                         f"members {h.members}")
    if spec.opaques:
        lines += ["", "# opaque blocks"]
        for name, o in spec.opaques.items():
            lines.append(f"{name}: {o.table} sql {value(o.sql)}")
    return "\n".join(lines) + "\n"
