"""Property templates (D9) and custom properties as proof obligations (SEMANTICS §7).

Every obligation is "assumptions ∧ ¬goal is UNSAT".  Assumptions always include the engine
constraints that hold on stored rows (CHECKs and FKs are validated), the policies of the role under
examination, and the transition triggers for UPDATE.  Policies are never assumed in invariant
preservation: trusted roles (owner, superuser) bypass them, so invariants must follow from
CHECKs, FKs and triggers alone.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from mls.compile.column import privileges
from mls.logic.term import (
    FALSE, TRUE, Sort, Term, Var, and_, eq, forall, fun, iff, implies, int_var, le, not_, or_,
)
from mls.logic.tvl import Encoder, col_null_var, col_value_var, ctx_var, rel_fun
from mls.props.effective import (
    effective, inclusion_pred, members_in, relations_of, row_constraints, transition_constraints,
)
from mls.spec.model import (
    And, CardinalityRule, CheckRule, ColRef, ColumnImmutable, Custom, InclusionRule,
    InvariantPreserved, LeastPrivilege, Lit, Member, MemberBody, MonotoneIn, Not, Or, Pred, Property,
    Spec, TenantIsolation, WriteSubsetRead,
)
from mls.logic.term import IntVal, add


@dataclass
class Obligation:
    property_id: str
    name: str
    assumptions: list[Term]
    goal: Term
    rows: dict[str, str]                               # row name -> table
    relations: list[tuple[str, str | None]] = field(default_factory=list)  # (relation, tag)
    encoders: list[Encoder] = field(default_factory=list)
    note: str = ""
    # for soundness obligations raised as compile errors
    path: str = ""
    failure_message: str = ""
    fix: str = ""
    # propositional obligations carry their verdict
    propositional: bool = False
    verdict: str | None = None
    message: str = ""

    def opaque_deps(self) -> set[str]:
        out: set[str] = set()
        for e in self.encoders:
            out |= e.opaque_deps
        return out

    def finish(self) -> "Obligation":
        for e in self.encoders:
            self.assumptions = self.assumptions + e.side_conditions
        return self


def _rels_in(spec: Spec, p: Pred | None) -> set[str]:
    return relations_of(spec, p)


def _T(enc: Encoder, p: Pred, row: str, table: str) -> Term:
    return enc.T(p, row, table)


def relation_axioms(spec: Spec, enc: Encoder) -> list[Term]:
    """Enforced inclusions between relation tables, as EPR axioms: an inclusion rule on the table
    backing relation A whose columns are exactly (A.key, A.value) and whose target is relation B
    gives ∀x,y: A(x,y) → B(x,y).  (FKs are validated, so stored facts satisfy them.)"""
    out = []
    x, y = int_var("ax.k"), int_var("ax.v")
    for inc in spec.inclusions():
        for aname, a in spec.relations.items():
            if a.table == inc.table and list(inc.columns) == [a.key, a.value]:
                out.append(forall([x, y], implies(enc.rel(aname)(x, y), enc.rel(inc.relation)(x, y))))
    return out


def select_assumptions(enc: Encoder, spec: Spec, table: str, role: str, row: str = "r") -> list[Term]:
    return [*relation_axioms(spec, enc), _T(enc, row_constraints(spec, table), row, table),
            _T(enc, effective(spec, table, role, "select", "using"), row, table)]


def insert_assumptions(enc: Encoder, spec: Spec, table: str, role: str, row: str = "new") -> list[Term]:
    return [*relation_axioms(spec, enc), _T(enc, effective(spec, table, role, "insert", "check"), row, table),
            _T(enc, row_constraints(spec, table), row, table)]


def update_assumptions(enc: Encoder, spec: Spec, table: str, role: str) -> list[Term]:
    return [*relation_axioms(spec, enc), _T(enc, row_constraints(spec, table), "old", table),
            _T(enc, effective(spec, table, role, "update", "using"), "old", table),
            _T(enc, effective(spec, table, role, "update", "check"), "new", table),
            _T(enc, row_constraints(spec, table), "new", table),
            _T(enc, transition_constraints(spec, table), "new", table)]


def delete_assumptions(enc: Encoder, spec: Spec, table: str, role: str, row: str = "old") -> list[Term]:
    return [*relation_axioms(spec, enc), _T(enc, row_constraints(spec, table), row, table),
            _T(enc, effective(spec, table, role, "delete", "using"), row, table)]


def _rows_for(cmd: str, table: str) -> dict[str, str]:
    if cmd == "select":
        return {"r": table}
    if cmd == "insert":
        return {"new": table}
    if cmd == "update":
        return {"old": table, "new": table}
    return {"old": table}


def _image(cmd: str, which: str = "new") -> str:
    return {"select": "r", "insert": "new", "delete": "old"}.get(cmd, which)


def _relations_of(spec: Spec, *preds: Pred | None, tag: str | None = None) -> list[tuple[str, str | None]]:
    rels: set[str] = set()
    for p in preds:
        rels |= _rels_in(spec, p)
    return [(r, tag) for r in sorted(rels)]


def _all_relations(spec: Spec, table: str, role: str, cmd: str) -> list[tuple[str, str | None]]:
    preds = [effective(spec, table, role, cmd, "using"), effective(spec, table, role, cmd, "check"),
             row_constraints(spec, table)]
    return _relations_of(spec, *preds)


# ----------------------------------------------------------------------------- templates
def tenant_isolation(spec: Spec, p: TenantIsolation) -> list[Obligation]:
    out = []
    goal_pred = Member(member=MemberBody(relation=p.relation, key=p.key, value=ColRef(col=p.column)))
    for role in spec.roles:
        for cmd in p.commands:
            enc = Encoder(spec)
            if cmd == "select":
                assumptions = select_assumptions(enc, spec, p.table, role)
            elif cmd == "insert":
                assumptions = insert_assumptions(enc, spec, p.table, role)
            elif cmd == "update":
                assumptions = update_assumptions(enc, spec, p.table, role)
            else:
                assumptions = delete_assumptions(enc, spec, p.table, role)
            img = _image(cmd)
            goal = _T(enc, goal_pred, img, p.table)
            out.append(Obligation(
                p.id, f"{cmd} as {role}: {img}.{p.column} ∈ {p.relation}[key]",
                assumptions, goal, _rows_for(cmd, p.table),
                _all_relations(spec, p.table, role, cmd) + [(p.relation, None)], [enc]).finish())
    return out


def column_immutable(spec: Spec, p: ColumnImmutable) -> list[Obligation]:
    out = []
    nullable = spec.tables[p.table].columns[p.column].nullable
    for role in spec.roles:
        enc = Encoder(spec)
        assumptions = update_assumptions(enc, spec, p.table, role)
        ov, nv = col_value_var("old", p.column), col_value_var("new", p.column)
        if nullable:
            on, nn = col_null_var("old", p.column), col_null_var("new", p.column)
            goal = or_(and_(on, nn), and_(not_(on), not_(nn), eq(ov, nv)))
        else:
            goal = eq(ov, nv)
        out.append(Obligation(
            p.id, f"update as {role}: new.{p.column} = old.{p.column}",
            assumptions, goal, _rows_for("update", p.table),
            _all_relations(spec, p.table, role, "update"), [enc]).finish())
    return out


def monotone_in(spec: Spec, p: MonotoneIn) -> list[Obligation]:
    """Shrinking the relation never grants: effective_small(r) → effective_big(r)."""
    out = []
    u, t = int_var("mono.k"), int_var("mono.v")
    big, small = rel_fun(p.relation, "big"), rel_fun(p.relation, "small")
    shrink = forall([u, t], implies(small(u, t), big(u, t)))
    for table in spec.tables:
        for role in spec.roles:
            for cmd, which in (("select", "using"), ("insert", "check"), ("update", "using"),
                               ("update", "check"), ("delete", "using")):
                pred = effective(spec, table, role, cmd, which)
                if isinstance(pred, Lit) or p.relation not in _rels_in(spec, pred):
                    continue
                consts: dict[str, int] = {}  # one interning for both state copies
                e_big = Encoder(spec, {p.relation: "big"}, consts=consts)
                e_small = Encoder(spec, {p.relation: "small"}, consts=consts)
                row = _image(cmd, "old" if which == "using" else "new")
                goal = implies(_T(e_small, pred, row, table), _T(e_big, pred, row, table))
                out.append(Obligation(
                    p.id, f"{cmd} {which} on {table} as {role}: admitted with fewer "
                          f"{p.relation} facts ⇒ admitted with more",
                    [shrink, *relation_axioms(spec, e_big), *relation_axioms(spec, e_small)], goal, {row: table},
                    [(p.relation, "small"), (p.relation, "big")] + _relations_of(spec, pred),
                    [e_big, e_small]).finish())
    return out


def write_subset_read(spec: Spec, p: WriteSubsetRead) -> list[Obligation]:
    out = []
    for role in spec.roles:
        sel = effective(spec, p.table, role, "select", "using")
        enc = Encoder(spec)
        out.append(Obligation(
            p.id, f"insert as {role}: insertable ⇒ visible",
            insert_assumptions(enc, spec, p.table, role), _T(enc, sel, "new", p.table),
            _rows_for("insert", p.table), _all_relations(spec, p.table, role, "insert") +
            _relations_of(spec, sel), [enc]).finish())
        enc = Encoder(spec)
        out.append(Obligation(
            p.id, f"update as {role}: new image ⇒ visible",
            update_assumptions(enc, spec, p.table, role), _T(enc, sel, "new", p.table),
            _rows_for("update", p.table), _all_relations(spec, p.table, role, "update") +
            _relations_of(spec, sel), [enc]).finish())
    return out


def least_privilege(spec: Spec, p: LeastPrivilege) -> list[Obligation]:
    extra = []
    for (role, table), g in sorted(privileges(spec).items()):
        allowed = set(p.allowed.get(role, {}).get(table, []))
        for cmd, cols in g.items():
            if cmd not in allowed:
                what = "all columns" if cols == "*" else f"columns {sorted(cols)}"
                extra.append(f"{role} may {cmd} {what} of {table}")
    verdict = "PROVED" if not extra else "REFUTED"
    msg = "granted privileges are within the declared allowance" if not extra else \
        "privileges beyond the declared allowance: " + "; ".join(extra)
    return [Obligation(p.id, "derived grants ⊆ declared", [], TRUE, {}, propositional=True,
                       verdict=verdict, message=msg)]


def custom(spec: Spec, p: Custom) -> list[Obligation]:
    enc = Encoder(spec)
    if p.command == "select":
        assumptions = select_assumptions(enc, spec, p.table, p.role)
    elif p.command == "insert":
        assumptions = insert_assumptions(enc, spec, p.table, p.role)
    elif p.command == "update":
        assumptions = update_assumptions(enc, spec, p.table, p.role)
    else:
        assumptions = delete_assumptions(enc, spec, p.table, p.role)
    img = _image(p.command, p.which)
    goal = _T(enc, p.goal, img, p.table)
    return [Obligation(p.id, f"{p.command} as {p.role}: goal holds for {img}",
                       assumptions, goal, _rows_for(p.command, p.table),
                       _all_relations(spec, p.table, p.role, p.command) + _relations_of(spec, p.goal),
                       [enc]).finish()]


# ----------------------------------------------------------------------------- induction
@dataclass
class Invariant:
    label: str
    table: str
    pred: Pred


def _invariants(spec: Spec, p: InvariantPreserved) -> list[Invariant]:
    out = []
    for inv in p.invariants:
        if inv.rule is not None:
            r = spec.rule(inv.rule)
            if isinstance(r, CheckRule):
                out.append(Invariant(f"{r.id} ({r.name})", r.table, r.predicate))
            elif isinstance(r, InclusionRule):
                out.append(Invariant(f"{r.id} ({r.name})", r.table, inclusion_pred(spec, r)))
        else:
            out.append(Invariant(f"{inv.table}: custom", inv.table, inv.predicate))
    return out


def cardinality_obligations(spec: Spec, p: InvariantPreserved, r: CardinalityRule) -> list[Obligation]:
    """cnt: group -> int.  Invariant ∀g. cnt(g) ≤ N.  Transitions on the table change one or two
    counts; the compiled trigger guards inserts and group moves with cnt(g) + 1 ≤ N (D4)."""
    N = IntVal(r.max)
    cnt, cnt2 = fun(f"cnt.{r.id}@before", Sort.INT, ret=Sort.INT), fun(f"cnt.{r.id}@after", Sort.INT, ret=Sort.INT)
    x = int_var("card.g")
    inv_before = forall([x], le(cnt(x), N))
    inv_after = forall([x], le(cnt2(x), N))
    nullable = spec.tables[r.table].columns[r.group].nullable
    out = []
    for cmd in ("insert", "update", "delete"):
        enc = Encoder(spec)
        rows = _rows_for(cmd, r.table)
        axioms: list[Term] = [inv_before]
        ng, og = col_value_var("new", r.group), col_value_var("old", r.group)
        nn = col_null_var("new", r.group) if nullable else FALSE
        on = col_null_var("old", r.group) if nullable else FALSE
        if cmd == "insert":
            # counted iff the group is not NULL; the trigger requires cnt(g) + 1 ≤ N
            axioms.append(implies(not_(nn), le(add(cnt(ng), IntVal(1)), N)))
            axioms.append(forall([x], iff(eq(cnt2(x), add(cnt(x), IntVal(1))), and_(not_(nn), eq(x, ng)))))
            axioms.append(forall([x], implies(not_(and_(not_(nn), eq(x, ng))), eq(cnt2(x), cnt(x)))))
        elif cmd == "delete":
            axioms.append(forall([x], implies(and_(not_(on), eq(x, og)), eq(cnt2(x), add(cnt(x), IntVal(-1))))))
            axioms.append(forall([x], implies(not_(and_(not_(on), eq(x, og))), eq(cnt2(x), cnt(x)))))
        else:
            moved = or_(and_(not_(on), nn), and_(on, not_(nn)), and_(not_(on), not_(nn), not_(eq(og, ng))))
            axioms.append(implies(and_(moved, not_(nn)), le(add(cnt(ng), IntVal(1)), N)))
            leaves = and_(moved, not_(on))   # old group loses one
            joins = and_(moved, not_(nn))    # new group gains one
            axioms.append(forall([x], implies(and_(leaves, eq(x, og)), eq(cnt2(x), add(cnt(x), IntVal(-1))))))
            axioms.append(forall([x], implies(and_(joins, eq(x, ng)), eq(cnt2(x), add(cnt(x), IntVal(1))))))
            axioms.append(forall([x], implies(not_(or_(and_(leaves, eq(x, og)), and_(joins, eq(x, ng)))),
                                              eq(cnt2(x), cnt(x)))))
        out.append(Obligation(p.id, f"{cmd} on {r.table} preserves {r.id} ({r.name}): at most {r.max} "
                                    f"rows per {r.group}", axioms, inv_after, rows, [], [enc]).finish())
    return out


def invariant_preserved(spec: Spec, p: InvariantPreserved) -> list[Obligation]:
    out = []
    for inv in p.invariants:
        if inv.rule is not None and isinstance(spec.rule(inv.rule), CardinalityRule):
            out.extend(cardinality_obligations(spec, p, spec.rule(inv.rule)))
    invs = _invariants(spec, p)
    if not invs:
        return out
    tables: set[str] = {i.table for i in invs}
    for i in invs:
        for r in _rels_in(spec, i.pred):
            tables.add(spec.relations[r].table)
    # transitions: INSERT / UPDATE / DELETE on every table the invariants mention
    for t in sorted(tables):
        backed = {name for name, rel in spec.relations.items() if rel.table == t}
        for cmd in ("insert", "update", "delete"):
            out.extend(_induction_step(spec, p, invs, t, cmd, backed))
    return out


def _relation_change(spec: Spec, table: str, cmd: str, backed: set[str]) -> list[Term]:
    """Axioms relating rel@before and rel@after for relations backed by `table`."""
    axioms = []
    x, y = int_var("chg.k"), int_var("chg.v")
    for rname in sorted(backed):
        rel = spec.relations[rname]
        before, after = rel_fun(rname, "before"), rel_fun(rname, "after")
        if cmd == "insert":
            nk, nv = col_value_var("new", rel.key), col_value_var("new", rel.value)
            axioms.append(forall([x, y], iff(after(x, y), or_(before(x, y), and_(eq(x, nk), eq(y, nv))))))
        elif cmd == "delete":
            ok, ov = col_value_var("old", rel.key), col_value_var("old", rel.value)
            axioms.append(before(ok, ov))
            axioms.append(forall([x, y], iff(after(x, y), and_(before(x, y), not_(and_(eq(x, ok), eq(y, ov)))))))
        else:
            ok, ov = col_value_var("old", rel.key), col_value_var("old", rel.value)
            nk, nv = col_value_var("new", rel.key), col_value_var("new", rel.value)
            axioms.append(before(ok, ov))
            axioms.append(forall([x, y], iff(after(x, y), or_(
                and_(before(x, y), not_(and_(eq(x, ok), eq(y, ov)))), and_(eq(x, nk), eq(y, nv))))))
    return axioms


def _restrict_guard(spec: Spec, table: str, cmd: str, backed: set[str], row: str, row_table: str) -> list[Term]:
    """ON DELETE/UPDATE RESTRICT as seen by the solver: the arbitrary row `row` of `row_table` does
    not reference the pair removed from a relation backed by `table`."""
    guards = []
    for inc in spec.inclusions():
        if inc.relation not in backed or inc.table != row_table:
            continue
        rel = spec.relations[inc.relation]
        ok, ov = col_value_var("old", rel.key), col_value_var("old", rel.value)
        c1, c2 = inc.columns
        t = spec.tables[row_table]
        refs = [eq(col_value_var(row, c1), ok), eq(col_value_var(row, c2), ov)]
        if t.columns[c1].nullable:
            refs.append(not_(col_null_var(row, c1)))
        if t.columns[c2].nullable:
            refs.append(not_(col_null_var(row, c2)))
        references_old = and_(*refs)
        if cmd == "delete":
            guards.append(not_(references_old))
        else:  # update: only when the pair changes
            nk, nv = col_value_var("new", rel.key), col_value_var("new", rel.value)
            unchanged = and_(eq(ok, nk), eq(ov, nv))
            guards.append(or_(unchanged, not_(references_old)))
    return guards


def _induction_step(spec: Spec, p: InvariantPreserved, invs: list[Invariant], table: str, cmd: str,
                    backed: set[str]) -> list[Obligation]:
    out = []
    before = Encoder(spec, {r: "before" for r in backed})
    after = Encoder(spec, {r: "after" for r in backed})
    change = _relation_change(spec, table, cmd, backed) + relation_axioms(spec, before)
    mod_rows = _rows_for(cmd, table)
    all_rels = [(r, "before") for r in sorted(backed)] + [(r, "after") for r in sorted(backed)]
    for inv in invs:
        touches = bool(_rels_in(spec, inv.pred) & backed)
        rel_list = all_rels + [(r, None) for r in sorted(_rels_in(spec, inv.pred) - backed)]
        # 1. the modified row itself (insert / update on the invariant's own table)
        if inv.table == table and cmd in ("insert", "update"):
            assumptions = list(change)
            if cmd == "update":
                assumptions += [before.T(inv.pred, "old", table),
                                before.T(row_constraints(spec, table), "old", table),
                                after.T(transition_constraints(spec, table), "new", table)]
            assumptions.append(after.T(row_constraints(spec, table), "new", table))
            out.append(Obligation(
                p.id, f"{cmd} on {table} preserves {inv.label} for the written row",
                assumptions, after.T(inv.pred, "new", table), dict(mod_rows), rel_list,
                [before, after]).finish())
        # 2. an arbitrary other row (affected only if the invariant reads a relation backed by table)
        if touches:
            row = f"row_{inv.table}"
            assumptions = list(change) + [before.T(inv.pred, row, inv.table),
                                          before.T(row_constraints(spec, inv.table), row, inv.table)]
            if cmd in ("delete", "update"):
                assumptions += _restrict_guard(spec, table, cmd, backed, row, inv.table)
            rows = dict(mod_rows)
            rows[row] = inv.table
            out.append(Obligation(
                p.id, f"{cmd} on {table} preserves {inv.label} for every other row of {inv.table}",
                assumptions, after.T(inv.pred, row, inv.table), rows, rel_list,
                [before, after]).finish())
    return out


# ----------------------------------------------------------------------------- soundness
def relation_visibility_obligations(spec: Spec) -> list[Obligation]:
    """A policy's membership subquery runs as the role under RLS on the relation table.  For every
    row x of that table with x.key = k, the role's effective SELECT must be TRUE."""
    out = []
    seen: set[tuple[str, str, str]] = set()
    for i, p in enumerate(spec.policies()):
        for e in (p.using, p.check):
            for m in members_in(e, []):
                rel = spec.relations[m.relation]
                k = (m.relation, p.role, m.key.model_dump_json())
                if k in seen:
                    continue
                seen.add(k)
                enc = Encoder(spec)
                kv, _ = enc.term(m.key, "x", rel.table)
                assumptions = [*relation_axioms(spec, enc), eq(col_value_var("x", rel.key), kv),
                               enc.T(row_constraints(spec, rel.table), "x", rel.table)]
                goal = enc.T(effective(spec, rel.table, p.role, "select", "using"), "x", rel.table)
                idx = spec.rules.index(p)
                out.append(Obligation(
                    "soundness", f"relation {m.relation} visible to {p.role}",
                    assumptions, goal, {"x": rel.table}, [(m.relation, None)], [enc],
                    path=f"$.rules[{idx}]",
                    failure_message=f"policy '{p.name}' uses relation '{m.relation}', but a "
                                    f"restrictive policy on '{rel.table}' can hide rows with "
                                    f"{rel.key} = the key from role '{p.role}', so the membership "
                                    f"subquery would be filtered and the atom would not mean "
                                    f"'(key, {rel.value}) ∈ {m.relation}'",
                    fix=f"relax the restrictive policies on '{rel.table}' for role '{p.role}' so "
                        f"that every row with {rel.key} = key is visible").finish())
    return out


def obligations_for(spec: Spec, prop: Property) -> list[Obligation]:
    if isinstance(prop, TenantIsolation):
        return tenant_isolation(spec, prop)
    if isinstance(prop, ColumnImmutable):
        return column_immutable(spec, prop)
    if isinstance(prop, MonotoneIn):
        return monotone_in(spec, prop)
    if isinstance(prop, WriteSubsetRead):
        return write_subset_read(spec, prop)
    if isinstance(prop, LeastPrivilege):
        return least_privilege(spec, prop)
    if isinstance(prop, InvariantPreserved):
        return invariant_preserved(spec, prop)
    if isinstance(prop, Custom):
        return custom(spec, prop)
    raise TypeError(prop)
