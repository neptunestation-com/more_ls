"""The terse rendering is a fixed function of the spec and covers every construct of the verified
fragment (D44).  Golden strings, no round trip: the syntax is for reading, not for input."""
from __future__ import annotations

from pydantic import TypeAdapter

from mls import terse
from mls.props.diff import AccessChange, DiffResult, IntegrityChange, to_markdown
from mls.props.runner import run_property
from mls.spec.model import Pred, Rule, Property
from tests.conftest import EXAMPLE, ORG_TREE
from tests.props.test_properties import base, make

P = TypeAdapter(Pred)


def pred(d) -> str:
    return terse.pred(P.validate_python(d))


def test_every_construct_of_the_fragment():
    assert pred({"lit": True}) == "true" and pred({"lit": False}) == "false"
    assert pred({"cmp": [{"col": "level"}, "<=", {"ctx": "clearance"}]}) == "level <= @clearance"
    assert pred({"cmp": [{"col": "title"}, "<>", {"const": "it's"}]}) == "title <> 'it''s'"
    assert pred({"cmp": [{"col": "archived"}, "=", {"const": True}]}) == "archived = true"
    assert pred({"cmp": [{"new": "tenant_id"}, "=", {"old": "tenant_id"}]}) == "new.tenant_id = old.tenant_id"
    assert pred({"is_null": {"col": "owner_id"}}) == "owner_id is null"
    assert pred({"member": {"relation": "member_of", "key": {"ctx": "uid"}, "value": {"col": "tenant_id"}}}) \
        == "member_of(@uid, tenant_id)"
    assert pred({"in_group": {"hierarchy": "org", "key": {"ctx": "uid"}, "value": {"col": "group_id"}}}) \
        == "in_group(org, @uid, group_id)"
    assert pred({"now_between": {"from": None, "until": {"col": "expires_at"}}}) == "now_between(-, expires_at)"
    assert pred({"now_between": {"from": {"col": "starts_at"}, "until": {"const": 5}}}) == "now_between(starts_at, 5)"
    assert pred({"now_between": {}}) == "now_between(-, -)"
    assert pred({"opaque": "gate"}) == "opaque(gate)"


def test_precedence_and_minimal_parentheses():
    a, b, c = {"cmp": [{"col": "a"}, "=", {"const": 1}]}, {"is_null": {"col": "b"}}, {"lit": True}
    assert pred({"and": [a, b, c]}) == "a = 1 and b is null and true"
    assert pred({"or": [{"and": [a, b]}, c]}) == "a = 1 and b is null or true"
    assert pred({"and": [{"or": [a, b]}, c]}) == "(a = 1 or b is null) and true"
    assert pred({"not": {"and": [a, b]}}) == "not (a = 1 and b is null)"
    assert pred({"not": b}) == "not b is null"
    assert pred({"and": [{"not": {"or": [a, b]}}, c]}) == "not (a = 1 or b is null) and true"
    assert pred({"and": [{"and": [a, b]}, c]}) == "a = 1 and b is null and true"
    assert pred({"and": [{"or": [a]}]}) == "a = 1"      # single-element lists add no parentheses


def test_rules_and_properties_of_the_examples(example_spec):
    text = terse.spec_listing(example_spec)
    assert ("R1 policy docs: authenticated select permissive using member_of(@uid, tenant_id) and "
            "level <= @clearance\n" in text)
    assert "R4 policy docs: authenticated update permissive using member_of(@uid, tenant_id) check " in text
    assert "R5 policy docs: authenticated all restrictive using not archived = 1\n" in text
    assert "R7 inclusion docs: (owner_id, tenant_id) in member_of on_delete restrict\n" in text
    assert "R9 transition docs: new.tenant_id = old.tenant_id\n" in text
    assert "P1 tenant_isolation docs: tenant_id in member_of(@uid) for select, insert, update\n" in text
    assert "P2 column_immutable docs: tenant_id\n" in text and "P3 monotone_in: member_of\n" in text
    assert "P4 invariant_preserved: R6; R7; R8\n" in text and "P5 write_subset_read docs\n" in text
    assert "P6 least_privilege: authenticated docs[select, insert, update]; authenticated memberships[select];" in text
    assert "P10 must_allow docs authenticated update: member_of(@uid, old.tenant_id) and old.archived = 0" in text
    org = terse.spec_listing(make(__import__("json").loads(ORG_TREE.read_text())))
    assert "R1 policy files: authenticated select permissive using in_group(org, @uid, group_id)\n" in org
    assert "R5 cardinality files: tenant_id -> tenants max 5\n" in org
    assert "P1 custom files authenticated select new: in_group(org, @uid, group_id)\n" in org
    assert "org: groups id id parent parent_id depth depth max_depth 3 members group_member\n" in org


def test_column_rule_opaque_and_invariant_predicate():
    r = TypeAdapter(Rule).validate_python({"id": "R4", "kind": "column_rule", "name": "hr_title", "table": "employees",
                                           "role": "hr", "columns": ["title", "grade"], "access": "write"})
    assert terse.rule(r) == "R4 column_rule employees: hr write title, grade"
    p = TypeAdapter(Property).validate_python({"id": "P5", "template": "invariant_preserved", "invariants": [
        {"rule": "R9"}, {"table": "salaries", "predicate": {"cmp": [{"col": "amount"}, ">=", {"const": 0}]}}]})
    assert terse.prop(p) == "P5 invariant_preserved: R9; salaries: amount >= 0"
    d = base()
    d["opaques"] = {"gate": {"table": "docs", "columns": ["level"], "context": ["clearance"],
                             "sql": "level < 1 or {ctx:clearance} >= 3"}}
    d["rules"].append({"id": "OP", "kind": "policy", "name": "gate", "table": "docs", "role": "authenticated",
                       "command": "all", "mode": "restrictive", "using": {"opaque": "gate"}})
    text = terse.spec_listing(make(d))
    assert "OP policy docs: authenticated all restrictive using opaque(gate)\n" in text
    assert "# opaque blocks\ngate: docs sql 'level < 1 or {ctx:clearance} >= 3'\n" in text


def test_scenario():
    d = base()
    d["rules"] = [r for r in d["rules"] if r["id"] != "R9"]
    spec = make(d)
    r = run_property(spec, next(p for p in spec.properties if p.id == "P2"))
    s = terse.scenario(r.obligations[0].scenario)
    assert s.startswith("@uid = 'uuid#") and "; old: docs {tenant_id = " in s and "; member_of = {('uuid#" in s


def test_diff_report_shows_before_and_after(example_spec):
    old = example_spec
    d = base()
    d["rules"].append({"id": "R10", "kind": "policy", "name": "owner_delete", "table": "docs", "role": "authenticated",
                       "command": "delete", "mode": "permissive", "using": {"cmp": [{"col": "owner_id"}, "=", {"ctx": "uid"}]}})
    new = make(d)
    res = DiffResult(
        access=[AccessChange("docs", "authenticated", "delete", "using", "granted", None, "REFUTED")],
        integrity=[IntegrityChange("docs", "changes", "rejected", None, "REFUTED")], grants=[], notes=[])
    md = to_markdown(old, new, res)
    assert "  - before: `false`\n  - after: `owner_id = @uid and not archived = 1`\n" in md
    assert "  - before: `new.tenant_id = old.tenant_id`\n  - after: `new.tenant_id = old.tenant_id`\n" in md
