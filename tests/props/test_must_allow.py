"""must_allow (D40): the lower bound on access.  Proved on the help-desk reference, refuted with a
counterexample when a grant is missing or a restrictive policy blocks it, refuted when the
condition is vacuous; caught by the mutation score; English rendering; checker scope."""
from __future__ import annotations

import copy
import json

import pytest

from mls import english
from mls.errors import SpecError
from mls.llm.mutate import grade_mutants
from mls.props.report import to_markdown
from mls.props.runner import run_all, run_property
from mls.spec import check_spec
from mls.spec.model import Spec
from tests.conftest import ROOT

HD = ROOT / "evals" / "cases" / "specs" / "hd_good.mls.json"


def old(c): return {"old": c}
def new(c): return {"new": c}
def eq(a, b): return {"cmp": [a, "=", b]}


def hd() -> dict:
    return json.loads(HD.read_text())


def make(d: dict) -> Spec:
    spec = Spec.model_validate(d)
    check_spec(spec)
    return spec


def prop(spec: Spec, pid: str):
    return next(p for p in spec.properties if p.id == pid)


def test_help_desk_closing_is_live_and_witnessed():
    spec = make(hd())
    r = run_property(spec, prop(spec, "P12"))  # an agent closes an open ticket
    assert r.verdict == "PROVED"
    witness = next(o for o in r.obligations if "some row" in o.name)
    assert witness.verdict == "PROVED" and witness.example is not None and witness.scenario is None
    ex = witness.example
    assert ex.rows["old"]["values"]["status"] == "'open'" and ex.rows["new"]["values"]["status"] == "'closed'"
    assert "example:" in to_markdown(spec, [r])


def test_restrictive_policy_without_with_check_refutes_closing():
    """The reference bug the safety properties could not see: R5 applied to the new row too."""
    d = hd()
    for x in d["rules"]:
        if x["id"] == "R5":
            x.pop("check")
    spec = make(d)
    r = run_property(spec, prop(spec, "P12"))
    assert r.verdict == "REFUTED"
    bad = next(o for o in r.obligations if o.verdict == "REFUTED")
    assert bad.scenario.rows["old"]["values"]["status"] == "'open'"
    assert bad.scenario.rows["new"]["values"]["status"] == "'closed'"
    assert "cannot" not in bad.name and "every row meeting the condition is admitted" in bad.name
    # the other must_allow properties still hold: reading and opening are untouched
    assert run_property(spec, prop(spec, "P9")).verdict == "PROVED"


def test_dropped_grant_is_refuted_and_detected_by_the_mutation_score():
    d = hd()
    d["rules"] = [x for x in d["rules"] if x["id"] != "R4"]
    spec = make(d)
    assert run_property(spec, prop(spec, "P12")).verdict == "REFUTED"
    base = {p["id"]: "PROVED" for p in hd()["properties"]}
    graded = {m.name: m for m in grade_mutants(hd(), base)}  # grade_mutants re-bases the properties itself
    # (drop:R4 leaves the restrictive R5 without a permissive policy, which the checker rejects, so it is not a mutant)
    assert graded["drop:R3"].detected and graded["drop:R3"].verdicts["P11"] == "REFUTED"
    assert graded["drop:R1"].detected and graded["drop:R1"].verdicts["P9"] == "REFUTED"


def test_vacuous_condition_is_refuted_with_a_message():
    d = hd()
    d["properties"].append({"id": "PV", "template": "must_allow", "description": "vacuous", "table": "tickets",
                            "role": "authenticated", "command": "select",
                            "when": {"and": [eq({"col": "status"}, {"const": "open"}), eq({"col": "status"}, {"const": "closed"})]}})
    spec = make(d)
    r = run_property(spec, prop(spec, "PV"))
    assert r.verdict == "REFUTED"
    w = next(o for o in r.obligations if "some row" in o.name)
    assert w.verdict == "REFUTED" and "no row can meet the condition" in w.message and w.scenario is None


def test_column_privileges_are_part_of_admitted():
    """cr_good: HR may write salary and title only; a must_allow that changes user_id is refuted."""
    d = json.loads((ROOT / "evals" / "cases" / "specs" / "cr_good.mls.json").read_text())
    d["properties"].append({"id": "PX", "template": "must_allow", "description": "hr reassigns a record", "table": "employees",
                            "role": "hr", "command": "update",
                            "when": {"and": [{"member": {"relation": "member_of", "key": {"ctx": "uid"}, "value": old("tenant_id")}},
                                             eq(new("id"), old("id")), eq(new("tenant_id"), old("tenant_id")),
                                             eq(new("title"), old("title")), eq(new("salary"), old("salary")),
                                             {"cmp": [new("user_id"), "<>", old("user_id")]}]}})
    spec = make(d)
    assert run_property(spec, prop(spec, "PX")).verdict == "REFUTED"
    assert run_property(spec, prop(spec, "P7")).verdict == "PROVED"


def test_english_rendering():
    spec = make(hd())
    assert english.prop(spec, prop(spec, "P9")) == \
        "authenticated can read every row of tickets where the row's customer_id is the current user."
    s = english.prop(spec, prop(spec, "P12"))
    assert s.startswith("authenticated can change a row of tickets whenever ") and "the previous status is 'open'" in s


def test_checker_rejects_old_new_outside_update_and_unknown_role():
    d = hd()
    d["properties"].append({"id": "PX", "template": "must_allow", "table": "tickets", "role": "nobody", "command": "select",
                            "when": eq(old("status"), {"const": "open"})})
    with pytest.raises(SpecError) as e:
        make(d)
    msg = str(e.value)
    assert "$.properties[12].role" in msg and "unknown role" in msg
    assert "$.properties[12].when" in msg and "must_allow" in msg


def test_example_specs_prove_their_must_allow_properties(example_spec, org_spec):
    for spec in (example_spec, org_spec):
        for r in run_all(spec):
            if r.template == "must_allow":
                assert r.verdict == "PROVED", (spec, r.id)
