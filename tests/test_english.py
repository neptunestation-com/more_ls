"""The English rendering is fixed and readable (D10)."""
from __future__ import annotations

from mls import english
from mls.props.runner import run_property
from tests.props.test_properties import base, make


def test_rules_and_properties(example_spec):
    text = english.spec_summary(example_spec)
    assert ("R1**: authenticated may read rows of docs where the row's tenant_id is among the current "
            "user's member_of and the row's level is at most the user's clearance." in text)
    assert "R5**: authenticated may read and change rows of docs only where not the row's archived is 1." in text
    assert "R9**: A change to a row of docs is allowed only if the new tenant_id is the previous tenant_id." in text
    assert "P3**: Removing entries from member_of never grants anyone new access." in text
    assert "member_of (table memberships)" in text


def test_scenario_english():
    d = base()
    d["rules"] = [r for r in d["rules"] if r["id"] != "R9"]
    spec = make(d)
    r = run_property(spec, next(p for p in spec.properties if p.id == "P2"))
    s = english.scenario(spec, r.obligations[0].scenario)
    assert s.startswith("the current user is uuid#") and "the existing row of docs" in s and "member_of has" in s


def test_hierarchy_and_cardinality_english():
    from tests.templates import specs

    text = english.spec_summary(specs.org_base_spec())
    assert ("R1**: authenticated may read rows of files where the row's group_id is one of the current "
            "user's org groups or lies below one." in text)
    assert ("R5**: A tenants row has at most 5 rows of files (counted by tenant_id); rows with no "
            "tenant_id are not counted." in text)
    assert "# Group hierarchies" in text
    assert ("org**: rows of groups form a tree (parent_id points to the parent id) at most 3 levels deep; "
            "a parent never changes; a user belongs to the groups listed in group_member and to everything "
            "below them." in text)
