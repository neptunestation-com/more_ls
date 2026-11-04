"""Schema, typecheck and fragment errors name the JSON path and the fix (invariant 7)."""
from __future__ import annotations

import copy
import json

import pytest

from mls.errors import SpecError
from mls.spec import check_spec, load_spec, spec_json_schema
from mls.spec.check import Checker
from mls.spec.model import Spec
from tests.conftest import EXAMPLE


def base() -> dict:
    return json.loads(EXAMPLE.read_text())


def errors(d: dict):
    spec = Spec.model_validate(d)
    return Checker(spec).run()


def has(errs, path: str, text: str) -> bool:
    return any(e.path == path and text in e.message for e in errs)


def test_example_is_clean():
    check_spec(load_spec(EXAMPLE))
    assert "properties" in spec_json_schema()


def test_unknown_column_names_path_and_alternatives():
    d = base()
    d["rules"][0]["using"]["and"][1]["cmp"][0] = {"col": "lvl"}
    errs = errors(d)
    assert has(errs, "$.rules[0].using.and[1].cmp[0]", "unknown column 'lvl'")
    assert any("one of: id, tenant_id" in e.fix for e in errs)


def test_context_not_allowed_in_check():
    d = base()
    d["rules"].append({"id": "X", "kind": "check", "name": "ctx_check", "table": "docs",
                       "predicate": {"cmp": [{"col": "level"}, "<=", {"ctx": "clearance"}]}})
    errs = errors(d)
    assert has(errs, "$.rules[9].predicate.cmp[2]", "context values are not allowed in a check predicate")


def test_negated_membership_rejected_in_policy():
    d = base()
    d["rules"].append({"id": "X", "kind": "policy", "name": "outsider_public", "table": "docs",
                       "role": "authenticated", "command": "select", "mode": "permissive",
                       "using": {"and": [{"not": {"member": {"relation": "member_of", "key": {"ctx": "uid"},
                                                             "value": {"col": "tenant_id"}}}},
                                         {"cmp": [{"col": "level"}, "=", {"const": 0}]}]}})
    errs = errors(d)
    e = next(e for e in errs if e.path == "$.rules[9].using.and[0].not")
    assert "anti-join" in e.message and "opaque" in e.fix


def test_nullable_membership_column_rejected():
    d = base()
    d["tables"]["docs"]["columns"]["tenant_id"]["nullable"] = True
    errs = errors(d)
    assert has(errs, "$.rules[0].using.and[0].member.value", "must be NOT NULL")


def test_type_mismatch_and_ordering_on_uuid():
    d = base()
    d["rules"][0]["using"]["and"][1]["cmp"] = [{"col": "owner_id"}, "<", {"ctx": "uid"}]
    errs = errors(d)
    assert has(errs, "$.rules[0].using.and[1].cmp", "operator < is not allowed on uuid")
    d = base()
    d["rules"][0]["using"]["and"][1]["cmp"] = [{"col": "level"}, "=", {"ctx": "uid"}]
    assert has(errors(d), "$.rules[0].using.and[1].cmp", "type mismatch: int = uuid")


def test_old_new_only_in_transitions():
    d = base()
    d["rules"][5]["predicate"]["and"][0]["cmp"][0] = {"new": "level"}
    assert has(errors(d), "$.rules[5].predicate.and[0].cmp[0]", "only allowed in transition")
    d = base()
    d["rules"][8]["predicate"]["cmp"][0] = {"col": "tenant_id"}
    assert has(errors(d), "$.rules[8].predicate.cmp[0]", "not allowed in a transition predicate")


def test_relation_needs_unique_pair():
    d = base()
    d["tables"]["admins"].pop("primary_key")
    errs = errors(d)
    e = next(e for e in errs if e.path == "$.relations.admin_of")
    assert "primary key or unique" in e.message and "tables.admins.unique" in e.fix


