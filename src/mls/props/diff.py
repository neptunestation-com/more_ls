"""Access diff between two spec versions (SEMANTICS §7, D7, D10).

For every table × role × command (and, for UPDATE, both the targeted-row USING side and the
new-image WITH CHECK side), two obligations over shared symbols:

  new grants:   constraints ∧ effective_v2(row) → effective_v1(row)   (a model = a row v2 admits
                                                                       that v1 denied)
  new denials:  constraints ∧ effective_v1(row) → effective_v2(row)

Shared symbols: columns that exist in both versions, context values, relations with the same
definition (same table, key and value columns) and opaque blocks with the same SQL.  A relation
or opaque block whose definition changed is a different symbol in v2 (tagged `@new`), so the
diff cannot be satisfied by pretending the two mean the same thing; every obligation that
depends on an opaque block is flagged (D3).  Rows are assumed to satisfy the stored-row
constraints of both versions (rows that are legitimate in both worlds).

The integrity diff compares, per table, the stored-row constraints (CHECKs and FKs) and the
transition rules of the two versions in the same way: rows (or changes) v1 accepted and v2
rejects, and the reverse.  The grant diff is propositional (derived privileges, D24).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from mls.compile.column import privileges
from mls.logic.term import FALSE, Term, and_
from mls.logic.tvl import Encoder
from mls.props.effective import effective, row_constraints, transition_constraints
from mls.props.obligations import (
    Obligation, _relations_of, _rows_for, net_terms as _net_terms, relation_axioms,
)
from mls.props.runner import ObligationResult, run_obligation
from mls.props.scenario import Scenario
from mls.spec.model import And, InGroup, Lit, Member, Not, OpaqueRef, Or, Pred, Spec, Table

ACCESS_CASES = (("select", "using"), ("insert", "check"), ("update", "using"), ("update", "check"),
                ("delete", "using"))


@dataclass
class AccessChange:
    table: str
    role: str
    command: str
    which: str                  # using | check
    direction: str              # "granted" (v2 admits more) | "denied" (v2 admits less)
    scenario: Scenario | None
    verdict: str                # REFUTED means "a difference exists"; PROVED means none; UNKNOWN
    opaque_deps: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.verdict == "REFUTED"


@dataclass
class IntegrityChange:
    table: str
    kind: str                   # "stored rows" | "changes"
    direction: str              # "rejected" (v2 stricter) | "accepted" (v2 looser)
    scenario: Scenario | None
    verdict: str
    opaque_deps: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.verdict == "REFUTED"


@dataclass
class GrantChange:
    role: str
    table: str
    command: str
    before: str | None          # "all columns" | "columns a, b" | None
    after: str | None


@dataclass
class DiffResult:
    access: list[AccessChange]
    integrity: list[IntegrityChange]
    grants: list[GrantChange]
    notes: list[str]

    @property
    def access_changes(self) -> list[AccessChange]:
        return [a for a in self.access if a.changed]

    @property
    def integrity_changes(self) -> list[IntegrityChange]:
        return [a for a in self.integrity if a.changed]

    @property
    def unknown(self) -> list:
        return [a for a in self.access + self.integrity if a.verdict == "UNKNOWN"]

    @property
    def any_change(self) -> bool:
        return bool(self.access_changes or self.integrity_changes or self.grants)


# ----------------------------------------------------------------------------- shared symbols
def merged_spec(old: Spec, new: Spec) -> Spec:
    """A view with the union of tables/columns, relations and context values (used to render
    scenarios that mention symbols of either version).  Not a valid spec; never check it."""
    tables = {}
    for name in {*old.tables, *new.tables}:
        cols = {}
        for s in (old, new):
            if name in s.tables:
                cols.update(s.tables[name].columns)
        pk = (new.tables.get(name) or old.tables[name]).primary_key
        tables[name] = Table.model_construct(columns=cols, primary_key=pk, unique=[])
    relations = {**old.relations, **new.relations}
    context = {**old.identity.context, **new.identity.context}
    identity = new.identity.model_copy(update={"context": context})
    return Spec.model_construct(mls_version=new.mls_version, identity=identity,
                                roles=sorted({*old.roles, *new.roles}), tables=tables,
                                relations=relations, hierarchies={**old.hierarchies, **new.hierarchies},
                                opaques={**old.opaques, **new.opaques}, rules=[], properties=[])


def changed_relations(old: Spec, new: Spec) -> set[str]:
    return {n for n, r in new.relations.items() if n in old.relations and old.relations[n] != r}


def changed_opaques(old: Spec, new: Spec) -> set[str]:
    return {n for n, o in new.opaques.items() if n in old.opaques and old.opaques[n] != o}


def encoders(old: Spec, new: Spec) -> tuple[Encoder, Encoder]:
    consts: dict[str, int] = {}
    e_old = Encoder(old, consts=consts)
    e_new = Encoder(new, {r: "new" for r in changed_relations(old, new)},
                    opaque_tags={o: "new" for o in changed_opaques(old, new)},
                    hier_tags={h: "new" for h in changed_hierarchies(old, new)}, consts=consts)
    return e_old, e_new


def _symbols(p: Pred | None, rels: set[str], opaques: set[str], spec: Spec | None = None) -> None:
    if isinstance(p, Member):
        rels.add(p.member.relation)
    elif isinstance(p, InGroup):
        if spec is not None:
            rels.add(spec.hierarchies[p.in_group.hierarchy].members)
        opaques.add(f"hierarchy:{p.in_group.hierarchy}")
    elif isinstance(p, OpaqueRef):
        opaques.add(p.opaque)
    elif isinstance(p, And):
        for a in p.and_:
            _symbols(a, rels, opaques, spec)
    elif isinstance(p, Or):
        for a in p.or_:
            _symbols(a, rels, opaques, spec)
    elif isinstance(p, Not):
        _symbols(p.not_, rels, opaques, spec)


def _same(old: Spec, new: Spec, p_old: Pred, p_new: Pred) -> bool:
    """Textually equal predicates mean the same thing unless they name a symbol whose
    definition changed between the versions."""
    if p_old != p_new:
        return False
    rels: set[str] = set()
    opaques: set[str] = set()
    _symbols(p_new, rels, opaques, new)
    hiers = {o[len("hierarchy:"):] for o in opaques if o.startswith("hierarchy:")}
    return (not (rels & changed_relations(old, new)) and not (opaques & changed_opaques(old, new))
            and not (hiers & (changed_hierarchies(old, new) | rebounded_hierarchies(old, new))))


def _shape(h):
    return (h.table, h.id, h.parent, h.depth, h.members)


def changed_hierarchies(old: Spec, new: Spec) -> set[str]:
    """Hierarchies whose tree (table, columns, members relation) differs: their parent links are
    different functions.  A change of the depth bound alone keeps the tree (see rebounded_hierarchies)."""
    return {n for n, h in new.hierarchies.items() if n in old.hierarchies and _shape(old.hierarchies[n]) != _shape(h)}


def rebounded_hierarchies(old: Spec, new: Spec) -> set[str]:
    return {n for n, h in new.hierarchies.items()
            if n in old.hierarchies and _shape(old.hierarchies[n]) == _shape(h)
            and old.hierarchies[n].max_depth != h.max_depth}


def _rels(old: Spec, new: Spec, *preds: Pred | None) -> list[tuple[str, str | None]]:
    changed = changed_relations(old, new)
    out: list[tuple[str, str | None]] = []
    for spec, tag in ((old, None), (new, "new")):
        for r, _ in _relations_of(spec, *preds):
            t = tag if (tag and r in changed) else None
            if (r, t) not in out:
                out.append((r, t))
    return out


def _common_rows(old: Spec, new: Spec, table: str, rows: dict[str, str], e_old: Encoder,
                 e_new: Encoder) -> list[Term]:
    """Stored-row constraints of both versions on every row image (rows legitimate in both worlds;
    a constraint change itself is reported by the integrity diff, not as an access change)."""
    out: list[Term] = [*relation_axioms(old, e_old), *relation_axioms(new, e_new)]
    for row in rows:
        out += [e_old.T(row_constraints(old, table), row, table),
                e_new.T(row_constraints(new, table), row, table)]
    return out


def _run(view: Spec, ob: Obligation, timeout_ms: int) -> ObligationResult:
    return run_obligation(view, ob, timeout_ms)


# ----------------------------------------------------------------------------- access
def _access_pair(old: Spec, new: Spec, view: Spec, table: str, role: str, cmd: str, which: str,
                 timeout_ms: int) -> list[AccessChange]:
    p_old = effective(old, table, role, cmd, which) if table in old.tables else Lit(lit=False)
    p_new = effective(new, table, role, cmd, which) if table in new.tables else Lit(lit=False)
    if _same(old, new, p_old, p_new):
        return [AccessChange(table, role, cmd, which, d, None, "PROVED") for d in ("granted", "denied")]
    e_old, e_new = encoders(old, new)
    img = {"select": "r", "insert": "new", "delete": "old"}.get(cmd, "old" if which == "using" else "new")
    rows = _rows_for(cmd, table)
    common = _common_rows(old, new, table, rows, e_old, e_new) if table in old.tables and table in new.tables else []
    t_old = e_old.T(p_old, img, table) if table in old.tables else FALSE  # no table: no access
    t_new = e_new.T(p_new, img, table) if table in new.tables else FALSE
    rels = _rels(old, new, p_old, p_new, row_constraints(old, table) if table in old.tables else None,
                 row_constraints(new, table) if table in new.tables else None)
    out = []
    for direction, (lhs, rhs) in (("granted", (t_new, t_old)), ("denied", (t_old, t_new))):
        ob = Obligation("diff", f"{cmd} {which} on {table} as {role}: {direction}",
                        [*common, lhs], rhs, dict(rows), rels, [e_old, e_new]).finish()
        res = _run(view, ob, timeout_ms)
        out.append(AccessChange(table, role, cmd, which, direction, res.scenario, res.verdict,
                                res.opaque_deps))
    return out


def access_diff(old: Spec, new: Spec, timeout_ms: int = 20000) -> list[AccessChange]:
    view = merged_spec(old, new)
    out = []
    for table in sorted({*old.tables, *new.tables}):
        for role in view.roles:
            for cmd, which in ACCESS_CASES:
                out.extend(_access_pair(old, new, view, table, role, cmd, which, timeout_ms))
    return out


# ----------------------------------------------------------------------------- integrity
def integrity_diff(old: Spec, new: Spec, timeout_ms: int = 20000) -> list[IntegrityChange]:
    view = merged_spec(old, new)
    out = []
    for table in sorted(set(old.tables) & set(new.tables)):
        for kind, rows, get in (("stored rows", {"r": table}, row_constraints),
                                ("changes", {"old": table, "new": table}, transition_constraints)):
            c_old, c_new = get(old, table), get(new, table)
            if _same(old, new, c_old, c_new):
                continue
            e_old, e_new = encoders(old, new)
            axioms = [*relation_axioms(old, e_old), *relation_axioms(new, e_new)]
            if kind == "changes":  # the existing row is a legitimate stored row
                axioms += [e_old.T(row_constraints(old, table), "old", table),
                           e_new.T(row_constraints(new, table), "old", table)]
            img = "r" if kind == "stored rows" else "new"
            t_old, t_new = e_old.T(c_old, img, table), e_new.T(c_new, img, table)
            rels = _rels(old, new, c_old, c_new)
            for direction, (lhs, rhs) in (("rejected", (t_old, t_new)), ("accepted", (t_new, t_old))):
                ob = Obligation("diff", f"{kind} of {table}: {direction}", [*axioms, lhs], rhs,
                                dict(rows), rels, [e_old, e_new]).finish()
                res = _run(view, ob, timeout_ms)
                out.append(IntegrityChange(table, kind, direction, res.scenario, res.verdict,
                                           res.opaque_deps))
    return out


# ----------------------------------------------------------------------------- behaviour (net effect)
NET_CASES = ("select", "insert", "update", "delete")


def _net_pair(old: Spec, new: Spec, view: Spec, table: str, role: str, cmd: str,
              timeout_ms: int) -> list[AccessChange]:
    e_old, e_new = encoders(old, new)
    in_old, in_new = table in old.tables, table in new.tables
    ps_old, t_old = _net_terms(old, table, role, cmd, e_old) if in_old else ([], FALSE)
    ps_new, t_new = _net_terms(new, table, role, cmd, e_new) if in_new else ([], FALSE)
    if in_old and in_new and all(_same(old, new, a, b) for a, b in zip(ps_old, ps_new)):
        return [AccessChange(table, role, cmd, "net", d, None, "PROVED") for d in ("granted", "denied")]
    rows = _rows_for(cmd, table)
    common: list[Term] = [*relation_axioms(old, e_old), *relation_axioms(new, e_new)]
    for row in rows:
        if row == "new":
            continue  # the written image is what is compared; existing rows are legitimate in both worlds
        if in_old:
            common.append(e_old.T(row_constraints(old, table), row, table))
        if in_new:
            common.append(e_new.T(row_constraints(new, table), row, table))
    rels = _rels(old, new, *ps_old, *ps_new)
    out = []
    for direction, (lhs, rhs) in (("granted", (t_new, t_old)), ("denied", (t_old, t_new))):
        ob = Obligation("diff", f"{cmd} on {table} as {role}, all layers: {direction}",
                        [*common, lhs], rhs, dict(rows), rels, [e_old, e_new]).finish()
        res = _run(view, ob, timeout_ms)
        out.append(AccessChange(table, role, cmd, "net", direction, res.scenario, res.verdict, res.opaque_deps))
    return out


def behaviour_diff(old: Spec, new: Spec, timeout_ms: int = 20000) -> list[AccessChange]:
    """The net-effect diff: per table × role × command, the rows the role can read or delete and
    the writes it can make, with policies, stored-row constraints and transition rules taken
    together.  A rule set that enforces a requirement at a different layer (a transition rule
    instead of a restrictive policy; an inclusion rule the insert policy already implies) has no
    net change here although the access and integrity diffs report it.  Denial by USING (the row
    is filtered) and by CHECK or a trigger (an error) are not distinguished."""
    view = merged_spec(old, new)
    return [c for table in sorted({*old.tables, *new.tables}) for role in view.roles
            for cmd in NET_CASES for c in _net_pair(old, new, view, table, role, cmd, timeout_ms)]


# ----------------------------------------------------------------------------- grants
def _fmt(g) -> str | None:
    if g is None:
        return None
    return "all columns" if g == "*" else "columns " + ", ".join(sorted(g))


def grant_diff(old: Spec, new: Spec) -> list[GrantChange]:
    po, pn = privileges(old), privileges(new)
    out = []
    for key in sorted({*po, *pn}):
        go, gn = po.get(key, {}), pn.get(key, {})
        for cmd in ("select", "insert", "update", "delete"):
            if go.get(cmd) != gn.get(cmd):
                out.append(GrantChange(key[0], key[1], cmd, _fmt(go.get(cmd)), _fmt(gn.get(cmd))))
    return out


# ----------------------------------------------------------------------------- entry
def diff(old: Spec, new: Spec, timeout_ms: int = 20000) -> DiffResult:
    notes = []
    if old.identity.adapter != new.identity.adapter:
        notes.append(f"identity adapter changed from {old.identity.adapter} to {new.identity.adapter}; "
                     f"context values are compared by name")
    for n, cv in new.identity.context.items():
        o = old.identity.context.get(n)
        if o is not None and o != cv:
            notes.append(f"context value '{n}' changed ({o.source}:{o.type} → {cv.source}:{cv.type}); "
                         f"treated as the same value")
    for h in sorted(rebounded_hierarchies(old, new)):
        notes.append(f"hierarchy '{h}': the depth bound changed from {old.hierarchies[h].max_depth} to "
                     f"{new.hierarchies[h].max_depth} (same tree, unrolled to the new bound)")
    for h in sorted(changed_hierarchies(old, new)):
        notes.append(f"hierarchy '{h}' is defined differently in the two versions (its parent links "
                     f"are treated as a different tree)")
    for r in sorted(changed_relations(old, new)):
        notes.append(f"relation '{r}' is defined differently in the two versions and is treated as "
                     f"a different relation")
    for o in sorted(changed_opaques(old, new)):
        notes.append(f"opaque block '{o}' has different SQL in the two versions and is treated as a "
                     f"different, unverified predicate")
    for t in sorted(set(new.tables) - set(old.tables)):
        notes.append(f"table '{t}' is new")
    for t in sorted(set(old.tables) - set(new.tables)):
        notes.append(f"table '{t}' is removed")
    return DiffResult(access_diff(old, new, timeout_ms), integrity_diff(old, new, timeout_ms),
                      grant_diff(old, new), notes)


# ----------------------------------------------------------------------------- reports
def to_json(old: Spec, new: Spec, d: DiffResult) -> dict:
    from mls import english

    view = merged_spec(old, new)
    return {
        "notes": d.notes,
        "assumptions": english.identity_lines(new) + [f"- {a}" for a in english.assumption_lines(new)],
        "summary": {"access_changes": len(d.access_changes), "integrity_changes": len(d.integrity_changes),
                    "grant_changes": len(d.grants), "unknown": len(d.unknown)},
        "access": [
            {"table": a.table, "role": a.role, "command": a.command, "which": a.which,
             "direction": a.direction, "english": _access_english(view, a),
             "before": _access_terse(old, new, a)[0], "after": _access_terse(old, new, a)[1],
             "scenario": a.scenario.to_dict() if a.scenario else None,
             "scenario_english": english.scenario(view, a.scenario) if a.scenario else None,
             "conditional_on": a.opaque_deps, "verdict": a.verdict}
            for a in d.access if a.verdict != "PROVED"
        ],
        "integrity": [
            {"table": i.table, "kind": i.kind, "direction": i.direction,
             "english": _integrity_english(i),
             "before": _integrity_terse(old, new, i)[0], "after": _integrity_terse(old, new, i)[1],
             "scenario": i.scenario.to_dict() if i.scenario else None,
             "scenario_english": english.scenario(view, i.scenario) if i.scenario else None,
             "conditional_on": i.opaque_deps, "verdict": i.verdict}
            for i in d.integrity if i.verdict != "PROVED"
        ],
        "grants": [{"role": g.role, "table": g.table, "command": g.command, "before": g.before,
                    "after": g.after} for g in d.grants],
    }


def _access_terse(old: Spec, new: Spec, a: AccessChange) -> tuple[str, str]:
    """The effective predicate before and after, terse (D44); `false` where the table or role has
    no policy."""
    from mls import terse

    def eff(spec: Spec) -> str:
        if a.table not in spec.tables:
            return "false"
        return terse.pred(effective(spec, a.table, a.role, a.command, a.which))
    return eff(old), eff(new)


def _integrity_terse(old: Spec, new: Spec, i: IntegrityChange) -> tuple[str, str]:
    from mls import terse

    def cons(spec: Spec) -> str:
        if i.table not in spec.tables:
            return "-"
        p = row_constraints(spec, i.table) if i.kind == "stored rows" else transition_constraints(spec, i.table)
        return terse.pred(p)
    return cons(old), cons(new)


def _access_english(view: Spec, a: AccessChange) -> str:
    from mls.english import VERB

    what = {"using": "rows", "check": "new rows"}[a.which]
    if a.command == "update":
        what = "existing rows" if a.which == "using" else "resulting rows"
    if a.direction == "granted":
        return f"{a.role} can now {VERB[a.command]} {what} of {a.table} that were not allowed before"
    return f"{a.role} can no longer {VERB[a.command]} some {what} of {a.table} that were allowed before"


def _integrity_english(i: IntegrityChange) -> str:
    if i.kind == "stored rows":
        return (f"some rows of {i.table} that were valid before are now rejected" if i.direction == "rejected"
                else f"some rows of {i.table} that were rejected before are now accepted")
    return (f"some changes to rows of {i.table} that were allowed before are now rejected" if i.direction == "rejected"
            else f"some changes to rows of {i.table} that were rejected before are now allowed")


def to_markdown(old: Spec, new: Spec, d: DiffResult) -> str:
    from mls import english

    view = merged_spec(old, new)
    lines = ["# MLS access diff", ""]
    s = to_json(old, new, d)["summary"]
    lines.append(f"**{s['access_changes']} access change(s), {s['integrity_changes']} integrity "
                 f"change(s), {s['grant_changes']} privilege change(s), {s['unknown']} undecided**")
    if d.notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in d.notes]
    lines += ["", "## Assumptions", "", *english.identity_lines(new),
              *[f"- {a}" for a in english.assumption_lines(new)]]
    lines += ["", "## Access changes", ""]
    shown = [a for a in d.access if a.verdict != "PROVED"]
    if not shown:
        lines.append("None: every role can read and change exactly the rows it could before.")
    for a in shown:
        tag = " (undecided: solver timeout)" if a.verdict == "UNKNOWN" else ""
        cond = f" — conditional on unverified SQL: {', '.join(a.opaque_deps)}" if a.opaque_deps else ""
        lines.append(f"- {_access_english(view, a)}{tag}{cond}")
        before, after = _access_terse(old, new, a)
        lines.append(f"  - before: `{before}`")
        lines.append(f"  - after: `{after}`")
        if a.scenario:
            lines.append(f"  - for example: {english.scenario(view, a.scenario)}")
    lines += ["", "## Integrity changes", ""]
    shown_i = [i for i in d.integrity if i.verdict != "PROVED"]
    if not shown_i:
        lines.append("None: the same rows and changes are accepted.")
    for i in shown_i:
        tag = " (undecided: solver timeout)" if i.verdict == "UNKNOWN" else ""
        cond = f" — conditional on unverified SQL: {', '.join(i.opaque_deps)}" if i.opaque_deps else ""
        lines.append(f"- {_integrity_english(i)}{tag}{cond}")
        before, after = _integrity_terse(old, new, i)
        lines.append(f"  - before: `{before}`")
        lines.append(f"  - after: `{after}`")
        if i.scenario:
            lines.append(f"  - for example: {english.scenario(view, i.scenario)}")
    lines += ["", "## Privilege changes", ""]
    if not d.grants:
        lines.append("None.")
    for g in d.grants:
        lines.append(f"- {g.role} on {g.table}, {g.command}: {g.before or 'nothing'} → {g.after or 'nothing'}")
    return "\n".join(lines) + "\n"
