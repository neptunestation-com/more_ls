"""Expected PROVED / REFUTED verdicts and counterexample shapes (ROADMAP M1 acceptance)."""
from __future__ import annotations

import copy
import json

import pytest

from mls.compile import compile_spec
from mls.errors import CompileError
from mls.logic.solve import solve
from mls.logic.term import not_
from mls.logic.tvl import Encoder
from mls.props.runner import run_all, run_property
from mls.spec import check_spec
from mls.spec.model import Spec
from tests.conftest import EXAMPLE


def base() -> dict:
    return json.loads(EXAMPLE.read_text())


def make(d: dict) -> Spec:
    spec = Spec.model_validate(d)
    check_spec(spec)
    return spec


def verdicts(spec: Spec) -> dict[str, str]:
    return {r.id: r.verdict for r in run_all(spec)}


def test_example_all_proved(example_spec):
    assert set(verdicts(example_spec).values()) == {"PROVED"}


def test_isolation_refuted_by_owner_read():
    """Only without the owner_in_tenant FK: with it, an owner is provably a member."""
    d = base()
    d["rules"] = [r for r in d["rules"] if r["id"] != "R7"]
    d["properties"][3]["invariants"] = [{"rule": "R6"}, {"rule": "R8"}]
    d["rules"].append({"id": "R10", "kind": "policy", "name": "owner_read", "table": "docs",
                       "role": "authenticated", "command": "select", "mode": "permissive",
                       "using": {"cmp": [{"col": "owner_id"}, "=", {"ctx": "uid"}]}})
    spec = make(d)
    r = run_property(spec, spec.properties[0])
    assert r.verdict == "REFUTED"
    bad = next(o for o in r.obligations if o.verdict == "REFUTED")
    sc = bad.scenario
    assert sc.rows["r"]["values"]["owner_id"] == sc.context["uid"]
    assert [sc.context["uid"], sc.rows["r"]["values"]["tenant_id"]] not in sc.relations["member_of"]
    d["rules"].append({"id": "R7", "kind": "inclusion", "name": "owner_in_tenant", "table": "docs",
                       "columns": ["owner_id", "tenant_id"], "relation": "member_of", "on_delete": "restrict"})
    assert run_property(make(d), spec.properties[0]).verdict == "PROVED"


def test_tenant_move_refuted_without_transition_proved_with_it(example_spec):
    d = base()
    d["rules"] = [r for r in d["rules"] if r["id"] != "R9"]
    spec = make(d)
    r = run_property(spec, next(p for p in spec.properties if p.id == "P2"))
    assert r.verdict == "REFUTED"
    sc = r.obligations[0].scenario
    old, new = sc.rows["old"]["values"], sc.rows["new"]["values"]
    assert old["tenant_id"] != new["tenant_id"]
    uid = sc.context["uid"]
    assert [uid, old["tenant_id"]] in sc.relations["member_of"]
    assert [uid, new["tenant_id"]] in sc.relations["member_of"]
    assert verdicts(example_spec)["P2"] == "PROVED"


def inline_invariants() -> list[dict]:
    return [
        {"rule": "R6"},
        {"table": "docs", "predicate": {"or": [{"is_null": {"col": "owner_id"}},
                                               {"member": {"relation": "member_of", "key": {"col": "owner_id"},
                                                           "value": {"col": "tenant_id"}}}]}},
        {"table": "admins", "predicate": {"member": {"relation": "member_of", "key": {"col": "user_id"},
                                                     "value": {"col": "tenant_id"}}}},
    ]


@pytest.mark.parametrize("drop,expected,failing", [
    (["R7", "R8"], "REFUTED", {"docs", "admins"}),
    (["R8"], "REFUTED", {"admins"}),
    ([], "PROVED", set()),
])
def test_membership_delete_induction_needs_both_fks(drop, expected, failing):
    d = base()
    d["rules"] = [r for r in d["rules"] if r["id"] not in drop]
    d["properties"] = [{"id": "P4", "template": "invariant_preserved", "invariants": inline_invariants()}]
    spec = make(d)
    r = run_property(spec, spec.properties[0])
    assert r.verdict == expected
    refuted = [o for o in r.obligations if o.verdict == "REFUTED"]
    deletes = [o for o in refuted if "delete on memberships" in o.name]
    tables = {r["table"] for o in deletes for n, r in o.scenario.rows.items() if n.startswith("row_")}
    assert tables == failing
    # without the FK, writes to the invariant's own table can also break it
    assert {o.name.split(" preserves")[0] for o in refuted if o not in deletes} <= {
        "insert on docs", "update on docs", "insert on admins", "update on admins",
        "update on memberships"}
    for o in deletes:
        deleted = o.scenario.rows["old"]["values"]
        assert [deleted["user_id"], deleted["tenant_id"]] in o.scenario.relations["member_of@before"]
        assert [deleted["user_id"], deleted["tenant_id"]] not in o.scenario.relations["member_of@after"]