def test_policy_shape_errors():
    d = base()
    d["rules"][2].pop("check")  # insert policy without check
    assert has(errors(d), "$.rules[2]", "an insert policy needs 'check'")
    d = base()
    d["rules"][0]["check"] = d["rules"][0]["using"]
    assert has(errors(d), "$.rules[0].check", "has no 'check'")


def test_duplicate_ids_and_unknown_relation():
    d = base()
    d["rules"][1]["id"] = "R1"
    assert has(errors(d), "$.rules[1].id", "duplicate rule id")
    d = base()
    d["rules"][6]["relation"] = "nope"
    errs = errors(d)
    assert has(errs, "$.rules[6].relation", "unknown relation 'nope'")


def test_timestamp_constant_rejected_and_now_between_typed():
    d = base()
    d["tables"]["docs"]["columns"]["valid_until"] = {"type": "timestamp", "nullable": True}
    d["rules"][0]["using"]["and"].append({"now_between": {"from": None, "until": {"col": "level"}}})
    errs = errors(d)
    assert has(errs, "$.rules[0].using.and[2].now_between.until", "must be a timestamp")
    d = base()
    d["tables"]["docs"]["columns"]["valid_until"] = {"type": "timestamp", "nullable": True}
    d["rules"][0]["using"]["and"].append({"cmp": [{"col": "valid_until"}, "<", {"const": "2026-01-01"}]})
    errs = errors(d)
    assert any("timestamp constants are not supported" in e.message for e in errs)


def test_schema_level_errors_are_reported_with_paths(tmp_path):
    bad = base()
    bad["rules"][0]["using"] = {"xor": []}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(SpecError) as ei:
        load_spec(p)
    assert any(e.path.startswith("$.rules[0].using") for e in ei.value.errors)


def test_opaque_scope_rules():
    d = base()
    d["opaques"] = {"blk": {"table": "docs", "columns": ["level"], "context": ["clearance"],
                            "sql": "level < 1 or {ctx:clearance} >= 3"}}
    d["rules"].append({"id": "X", "kind": "check", "name": "c", "table": "docs", "predicate": {"opaque": "blk"}})
    errs = errors(d)
    assert has(errs, "$.rules[9].predicate.opaque", "reads context values")
    d["rules"][-1] = {"id": "X", "kind": "policy", "name": "p", "table": "admins", "role": "authenticated",
                      "command": "select", "mode": "restrictive", "using": {"opaque": "blk"}}
    errs = errors(d)
    assert has(errs, "$.rules[9].using.opaque", "declared for table 'docs'")


def test_column_rule_validation():
    d = base()
    d["rules"].append({"id": "C1", "kind": "column_rule", "role": "authenticated", "table": "docs",
                       "columns": ["id", "nope"], "access": "read"})
    assert has(errors(d), "$.rules[9].columns[1]", "unknown column 'nope'")


# ---------------------------------------------------------------- Milestone 3: hierarchies, cardinality

def org() -> dict:
    from tests.templates.specs import org_base
    return org_base()


def test_org_example_is_clean():
    assert errors(org()) == []


def test_hierarchy_shape_errors():
    d = org()
    d["tables"]["groups"]["primary_key"] = ["id", "tenant_id"]
    d["tables"]["groups"]["columns"]["parent_id"]["nullable"] = False
    d["tables"]["groups"]["columns"]["depth"] = {"type": "text", "nullable": False}
    errs = errors(d)
    assert has(errs, "$.hierarchies.org.id", "single-column primary key")
    assert has(errs, "$.hierarchies.org.parent", "must be nullable")
    assert has(errs, "$.hierarchies.org.depth", "int NOT NULL")
    assert all(e.fix for e in errs if e.path.startswith("$.hierarchies"))


