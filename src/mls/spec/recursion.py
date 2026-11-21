"""Which lookups go through a helper (D67, #34; the rule is D66's, measured in FINDINGS).

A membership in a policy compiles to a subquery the caller runs, under the SELECT policies of the
relation's tables (the visibility policies D21 and D56 add among them).  When those lead back to
the policy's own table Postgres refuses every statement that reads it ('infinite recursion
detected in policy'): every compiled policy contains a subquery, the context value.  Such a lookup
instead calls a SECURITY DEFINER helper owned by `mls_internal` (`compile/lookup.py`), which reads
the tables without the caller's policies, so its lookup is no step of the expansion.

`routed(spec)` is the set of (policy table, relation) whose lookups go through a helper: those
whose relation reads the policy's own table, then, in rule order, each lookup that still closes a
cycle once the earlier ones are routed, until none does.  Per role the graph differs (the
subqueries run as the caller), but a helper means the same for every role, so a lookup routed for
one role is routed for all.
"""
from __future__ import annotations

from dataclasses import dataclass

from mls.spec.model import And, Member, MemberBody, Not, Or, PolicyRule, Pred, Spec


@dataclass(frozen=True)
class Use:
    role: str
    table: str          # the policy's table
    rule_id: str
    relation: str
    tables: tuple[str, ...]   # the relation's levels' tables, base first
    reads_rows: bool    # a SELECT (or FOR ALL) policy's USING: a step of a later lookup's expansion


def members(p: Pred | None, out: list[MemberBody]) -> list[MemberBody]:
    if isinstance(p, Member):
        out.append(p.member)
    elif isinstance(p, And):
        for a in p.and_:
            members(a, out)
    elif isinstance(p, Or):
        for a in p.or_:
            members(a, out)
    elif isinstance(p, Not):
        members(p.not_, out)
    return out


def uses(spec: Spec) -> list[Use]:
    from mls.spec.chain import levels

    out = []
    for r in spec.rules:
        if not isinstance(r, PolicyRule):
            continue
        for expr, which in ((r.using, "using"), (r.check, "check")):
            for m in members(expr, []):
                lv = tuple(lvl.table for lvl in levels(spec, m.relation))
                out.append(Use(r.role, r.table, r.id, m.relation, lv,
                               which == "using" and r.command in ("select", "all")))
    return out


def reads(all_uses: list[Use], skip: set[tuple[str, str]]) -> dict[str, dict[str, list[tuple[str, Use]]]]:
    """role -> table -> [(table read, the lookup)]: the SELECT-policy steps, and the visibility
    policy on hop table i reading the levels before it, for every lookup not in `skip`."""
    g: dict[str, dict[str, list[tuple[str, Use]]]] = {}
    for u in all_uses:
        if (u.table, u.relation) in skip:
            continue
        rg = g.setdefault(u.role, {})
        if u.reads_rows:
            for t in u.tables:
                rg.setdefault(u.table, []).append((t, u))
        for h in range(1, len(u.tables)):
            for t in u.tables[:h]:
                rg.setdefault(u.tables[h], []).append((t, u))
    return g


def back_path(g: dict[str, list[tuple[str, Use]]], src: str, dst: str) -> list[tuple[str, Use]] | None:
    """The steps from `src` back to `dst` (breadth first), or None."""
    todo, prev = [src], {src: None}
    while todo:
        t = todo.pop(0)
        if t == dst:
            out = []
            while prev[t] is not None:
                t0, u = prev[t]
                out.append((t, u))
                t = t0
            return out[::-1]
        for nxt, u in g.get(t, []):
            if nxt not in prev:
                prev[nxt] = (t, u)
                todo.append(nxt)
    return None


_cache: dict[int, tuple[Spec, frozenset[tuple[str, str]]]] = {}


def routed(spec: Spec) -> frozenset[tuple[str, str]]:
    hit = _cache.get(id(spec))
    if hit is not None and hit[0] is spec:
        return hit[1]
    all_uses = uses(spec)
    out = {(u.table, u.relation) for u in all_uses if u.table in u.tables}
    changed = True
    while changed:
        changed = False
        g = reads(all_uses, out)
        for u in all_uses:
            if (u.table, u.relation) in out:
                continue
            if any(back_path(g.get(u.role, {}), t, u.table) for t in dict.fromkeys(u.tables)):
                out.add((u.table, u.relation))
                changed = True
                break
    res = frozenset(out)
    _cache[id(spec)] = (spec, res)
    return res


def is_routed(spec: Spec, table: str, relation: str) -> bool:
    return (table, relation) in routed(spec)
