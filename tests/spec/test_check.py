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
    """The checker's errors on `d`, and the same fixture through the terse syntax (#15): the same
    errors, path for path and message for message, each with a source span."""
    from mls.spec.syntax import parse_source
    from mls.terse import render

    spec = Spec.model_validate(d)
    errs = Checker(spec).run()
    tspec, source = parse_source(render(spec), "fixture.mls")
    assert tspec == spec
    terrs = source.locate(Checker(tspec).run())
    key = [(e.path, e.message, e.fix, e.code) for e in errs]
    assert [(e.path, e.message, e.fix, e.code) for e in terrs] == key
    assert all(e.span is not None and e.span.file == "fixture.mls" and e.span.text for e in terrs), \
        [e.path for e in terrs if e.span is None]
    assert all(e.span is None for e in errs)
    return errs


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


def test_bigint_compares_with_int_and_bounds_constants():
    """D53: int and bigint compare (in a comparison, a membership, an inclusion and a cardinality
    group); a constant outside the type's range is an error naming the fix."""
    d = base()
    d["identity"]["context"]["clearance"]["type"] = "bigint"   # level <= @clearance: int vs bigint
    for t in ("memberships", "admins"):
        d["tables"][t]["columns"]["tenant_id"]["type"] = "bigint"  # member_of value and the inclusions
    assert errors(d) == []
    d["rules"][5]["predicate"]["and"][1]["cmp"][2] = {"const": 2**31}  # level <= 2147483648, level is int
    errs = errors(d)
    e = next(e for e in errs if e.path == "$.rules[5].predicate.and[1].cmp[2]")
    assert "out of range for int" in e.message and "bigint" in e.fix
    d["tables"]["docs"]["columns"]["level"]["type"] = "bigint"
    assert errors(d) == []
    d["rules"][5]["predicate"]["and"][1]["cmp"][2] = {"const": 2**63}
    assert has(errors(d), "$.rules[5].predicate.and[1].cmp[2]", "out of range for bigint")
    d = base()
    d["identity"]["context"]["clearance"]["type"] = "bigint"
    d["rules"][0]["using"]["and"][1]["cmp"] = [{"col": "owner_id"}, "=", {"ctx": "clearance"}]
    assert has(errors(d), "$.rules[0].using.and[1].cmp", "type mismatch: uuid = bigint")


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


def test_a_second_least_privilege_property_is_refused():
    """D63: a role the allowance does not list is allowed nothing, so a second property, one per
    role, could only be refuted by the first one's roles."""
    d = base()
    lp = next(p for p in d["properties"] if p["template"] == "least_privilege")
    d["properties"].append({**copy.deepcopy(lp), "id": "P99"})
    errs = errors(d)
    i = len(d["properties"]) - 1
    assert has(errs, f"$.properties[{i}]", "a second least_privilege property")
    assert any(f"into '{lp['id']}' and remove 'P99'" in e.fix for e in errs)
    assert [e.path for e in errs] == [f"$.properties[{i}]"]


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


# ----------------------------------------------------------------------------- M5: table modes (D45)
def test_observed_table_cannot_be_named():
    d = base()
    d["tables"]["docs"]["mode"] = "observed"
    errs = errors(d)
    assert has(errs, "$.rules[0].table", "table 'docs' is observed; a rule cannot name it")
    assert any(e.path == "$.rules[0].table" and 'set tables.docs.mode to "adopted"' in e.fix for e in errs)
    assert has(errs, "$.properties[0].table", "a property cannot name it")
    d = base()
    d["tables"]["memberships"]["mode"] = "observed"
    errs = errors(d)
    assert has(errs, "$.relations.member_of.table", "a relation cannot name it")