def test_hierarchy_member_type_and_depth_bound():
    d = org()
    d["tables"]["group_members"]["columns"]["group_id"]["type"] = "uuid"
    assert has(errors(d), "$.hierarchies.org.members", "must have the type of 'id'")
    d = org()
    d["hierarchies"]["org"]["max_depth"] = 17
    with pytest.raises(Exception):  # the declared bound (invariant 6) is capped by the schema
        Spec.model_validate(d)


def test_in_group_fragment_rules():
    d = org()
    d["rules"][0]["using"] = {"not": d["rules"][0]["using"]}
    errs = errors(d)
    assert has(errs, "$.rules[0].using.not", "negated in_group")
    d = org()
    d["rules"][0]["using"]["in_group"]["hierarchy"] = "nope"
    assert has(errors(d), "$.rules[0].using.in_group.hierarchy", "unknown hierarchy 'nope'")
    d = org()
    d["rules"].append({"id": "X", "kind": "check", "name": "x", "table": "files",
                       "predicate": {"in_group": {"hierarchy": "org", "key": {"col": "id"}, "value": {"col": "group_id"}}}})
    assert has(errors(d), "$.rules[6].predicate", "in_group is not allowed in a check predicate")
    d = org()
    d["tables"]["files"]["columns"]["group_id"]["type"] = "text"
    assert has(errors(d), "$.rules[0].using.in_group.value", "does not match the hierarchy id")


def test_cardinality_shape_errors():
    d = org()
    r = next(x for x in d["rules"] if x["kind"] == "cardinality")
    r["parent"] = "files"
    assert has(errors(d), "$.rules[4].parent", "different table")
    r["parent"] = "group_members"
    assert has(errors(d), "$.rules[4].parent", "single-column primary key")
    r["parent"] = "tenants"
    r["group"] = "nope"
    assert has(errors(d), "$.rules[4].group", "unknown column 'nope'")
    d = org()
    d["properties"][2]["invariants"] = [{"rule": "R1"}]
    assert any("invariants" in e.message or "cardinality" in e.message for e in errors(d))


def test_in_errors_name_the_in_paths():
    def pol(pred):
        d = base()
        d["rules"].append({"id": "X", "kind": "policy", "name": "x", "table": "docs", "role": "authenticated",
                           "command": "select", "mode": "permissive", "using": pred})
        return errors(d)

    errs = pol({"in": [{"col": "level"}, []]})
    e = next(e for e in errs if e.path == "$.rules[9].using.in[1]")
    assert "at least one constant" in e.message and "lit" in e.fix
    errs = pol({"in": [{"col": "level"}, [{"const": 1}, {"const": "two"}]]})
    assert has(errs, "$.rules[9].using.in", "type mismatch: int = text")
    errs = pol({"in": [{"col": "owner_id"}, [{"const": "00000000-0000-0000-0000-000000000001"}, {"const": "nope"}]]})
    assert has(errs, "$.rules[9].using.in[1][1]", "not a UUID literal")
    errs = pol({"in": [{"col": "lvl"}, [{"const": 1}]]})
    assert has(errs, "$.rules[9].using.in[0]", "unknown column 'lvl'")
    errs = pol({"in": [{"const": 1}, [{"const": 1}]]})
    assert has(errs, "$.rules[9].using.in[0]", "not a constant")
    assert not pol({"in": [{"col": "level"}, [{"const": 1}, {"const": 2}]]})


def test_in_is_in_the_schema_and_shape_errors_name_the_path():
    assert "In" in spec_json_schema()["$defs"]
    from pydantic import ValidationError
    from mls.spec.check import pydantic_errors_to_mls

    with pytest.raises(ValidationError) as ei:
        Spec.model_validate({**base(), "rules": [{"id": "X", "kind": "check", "name": "x", "table": "docs",
                                                   "predicate": {"in": [{"col": "level"}, {"const": 1}]}}]})
    errs = pydantic_errors_to_mls(ei.value)
    assert any(e.path.startswith("$.rules[0].predicate.in") for e in errs), [e.path for e in errs]
