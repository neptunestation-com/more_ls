"""Plain-English rendering of rules, properties and scenarios (D10, D15).

Humans approve properties and scenarios; they never read MLS or DDL.  The renderer is a fixed
function of the spec so the same construct always reads the same way (and an LLM-generated spec
can be checked against the requirement it came from).
"""
from __future__ import annotations

from typing import Any

from mls.spec.model import (
    CONTEXT_NOW, And, CardinalityRule, CheckRule, ColRef, ColumnImmutable, ColumnRule, Cmp, Const, CtxRef, Custom,
    InclusionRule, InGroup, InvariantPreserved, IsNull, LeastPrivilege, Lit, Member, MonotoneIn, MustAllow, NewRef, Not,
    NowBetween, OldRef, OpaqueRef, Or, PolicyRule, Pred, Property, Rule, Spec, TenantIsolation,
    TransitionRule, WriteSubsetRead,
)

OPS = {"=": "is", "<>": "is not", "<": "is less than", "<=": "is at most", ">": "is greater than",
       ">=": "is at least"}
VERB = {"select": "read", "insert": "insert", "update": "update", "delete": "delete", "all": "read and change"}
USER_SOURCES = ("auth.uid", "current_user")


def ctx_phrase(spec: Spec, name: str) -> str:
    if name == CONTEXT_NOW:
        return "the current time"
    cv = spec.identity.context.get(name)
    if cv is not None and cv.source in USER_SOURCES:
        return "the current user"
    return f"the user's {name}"


def term(spec: Spec, t, row: str = "the row") -> str:
    if isinstance(t, ColRef):
        return f"{row}'s {t.col}"
    if isinstance(t, OldRef):
        return f"the previous {t.old}"
    if isinstance(t, NewRef):
        return f"the new {t.new}"
    if isinstance(t, CtxRef):
        return ctx_phrase(spec, t.ctx)
    if isinstance(t, Const):
        v = t.const
        if isinstance(v, bool):
            return "true" if v else "false"
        return repr(v) if isinstance(v, str) else str(v)
    raise TypeError(t)


def pred(spec: Spec, p: Pred, row: str = "the row", top: bool = True) -> str:
    def wrap(s: str) -> str:
        return s if top else f"({s})"

    if isinstance(p, Lit):
        return "always" if p.lit else "never"
    if isinstance(p, And):
        return wrap(" and ".join(pred(spec, a, row, False) for a in p.and_))
    if isinstance(p, Or):
        return wrap(" or ".join(pred(spec, a, row, False) for a in p.or_))
    if isinstance(p, Not):
        return f"not {pred(spec, p.not_, row, False)}"
    if isinstance(p, IsNull):
        return f"{term(spec, p.is_null, row)} is empty"
    if isinstance(p, Cmp):
        l, op, r = p.cmp
        return f"{term(spec, l, row)} {OPS[op]} {term(spec, r, row)}"
    if isinstance(p, Member):
        m = p.member
        return f"{term(spec, m.value, row)} is among {term(spec, m.key, row)}'s {m.relation}"
    if isinstance(p, InGroup):
        g = p.in_group
        return (f"{term(spec, g.value, row)} is one of {term(spec, g.key, row)}'s {g.hierarchy} "
                f"groups or lies below one")
    if isinstance(p, NowBetween):
        nb = p.now_between
        parts = []
        if nb.from_ is not None:
            parts.append(f"{term(spec, nb.from_, row)} (if set) is not after the current time")
        if nb.until is not None:
            parts.append(f"the current time is before {term(spec, nb.until, row)} (if set)")
        return wrap(" and ".join(parts)) if parts else "always"
    if isinstance(p, OpaqueRef):
        return f"the unverified SQL block '{p.opaque}' holds"
    raise TypeError(p)


def rule(spec: Spec, r: Rule) -> str:
    if isinstance(r, PolicyRule):
        return policy(spec, r)
    if isinstance(r, CheckRule):
        return f"Every row of {r.table} satisfies: {pred(spec, r.predicate)}."
    if isinstance(r, InclusionRule):
        rel = spec.relations[r.relation]
        c1, c2 = r.columns
        return (f"Every row of {r.table} with both {c1} and {c2} set has ({c1}, {c2}) listed in "
                f"{r.relation} (table {rel.table}); such a {r.relation} entry cannot be removed or "
                f"changed while a row of {r.table} refers to it.")
    if isinstance(r, TransitionRule):
        return f"A change to a row of {r.table} is allowed only if {pred(spec, r.predicate)}."
    if isinstance(r, CardinalityRule):
        return (f"A {r.parent} row has at most {r.max} rows of {r.table} (counted by {r.group}); "
                f"rows with no {r.group} are not counted.")
    if isinstance(r, ColumnRule):
        what = "read" if r.access == "read" else "write"
        return f"{r.role} may {what} only the columns {', '.join(r.columns)} of {r.table}."
    raise TypeError(r)


