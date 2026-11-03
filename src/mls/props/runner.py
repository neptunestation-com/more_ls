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


def run_obligation(spec: Spec, ob: Obligation, timeout_ms: int = 20000, keep_smt: bool = False) -> ObligationResult:
    if ob.propositional:
        return ObligationResult(ob.name, ob.verdict or "UNKNOWN", message=ob.message)
    res = solve(ob.assumptions, ob.goal, timeout_ms)
    deps = sorted(ob.opaque_deps())
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


def run_property(spec: Spec, prop: Property, timeout_ms: int = 20000, keep_smt: bool = False) -> PropertyResult:
    obs = obligations_for(spec, prop)
    results = [run_obligation(spec, ob, timeout_ms, keep_smt) for ob in obs]
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