def test_assume_is_refused_where_the_proofs_cannot_state_it():
    d = base()
    d["tables"]["docs"]["assume"] = [{"owner": "authenticated"}]
    errs = errors(d)  # owned: the hardening is emitted, not assumed
    assert has(errs, "$.tables.docs.assume", "table 'docs' is owned")
    assert any('set mode to "adopted"' in e.fix for e in errs)
    d["tables"]["docs"]["mode"] = "adopted"
    assert errors(d) == []
    d["tables"]["docs"]["assume"] = [{"owner": "nobody"}, {"rls": "disabled"}, {"bypassrls": "authenticated"},
                                     {"bypassrls": "authenticated"}]
    errs = errors(d)
    assert has(errs, "$.tables.docs.assume[0].owner", "'nobody' is not an app role")
    assert has(errs, "$.tables.docs.assume[1].rls", "not an assumption the proofs can state")
    assert any(e.path == "$.tables.docs.assume[1].rls" and 'mode to "observed"' in e.fix for e in errs)
    assert has(errs, "$.tables.docs.assume[3]", "duplicate assumption")
    # D54: a bypass path is spelled as drift reports it; spacing does not make two of them
    d["tables"]["docs"]["assume"] = [{"bypass": "view doc_index"}, {"bypass": "function can_read(integer, text)"},
                                     {"bypass": "function  can_read(integer, text)"}, {"bypass": "trigger t"},
                                     {"bypass": "function f"}]
    errs = errors(d)
    assert has(errs, "$.tables.docs.assume[2]", "duplicate assumption")
    assert has(errs, "$.tables.docs.assume[3].bypass", "'trigger t' is not a bypass path")
    assert has(errs, "$.tables.docs.assume[4].bypass", "'function f' is not a bypass path")
    assert not any(e.path in ("$.tables.docs.assume[0].bypass", "$.tables.docs.assume[1].bypass") for e in errs)
    d["tables"]["docs"]["mode"] = "observed"
    d["tables"]["docs"]["assume"] = [{"rls": "not_forced"}]
    d["rules"] = [r for r in d["rules"] if r["table"] != "docs"]
    d["properties"] = []
    assert has(errors(d), "$.tables.docs.assume", "observed and not governed")


def test_mode_shape_errors_name_the_path():
    from mls.spec.check import pydantic_errors_to_mls
    from pydantic import ValidationError

    d = base()
    d["tables"]["docs"]["mode"] = "leased"
    with pytest.raises(ValidationError) as ei:
        Spec.model_validate(d)
    assert pydantic_errors_to_mls(ei.value)[0].path == "$.tables.docs.mode"
    d["tables"]["docs"]["mode"] = "adopted"
    d["tables"]["docs"]["assume"] = [{"trusted": "x"}]
    with pytest.raises(ValidationError) as ei:
        Spec.model_validate(d)
    e = pydantic_errors_to_mls(ei.value)[0]
    assert e.path == "$.tables.docs.assume[0]" and "{owner}, {bypassrls}, {rls}, {bypass}" in e.message
    schema = spec_json_schema()
    assert "mode" in schema["$defs"]["Table"]["properties"]


def test_catalog_names_are_case_sensitive():
    """Tables, columns and roles are named as the catalog names them (D58): mixed case is kept and
    quoted wherever MLS emits it, so `userId` and `userid` are two columns."""
    d = base()
    d["roles"] = ["DocReader"]
    for r in d["rules"]:
        if r.get("role") == "authenticated":
            r["role"] = "DocReader"
    d["properties"] = []
    d["tables"]["docs"]["columns"]["ownerId"] = d["tables"]["docs"]["columns"]["owner_id"]
    d["tables"]["docs"]["columns"]["ownerid"] = {"type": "int", "nullable": True}
    d["tables"]["Audit_Log2"] = {"columns": {"eventAt": {"type": "timestamp", "nullable": False}}}
    assert errors(d) == []