def policy(spec: Spec, p: PolicyRule) -> str:
    verb = VERB[p.command]
    if p.mode == "restrictive":
        s = f"{p.role} may {verb} rows of {p.table} only where {pred(spec, p.using)}"
        if p.command in ("update", "all") and p.check is not None:
            s += f", and only to rows where {pred(spec, p.check, 'the new row')}"
        return s + "."
    if p.command == "insert":
        return f"{p.role} may insert a row into {p.table} where {pred(spec, p.check, 'the new row')}."
    s = f"{p.role} may {verb} rows of {p.table} where {pred(spec, p.using)}"
    if p.command in ("update", "all"):
        chk = p.check if p.check is not None else p.using
        s += f", provided the new row satisfies: {pred(spec, chk, 'the new row')}"
    return s + "."


def prop(spec: Spec, p: Property) -> str:
    if isinstance(p, TenantIsolation):
        cmds = ", ".join(VERB[c] for c in p.commands)
        return (f"Any row of {p.table} a user can {cmds} has a {p.column} among "
                f"{term(spec, p.key)}'s {p.relation}.")
    if isinstance(p, ColumnImmutable):
        return f"No permitted change to a row of {p.table} alters its {p.column}."
    if isinstance(p, MonotoneIn):
        return f"Removing entries from {p.relation} never grants anyone new access."
    if isinstance(p, WriteSubsetRead):
        return f"Whatever a user can insert into or update in {p.table}, that user can also read."
    if isinstance(p, LeastPrivilege):
        parts = [(f"{role} may only {', '.join(VERB[c] for c in cmds)} {table}" if cmds
                  else f"{role} has no access to {table}")
                 for role, tables in p.allowed.items() for table, cmds in tables.items()]
        return "No role gets more than declared: " + "; ".join(parts) + "."
    if isinstance(p, InvariantPreserved):
        labels = []
        for inv in p.invariants:
            if inv.rule is not None:
                r = spec.rule(inv.rule)
                labels.append(f"{inv.rule} ({r.name})")
            else:
                labels.append(f"rows of {inv.table} satisfy {pred(spec, inv.predicate)}")
        return "Every permitted change keeps these true: " + "; ".join(labels) + "."
    if isinstance(p, Custom):
        img = "the new row" if p.command in ("insert", "update") and p.which == "new" else "the row"
        return (f"Whenever {p.role} can {VERB[p.command]} a row of {p.table}, "
                f"{pred(spec, p.goal, img)}.")
    if isinstance(p, MustAllow):
        if p.command == "update":
            return f"{p.role} can change a row of {p.table} whenever {pred(spec, p.when, 'the new row')}."
        img = "the new row" if p.command == "insert" else "the row"
        return f"{p.role} can {VERB[p.command]} every row of {p.table} where {pred(spec, p.when, img)}."
    raise TypeError(p)


def scenario(spec: Spec, sc) -> str:
    """One paragraph: who, which rows, which memberships."""
    parts = []
    if sc.context:
        ctx = ", ".join(f"{ctx_phrase(spec, k)} is {_v(v)}" for k, v in sc.context.items())
        parts.append(ctx)
    for name, r in sc.rows.items():
        vals = ", ".join(f"{k} = {_v(v)}" for k, v in r["values"].items())
        parts.append(f"{_row_label(name)} of {r['table']} with {vals}")
    for name, pairs in sc.relations.items():
        shown = ", ".join(f"({_v(a)}, {_v(b)})" for a, b in pairs) if pairs else "no entries"
        parts.append(f"{name} has {shown}")
    return "; ".join(parts) + "." if parts else "(no particular data)"


def _row_label(name: str) -> str:
    return {"r": "a row", "new": "the new row", "old": "the existing row", "x": "a row"}.get(
        name, f"row {name}")


def _v(v: Any) -> str:
    if v is None:
        return "empty"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def spec_summary(spec: Spec) -> str:
    lines = ["# What the rules say", ""]
    for r in spec.rules:
        lines.append(f"- **{r.id}**: {rule(spec, r)}")
    lines += ["", "# What is guaranteed (properties)", ""]
    for p in spec.properties:
        lines.append(f"- **{p.id}**: {prop(spec, p)}")
    if spec.hierarchies:
        lines += ["", "# Group hierarchies", ""]
        for name, h in spec.hierarchies.items():
            lines.append(f"- **{name}**: rows of {h.table} form a tree ({h.parent} points to the parent "
                         f"{h.id}) at most {h.max_depth} levels deep; a parent never changes; a user "
                         f"belongs to the groups listed in {h.members} and to everything below them.")
    if spec.opaques:
        lines += ["", "# Unverified SQL blocks", ""]
        for name, o in spec.opaques.items():
            lines.append(f"- **{name}** on {o.table}: `{o.sql}` (not checked by the prover; "
                         f"proofs that use it are conditional on it)")
    return "\n".join(lines) + "\n"
