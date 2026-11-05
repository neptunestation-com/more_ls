"""The terse syntax is a fixed function of the model and covers every construct of the whole
spec (D44, D50): one golden line per construct, and a check that the model has nothing the
renderer does not know (a construct without a terse form fails here)."""
from __future__ import annotations

import json
import typing

from pydantic import TypeAdapter

from mls import terse
from mls.props.diff import AccessChange, DiffResult, IntegrityChange, to_markdown
from mls.props.runner import run_property
from mls.spec.model import (Assumption, Pred, Property, Rule, Spec, Term)
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
    assert pred({"in": [{"col": "level"}, [{"const": 2}, {"const": 3}]]}) == "level in (2, 3)"
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


def test_reserved_words_and_odd_names_are_quoted():
    assert pred({"cmp": [{"col": "check"}, "=", {"const": 1}]}) == '"check" = 1'
    assert pred({"cmp": [{"old": "from"}, "=", {"new": "select"}]}) == 'old."from" = new."select"'
    assert pred({"cmp": [{"col": "two words"}, "=", {"ctx": "and"}]}) == '"two words" = @"and"'
    assert pred({"cmp": [{"col": 'say "hi"'}, "=", {"const": 1}]}) == '"say ""hi""" = 1'
    assert pred({"cmp": [{"col": "id"}, "=", {"const": 1}]}) == "id = 1"  # positional words stay bare
    assert terse.ident("R2.docs", dotted=True) == "R2.docs" and terse.ident("R2.docs") == '"R2.docs"'
    assert terse.dollar("a = 1") == "$$ a = 1 $$"
    assert terse.dollar("x like '$$'") == "$mls$ x like '$$' $mls$"
    assert terse.dollar("$$ and $mls$") == "$mls2$ $$ and $mls$ $mls2$"


def test_rules_and_properties_of_the_examples(example_spec):
    text = terse.render(example_spec)
    assert ("  R1 policy member_read on docs: authenticated select permissive using member_of(@uid, tenant_id) and "
            "level <= @clearance requirement 'REQ-1'\n" in text)
    assert "  R4 policy member_update on docs: authenticated update permissive using member_of(@uid, tenant_id) check " in text
    assert "  R5 policy not_archived on docs: authenticated all restrictive using not archived = 1 requirement 'REQ-4'\n" in text
    assert "  R7 inclusion owner_in_tenant on docs: (owner_id, tenant_id) in member_of on_delete restrict requirement 'REQ-6'\n" in text
    assert "  R9 transition tenant_immutable on docs: new.tenant_id = old.tenant_id requirement 'REQ-7'\n" in text
    assert "  P1 tenant_isolation docs: tenant_id in member_of(@uid) for select, insert, update description '" in text
    assert "  P2 column_immutable docs: tenant_id description '" in text and "  P3 monotone_in: member_of description '" in text
    assert "  P4 invariant_preserved: R6; R7; R8 description '" in text and "  P5 write_subset_read docs description '" in text
    assert "  P6 least_privilege: authenticated docs[select, insert, update]; authenticated memberships[select];" in text
    assert "  P10 must_allow docs authenticated update: member_of(@uid, old.tenant_id) and old.archived = 0" in text
    org = terse.render(make(json.loads(ORG_TREE.read_text())))
    assert "  R1 policy group_read on files: authenticated select permissive using in_group(org, @uid, group_id)" in org
    assert "  R5 cardinality files_per_tenant on files: tenant_id -> tenants max 5 requirement 'REQ-4'\n" in org
    assert "  P1 custom files authenticated select new: in_group(org, @uid, group_id) description '" in org
    assert "\nhierarchy org = groups id id parent parent_id depth depth max_depth 3 members group_member\n" in org
    assert terse.spec_listing is terse.render


