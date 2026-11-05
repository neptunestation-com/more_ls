"""Eval case format (D6; corpus built in Milestone 4).

A case is a JSON file:
  {"id": "...", "requirements": "plain-English business requirements",
   "expected_properties": ["plain-English property statements the LLM should produce"],
   "reference_spec": "relative/path.mls.json"  (or an inline spec object),
   "expected_verdicts": {"P1": "PROVED", ...},
   "expected_check_errors": [{"path": "$.rules[3]", "contains": "negated membership"}]  (optional)}
A case passes when the reference spec checks (or fails exactly as expected) and every listed
property has the expected verdict.

Milestone 4 additions: "skeleton" (the schema both LLM passes are given; default: the reference
spec without rules and properties), "expected_loop": {"status": "proved" | "escalated"} (default:
proved iff every expected verdict is PROVED), "origin": "seed" | "synthetic", and for synthetic
cases "base_case" and "mutation".  `run_llm_case` runs the loop on the requirements and grades the
result (mls/llm/grade.py); `synthetic_cases` writes solver-graded mutants of a case.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from mls.errors import SpecError
from mls.spec import check_spec
from mls.spec.model import Spec


@dataclass
class CaseResult:
    case_id: str
    ok: bool
    message: str


def load_case(path: Path) -> dict:
    return json.loads(path.read_text())


def run_case(path: Path) -> CaseResult:
    from mls.props.runner import run_all

    case = load_case(path)
    ref = case["reference_spec"]
    data = ref if isinstance(ref, dict) else json.loads((path.parent / ref).read_text())
    spec = Spec.model_validate(data)
    expected_errors = case.get("expected_check_errors")
    try:
        spec = check_spec(spec)
    except SpecError as e:
        if not expected_errors:
            return CaseResult(case["id"], False, "unexpected check errors: " + "; ".join(str(x) for x in e.errors))
        for exp in expected_errors:
            if not any(x.path == exp["path"] and exp["contains"] in x.message for x in e.errors):
                return CaseResult(case["id"], False, f"expected error at {exp['path']} containing "
                                                     f"{exp['contains']!r}; got: " + "; ".join(str(x) for x in e.errors))
        return CaseResult(case["id"], True, "check errors as expected")
    if expected_errors:
        return CaseResult(case["id"], False, "expected check errors but the spec checked clean")
    results = {r.id: r.verdict for r in run_all(spec)}
    bad = [f"{k}: expected {v}, got {results.get(k)}" for k, v in case.get("expected_verdicts", {}).items()
           if results.get(k) != v]
    if case.get("start_rules") is not None and case.get("mutant_verdicts"):
        # a synthetic case: the mutant rules must give the mutant verdicts under the reference properties
        from mls.llm.mutate import portable
        mutant = Spec.model_validate({**portable(data), "rules": case["start_rules"]})
        mutant = check_spec(mutant)
        got = {r.id: r.verdict for r in run_all(mutant)}
        bad += [f"mutant {k}: expected {v}, got {got.get(k)}" for k, v in case["mutant_verdicts"].items()
                if got.get(k) != v]
    if bad:
        return CaseResult(case["id"], False, "; ".join(bad))
    return CaseResult(case["id"], True, f"{len(results)} properties as expected")


def reference_spec(case: dict, path: Path) -> Spec:
    ref = case["reference_spec"]
    data = ref if isinstance(ref, dict) else json.loads((path.parent / ref).read_text())
    return Spec.model_validate(data)


def case_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        files.extend(sorted(p.rglob("*.json")) if p.is_dir() else [p])
    return [f for f in files if "specs" not in f.parts]


def generation_case(case: dict, repair: bool) -> str | None:
    """Why a case is not run by `mls eval --llm` (None: it is run)."""
    if case.get("expected_check_errors"):
        return "its reference spec does not check (a checker case)"
    if case.get("origin") == "synthetic" and not repair:
        return "a synthetic case has its base case's requirements; it is a repair benchmark (--repair)"
    return None


def run_llm_case(path: Path, client, max_repairs: int = 3, timeout_ms: int = 20000, repair_from_reference: bool = False,
                 syntax: str | None = None):
    """Run the loop on a case and grade it.  With `repair_from_reference` the rules pass is skipped
    and the loop starts from the case's `start_rules` (a synthetic case: the mutant rules) or else
    from the reference rules: a repair benchmark."""
    from mls.llm.grade import grade
    from mls.llm.loop import generate
    from mls.llm.schemas import skeleton_of
    from mls.spec import check_spec

    case = load_case(path)
    ref = reference_spec(case, path)
    ref = check_spec(ref)
    skeleton = case.get("skeleton") or skeleton_of(ref)
    start_rules = None
    if repair_from_reference:
        start_rules = case.get("start_rules") or [r.model_dump(by_alias=True, exclude_none=True) for r in ref.rules]
    from mls.llm.loop import DEFAULT_SYNTAX

    result = generate(case["requirements"], skeleton, client, max_repairs, timeout_ms, start_rules=start_rules,
                      syntax=syntax or DEFAULT_SYNTAX)
    return grade(case, result, ref, timeout_ms), result


def synthetic_cases(path: Path, out_dir: Path, timeout_ms: int = 20000) -> tuple[list[Path], list[str]]:
    """Write one case per detected mutant of `path`'s reference spec; return the files and the
    names of the undetected mutants (gaps in the property set).  A synthetic case is a repair
    benchmark: the loop starts from the mutant rules (`start_rules`, `--repair`) and must end
    equivalent to the base reference, whose properties and verdicts the case keeps."""
    from mls.llm.mutate import grade_mutants

    case = load_case(path)
    ref = reference_spec(case, path)
    data = ref.model_dump(by_alias=True, exclude_none=True)
    written, missed = [], []
    out_dir.mkdir(parents=True, exist_ok=True)
    for m in grade_mutants(data, case.get("expected_verdicts", {}), timeout_ms):
        if not m.detected:
            missed.append(m.name)
            continue
        new = {"id": f"{case['id']}__{m.name.replace(':', '_')}", "origin": "synthetic", "base_case": case["id"],
               "mutation": m.name,
               "requirements": case["requirements"],
               "expected_properties": case.get("expected_properties", []),
               "reference_spec": data, "expected_verdicts": case.get("expected_verdicts", {}),
               "start_rules": m.data["rules"], "mutant_verdicts": m.verdicts,
               "expected_loop": {"status": "proved"}}
        f = out_dir / f"{new['id']}.json"
        f.write_text(json.dumps(new, indent=1) + "\n")
        written.append(f)
    return written, missed


def run_cases(paths: list[Path]) -> list[CaseResult]:
    return [run_case(f) for f in case_files(paths)]
