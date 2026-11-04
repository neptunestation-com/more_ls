"""Grading a loop result against an eval case (D6, D38): the generated spec must check, its
properties must be proved, the reference properties must have their expected verdicts on the
generated rules, and the generated rules must let every role do exactly what the reference lets it
do (the net-effect diff of Milestone 2's solver diff, plus the grant diff).  The layer-by-layer
access/integrity diff is reported too but does not decide correctness: enforcing a requirement at
another layer is not a mistake.  Metrics: outcome match, spec correctness, repair rounds, human
escalation."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from mls.errors import SpecError
from mls.llm.loop import LoopResult
from mls.props.diff import behaviour_diff, diff, grant_diff
from mls.props.runner import run_all
from mls.spec import check_spec
from mls.props.effective import inclusion_pred
from mls.spec.model import InclusionRule, InvariantPreserved, Spec


@dataclass
class CaseGrade:
    case_id: str
    expected_status: str
    status: str
    outcome_match: bool
    checked: bool
    all_proved: bool
    reference_properties: dict[str, str] = field(default_factory=dict)   # id -> verdict on the generated rules
    reference_properties_hold: bool | None = None
    equivalent: bool | None = None          # layer by layer: no access / integrity / grant change
    differences: list[str] = field(default_factory=list)
    net_equivalent: bool | None = None      # every role can do exactly the same things (behaviour_diff + grants)
    net_differences: list[str] = field(default_factory=list)
    repair_rounds: int = 0
    escalated: bool = False
    correct: bool = False                   # outcome match; for proved cases also reference properties + net equivalence

    def to_dict(self) -> dict:
        return asdict(self)


def expected_status(case: dict) -> str:
    """`proved` unless the case says otherwise: a case's requirements are self-consistent, so the
    loop should prove its own properties; what the *reference* properties say about the generated
    rules (`expected_verdicts`, REFUTED included) is checked separately."""
    return case.get("expected_loop", {}).get("status") or "proved"


def portable_properties(reference: Spec) -> list[dict]:
    """The reference properties with rule references replaced by their predicates, so they can be
    evaluated on a different rule set (rule ids are not shared)."""
    out = []
    for p in reference.properties:
        d = p.model_dump(by_alias=True, exclude_none=True)
        if isinstance(p, InvariantPreserved):
            invs = []
            for inv in p.invariants:
                if inv.rule is None:
                    invs.append(inv.model_dump(exclude_none=True))
                    continue
                r = reference.rule(inv.rule)
                pred = getattr(r, "predicate", None)
                if pred is None and isinstance(r, InclusionRule):
                    pred = inclusion_pred(reference, r)  # the FK as a predicate (NULLs pass)
                if pred is not None:
                    invs.append({"table": r.table, "predicate": pred.model_dump(by_alias=True, exclude_none=True)})
            if not invs:
                continue
            d["invariants"] = invs
        out.append(d)
    return out


def grade(case: dict, result: LoopResult, reference: Spec, timeout_ms: int = 20000) -> CaseGrade:
    exp = expected_status(case)
    g = CaseGrade(case["id"], exp, result.status, result.status == exp, result.spec is not None and not result.errors,
                  result.status == "proved", repair_rounds=result.repair_rounds, escalated=result.escalated)
    if result.spec is None or result.status in ("invalid", "failed"):
        return g
    gen = result.spec
    # the reference properties on the generated rules
    try:
        data = gen.model_dump(by_alias=True, exclude_none=True)
        data["properties"] = portable_properties(reference)
        probe = Spec.model_validate(data)
        probe = check_spec(probe)
        g.reference_properties = {r.id: r.verdict for r in run_all(probe, timeout_ms)}
        expected = case.get("expected_verdicts", {})
        g.reference_properties_hold = all(g.reference_properties.get(k) == v for k, v in expected.items()
                                          if k in g.reference_properties)
    except (SpecError, Exception) as e:  # noqa: BLE001 - a probe that does not check is a mismatch
        g.reference_properties_hold = False
        g.differences.append(f"reference properties do not apply to the generated schema: {e}")
    # equivalence by solver diff
    try:
        d = diff(reference, gen, timeout_ms)
        g.differences += [f"access: {a.table} {a.role} {a.command} {a.which} {a.direction}" for a in d.access_changes]
        g.differences += [f"integrity: {i.table} {i.kind} {i.direction}" for i in d.integrity_changes]
        g.differences += [f"grant: {x.role} {x.table} {x.command} {x.before} -> {x.after}" for x in d.grants]
        g.equivalent = not d.any_change
    except Exception as e:  # noqa: BLE001
        g.equivalent = False
        g.differences.append(f"diff failed: {e}")
    # net equivalence: what each role can do, all layers together
    try:
        net = behaviour_diff(reference, gen, timeout_ms)
        g.net_differences = [f"{a.table} {a.role} {a.command} {a.direction}" + ("" if a.changed else f" ({a.verdict})")
                             for a in net if a.verdict != "PROVED"]
        g.net_differences += [f"grant: {x.role} {x.table} {x.command} {x.before} -> {x.after}"
                              for x in grant_diff(reference, gen)]
        g.net_equivalent = not g.net_differences
    except Exception as e:  # noqa: BLE001
        g.net_equivalent = False
        g.net_differences.append(f"diff failed: {e}")
    g.correct = g.outcome_match and (exp != "proved" or
                                     (g.reference_properties_hold is True and g.net_equivalent is True))
    return g


def summarize(grades: list[CaseGrade]) -> dict[str, Any]:
    n = len(grades)
    proved_cases = [g for g in grades if g.expected_status == "proved"]
    def rate(xs, pred):
        return (sum(1 for x in xs if pred(x)) / len(xs)) if xs else None
    return {
        "cases": n,
        "outcome_match_rate": rate(grades, lambda g: g.outcome_match),
        "spec_correctness_rate": rate(grades, lambda g: g.correct),
        "net_equivalence_rate": rate(proved_cases, lambda g: g.net_equivalent is True),
        "equivalence_rate": rate(proved_cases, lambda g: g.equivalent is True),
        "reference_properties_hold_rate": rate(proved_cases, lambda g: g.reference_properties_hold is True),
        "mean_repair_rounds": (sum(g.repair_rounds for g in grades) / n) if n else None,
        "human_escalation_rate": rate(grades, lambda g: g.escalated),
        "failed": [g.case_id for g in grades if not g.correct],
    }
