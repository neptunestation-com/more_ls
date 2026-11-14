"""Run proof obligations and assemble property results."""
from __future__ import annotations

from dataclasses import dataclass, field

from mls.logic.solve import solve
from mls.props.obligations import Obligation, obligations_for
from mls.props.scenario import Scenario, render_scenario
from mls.spec.model import Property, Spec


@dataclass
class ObligationResult:
    name: str
    verdict: str  # PROVED | REFUTED | UNKNOWN
    scenario: Scenario | None = None
    message: str = ""
    example: Scenario | None = None   # a witness obligation's model: a row the property admits
    opaque_deps: list[str] = field(default_factory=list)
    smtlib: str = ""


@dataclass
class PropertyResult:
    id: str
    template: str
    description: str | None
    verdict: str
    obligations: list[ObligationResult]
    conditional_on: list[str]


def occurring_opaques(ob: Obligation) -> list[str]:
    """The blocks whose symbols occur in the obligation's own terms: an encoder shared between
    obligations (a lower bound and its witness) records every block it encoded for either (#27)."""
    from mls.logic.term import collect

    axioms = set().union(*(e.opaque_axioms for e in ob.encoders))  # well-formedness, not a use
    vars, funs = {}, {}
    for t in [*ob.assumptions, ob.goal]:
        if t not in axioms:
            collect(t, vars, funs)
    names = set(vars) | set(funs)
    return sorted(d for d in ob.opaque_deps()
                  if any(n.startswith((f"opaque.{d}.", f"opaque.{d}@")) for n in names))


def run_obligation(spec: Spec, ob: Obligation, timeout_ms: int = 20000, keep_smt: bool = False) -> ObligationResult:
    if ob.propositional:
        return ObligationResult(ob.name, ob.verdict or "UNKNOWN", message=ob.message)
    res = solve(ob.assumptions, ob.goal, timeout_ms)
    deps = occurring_opaques(ob)
    smt = res.smtlib() if keep_smt else ""
    if ob.witness:  # goal is FALSE: "refuted" means the assumptions have a model, i.e. a witness
        if res.status == "refuted":
            return ObligationResult(ob.name, "PROVED", example=render_scenario(spec, ob, res.model),
                                    opaque_deps=deps, smtlib=smt)
        if res.proved:
            return ObligationResult(ob.name, "REFUTED", message="no row can meet the condition (together with "
                                    "the table's constraints), so the property would require nothing",
                                    opaque_deps=deps, smtlib=smt)
        return ObligationResult(ob.name, "UNKNOWN", message="solver timeout", opaque_deps=deps, smtlib=smt)
    if res.proved:
        return ObligationResult(ob.name, "PROVED", opaque_deps=deps, smtlib=smt)
    if res.status == "refuted":
        return ObligationResult(ob.name, "REFUTED", render_scenario(spec, ob, res.model),
                                opaque_deps=deps, smtlib=smt)
    return ObligationResult(ob.name, "UNKNOWN", message="solver timeout", opaque_deps=deps, smtlib=smt)


def needed_opaques(spec: Spec, prop: Property, name: str, deps: list[str], timeout_ms: int) -> list[str]:
    """The blocks a PROVED obligation's proof needs (#27, REFERENCE §8): a block it still proves
    without, every occurrence an independent unconstrained value, is dropped.  Freeing a block
    over-approximates whatever its SQL does, so a proof that survives holds for every block."""
    from mls.logic.tvl import free_opaques

    needed = []
    for d in deps:
        with free_opaques({d}):
            ob = next((o for o in obligations_for(spec, prop) if o.name == name), None)
            res = solve(ob.assumptions, ob.goal, timeout_ms) if ob is not None else None
        if res is None or not res.proved:
            needed.append(d)
    return needed


def run_property(spec: Spec, prop: Property, timeout_ms: int = 20000, keep_smt: bool = False) -> PropertyResult:
    obs = obligations_for(spec, prop)
    results = [run_obligation(spec, ob, timeout_ms, keep_smt) for ob in obs]
    for ob, r in zip(obs, results):
        if r.verdict == "PROVED" and r.opaque_deps and not ob.witness and not ob.propositional:
            r.opaque_deps = needed_opaques(spec, prop, ob.name, r.opaque_deps, timeout_ms)
    if any(r.verdict == "REFUTED" for r in results):
        verdict = "REFUTED"
    elif any(r.verdict == "UNKNOWN" for r in results):
        verdict = "UNKNOWN"
    else:
        verdict = "PROVED"
    deps = sorted({d for r in results for d in r.opaque_deps})
    return PropertyResult(prop.id, prop.template, prop.description, verdict, results, deps)


def run_all(spec: Spec, timeout_ms: int = 20000, keep_smt: bool = False) -> list[PropertyResult]:
    return [run_property(spec, p, timeout_ms, keep_smt) for p in spec.properties]