def test_column_rule_opaque_and_invariant_predicate():
    r = TypeAdapter(Rule).validate_python({"id": "R4", "kind": "column_rule", "name": "hr_title", "table": "employees",
                                           "role": "hr", "columns": ["title", "grade"], "access": "write"})
    assert terse.rule(r) == "R4 column_rule hr_title on employees: hr write title, grade"
    r = TypeAdapter(Rule).validate_python({"id": "R4", "kind": "column_rule", "table": "employees",
                                           "role": "hr", "columns": ["title"], "access": "read"})
    assert terse.rule(r) == "R4 column_rule on employees: hr read title"
    p = TypeAdapter(Property).validate_python({"id": "P5", "template": "invariant_preserved", "invariants": [
        {"rule": "R9"}, {"table": "salaries", "predicate": {"cmp": [{"col": "amount"}, ">=", {"const": 0}]}}]})
    assert terse.prop(p) == "P5 invariant_preserved: R9; salaries: amount >= 0"
    p = TypeAdapter(Property).validate_python({"id": "S1", "template": "role_separation", "table": "approvals",
                                               "roles": ["requester", "approver"], "command": "update",
                                               "description": "four eyes"})
    assert terse.prop(p) == "S1 role_separation approvals update: requester | approver description 'four eyes'"
    d = base()
    d["opaques"] = {"gate": {"table": "docs", "columns": ["level"], "context": ["clearance"],
                             "sql": "level < 1 or {ctx:clearance} >= 3"}}
    d["rules"].append({"id": "OP", "kind": "policy", "name": "gate", "table": "docs", "role": "authenticated",
                       "command": "all", "mode": "restrictive", "using": {"opaque": "gate"}})
    text = terse.render(make(d))
    assert "  OP policy gate on docs: authenticated all restrictive using opaque(gate)\n" in text
    assert "\nopaque gate on docs (level, @clearance) relations () sql $$ level < 1 or {ctx:clearance} >= 3 $$\n" in text


def whole() -> dict:
    """Every section and every table option in one spec."""
    d = base()
    d["tables"]["docs"]["unique"] = [["tenant_id", "owner_id"]]
    d["tables"]["docs"]["tags"] = ["tenant", "pii"]
    d["tables"]["docs"]["mode"] = "adopted"
    d["tables"]["docs"]["assume"] = [{"owner": "authenticated"}, {"rls": "not_forced"}, {"bypassrls": "svc"}]
    d["tables"]["audit"] = {"columns": {"id": {"type": "int", "nullable": False}}, "mode": "observed"}
    d["tables"]["select"] = {"columns": {"from": {"type": "text", "nullable": True}}, "primary_key": None}
    d["opaques"] = {"gate": {"table": "docs", "columns": ["level"], "context": ["clearance"], "relations": ["member_of"],
                             "sql": "level < 1"}}
    d["rules"] = [r for r in d["rules"] if r["id"] in ("R1", "R6")]
    r1 = d["rules"][0]
    del r1["table"]
    r1["applies_to"] = {"or": [{"and": [{"has_column": ["tenant_id", "int"]}, {"tagged": "tenant"}]},
                               {"named": ["docs"]}, {"all": True}]}
    d["rules"].append({"id": "R1.docs", "kind": "policy", "name": "member_read", "table": "docs", "from": "R1",
                       "role": "authenticated", "command": "select", "mode": "permissive", "using": {"opaque": "gate"}})
    d["properties"] = d["properties"][:1]
    return d