def test_naive_check_admits_null_rows(example_spec):
    """CHECK (p) admits rows where p is not FALSE; CHECK ((p) IS TRUE) admits iff p is TRUE."""
    from pydantic import TypeAdapter
    from mls.spec.model import Pred

    pred = TypeAdapter(Pred).validate_python(
        {"or": [{"cmp": [{"col": "archived"}, "=", {"const": 0}]}, {"cmp": [{"col": "archived"}, "=", {"const": 1}]}]})
    enc = Encoder(example_spec)
    t, f = enc.tf(pred, "r", "docs")
    naive = solve([not_(f)], t)
    assert naive.status == "refuted"
    assert naive.model.var("r.archived.null", "Bool") is True
    assert solve([t], t).proved


def test_write_subset_read_refuted_when_check_is_weaker():
    d = base()
    d["rules"][3]["check"] = {"member": {"relation": "member_of", "key": {"ctx": "uid"}, "value": {"col": "tenant_id"}}}
    spec = make(d)
    r = run_property(spec, next(p for p in spec.properties if p.id == "P5"))
    assert r.verdict == "REFUTED"
    sc = next(o.scenario for o in r.obligations if o.verdict == "REFUTED")
    assert sc.rows["new"]["values"]["level"] > sc.context["clearance"]


def test_least_privilege_refuted_lists_extra_privilege():
    d = base()
    d["properties"] = [{"id": "P6", "template": "least_privilege",
                        "allowed": {"authenticated": {"docs": ["select"], "memberships": ["select"], "admins": ["select"]}}}]
    spec = make(d)
    r = run_property(spec, spec.properties[0])
    assert r.verdict == "REFUTED"
    assert "authenticated may insert all columns of docs" in r.obligations[0].message


def test_monotonicity_refuted_by_opaque_reading_relation():
    d = base()
    d["opaques"] = {"outsider": {"table": "docs", "columns": ["tenant_id"], "context": ["uid"],
                                 "relations": ["member_of"],
                                 "sql": "not exists (select 1 from memberships m where m.user_id = {ctx:uid} and m.tenant_id = tenant_id)"}}
    d["rules"].append({"id": "R10", "kind": "policy", "name": "outsider_public", "table": "docs",
                       "role": "authenticated", "command": "select", "mode": "permissive",
                       "using": {"and": [{"opaque": "outsider"}, {"cmp": [{"col": "level"}, "=", {"const": 0}]}]}})
    spec = make(d)
    r = run_property(spec, next(p for p in spec.properties if p.id == "P3"))
    assert r.verdict == "REFUTED"
    assert r.conditional_on == ["outsider"]
    # full opaque reporting: the summary counts conditional proofs and the assumptions quote the SQL
    from mls.props.report import to_json, to_markdown
    results = run_all(spec)
    n_cond = sum(1 for x in results if x.verdict == "PROVED" and x.conditional_on)
    assert n_cond > 0
    md = to_markdown(spec, results)
    assert f"({n_cond} conditional on opaque blocks)" in md
    assert "opaque block 'outsider' on docs (columns tenant_id; context uid; relations member_of) is unverified SQL: `not exists" in md
    assert to_json(spec, results)["summary"]["CONDITIONAL"] == n_cond
    # and isolation is now conditional on the block: the block could admit any row
    p1 = run_property(spec, spec.properties[0])
    assert p1.verdict == "REFUTED" and p1.conditional_on == ["outsider"]


def test_custom_property():
    d = base()
    d["properties"] = [{"id": "C1", "template": "custom", "table": "docs", "role": "authenticated",
                        "command": "select", "goal": {"not": {"cmp": [{"col": "archived"}, "=", {"const": 1}]}}},
                       {"id": "C2", "template": "custom", "table": "docs", "role": "authenticated",
                        "command": "select", "goal": {"is_null": {"col": "archived"}}}]
    spec = make(d)
    v = verdicts(spec)
    assert v == {"C1": "PROVED", "C2": "REFUTED"}


def test_relation_visibility_obligation_blocks_compile():
    d = base()
    d["rules"].append({"id": "R10", "kind": "policy", "name": "positive_tenants", "table": "memberships",
                       "role": "authenticated", "command": "select", "mode": "restrictive",
                       "using": {"cmp": [{"col": "tenant_id"}, ">", {"const": 0}]}})
    spec = make(d)
    with pytest.raises(CompileError) as ei:
        compile_spec(spec)
    e = ei.value.errors[0]
    assert e.path == "$.rules[0]" and "membership subquery would be filtered" in e.message and e.fix
    # a restrictive policy that keeps the user's own rows visible is fine
    d["rules"][-1]["using"] = {"cmp": [{"col": "user_id"}, "=", {"ctx": "uid"}]}
    compile_spec(make(d))


def test_report_formats(example_spec):
    from mls.props.report import dumps_json, to_markdown

    results = run_all(example_spec)
    md = to_markdown(example_spec, results)
    assert "## Assumptions" in md and "identity adapter 'supabase'" in md
    js = json.loads(dumps_json(example_spec, results))
    assert js["summary"]["PROVED"] == 10 and js["properties"][0]["obligations"]
