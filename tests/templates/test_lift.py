"""The lifter (D46) on real Postgres: compile a fixture, deploy, import, lift.  The lifted spec
has no opaque block, its drift against the deployment is clean, and it is net-equivalent to the
source per `behaviour_diff`, on PG 15-18.  The hand-written idioms are deployed as policies and
read back from the live catalog into the shapes `tests/pg/test_lift.py` pins; a policy calling a
user function stays opaque, a one-line SQL wrapper included."""
from __future__ import annotations

import json

import pytest

from mls.compile import compile_spec
from mls.pg.adopt import import_schema
from mls.pg.drift import drift
from mls.pg.harness import Harness
from mls.props.diff import behaviour_diff
from mls.spec import check_spec
from mls.spec.model import Spec
from tests.pg.test_lift import IDIOMS
from tests.templates import specs

pytestmark = pytest.mark.pg

ROUND_TRIP = {
    "policy": specs.policy_spec,
    "in": specs.in_spec,
    "time": specs.time_spec,
    "check": specs.check_nullable_spec,
    "transition": specs.transition_nullable_spec,
    "column": specs.column_rule_spec,
    "fk": specs.fk_spec,
    "session_setting": specs.session_spec,
    "pg_roles": specs.pg_roles_spec,
}


def renamed_like(source: Spec, imported: dict) -> Spec:
    """The imported spec with its context values named as the source names them (the import
    cannot know a name; the source is the value's identity)."""
    by_src = {cv.source: n for n, cv in source.identity.context.items()}
    text = json.dumps(imported)
    for n, cv in imported["identity"]["context"].items():
        want = by_src.get(cv["source"])
        if want and want != n:
            text = text.replace(json.dumps({"ctx": n}), json.dumps({"ctx": want}))
            text = text.replace(f'"{n}": {{"source"', f'"{want}": {{"source"')
    return check_spec(Spec.model_validate(json.loads(text)))


def deploy(spec: Spec, dsn: str, name: str) -> Harness:
    h = Harness(spec, compile_spec(spec), dsn, name)
    h.deploy()
    return h


@pytest.mark.parametrize("name", list(ROUND_TRIP))
def test_round_trip_per_template(name, pg_dsn):
    source = ROUND_TRIP[name]()
    h = deploy(source, pg_dsn, f"mls_lift_{name}")
    try:
        res = import_schema(h.conn)
        assert res.spec["opaques"] == {}, res.notes
        imported = renamed_like(source, res.spec)
        d = drift(h.conn, imported, compile_spec(imported))
        assert d.ok and d.items == [], [str(i) for i in d.items]
        bad = [c for c in behaviour_diff(source, imported) if c.verdict != "PROVED"]
        assert bad == [], [str(c) for c in bad]
        # the compiler's own relation policies and grants are recognized, not re-imported as rules
        assert not any(p.name.startswith("mls_rel_") for p in imported.policies())
        assert any("relation-visibility policy" in n for n in res.notes)
        # what the source declares as rules comes back as rules of the same kind and name
        kinds = {(r.kind, r.name) for r in source.rules if r.kind != "column_rule"}
        assert kinds <= {(r.kind, r.name) for r in imported.rules}
    finally:
        h.close()


def test_opaque_block_whose_sql_is_in_the_fragment_is_lifted(pg_dsn):
    source = specs.opaque_spec()
    h = deploy(source, pg_dsn, "mls_lift_opaque")
    try:
        res = import_schema(h.conn)
        assert res.spec["opaques"] == {}
        imported = check_spec(Spec.model_validate(res.spec))
        gate = next(p for p in imported.policies() if p.name == "opaque_gate")
        assert gate.using.model_dump(mode="json", by_alias=True, exclude_none=True) == {
            "or": [{"cmp": [{"col": "level"}, "<", {"const": 1}]}, {"cmp": [{"ctx": "clearance"}, ">=", {"const": 3}]}]}
        d = drift(h.conn, imported, compile_spec(imported))
        assert d.ok and d.items == [], [str(i) for i in d.items]
        # net-equivalent to the source with the block replaced by what its SQL says
        src = json.loads(json.dumps(specs.base()))
        src = specs.opaque_spec().model_dump(by_alias=True, exclude_none=True, exclude_defaults=True)
        src["opaques"] = {}
        for r in src["rules"]:
            if r["name"] == "opaque_gate":
                r["using"] = gate.using.model_dump(mode="json", by_alias=True, exclude_none=True)
                r.pop("check", None)
        bad = [c for c in behaviour_diff(check_spec(Spec.model_validate(src)), imported) if c.verdict != "PROVED"]
        assert bad == [], [str(c) for c in bad]
    finally:
        h.close()