EXPECTED = """mls 0.1
comment 'Tenant-scoped documents with membership-based access, clearance levels, an archived flag, and integrity rules. Mirrors prototype/demo.py and prototype/integrity.py (GOOD corpus).'

identity supabase { uid: auth.uid uuid; clearance: jwt.claims.clearance int }
roles authenticated

table memberships owned {
  user_id uuid not null,
  tenant_id int not null;
  primary_key (user_id, tenant_id)
}

table admins owned {
  user_id uuid not null,
  tenant_id int not null;
  primary_key (user_id, tenant_id)
}

table docs adopted {
  id int not null,
  tenant_id int not null,
  owner_id uuid,
  level int not null,
  archived int;
  primary_key (id);
  unique (tenant_id, owner_id);
  tags (tenant, pii);
  assume ('owner authenticated', 'rls not_forced', 'bypassrls svc')
}

table audit observed {
  id int not null
}

table "select" owned {
  "from" text
}

relation member_of = memberships(user_id, tenant_id)
relation admin_of = admins(user_id, tenant_id)

opaque gate on docs (level, @clearance) relations (member_of) sql $$ level < 1 $$

rules {
  R1 policy member_read applies_to or(and(has_column(tenant_id int), tagged(tenant)), named(docs), all): authenticated select permissive using member_of(@uid, tenant_id) and level <= @clearance requirement 'REQ-1'
  R6 check level_range on docs: level >= 0 and level <= 3 requirement 'REQ-5'
  R1.docs policy member_read on docs from R1: authenticated select permissive using opaque(gate)
}

properties {
  P1 tenant_isolation docs: tenant_id in member_of(@uid) for select, insert, update description 'a user only touches docs of tenants they belong to'
}
"""


def test_whole_spec_golden():
    spec = Spec.model_validate(whole())  # unchecked: the selector rule is still a selector
    assert terse.render(spec) == EXPECTED
    # the empty forms
    d = whole()
    d["identity"] = {"adapter": "pg_roles"}
    d["rules"] = []
    d["properties"] = []
    text = terse.render(Spec.model_validate(d))
    assert "\nidentity pg_roles { }\n" in text and text.endswith("rules {\n}\n\nproperties {\n}\n")


def _members(union) -> set[type]:
    out: set[type] = set()
    for arg in typing.get_args(typing.get_args(union)[0]):
        inner = typing.get_args(arg)[0] if typing.get_origin(arg) is typing.Annotated else arg
        out.add(inner)
    return out


def test_the_renderer_covers_the_whole_model():
    """A construct added to the model without a terse form fails here (D50)."""
    for cls, fields in terse.RENDERED_FIELDS.items():
        assert set(cls.model_fields) == fields, cls.__name__
    for union in (Rule, Property, Pred, Term, Assumption):
        assert _members(union) <= terse.RENDERED_CLASSES, union
    # and every rule kind's own fields are rendered: a rule kind is its base fields plus these
    own = {"policy": {"kind", "role", "command", "mode", "using", "check"}, "check": {"kind", "predicate"},
           "inclusion": {"kind", "columns", "relation", "on_delete"}, "transition": {"kind", "predicate"},
           "cardinality": {"kind", "group", "parent", "max"}, "column_rule": {"kind", "role", "columns", "access"}}
    for cls in _members(Rule):
        kind = typing.get_args(cls.model_fields["kind"].annotation)[0]
        assert set(cls.model_fields) - terse.RENDERED_FIELDS[terse.RuleBase] == own[kind], cls.__name__


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


def test_in_renders_as_its_expansion_after_check():
    """`in` is sugar the checker expands: the listing of a checked spec with `in` and of its
    manual expansion are byte-identical (ROADMAP Milestone 5); before `check` it is written as
    `level in (2, 3)`."""
    from mls.spec import check_spec

    def listing(using, checked=True) -> str:
        d = base()
        d["rules"].append({"id": "R10", "kind": "policy", "name": "levels", "table": "docs", "role": "authenticated",
                           "command": "select", "mode": "permissive", "using": using})
        spec = Spec.model_validate(d)
        return terse.render(check_spec(spec) if checked else spec)

    sugar = {"and": [{"is_null": {"col": "owner_id"}}, {"in": [{"col": "level"}, [{"const": 2}, {"const": 3}]]}]}
    manual = listing({"and": [{"is_null": {"col": "owner_id"}},
                              {"or": [{"cmp": [{"col": "level"}, "=", {"const": 2}]},
                                      {"cmp": [{"col": "level"}, "=", {"const": 3}]}]}]})
    assert listing(sugar) == manual
    assert "R10 policy levels on docs: authenticated select permissive using owner_id is null and (level = 2 or level = 3)" in manual
    assert "R10 policy levels on docs: authenticated select permissive using owner_id is null and level in (2, 3)" in listing(sugar, checked=False)
