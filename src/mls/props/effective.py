"""Postgres policy combination (SEMANTICS §5) and the engine constraints that hold on rows.

effective = (OR of permissive) AND (AND of restrictive); no permissive policy → deny.
WITH CHECK defaults to USING for ALL and UPDATE policies.
"""
from __future__ import annotations

from mls.spec.model import (
    And, CheckRule, Cmp, InclusionRule, IsNull, Lit, Member, MemberBody, Not, Or, PolicyRule, Pred,
    Spec, ColRef, TransitionRule,
)

Which = str  # "using" | "check"


def _and(*ps: Pred) -> Pred:
    ps = [p for p in ps if not (isinstance(p, Lit) and p.lit)]
    if any(isinstance(p, Lit) and not p.lit for p in ps):
        return Lit(lit=False)
    if not ps:
        return Lit(lit=True)
    return ps[0] if len(ps) == 1 else And.model_construct(and_=list(ps))


def _or(*ps: Pred) -> Pred:
    ps = [p for p in ps if not (isinstance(p, Lit) and not p.lit)]
    if any(isinstance(p, Lit) and p.lit for p in ps):
        return Lit(lit=True)
    if not ps:
        return Lit(lit=False)
    return ps[0] if len(ps) == 1 else Or.model_construct(or_=list(ps))


def policy_expr(p: PolicyRule, which: Which) -> Pred | None:
    if which == "using":
        return p.using
    return p.check if p.check is not None else p.using


_implicit_cache: dict[int, tuple[Spec, list[PolicyRule]]] = {}


def implicit_policies(spec: Spec) -> list[PolicyRule]:
    hit = _implicit_cache.get(id(spec))
    if hit is not None and hit[0] is spec:
        return hit[1]
    out = _implicit_policies(spec)
    _implicit_cache[id(spec)] = (spec, out)
    return out


def _implicit_policies(spec: Spec) -> list[PolicyRule]:
    """Membership subqueries run as the current role, and RLS is forced on every MLS table.  So for
    every relation a policy of role R uses with key k, the compiler adds a permissive SELECT policy
    on the relation's table for R: `key = k`.  This is the least grant that makes the membership
    atom mean "(k, col) ∈ relation" (D21).  Restrictive policies on the relation table still apply;
    the compiler proves they do not filter those rows (relation visibility obligation)."""
    out: list[PolicyRule] = []
    seen: set[tuple[str, str, str]] = set()
    for p in spec.policies():
        for e in (p.using, p.check):
            for m in members_in(e, []):
                rel = spec.relations[m.relation]
                key_json = m.key.model_dump_json()
                k = (rel.table, p.role, key_json)
                if k in seen:
                    continue
                seen.add(k)
                n = sum(1 for s_ in seen if s_[0] == rel.table and s_[1] == p.role)
                out.append(PolicyRule(
                    id=f"implicit:{m.relation}:{p.role}:{n}",
                    name=f"mls_rel_{m.relation}_{p.role}_{n}",
                    table=rel.table, kind="policy", role=p.role, command="select",
                    mode="permissive",
                    using=Cmp(cmp=(ColRef(col=rel.key), "=", m.key))))
    return out


def members_in(p: Pred | None, out: list[MemberBody]) -> list[MemberBody]:
    if p is None:
        return out
    if isinstance(p, Member):
        out.append(p.member)
    elif isinstance(p, And):
        for a in p.and_:
            members_in(a, out)
    elif isinstance(p, Or):
        for a in p.or_:
            members_in(a, out)
    elif isinstance(p, Not):
        members_in(p.not_, out)
    return out


def all_policies(spec: Spec) -> list[PolicyRule]:
    return spec.policies() + implicit_policies(spec)


def policies_for(spec: Spec, table: str, role: str, cmd: str, skip: tuple[str, ...] = ()) -> list[PolicyRule]:
    return [p for p in all_policies(spec)
            if p.table == table and p.role == role and p.command in (cmd, "all") and p.id not in skip]


def effective(spec: Spec, table: str, role: str, cmd: str, which: Which,
              skip: tuple[str, ...] = ()) -> Pred:
    """Effective predicate for `cmd` on `table` for `role`; which = 'using' | 'check'."""
    ps = policies_for(spec, table, role, cmd, skip)
    perm = [e for p in ps if p.mode == "permissive" and (e := policy_expr(p, which)) is not None]
    rest = [e for p in ps if p.mode == "restrictive" and (e := policy_expr(p, which)) is not None]
    if not perm:
        return Lit(lit=False)
    return _and(_or(*perm), *rest)


def inclusion_pred(spec: Spec, r: InclusionRule) -> Pred:
    """FK MATCH SIMPLE (SEMANTICS §4): is_null(c1) ∨ is_null(c2) ∨ member(rel, c1, c2) over the
    nullable columns."""
    t = spec.tables[r.table]
    parts: list[Pred] = []
    for c in r.columns:
        if t.columns[c].nullable:
            parts.append(IsNull(is_null=ColRef(col=c)))
    parts.append(Member(member=MemberBody(relation=r.relation, key=ColRef(col=r.columns[0]),
                                          value=ColRef(col=r.columns[1]))))
    return _or(*parts)


def row_constraints(spec: Spec, table: str) -> Pred:
    """Engine-enforced single-row constraints on every stored row of `table`: CHECKs and FKs."""
    parts: list[Pred] = [c.predicate for c in spec.checks() if c.table == table]
    parts += [inclusion_pred(spec, i) for i in spec.inclusions() if i.table == table]
    return _and(*parts)


def transition_constraints(spec: Spec, table: str) -> Pred:
    """Every transition rule on `table` (old/new predicates)."""
    return _and(*[t.predicate for t in spec.transitions() if t.table == table])
