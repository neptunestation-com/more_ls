"""Access diff (SEMANTICS §7): new grants and new denials with scenarios; constraint-aware;
opaque blocks flagged; integrity and privilege diffs."""
from __future__ import annotations

import json

from mls.props.diff import diff, to_json, to_markdown
from mls.spec import check_spec
from mls.spec.model import Spec
from tests.conftest import EXAMPLE
from tests.templates.specs import V2


def base() -> dict:
    return json.loads(EXAMPLE.read_text())


def make(d: dict) -> Spec:
    spec = Spec.model_validate(d)
    check_spec(spec)
    return spec


def changes(d):
    return {(a.table, a.role, a.command, a.which, a.direction) for a in d.access_changes}


def test_same_spec_has_no_diff(example_spec):
    d = diff(example_spec, example_spec)
    assert not d.any_change and d.unknown == []


def test_added_policy_is_a_new_grant_with_scenario(example_spec):
    d = base()
    d["rules"] = [r for r in d["rules"] if r["id"] != "R2"]  # no admin_read
    old = make(d)
    res = diff(old, example_spec)
    assert ("docs", "authenticated", "select", "using", "granted") in changes(res)
    assert ("docs", "authenticated", "select", "using", "denied") not in changes(res)
    a = next(a for a in res.access_changes if a.table == "docs" and a.command == "select")
    sc = a.scenario
    row, uid = sc.rows["r"]["values"], sc.context["uid"]
    assert [uid, row["tenant_id"]] in sc.relations["admin_of"]
    assert row["level"] > sc.context["clearance"]  # the only way admin_read adds anything
    # the implicit relation policy (D21) on admins is a new grant too, and a new privilege
    assert ("admins", "authenticated", "select", "using", "granted") in changes(res)
    assert any(g.table == "admins" and g.after for g in res.grants)
    # the reverse diff reports the same change as a denial
    back = diff(example_spec, old)
    assert ("docs", "authenticated", "select", "using", "denied") in changes(back)


def test_diff_respects_constraints(example_spec):
    """Restricting admin_read to level <= 3 changes nothing: the CHECK already bounds level."""
    d = base()
    for r in d["rules"]:
        if r["id"] == "R2":
            r["using"] = {"and": [r["using"], {"cmp": [{"col": "level"}, "<=", {"const": 3}]}]}
    assert not diff(example_spec, make(d)).access_changes


def test_example_v1_to_v2():
    old, new = make(base()), make(json.loads(V2.read_text()))
    res = diff(old, new)
    ch = changes(res)
    assert ("docs", "authenticated", "delete", "using", "granted") in ch
    assert ("tenant_settings", "authenticated", "select", "using", "granted") in ch
    assert ("docs", "authenticated", "select", "using", "granted") not in ch
    kinds = {(i.table, i.kind, i.direction) for i in res.integrity_changes}
    assert ("docs", "stored rows", "accepted") in kinds     # level 4 now allowed
    assert ("docs", "stored rows", "rejected") in kinds     # title '' now rejected
    assert ("docs", "changes", "rejected") in kinds         # owner now immutable
    assert any(g.command == "delete" and g.table == "docs" for g in res.grants)
    md = to_markdown(old, new, res)
    assert "can now delete rows of docs" in md and "for example:" in md
    assert "  - before: `false`\n  - after: `owner_id = @uid and not archived = 1`" in md
    j = to_json(old, new, res)
    assert j["summary"]["access_changes"] == 2 and all(a["scenario_english"] for a in j["access"])
    assert all(a["before"] and a["after"] for a in j["access"] + j["integrity"])
    assert "table 'tenant_settings' is new" in res.notes


def test_opaque_change_is_flagged(example_spec):
    d = base()
    d["opaques"] = {"gate": {"table": "docs", "columns": ["level"], "context": ["clearance"],
                             "sql": "level < 1 or {ctx:clearance} >= 3"}}
    d["rules"].append({"id": "OP", "kind": "policy", "name": "gate", "table": "docs", "role": "authenticated",
                       "command": "all", "mode": "restrictive", "using": {"opaque": "gate"}})
    with_gate = make(d)
    res = diff(example_spec, with_gate)
    a = next(a for a in res.access_changes if a.command == "select" and a.direction == "denied")
    assert a.opaque_deps == ["gate"]
    assert "conditional on unverified SQL: gate" in to_markdown(example_spec, with_gate, res)
    d2 = json.loads(json.dumps(d))
    d2["opaques"]["gate"]["sql"] = "level < 2"
    res2 = diff(with_gate, make(d2))
    assert any("different SQL" in n for n in res2.notes)
    assert {a.direction for a in res2.access_changes if a.command == "select"} == {"granted", "denied"}


def test_hierarchy_diff():
    """Replacing direct membership by in_group grants access (and denies nothing); lowering the
    depth bound denies access to the deepest groups and the diff names the changed hierarchy."""
    from tests.templates import specs

    flat, org = specs.org_flat_spec(), specs.org_base_spec()
    res = diff(flat, org)
    assert ("files", "authenticated", "select", "using", "granted") in changes(res)
    assert ("files", "authenticated", "select", "using", "denied") not in changes(res)
    a = next(a for a in res.access_changes if a.table == "files" and a.command == "select")
    assert a.scenario.hierarchies
    d = specs.org_base()
    d["hierarchies"]["org"]["max_depth"] = 2
    shallow = specs.make(d)
    res = diff(org, shallow)
    assert ("files", "authenticated", "select", "using", "denied") in changes(res)
    assert ("files", "authenticated", "select", "using", "granted") not in changes(res)
    assert any("depth bound changed from 3 to 2" in n for n in res.notes)
    assert not diff(org, org).any_change
    # a different members relation is a different tree: both directions change
    d = specs.org_base()
    d["relations"]["other"] = {"table": "group_members", "key": "user_id", "value": "group_id"}
    d["hierarchies"]["org"]["members"] = "other"
    d["properties"] = [p for p in d["properties"] if p["id"] != "P2"]
    res = diff(org, specs.make(d))
    assert any("defined differently" in n for n in res.notes)
    assert ("files", "authenticated", "select", "using", "denied") in changes(res)
