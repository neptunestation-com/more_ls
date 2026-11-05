"""Selectors (D48): `applies_to` is expanded by the checker into concrete rules; a selector
rule and its hand-written expansion are the same spec downstream; no match is an error."""
from __future__ import annotations

import json

import pytest

from mls.english import spec_summary
from mls.errors import SpecError
from mls.props.diff import access_diff
from mls.spec import check_spec
from mls.spec.model import Spec, load_spec
from mls.terse import spec_listing
from tests.conftest import EXAMPLE

FILES = {"columns": {"id": {"type": "int", "nullable": False}, "tenant_id": {"type": "int", "nullable": False},
                     "size": {"type": "int", "nullable": False}}, "primary_key": ["id"], "tags": ["tenant"]}


def base() -> dict:
    d = json.loads(EXAMPLE.read_text())
    d["tables"]["files"] = json.loads(json.dumps(FILES))
    d["properties"] = [p for p in d["properties"] if p["id"] != "P6"]  # least_privilege lists tables by hand
    return d


def with_selector(sel: dict, rule_id: str = "R2") -> dict:
    d = base()
    for r in d["rules"]:
        if r["id"] == rule_id:
            del r["table"]
            r["applies_to"] = sel
    return d


def test_selector_expands_to_one_rule_per_governed_table():
    spec = check_spec(Spec.model_validate(with_selector({"has_column": ["tenant_id", "int"]})))
    exp = spec.expansions("R2")
    assert [(r.id, r.table, r.from_, r.name) for r in exp] == [
        ("R2.memberships", "memberships", "R2", "admin_read"), ("R2.admins", "admins", "R2", "admin_read"),
        ("R2.docs", "docs", "R2", "admin_read"), ("R2.files", "files", "R2", "admin_read")]
    assert all(r.applies_to is not None for r in exp)
    assert not any(r.id == "R2" for r in spec.rules)
    # the selector forms
    for sel, tables in [({"named": ["docs", "files"]}, ["docs", "files"]),
                        ({"tagged": "tenant"}, ["files"]),
                        ({"all": True}, ["memberships", "admins", "docs", "files"]),
                        ({"and": [{"has_column": ["tenant_id"]}, {"has_column": ["level"]}]}, ["docs"]),
                        ({"or": [{"tagged": "tenant"}, {"named": ["admins"]}]}, ["admins", "files"]),
                        ({"has_column": ["tenant_id", "text"]}, None)]:
        if tables is None:
            with pytest.raises(SpecError):
                check_spec(Spec.model_validate(with_selector(sel)))
            continue
        spec = check_spec(Spec.model_validate(with_selector(sel)))
        assert [r.table for r in spec.expansions("R2")] == tables, sel


def test_no_match_is_an_error_with_the_path_and_the_fix():
    d = with_selector({"tagged": "legacy"})
    d["tables"]["audit"] = {"columns": {"id": {"type": "int", "nullable": False}}, "mode": "observed",
                            "tags": ["legacy"]}
    with pytest.raises(SpecError) as ei:
        check_spec(Spec.model_validate(d))
    e = ei.value.errors[0]
    assert e.path == "$.rules[1].applies_to"
    assert "rule 'R2' applies to no table (is tagged legacy)" in e.message
    assert "observed table audit would match" in e.message and '"adopted"' in e.message
    assert 'name the table with "table"' in e.fix


def test_errors_inside_an_expansion_report_under_the_selector_rule():
    d = with_selector({"has_column": ["tenant_id"]})
    d["rules"][1]["using"] = {"cmp": [{"col": "level"}, "<=", {"ctx": "clearance"}]}  # files has no level
    with pytest.raises(SpecError) as ei:
        check_spec(Spec.model_validate(d))
    paths = {e.path for e in ei.value.errors}
    assert paths == {"$.rules[1].using.cmp[0]"}
    assert any("of table 'files'" in e.message for e in ei.value.errors)