@pytest.mark.parametrize("name", ["owner.id", "owner id", "owner$id", "naïve", "1st", "x" * 64, 'say"hi'])
def test_a_catalog_name_outside_the_rule_is_refused_with_the_rule(name):
    d = base()
    d["tables"]["docs"]["columns"][name] = {"type": "int", "nullable": True}
    d["tables"][name] = {"columns": {"id": {"type": "int", "nullable": False}}}
    errs = errors(d)
    assert has(errs, f"$.tables.docs.columns.{name}", "is not a valid column identifier")
    assert has(errs, f"$.tables.{name}", "is not a valid table identifier")
    assert all("letters of either case, digits and underscores" in e.fix for e in errs
               if "valid" in e.message)


def test_mls_names_stay_lower_case():
    """A relation is MLS's own name (it becomes part of generated object names), not the catalog's."""
    d = base()
    d["relations"]["MemberOf"] = d["relations"].pop("member_of")
    assert has(errors(d), "$.relations.MemberOf", "is not a valid relation identifier")


def test_enum_columns_compare_with_their_labels():
    """D59: an enum column (an adopted table's, of a declared type) compares by =, <> and `in`
    with labels of its type; anything else is an error listing the labels."""
    from tests.templates.specs import enum_dict

    assert errors(enum_dict()) == []
    st = {"col": "status"}
    for cmp, path, text in [
        ([st, "=", {"const": "Archived"}], "cmp[2]", "'Archived' is not a label of the column's enum type"),
        ([st, "<", {"const": "Draft"}], "cmp", "operator < is not allowed on enum values"),
        ([st, "=", {"col": "level"}], "cmp", "type mismatch: enum = int"),
        ([st, "=", {"ctx": "uid"}], "cmp", "type mismatch: enum = uuid"),
        ([st, "=", {"const": 1}], "cmp", "type mismatch: enum = int"),
    ]:
        d = enum_dict()
        d["rules"][-2]["using"] = {"cmp": cmp}  # R11, a policy (context values allowed)
        errs = errors(d)
        assert has(errs, f"$.rules[{len(d['rules']) - 2}].using.{path}", text), [e.message for e in errs]
    d = enum_dict()
    d["rules"][-3]["using"]["and"][1]["in"][1][0] = {"const": "draft"}  # labels are case-sensitive
    errs = errors(d)
    assert has(errs, f"$.rules[{len(d['rules']) - 3}].using.and[1].in[1][0]", "'draft' is not a label")
    assert any("'Draft', 'In review'" in e.fix for e in errs)


def test_enum_declarations_and_columns_are_checked():
    from tests.templates.specs import enum_dict

    d = enum_dict()
    d["enums"]["DocStatus"] = ["Draft", "Draft"]
    d["enums"]["Bad Name"] = []
    errs = errors(d)
    assert has(errs, "$.enums.DocStatus[1]", "duplicate label 'Draft'")
    assert has(errs, "$.enums.Bad Name", "is not a valid enum type identifier")
    assert has(errs, "$.enums.Bad Name", "an enum needs at least one label")
    d = enum_dict()
    d["tables"]["docs"]["columns"]["status"]["enum"] = "Status"
    assert has(errors(d), "$.tables.docs.columns.status.enum", "('Status' is not declared)")
    d = enum_dict()
    d["tables"]["docs"]["mode"] = "owned"
    errs = errors(d)
    assert has(errs, "$.tables.docs.columns.status.type", "table 'docs' is owned")
    assert any("status in ('A', 'B', ...)" in e.fix for e in errs)
    d = enum_dict()
    d["tables"]["docs"]["columns"]["level"]["enum"] = "DocStatus"
    assert has(errors(d), "$.tables.docs.columns.level.enum", "column 'level' is int, not enum")
    d = enum_dict()
    d["identity"]["context"]["clearance"]["type"] = "enum"
    assert has(errors(d), "$.identity.context.clearance.type", "a context value cannot be an enum")


