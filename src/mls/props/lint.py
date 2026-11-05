"""Policy-set hygiene (D49): findings of `mls lint`, never verdicts of `mls prove`.

- coverage: a column rule whose role has no permissive policy for the access it grants (the
  grant is dead); a governed table on which no role has any permissive policy; an `observed`
  table a selector would match (D48) or that has the column a `tenant_isolation` property names.
- contradiction: a policy whose `using` (or `check`) no stored row can satisfy: it never admits
  a row (the witness obligation of D40 applied to every policy).
- subsumption: permissive `p1`, `p2` on the same table and role, `p2`'s commands within
  `p1`'s, with `T(p2) → T(p1)` valid under the stored-row constraints on the (T, F) encoding.
  The two-valued check (`p2 ∧ ¬p1` UNSAT) is wrong where `p1` is UNKNOWN on a row `p2` admits.
- redundancy: a policy whose removal leaves the effective predicate (`props/effective.py`)
  equivalent for every command and side it takes part in, on its table and on the tables of
  the relations it reads.  Policies already reported as subsumed are not checked again.

A finding whose proof relies on an opaque block is conditional on it, as proofs are.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from mls.logic.solve import solve
from mls.logic.term import FALSE, Term, and_, implies
from mls.logic.tvl import Encoder
from mls.props.effective import all_policies, effective, policy_expr, relations_of, row_constraints
from mls.props.obligations import relation_axioms
from mls.spec.model import PolicyRule, Pred, Spec, TenantIsolation
from mls.spec.selectors import phrase, selected

COMMANDS = ("select", "insert", "update", "delete")
KINDS = ("coverage", "contradiction", "subsumption", "redundancy")


@dataclass
class Finding:
    kind: str
    table: str
    message: str
    rules: list[str] = field(default_factory=list)
    conditional_on: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        cond = f" (conditional on opaque block{'s' if len(self.conditional_on) > 1 else ''} " \
               f"{', '.join(self.conditional_on)})" if self.conditional_on else ""
        return f"{self.kind}: {self.message}{cond}"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "table": self.table, "message": self.message, "rules": self.rules,
                "conditional_on": self.conditional_on}


def _label(p: PolicyRule) -> str:
    return f"policy {p.id} ({p.name})"


def _commands(p: PolicyRule) -> tuple[str, ...]:
    return COMMANDS if p.command == "all" else (p.command,)


def _sides(cmd: str) -> list[tuple[str, str]]:
    """(which, row image) pairs a command evaluates."""
    return {"select": [("using", "r")], "delete": [("using", "old")], "insert": [("check", "new")],
            "update": [("using", "old"), ("check", "new")]}[cmd]


def _rows(cmd: str) -> list[str]:
    return [img for _, img in _sides(cmd)]


class _Prover:
    """Validity under the stored-row constraints, on the three-valued encoding."""

    def __init__(self, spec: Spec, timeout_ms: int):
        self.spec = spec
        self.timeout_ms = timeout_ms

    def base(self, enc: Encoder, table: str, rows: list[str]) -> list[Term]:
        return [*relation_axioms(self.spec, enc),
                *[enc.T(row_constraints(self.spec, table), r, table) for r in rows]]

    def unsat(self, enc: Encoder, assumptions: list[Term]) -> bool | None:
        res = solve(assumptions + enc.side_conditions, FALSE, self.timeout_ms)
        return None if res.status == "unknown" else res.proved

    def valid(self, enc: Encoder, assumptions: list[Term], goal: Term) -> bool | None:
        res = solve(assumptions + enc.side_conditions, goal, self.timeout_ms)
        return None if res.status == "unknown" else res.proved


# ----------------------------------------------------------------------------- coverage
def coverage(spec: Spec) -> list[Finding]:
    out: list[Finding] = []
    perm = {(p.table, p.role, c) for p in all_policies(spec) if p.mode == "permissive" for c in _commands(p)}
    for cr in spec.column_rules():
        cmds = ("select",) if cr.access == "read" else ("insert", "update")
        if not any((cr.table, cr.role, c) in perm for c in cmds):
            out.append(Finding("coverage", cr.table,
                               f"column rule {cr.id}{f' ({cr.name})' if cr.name else ''} limits what {cr.role} may "
                               f"{cr.access} on "
                               f"{cr.table}, but no permissive policy lets {cr.role} "
                               f"{' or '.join(cmds)} there: the grant is dead", [cr.id]))
    for name in spec.governed():
        if not any(t == name for t, _, _ in perm):
            out.append(Finding("coverage", name,
                               f"no role has a permissive policy on {name}: row level security is enabled and "
                               f"nobody can select, insert, update or delete"))
    observed = [n for n, t in spec.tables.items() if t.mode == "observed"]
    if observed:
        seen: set[str] = set()
        for r in spec.rules:
            if r.from_ is None or r.applies_to is None or r.from_ in seen:
                continue
            seen.add(r.from_)
            for t in selected(r.applies_to, spec, observed=True):
                if t in observed:
                    out.append(Finding("coverage", t,
                                       f"observed table {t} matches the selector of rule {r.from_} "
                                       f"({phrase(r.applies_to)}) but is not governed: set its mode to "
                                       f"\"adopted\" to apply the rule, or leave it observed", [r.from_]))
        for p in spec.properties:
            if isinstance(p, TenantIsolation):
                for t in observed:
                    if p.column in spec.tables[t].columns:
                        out.append(Finding("coverage", t,
                                           f"observed table {t} has column {p.column}, which property {p.id} "
                                           f"names as the tenant column of {p.table}: is it tenant-scoped too?"))
    return out


# ----------------------------------------------------------------------------- contradiction
def contradiction(spec: Spec, prover: _Prover) -> list[Finding]:
    out: list[Finding] = []
    for p in spec.policies():
        sides: list[tuple[str, str, Pred]] = []
        if p.command != "insert" and p.using is not None:
            sides.append(("using", "r" if p.command == "select" else "old", p.using))
        if p.command in ("insert", "update", "all") and p.check is not None:
            sides.append(("check", "new", p.check))
        if p.command == "insert" and p.check is None and p.using is not None:
            sides.append(("check", "new", p.using))
        for which, img, expr in sides:
            enc = Encoder(spec)
            res = prover.unsat(enc, [*prover.base(enc, p.table, [img]), enc.T(expr, img, p.table)])
            if res:
                out.append(Finding("contradiction", p.table,
                                   f"{_label(p)} never admits a row: its {which} cannot be TRUE on any row "
                                   f"that satisfies the constraints of {p.table}", [p.id], sorted(enc.opaque_deps)))
    return out


# ----------------------------------------------------------------------------- subsumption
def _implied(spec: Spec, prover: _Prover, table: str, weaker: PolicyRule, stronger: PolicyRule,
             cmds: tuple[str, ...]) -> tuple[bool | None, set[str]]:
    """T(stronger) → T(weaker) on every side the commands evaluate, under the constraints."""
    deps: set[str] = set()
    sides = {(which, img) for c in cmds for which, img in _sides(c)}
    for which, img in sorted(sides):
        s, w = policy_expr(stronger, which), policy_expr(weaker, which)
        if s is None or w is None:
            return False, deps
        enc = Encoder(spec)
        ok = prover.valid(enc, [*prover.base(enc, table, [img]), enc.T(s, img, table)], enc.T(w, img, table))
        deps |= enc.opaque_deps
        if not ok:
            return ok, deps
    return True, deps


def subsumption(spec: Spec, prover: _Prover, skip: set[str] = frozenset()) -> list[Finding]:
    out: list[Finding] = []
    perm = [p for p in spec.policies() if p.mode == "permissive" and p.id not in skip]
    done: set[tuple[str, str]] = set()
    subsumed: set[str] = set()  # reported once: by the first policy that subsumes it
    for i, p1 in enumerate(perm):
        for p2 in perm[i + 1:]:
            if (p1.table, p1.role) != (p2.table, p2.role) or p1.id in subsumed or p2.id in subsumed:
                continue
            c1, c2 = set(_commands(p1)), set(_commands(p2))
            # the policy whose commands lie within the other's is the candidate
            pairs = [(p1, p2)] if c2 <= c1 else []
            if c1 <= c2:
                pairs.append((p2, p1))
            for wide, narrow in pairs:
                if (wide.id, narrow.id) in done:
                    continue
                ok, deps = _implied(spec, prover, p1.table, wide, narrow, _commands(narrow))
                if ok:
                    done.add((wide.id, narrow.id))
                    subsumed.add(narrow.id)
                    back, _ = _implied(spec, prover, p1.table, narrow, wide, _commands(narrow)) \
                        if _commands(wide) == _commands(narrow) else (False, set())
                    if back:
                        done.add((narrow.id, wide.id))
                        subsumed.add(wide.id)
                        msg = (f"{_label(narrow)} and {_label(wide)} on {p1.table} admit the same rows for "
                               f"{p1.role}: one of them is enough")
                    else:
                        msg = (f"{_label(narrow)} is subsumed by {_label(wide)} on {p1.table}: every row it "
                               f"admits to {p1.role}, the other admits too")
                    out.append(Finding("subsumption", p1.table, msg, [narrow.id, wide.id], sorted(deps)))
                    break
    return out


# ----------------------------------------------------------------------------- redundancy
def _equivalent(spec: Spec, without: Spec, prover: _Prover, table: str, role: str, cmd: str, which: str,
                img: str) -> tuple[bool | None, set[str]]:
    e1, e2 = effective(spec, table, role, cmd, which), effective(without, table, role, cmd, which)
    if e1 == e2:
        return True, set()
    enc = Encoder(spec)
    base = prover.base(enc, table, [img])
    t1, t2 = enc.T(e1, img, table), enc.T(e2, img, table)
    ok = prover.valid(enc, base, and_(implies(t1, t2), implies(t2, t1)))
    return ok, set(enc.opaque_deps)


def redundancy(spec: Spec, prover: _Prover, skip: set[str] = frozenset()) -> list[Finding]:
    out: list[Finding] = []
    for p in spec.policies():
        if p.id in skip:
            continue
        without = spec.model_copy(update={"rules": [r for r in spec.rules if r.id is not p.id and r.id != p.id]})
        cases = [(p.table, c, which, img) for c in _commands(p) for which, img in _sides(c)]
        for rel in sorted(relations_of(spec, p.using) | relations_of(spec, p.check)):
            cases.append((spec.relations[rel].table, "select", "using", "r"))
        deps: set[str] = set()
        redundant = True
        for table, cmd, which, img in cases:
            ok, d = _equivalent(spec, without, prover, table, p.role, cmd, which, img)
            deps |= d
            if not ok:
                redundant = False
                break
        if redundant:
            what = ", ".join(_commands(p))
            out.append(Finding("redundancy", p.table,
                               f"{_label(p)} on {p.table} is redundant: without it {p.role} can {what} exactly "
                               f"the same rows", [p.id], sorted(deps)))
    return out


# ----------------------------------------------------------------------------- all
def lint_spec(spec: Spec, timeout_ms: int = 20000) -> list[Finding]:
    """Every hygiene finding, coverage first, then the solver-backed ones."""
    prover = _Prover(spec, timeout_ms)
    out = coverage(spec)
    con = contradiction(spec, prover)
    sub = subsumption(spec, prover, {r for f in con for r in f.rules})  # FALSE is within anything
    out += con + sub
    # a contradictory policy is trivially redundant, a subsumed or equivalent one was reported
    out += redundancy(spec, prover, {r for f in con + sub for r in f.rules})
    return out
