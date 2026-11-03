"""Solver-graded synthetic mutations of a reference spec (D6): each mutation weakens or removes
a rule; the properties either catch it (some verdict changes: a new eval case whose expected
outcome is escalation, and a repair benchmark) or miss it (reported as a coverage gap of the
property set, like a mutation score)."""
from __future__ import annotations

import copy
from dataclasses import dataclass

from mls.errors import SpecError
from mls.props.runner import run_all
from mls.spec import check_spec
from mls.spec.model import Spec


@dataclass
class Mutant:
    name: str
    data: dict
    verdicts: dict[str, str] | None = None   # None: the mutant does not check
    detected: bool = False


def _drop_conjunct(pred: dict) -> dict | None:
    if isinstance(pred, dict) and "and" in pred and len(pred["and"]) >= 2:
        return {"and": pred["and"][:-1]} if len(pred["and"]) > 2 else pred["and"][0]
    return None


def mutants(data: dict) -> list[Mutant]:
    out = []
    for i, r in enumerate(data.get("rules", [])):
        rid = r.get("id", str(i))
        if r["kind"] != "column_rule":
            d = copy.deepcopy(data)
            del d["rules"][i]
            props = []
            for p in d.get("properties", []):
                if p.get("template") == "invariant_preserved":
                    p["invariants"] = [inv for inv in p["invariants"] if inv.get("rule") != rid]
                    if not p["invariants"]:
                        continue
                props.append(p)
            d["properties"] = props
            out.append(Mutant(f"drop:{rid}", d))
        if r["kind"] == "policy":
            if r["mode"] == "restrictive":
                d = copy.deepcopy(data)
                d["rules"][i]["mode"] = "permissive"
                out.append(Mutant(f"permissive:{rid}", d))
            for key in ("using", "check"):
                weaker = _drop_conjunct(r.get(key))
                if weaker is not None:
                    d = copy.deepcopy(data)
                    d["rules"][i][key] = weaker
                    out.append(Mutant(f"loosen_{key}:{rid}", d))
        if r["kind"] in ("check", "transition"):
            weaker = _drop_conjunct(r.get("predicate"))
            if weaker is not None:
                d = copy.deepcopy(data)
                d["rules"][i]["predicate"] = weaker
                out.append(Mutant(f"loosen:{rid}", d))
        if r["kind"] == "cardinality":
            d = copy.deepcopy(data)
            d["rules"][i]["max"] = r["max"] + 1
            out.append(Mutant(f"raise:{rid}", d))
    return out


def portable(data: dict) -> dict:
    """The spec with its properties re-based on predicates instead of rule ids, so that dropping a
    rule leaves the property set intact (an invariant whose CHECK is dropped is then refuted, i.e.
    detected, instead of silently forgotten) and the same properties apply to any rule set."""
    from mls.llm.grade import portable_properties
    return {**data, "properties": portable_properties(Spec.model_validate(data))}


def grade_mutants(data: dict, base_verdicts: dict[str, str], timeout_ms: int = 20000) -> list[Mutant]:
    out = []
    for m in mutants(portable(data)):
        try:
            spec = Spec.model_validate(m.data)
            check_spec(spec)
        except (SpecError, ValueError):
            continue  # a mutant the checker rejects is not a candidate (an LLM could not emit it)
        m.verdicts = {r.id: r.verdict for r in run_all(spec, timeout_ms)}
        # detected = a property the base proves no longer proves (the mutant weakened something).
        # A mutant that makes a refuted property prove (dropping a planted leak) is a strengthening:
        # there is nothing to repair, so it is not a case; whether a lost grant is caught is the
        # liveness gap (D39).
        m.detected = any(v == "PROVED" and m.verdicts.get(k) != "PROVED"
                         for k, v in base_verdicts.items() if k in m.verdicts)
        out.append(m)
    return out
