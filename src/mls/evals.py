"""Eval case format (D6; corpus built in Milestone 4).

A case is a JSON file:
  {"id": "...", "requirements": "plain-English business requirements",
   "expected_properties": ["plain-English property statements the LLM should produce"],
   "reference_spec": "relative/path.mls.json"  (or an inline spec object),
   "expected_verdicts": {"P1": "PROVED", ...},
   "expected_check_errors": [{"path": "$.rules[3]", "contains": "negated membership"}]  (optional)}
A case passes when the reference spec checks (or fails exactly as expected) and every listed
property has the expected verdict.
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
        check_spec(spec)
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
    if bad:
        return CaseResult(case["id"], False, "; ".join(bad))
    return CaseResult(case["id"], True, f"{len(results)} properties as expected")


def run_cases(paths: list[Path]) -> list[CaseResult]:
    files: list[Path] = []
    for p in paths:
        files.extend(sorted(p.glob("*.json")) if p.is_dir() else [p])
    return [run_case(f) for f in files]
