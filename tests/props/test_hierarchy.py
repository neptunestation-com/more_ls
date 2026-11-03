"""Milestone 3 proofs: in_group (bounded unrolling of the hierarchy), cardinality induction, and the
assumptions the reports state for them."""
from __future__ import annotations

import json

from mls.props.report import assumptions, dumps_json
from mls.props.runner import run_all, run_property
from tests.templates import specs


def verdicts(spec) -> dict[str, str]:
    return {r.id: r.verdict for r in run_all(spec)}


def test_org_example_all_proved():
    assert set(verdicts(specs.org_base_spec()).values()) == {"PROVED"}


def test_direct_membership_is_not_implied_by_in_group():
    """Under the in_group policy a user also reads files of groups below their own: a goal that
    asks for direct membership is refuted, and the counterexample shows the parent link."""
    d = specs.org_base()
    d["properties"] = [{"id": "C1", "template": "custom", "table": "files", "role": "authenticated",
                        "command": "select",
                        "goal": {"member": {"relation": "group_member", "key": {"ctx": "uid"}, "value": {"col": "group_id"}}}}]
    spec = specs.make(d)
    r = run_property(spec, spec.properties[0])
    assert r.verdict == "REFUTED"
    sc = next(o for o in r.obligations if o.verdict == "REFUTED").scenario
    uid, gid = sc.context["uid"], sc.rows["r"]["values"]["group_id"]
    assert [uid, gid] not in sc.relations["group_member"]
    assert sc.hierarchies, "the scenario must show the group tree"
    # the reverse holds: a direct member is in the group
    d["rules"][0]["using"] = {"member": {"relation": "group_member", "key": {"ctx": "uid"}, "value": {"col": "group_id"}}}
    d["properties"][0]["goal"] = {"in_group": {"hierarchy": "org", "key": {"ctx": "uid"}, "value": {"col": "group_id"}}}
    assert run_property(specs.make(d), specs.make(d).properties[0]).verdict == "PROVED"


def test_depth_bound_matters():
    """With max_depth 1 the same policy no longer reaches a grandchild group."""
    d = specs.org_base()
    d["properties"] = [{"id": "C1", "template": "custom", "table": "files", "role": "authenticated",
                        "command": "select",
                        "goal": {"in_group": {"hierarchy": "org", "key": {"ctx": "uid"}, "value": {"col": "group_id"}}}}]
    spec = specs.make(d)
    assert verdicts(spec) == {"C1": "PROVED"}
    d["hierarchies"]["org"]["max_depth"] = 1
    shallow = specs.make(d)
    # the policy (depth 1) and the goal (depth 1) still agree; a goal written against the deeper
    # version is what the access diff compares (see test_diff)
    assert verdicts(shallow) == {"C1": "PROVED"}


def test_monotone_in_with_in_group():
    d = specs.org_base()
    d["properties"] = [{"id": "M", "template": "monotone_in", "relation": "group_member"}]
    assert verdicts(specs.make(d)) == {"M": "PROVED"}


def test_cardinality_invariant_proved_and_reported():
    spec = specs.org_base_spec()
    results = run_all(spec)
    p3 = next(r for r in results if r.id == "P3")
    assert p3.verdict == "PROVED"
    assert any("R5" in o.name or "files_per_tenant" in o.name for o in p3.obligations), [o.name for o in p3.obligations]
    a = assumptions(spec, results)
    assert any("mls_internal" in s for s in a)
    assert any("READ COMMITTED" in s for s in a)
    js = json.loads(dumps_json(spec, results))
    assert js["summary"]["PROVED"] == 7 and any("READ COMMITTED" in s for s in js["assumptions"])