def test_a_relation_is_unique_through_a_key_column():
    """D60: `docs(tenant_id, id)` holds each pair once because `id` is the primary key, so it is a
    relation (the owner-of-parent shape); a foreign key still needs a key on exactly the pair, so
    an inclusion rule into it is refused with that fix."""
    d = base()
    d["relations"]["doc_tenant"] = {"table": "docs", "key": "tenant_id", "value": "id"}
    assert errors(d) == []
    d["tables"]["admins"]["columns"]["doc_id"] = {"type": "int", "nullable": False}
    d["rules"].append({"id": "R10", "kind": "inclusion", "name": "admin_doc", "table": "admins",
                       "columns": ["tenant_id", "doc_id"], "relation": "doc_tenant", "on_delete": "restrict"})
    errs = errors(d)
    e = next(e for e in errs if e.path == "$.rules[9].relation")
    assert "references a primary key or unique constraint on exactly those two columns" in e.message
    assert e.fix == 'add ["tenant_id", "id"] to tables.docs.unique'
    d["tables"]["docs"]["unique"] = [["tenant_id", "id"]]
    assert errors(d) == []
    d = base()
    d["relations"]["doc_owner"] = {"table": "docs", "key": "level", "value": "tenant_id"}
    assert has(errors(d), "$.relations.doc_owner", "(level, tenant_id) is not unique in 'docs'")


def test_an_enum_relation_key_is_one_of_its_labels():
    """D60: `docs(status, id)` with an enum key column is read with a label as its key (the
    documents in a state)."""
    from tests.templates.specs import enum_dict

    def with_key(key: dict) -> dict:
        d = enum_dict()
        d["tables"]["docs"]["columns"]["status"]["nullable"] = False
        d["relations"]["doc_state"] = {"table": "docs", "key": "status", "value": "id"}
        d["rules"].append({"id": "R12", "kind": "policy", "name": "drafts", "table": "admins",
                           "role": "authenticated", "command": "select", "mode": "permissive",
                           "using": {"member": {"relation": "doc_state", "key": key, "value": {"col": "tenant_id"}}}})
        return d

    assert errors(with_key({"const": "In review"})) == []
    path = f"$.rules[{len(with_key({'const': 'x'})['rules']) - 1}].using.member.key"
    assert has(errors(with_key({"const": "Archived"})), path, "'Archived' is not a label of docs.status's enum type")
    assert has(errors(with_key({"ctx": "uid"})), path, "relation key docs.status is an enum column")


def test_a_relation_condition_is_checked_and_its_in_expanded():
    """D61: a relation's condition reads its own table's columns and constants; an `in` in it is
    expanded like any other (nothing downstream of the checker sees one)."""
    from mls.spec.model import Or
    from tests.templates.specs import condition_spec

    d = json.loads(condition_spec().model_dump_json(by_alias=True, exclude_none=True))
    d["relations"]["lead_of"]["where"] = {"in": [{"col": "role"}, [{"const": "a"}, {"const": "b"}]]}
    assert errors(d) == []
    assert isinstance(check_spec(Spec.model_validate(d)).relations["lead_of"].where, Or)
    for where, text in [({"cmp": [{"col": "role"}, "=", {"ctx": "uid"}]}, "context values are not allowed"),
                        ({"member": {"relation": "member_of", "key": {"ctx": "uid"}, "value": {"col": "team_id"}}},
                         "membership is not allowed in a relation condition"),
                        ({"cmp": [{"col": "title"}, "=", {"const": "x"}]}, "unknown column 'title'")]:
        d["relations"]["lead_of"]["where"] = where
        assert any(e.path.startswith("$.relations.lead_of.where") and text in e.message for e in errors(d)), where
    d["relations"]["lead_of"]["where"] = {"cmp": [{"col": "role"}, "=", {"const": "a"}]}
    d["rules"].append({"id": "R9", "kind": "inclusion", "name": "lead_task", "table": "tasks",
                       "columns": ["title", "project_id"], "relation": "lead_of", "on_delete": "restrict"})
    assert has(errors(d), f"$.rules[{len(d['rules']) - 1}].relation", "'lead_of' has a condition")