IDIOM_SQL = {
    "bare_auth_uid": "auth.uid() = owner_id",
    "bare_claim": "((auth.jwt() ->> 'clearance')::int >= level)",
    "in_subquery": "tenant_id in (select tenant_id from memberships where user_id = auth.uid())",
    "correlated_exists": "exists (select 1 from memberships m where m.tenant_id = docs.tenant_id and m.user_id = auth.uid())",
    "exists_unaliased": ("exists (select 1 from memberships where memberships.user_id = (select auth.uid()) "
                         "and memberships.tenant_id = docs.tenant_id)"),
    "in_list_keyword": "level in (2, 3)",
    "not_in_list": "level not in (2, 3)",
    "not_in_list_compiled": "not (level = any (array[2, 3]))",
    "bool_column": "not archived_flag",
    "claim_text_const": "(auth.jwt() ->> 'role') = 'editor'",
    "flipped_ctx": "(select (auth.jwt() ->> 'clearance')::int) >= level",
}


def test_idioms_read_back_from_the_live_catalog(pg_dsn):
    """Every hand-written idiom, deployed as a policy and deparsed by this version, lifts to the
    shape the golden test pins."""
    assert set(IDIOM_SQL) == set(IDIOMS)
    source = specs.policy_spec()
    h = deploy(source, pg_dsn, "mls_lift_idioms")
    try:
        with h.conn.cursor() as cur:
            cur.execute("alter table docs add column archived_flag boolean not null default false")
            cur.execute("alter table docs add column title text")
            for name, sql in IDIOM_SQL.items():
                cur.execute(f"create policy {name} on docs for select to authenticated using ({sql})")
            cur.execute("create policy like_title on docs for select to authenticated "
                        "using (owner_id = auth.uid() and title like 'draft%')")
        res = import_schema(h.conn)
        by_name = {r["name"]: r for r in res.spec["rules"] if r["kind"] == "policy"}
        for name, (_, expected, hint) in IDIOMS.items():
            assert by_name[name]["using"] == expected, (name, res.notes)
            if hint:
                assert any(n.startswith("lint: ") and name in n and hint in n for n in res.notes), (name, res.notes)
        like = by_name["like_title"]["using"]
        assert like["and"][0] == {"cmp": [{"col": "owner_id"}, "=", {"ctx": "uid"}]}
        block = res.spec["opaques"][like["and"][1]["opaque"]]
        assert block["columns"] == ["title"] and "~~" in block["sql"]
        assert list(res.spec["opaques"]) == [like["and"][1]["opaque"]]
        check_spec(Spec.model_validate(res.spec))
    finally:
        h.close()


def test_user_function_stays_opaque_even_as_a_one_line_wrapper(pg_dsn):
    source = specs.policy_spec()
    h = deploy(source, pg_dsn, "mls_lift_fn")
    try:
        with h.conn.cursor() as cur:
            cur.execute("create function is_member(u uuid, t integer) returns boolean language sql stable as $$ "
                        "select exists (select 1 from memberships where user_id = u and tenant_id = t) $$")
            cur.execute("grant execute on function is_member(uuid, integer) to authenticated")
            cur.execute("create policy wrapped on docs for select to authenticated "
                        "using (is_member((select auth.uid()), tenant_id) and level <= 2)")
        res = import_schema(h.conn)
        wrapped = next(r for r in res.spec["rules"] if r["name"] == "wrapped")
        assert wrapped["using"]["and"][1] == {"cmp": [{"col": "level"}, "<=", {"const": 2}]}
        block = res.spec["opaques"][wrapped["using"]["and"][0]["opaque"]]
        assert block["sql"].startswith("is_member(") and block["columns"] == ["tenant_id"] and block["context"] == ["uid"]
        assert any("wrapped" in n and "never inlined" in n and "D34" in n for n in res.notes)
        assert list(res.spec["opaques"]) == [wrapped["using"]["and"][0]["opaque"]]
    finally:
        h.close()


def test_hierarchy_helper_stays_opaque_until_the_hierarchy_is_declared(pg_dsn):
    source = specs.hierarchy_spec()
    h = deploy(source, pg_dsn, "mls_lift_hier")
    try:
        res = import_schema(h.conn)
        reads = [r for r in res.spec["rules"] if r["kind"] == "policy" and r["name"] == "group_read"]
        assert reads and "opaque" in reads[0]["using"]
        assert any("group_read" in n and "hierarchy the spec does not declare" in n for n in res.notes)
        assert any("trigger mls_h_org_depth" in n and "not imported" in n for n in res.notes)
    finally:
        h.close()