def test_shape_errors(tmp_path):
    d = base()
    d["rules"][1]["applies_to"] = {"all": True}  # table and applies_to
    (tmp_path / "s.json").write_text(json.dumps(d))
    with pytest.raises(SpecError) as ei:
        load_spec(tmp_path / "s.json")
    assert ei.value.errors[0].path == "$.rules[1]" and "exclusive" in ei.value.errors[0].message
    d = base()
    del d["rules"][1]["table"]
    (tmp_path / "s.json").write_text(json.dumps(d))
    with pytest.raises(SpecError) as ei:
        load_spec(tmp_path / "s.json")
    assert 'needs "table"' in ei.value.errors[0].message and "applies_to" in ei.value.errors[0].message
    d = with_selector({"all": True, "tagged": "x"})
    (tmp_path / "s.json").write_text(json.dumps(d))
    with pytest.raises(SpecError) as ei:
        load_spec(tmp_path / "s.json")
    assert "exactly one of" in ei.value.errors[0].message
    d = with_selector({"in_schema": "public"})
    (tmp_path / "s.json").write_text(json.dumps(d))
    with pytest.raises(SpecError):
        load_spec(tmp_path / "s.json")


def manual_expansion(sel: dict) -> dict:
    """The rules the checker would write, by hand: the same ids, `from`, no selector."""
    expanded = check_spec(Spec.model_validate(with_selector(sel)))
    d = base()
    out = []
    for r in d["rules"]:
        if r["id"] != "R2":
            out.append(r)
            continue
        for x in expanded.expansions("R2"):
            y = {k: v for k, v in r.items() if k != "table"}
            out.append({**y, "id": x.id, "table": x.table, "from": "R2"})
    d["rules"] = out
    return d


def test_selector_and_manual_expansion_are_the_same_spec():
    sel = {"has_column": ["tenant_id", "int"]}
    a = check_spec(Spec.model_validate(with_selector(sel)))
    b = check_spec(Spec.model_validate(manual_expansion(sel)))
    assert spec_listing(a) == spec_listing(b)  # byte-identical terse
    assert "R2.files policy files: authenticated select permissive using admin_of(@uid, tenant_id)" in spec_listing(a)
    assert [c for c in access_diff(a, b) if c.verdict != "PROVED"] == []
    # explain: the selector once, the expansion as a list
    md = spec_summary(a)
    assert "- **R2**: applies to every governed table that has column tenant_id of type int: memberships, admins, " \
           "docs, files" in md
    assert "  - **R2.files**: authenticated may read rows of files where" in md
    assert md.count("**R2.") == 4
    # the manual expansion explains rule by rule
    assert "applies to every" not in spec_summary(b) and "- **R2.files**:" in spec_summary(b)


def test_diff_reports_per_concrete_rule():
    sel = {"has_column": ["tenant_id", "int"]}
    a = check_spec(Spec.model_validate(with_selector(sel)))
    d = with_selector(sel)
    d["rules"][1]["applies_to"] = {"named": ["docs", "admins", "memberships"]}  # files dropped
    b = check_spec(Spec.model_validate(d))
    changed = {(c.table, c.command) for c in access_diff(a, b) if c.verdict != "PROVED"}
    assert changed == {("files", "select")}


def test_invariant_reference_to_a_selector_rule_expands():
    d = base()
    d["rules"].append({"id": "C9", "kind": "check", "name": "positive_key", "applies_to": {"has_column": ["id", "int"]},
                       "predicate": {"cmp": [{"col": "id"}, ">=", {"const": 0}]}})
    d["properties"].append({"id": "P99", "template": "invariant_preserved", "invariants": [{"rule": "C9"}]})
    spec = check_spec(Spec.model_validate(d))
    p9 = next(p for p in spec.properties if p.id == "P99")
    assert [i.rule for i in p9.invariants] == ["C9.docs", "C9.files"]
    assert "C9.docs (positive_key); C9.files (positive_key)" in spec_summary(spec)


def test_late_binding_adds_the_rule_to_a_new_table():
    d = with_selector({"has_column": ["tenant_id", "int"]})
    before = [r.table for r in check_spec(Spec.model_validate(d)).expansions("R2")]
    d["tables"]["notes"] = {"columns": {"id": {"type": "int", "nullable": False},
                                        "tenant_id": {"type": "int", "nullable": False}},
                            "primary_key": ["id"], "mode": "adopted"}
    after = [r.table for r in check_spec(Spec.model_validate(d)).expansions("R2")]
    assert after == before + ["notes"]


def test_an_already_expanded_rule_is_kept():
    sel = {"named": ["docs"]}
    d = manual_expansion(sel)
    spec = check_spec(Spec.model_validate(d))
    assert [r.id for r in spec.rules if r.from_ == "R2"] == ["R2.docs"]
    # checking the checked spec again is the identity on the rules
    again = check_spec(spec)
    assert [r.id for r in again.rules] == [r.id for r in spec.rules]
