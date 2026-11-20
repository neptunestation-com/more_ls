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
    spec = check_spec(spec)
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
    member = {"member": {"relation": "member_of", "key": {"ctx": "uid"}, "value": {"col": "tenant_id"}}}
    d["properties"].append({"id": "C1", "template": "custom", "table": "docs", "role": "authenticated",
                            "command": "select", "goal": {"or": [{"opaque": "outsider"}, member]}})
    spec = make(d)
    results = run_all(spec)
    by_id = {x.id: x for x in results}
    # conditional only where the proof needs the block (#27): C1 holds for the rows R10 admits
    # because the goal reads the same block on the same row; write ⊆ read and the lower bounds
    # hold through the other policies whatever the block returns
    assert by_id["C1"].verdict == "PROVED" and by_id["C1"].conditional_on == ["outsider"]
    for pid in ("P5", "P7", "P8"):
        assert by_id[pid].verdict == "PROVED" and by_id[pid].conditional_on == [], pid
    n_cond = sum(1 for x in results if x.verdict == "PROVED" and x.conditional_on)
    assert n_cond == 1
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


def test_role_separation_proved_and_refuted():
    """D49: no row of docs is read by both authenticated and an auditor who reads archived rows
    (R5 keeps archived rows from authenticated); an auditor reading level-0 rows shares rows."""
    d = base()
    d["roles"].append("auditor")
    d["rules"].append({"id": "A1", "kind": "policy", "name": "auditor_read", "table": "docs", "role": "auditor",
                       "command": "select", "mode": "permissive",
                       "using": {"cmp": [{"col": "archived"}, "=", {"const": 1}]}})
    d["properties"] = [{"id": "S1", "template": "role_separation", "table": "docs",
                        "roles": ["authenticated", "auditor"], "command": "select"}]
    spec = make(d)
    r = run_property(spec, spec.properties[0])
    assert r.verdict == "PROVED" and r.obligations[0].name == "select: no row is admitted to both authenticated and auditor"
    d["rules"][-1]["using"] = {"cmp": [{"col": "level"}, "<=", {"const": 0}]}
    spec = make(d)
    r = run_property(spec, spec.properties[0])
    assert r.verdict == "REFUTED"
    sc = r.obligations[0].scenario
    row = sc.rows["r"]["values"]
    assert row["level"] <= 0 and row["archived"] != 1  # both roles admit it: a member's unarchived level-0 row
    assert [sc.context["uid"], row["tenant_id"]] in sc.relations["member_of"] or \
        [sc.context["uid"], row["tenant_id"]] in sc.relations["admin_of"] or row["owner_id"] == sc.context["uid"]
    # the same role twice, or an unknown role, is refused by the checker
    from mls.errors import SpecError
    d["properties"][0]["roles"] = ["authenticated", "nobody"]
    with pytest.raises(SpecError) as ei:
        make(d)
    assert ei.value.errors[0].path == "$.properties[0].roles[1]"


def test_a_stored_row_of_a_relation_table_is_in_the_relation(monkeypatch):
    """The takeover example's P8 is about the membership table itself (a user sees only
    memberships of their own teams, through `user_id = @uid`): it holds because the row read is
    in the relation.  Without the relation row axiom the solver picks a row the relation does
    not contain, a state no database can be in, and refutes it."""
    from mls.props import obligations
    from mls.props.runner import run_all
    from mls.spec import load_checked
    from tests.conftest import ROOT

    spec = load_checked(ROOT / "examples" / "starter_takeover.mls")
    assert {r.id: r.verdict for r in run_all(spec)} == {f"P{i}": "PROVED" for i in range(1, 10)}
    monkeypatch.setattr(obligations, "relation_row_axioms", lambda enc, rows: [])
    assert {r.id: r.verdict for r in run_all(spec) if r.id == "P8"} == {"P8": "REFUTED"}


RECIPE = """mls 0.1
identity supabase { uid: auth.uid uuid }
roles authenticated

table projects adopted {
  id bigint not null,
  owner_id uuid not null,
  team_id bigint not null,
  deleted_at timestamp;
  primary_key (id)
}

table team_members adopted {
  team_id bigint not null,
  user_id uuid not null;
  primary_key (team_id, user_id)
}

relation team_members = team_members(user_id, team_id)

opaque is_admin on projects (@uid) relations () sql $$ public.is_admin() $$

rules {
  R1 policy owner_all on projects: authenticated all permissive using owner_id = @uid
  R4 policy admin_read on projects: authenticated select permissive using opaque(is_admin)
  R5 policy hide_deleted on projects: authenticated select restrictive using deleted_at is null
}

properties {
  P2 must_allow projects authenticated select: owner_id = @uid and deleted_at is null
  P3 custom projects authenticated select new: deleted_at is null
  P4 custom projects authenticated select new: owner_id = @uid or opaque(is_admin)
}
"""


def test_a_proof_is_conditional_only_on_the_blocks_it_needs():
    """GUIDE §7's `is_admin()` recipe (#27): the restrictive policy alone proves P3, whatever the
    block returns; P2's witness shares an encoder with the access layers, which read the block,
    but its own formula does not (its well-formedness axiom is not a use); P4 needs the block to
    be the same value in the policy and in the goal, so it stays conditional."""
    from mls.spec.syntax import parse

    results = {r.id: r for r in run_all(check_spec(parse(RECIPE)))}
    assert {k: (r.verdict, r.conditional_on) for k, r in results.items()} == {
        "P2": ("PROVED", []), "P3": ("PROVED", []), "P4": ("PROVED", ["is_admin"])}


def test_both_state_copies_intern_text_constants_alike():
    """The induction step encodes the old row with one encoder and the new row with another; they
    must number text constants alike, or 'x' before and 'z' after can become the same value.  An
    update that keeps the status or sets it to 'z' preserves `status <> 'x'`."""
    d = base()
    d["tables"]["docs"]["columns"]["status"] = {"type": "text", "nullable": False}
    d["rules"].append({"id": "T1", "kind": "transition", "name": "status_kept_or_z", "table": "docs",
                       "predicate": {"or": [{"cmp": [{"new": "status"}, "=", {"old": "status"}]},
                                            {"cmp": [{"new": "status"}, "=", {"const": "z"}]}]}})
    d["properties"] = [{"id": "P1", "template": "invariant_preserved",
                        "invariants": [{"table": "docs", "predicate": {"cmp": [{"col": "status"}, "<>",
                                                                               {"const": "x"}]}}]}]
    spec = make(d)
    r = run_property(spec, spec.properties[0])
    update = [o for o in r.obligations if o.name.startswith("update on docs preserves")]
    assert update and all(o.verdict == "PROVED" for o in update), [(o.name, o.verdict) for o in update]


def test_an_enum_column_holds_only_its_labels():
    """D59: Postgres refuses any other value, and the proofs assume it.  `status <> 'In review'`
    leaves exactly the three states the policy lists, so P11 holds; with a fourth label the
    policy does not list, the counterexample is a document in that state."""
    from tests.templates.specs import ENUM_LABELS, enum_spec

    spec = enum_spec()
    assert run_property(spec, next(p for p in spec.properties if p.id == "P11")).verdict == "PROVED"
    spec = enum_spec(ENUM_LABELS + ["Extra"])
    r = run_property(spec, next(p for p in spec.properties if p.id == "P11"))
    assert r.verdict == "REFUTED"
    assert {o.scenario.rows["r"]["values"]["status"] for o in r.obligations if o.verdict == "REFUTED"} == {"'Extra'"}
