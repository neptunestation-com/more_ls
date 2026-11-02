"""Postgres policy combination (SEMANTICS §5) and the engine constraints that hold on rows.

effective = (OR of permissive) AND (AND of restrictive); no permissive policy → deny.
WITH CHECK defaults to USING for ALL and UPDATE policies.
"""
from __future__ import annotations

from dataclasses import dataclass

from mls.spec.model import (
    And, CheckRule, Cmp, InclusionRule, InGroup, IsNull, Lit, Member, MemberBody, NewRef, Not, OldRef,
    Or, PolicyRule, Pred, Spec, ColRef, TransitionRule,
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


def in_groups_in(p: Pred | None, out: list) -> list:
    if p is None:
        return out
    if isinstance(p, InGroup):
        out.append(p.in_group)
    elif isinstance(p, And):
        for a in p.and_:
            in_groups_in(a, out)
    elif isinstance(p, Or):
        for a in p.or_:
            in_groups_in(a, out)
    elif isinstance(p, Not):
        in_groups_in(p.not_, out)
    return out


def relations_of(spec: Spec, p: Pred | None) -> set[str]:
    """Relations a predicate depends on: membership atoms and the members relation of every
    hierarchy it uses."""
    out = {m.relation for m in members_in(p, [])}
    # (a hierarchy of the other version in an access diff is simply absent here)
    out |= {spec.hierarchies[g.hierarchy].members for g in in_groups_in(p, []) if g.hierarchy in spec.hierarchies}
    return out


def all_policies(spec: Spec) -> list[PolicyRule]:
    return spec.policies() + implicit_policies(spec)


def implicit_transitions(spec: Spec) -> list[TransitionRule]:
    """D5: a hierarchy's parent column is immutable (NULL-aware), so depth never changes after
    insert and the tree cannot acquire cycles."""
    out = []
    for name, h in spec.hierarchies.items():
        out.append(TransitionRule(
            id=f"implicit:hierarchy:{name}", kind="transition", name=f"mls_h_{name}_parent_immutable",
            table=h.table,
            predicate=Or.model_construct(or_=[
                And.model_construct(and_=[IsNull(is_null=OldRef(old=h.parent)), IsNull(is_null=NewRef(new=h.parent))]),
                Cmp(cmp=(NewRef(new=h.parent), "=", OldRef(old=h.parent)))])))
    return out


def all_transitions(spec: Spec) -> list[TransitionRule]:
    return spec.transitions() + implicit_transitions(spec)


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


def hierarchy_checks(spec: Spec, table: str) -> list[Pred]:
    """The compiler's depth bound on a hierarchy table (D5): 0 <= depth <= max_depth."""
    from mls.spec.model import Const

    out: list[Pred] = []
    for h in spec.hierarchies.values():
        if h.table == table:
            out.append(And.model_construct(and_=[
                Cmp(cmp=(ColRef(col=h.depth), ">=", Const(const=0))),
                Cmp(cmp=(ColRef(col=h.depth), "<=", Const(const=h.max_depth)))]))
    return out


def row_constraints(spec: Spec, table: str) -> Pred:
    """Engine-enforced single-row constraints on every stored row of `table`: CHECKs, FKs and
    the hierarchy depth bound."""
    parts: list[Pred] = [c.predicate for c in spec.checks() if c.table == table]
    parts += [inclusion_pred(spec, i) for i in spec.inclusions() if i.table == table]
    parts += hierarchy_checks(spec, table)
    return _and(*parts)


@dataclass(frozen=True)
class SimpleFk:
    """A single-column foreign key the hierarchy and cardinality templates emit (RESTRICT both ways)."""
    table: str
    column: str
    parent: str
    parent_column: str
    name: str


def simple_fks(spec: Spec) -> list[SimpleFk]:
    out = []
    for name, h in spec.hierarchies.items():
        rel = spec.relations[h.members]
        out.append(SimpleFk(h.table, h.parent, h.table, h.id, f"mls_h_{name}_parent"))
        out.append(SimpleFk(rel.table, rel.value, h.table, h.id, f"mls_h_{name}_member"))
    for c in spec.cardinalities():
        out.append(SimpleFk(c.table, c.group, c.parent, spec.tables[c.parent].primary_key[0],
                            f"mls_card_{c.name}_parent"))
    return out


def transition_constraints(spec: Spec, table: str) -> Pred:
    """Every transition rule on `table` (old/new predicates), including the implicit ones."""
    return _and(*[t.predicate for t in all_transitions(spec) if t.table == table])
