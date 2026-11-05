"""The lifter (D46) without Postgres: the deparsed text of every shape the compiler emits and of
every hand-written idiom, pinned to the predicate it lifts to.  The strings are what PG 15-18
deparse (identical across the four for these shapes; `tests/templates/test_lift.py` re-derives
them from a live catalog on every version).  The golden values are shapes (predicate JSON), not
strings (D30)."""
from __future__ import annotations

import json

import pytest

from mls.errors import SpecError
from mls.pg.lift import (
    Env, Unreadable, lift, now_between_shape, nullable_columns, parse, strip_check, transition_expr,
)
from mls.spec import check_spec
from mls.spec.model import Spec, load_spec
from tests.conftest import EXAMPLE
from tests.templates import specs

UID = "( SELECT auth.uid() AS uid)"
CLEARANCE = "( SELECT ((auth.jwt() ->> 'clearance'::text))::integer AS int4)"
MEMBER = ("(tenant_id = ANY (ARRAY( SELECT memberships.tenant_id\n   FROM memberships\n"
          "  WHERE (memberships.user_id = ( SELECT auth.uid() AS uid)))))")
MEMBER_ATOM = {"member": {"relation": "member_of", "key": {"ctx": "uid"}, "value": {"col": "tenant_id"}}}
LEVEL_LE = {"cmp": [{"col": "level"}, "<=", {"ctx": "clearance"}]}


def env_of(spec: Spec | None = None, **cols) -> Env:
    spec = spec or check_spec(Spec.model_validate(json.loads(EXAMPLE.read_text())))
    env = Env.of_spec(spec)
    for c, (ty, nullable) in cols.items():
        env.tables["docs"][c] = (ty, nullable)
    return env


class Sink:
    """Registers the opaque blocks and relations the lifter asks for, as the import does."""

    def __init__(self, env: Env, table: str = "docs"):
        self.env, self.table, self.blocks = env, table, {}

    def opaque(self, sql, cols, ctx, rels):
        name = f"op{len(self.blocks) + 1}"
        self.blocks[name] = {"table": self.table, "columns": cols, "context": ctx, "relations": rels, "sql": sql}
        self.env.opaques[name] = dict(self.blocks[name])
        return name

    def relation(self, table, key, value):
        for n, trip in self.env.relations.items():
            if trip == (table, key, value):
                return n
        name = f"{table}_{key}_{value}"
        self.env.relations[name] = (table, key, value)
        return name


def do(text: str, scope: str = "policy", env: Env | None = None, top_is_true: bool = False, table: str = "docs"):
    env = env or env_of()
    sink = Sink(env, table)
    r = lift(text, table, env, scope, sink.opaque, sink.relation, top_is_true=top_is_true)
    return r, sink


# ----------------------------------------------------------------------------- compiler shapes
COMPILER_SHAPES = {
    "member_and_cmp": (f"({MEMBER} AND (level <= {CLEARANCE}))", {"and": [MEMBER_ATOM, LEVEL_LE]}),
    "not_cmp_const": ("(NOT (archived = 1))", {"not": {"cmp": [{"col": "archived"}, "=", {"const": 1}]}}),
    "in_list": ("(level = ANY (ARRAY[2, 3]))", {"in": [{"col": "level"}, [{"const": 2}, {"const": 3}]]}),
    "ctx_cmp_const": (f"((level < 1) OR ({CLEARANCE} >= 3))",
                      {"or": [{"cmp": [{"col": "level"}, "<", {"const": 1}]},
                              {"cmp": [{"ctx": "clearance"}, ">=", {"const": 3}]}]}),
    "is_null": ("(owner_id IS NULL)", {"is_null": {"col": "owner_id"}}),
    "is_not_null": ("(owner_id IS NOT NULL)", {"not": {"is_null": {"col": "owner_id"}}}),
    "lit": ("true", {"lit": True}),
    "uuid_const": ("(owner_id = '00000000-0000-0000-0000-000000000001'::uuid)",
                   {"cmp": [{"col": "owner_id"}, "=", {"const": "00000000-0000-0000-0000-000000000001"}]}),
    "negative_const": ("(level >= '-1'::integer)", {"cmp": [{"col": "level"}, ">=", {"const": -1}]}),
}


@pytest.mark.parametrize("name", list(COMPILER_SHAPES))
def test_compiler_shapes_lift_exactly(name):
    text, expected = COMPILER_SHAPES[name]
    r, sink = do(text)
    assert (r.kind, r.pred) == ("lifted", expected)
    assert not sink.blocks and not r.reasons and not r.hints


def test_now_between_folds_back():
    now = "( SELECT now() AS now)"
    text = (f"(((valid_from IS NULL) OR (valid_from <= {now})) AND "
            f"((valid_until IS NULL) OR ({now} < valid_until)))")
    r, _ = do(text, env=env_of(valid_from=("timestamp", True), valid_until=("timestamp", True)))
    assert r.pred == {"now_between": {"from": {"col": "valid_from"}, "until": {"col": "valid_until"}}}
    half = f"((valid_until IS NULL) OR ({now} < valid_until))"
    r, _ = do(half, env=env_of(valid_until=("timestamp", True)))
    assert r.pred == {"now_between": {"from": None, "until": {"col": "valid_until"}}}
    # the fold is a shape over the lifted predicate, so a hand-written half reads the same
    assert now_between_shape({"or": [{"is_null": {"col": "f"}}, {"cmp": [{"col": "f"}, "<=", {"ctx": "now"}]}]}) \
        == {"now_between": {"from": {"col": "f"}, "until": None}}


def test_check_template_is_read_with_its_is_true_stripped():
    d = "CHECK ((((level >= 0) AND (level <= 3)) IS TRUE))"
    expr = strip_check(d)
    r, _ = do(expr, scope="check", top_is_true=True)
    assert r.kind == "lifted"
    assert r.pred == {"and": [{"cmp": [{"col": "level"}, ">=", {"const": 0}]},
                              {"cmp": [{"col": "level"}, "<=", {"const": 3}]}]}
    # the compiled `in` inside a CHECK
    r, _ = do(strip_check("CHECK (((flag = ANY (ARRAY[0, 1])) IS TRUE))"), scope="check", top_is_true=True,
              env=env_of(flag=("int", True)))
    assert r.pred == {"in": [{"col": "flag"}, [{"const": 0}, {"const": 1}]]}
    # IS TRUE anywhere else is not read: the fragment is three-valued
    r, sink = do("(((level >= 0) IS TRUE) AND (level <= 3))", scope="check")
    assert r.kind == "partial" and "three-valued" in r.reasons[0]


def test_bare_check_reports_its_nullable_columns():
    assert nullable_columns("((level >= 0) AND (archived = 0))", env_of().tables["docs"]) == {"archived"}
    assert nullable_columns("(level >= 0)", env_of().tables["docs"]) == set()
    assert nullable_columns("(level >= 0 AND", env_of().tables["docs"]) is None


def test_in_group_lifts_when_the_hierarchy_is_declared():
    spec = specs.hierarchy_spec()
    text = "(group_id = ANY (( SELECT mls_reach_org(( SELECT auth.uid() AS uid)) AS mls_reach_org)::integer[]))"
    r, _ = do(text, env=Env.of_spec(spec), table="files")
    assert r.pred == {"in_group": {"hierarchy": "org", "key": {"ctx": "uid"}, "value": {"col": "group_id"}}}
    # the same text against a spec without the hierarchy: a helper of an undeclared hierarchy
    r, sink = do(text, table="docs", env=env_of(group_id=("int", False)))
    assert r.kind == "opaque" and "hierarchy the spec does not declare" in r.reasons[0]


def test_transition_template_is_read_back():
    tdef = 'CREATE TRIGGER tenant_immutable BEFORE UPDATE ON docs FOR EACH ROW EXECUTE FUNCTION mls_tr_docs_tenant_immutable()'
    body = ("\nbegin\n  if not ((new.\"tenant_id\" = old.\"tenant_id\") is true) then\n"
            "    raise exception 'MLS transition tenant_immutable on docs violated' using errcode = 'check_violation';\n"
            "  end if;\n  return new;\nend ")
    expr = transition_expr(tdef, "mls_tr_docs_tenant_immutable", body)
    assert expr == '(new."tenant_id" = old."tenant_id")'
    r, _ = do(expr, scope="transition")
    assert r.pred == {"cmp": [{"new": "tenant_id"}, "=", {"old": "tenant_id"}]}
    # a hand-written trigger, even on the template's shape with another function, is not read
    assert transition_expr(tdef.replace("mls_tr_docs_tenant_immutable", "touch"), "touch", body) is None
    assert transition_expr(tdef, "mls_tr_docs_tenant_immutable", "begin return new; end") is None


# ----------------------------------------------------------------------------- hand-written idioms
IDIOMS = {
    "bare_auth_uid": ("(auth.uid() = owner_id)", {"cmp": [{"col": "owner_id"}, "=", {"ctx": "uid"}]},
                      "index-friendly form"),
    "bare_claim": ("((auth.jwt() ->> 'clearance'::text))::integer >= level",
                   {"cmp": [{"col": "level"}, "<=", {"ctx": "clearance"}]}, "index-friendly form"),
    "in_subquery": ("(tenant_id IN ( SELECT memberships.tenant_id\n   FROM memberships\n"
                    "  WHERE (memberships.user_id = auth.uid())))", MEMBER_ATOM, "index-driven shape"),
    "correlated_exists": ("(EXISTS ( SELECT 1\n   FROM memberships m\n"
                          "  WHERE ((m.tenant_id = docs.tenant_id) AND (m.user_id = auth.uid()))))",
                          MEMBER_ATOM, "correlated EXISTS"),
    "exists_unaliased": ("(EXISTS ( SELECT 1\n   FROM memberships\n"
                         "  WHERE ((memberships.user_id = ( SELECT auth.uid() AS uid)) AND "
                         "(memberships.tenant_id = docs.tenant_id))))", MEMBER_ATOM, "correlated EXISTS"),
    "in_list_keyword": ("(level IN (2, 3))", {"in": [{"col": "level"}, [{"const": 2}, {"const": 3}]]}, None),
    "not_in_list": ("(level <> ALL (ARRAY[2, 3]))",
                    {"not": {"in": [{"col": "level"}, [{"const": 2}, {"const": 3}]]}}, None),
    "not_in_list_compiled": ("(NOT (level = ANY (ARRAY[2, 3])))",
                             {"not": {"in": [{"col": "level"}, [{"const": 2}, {"const": 3}]]}}, None),
    "bool_column": ("(NOT archived_flag)", {"not": {"cmp": [{"col": "archived_flag"}, "=", {"const": True}]}}, None),
    "claim_text_const": ("((auth.jwt() ->> 'role'::text) = 'editor'::text)",
                         {"cmp": [{"ctx": "role"}, "=", {"const": "editor"}]}, "index-friendly form"),
    "flipped_ctx": (f"({CLEARANCE} >= level)", LEVEL_LE, None),
}


@pytest.mark.parametrize("name", list(IDIOMS))
def test_idioms_lift_to_the_fragment_with_a_hint(name):
    text, expected, hint = IDIOMS[name]
    env = env_of(archived_flag=("bool", False))
    env.context["role"] = ("jwt.claims.role", "text")
    env.invalidate()
    r, sink = do(text, env=env)
    assert (r.kind, r.pred) == ("lifted", expected), r.reasons
    assert not sink.blocks
    if hint:
        assert any(hint in h for h in r.hints), r.hints
    else:
        assert not [h for h in r.hints if "index-friendly" not in h], r.hints


def test_like_stays_an_opaque_block_over_the_column():
    env = env_of(title=("text", True))
    r, sink = do("((owner_id = auth.uid()) AND (title ~~ 'draft%'::text))", env=env)
    assert r.kind == "partial"
    assert r.pred == {"and": [{"cmp": [{"col": "owner_id"}, "=", {"ctx": "uid"}]}, {"opaque": "op1"}]}
    assert sink.blocks["op1"] == {"table": "docs", "columns": ["title"], "context": [], "relations": [],
                                  "sql": "(title ~~ 'draft%'::text)"}
    assert "LIKE is not in the fragment" in r.reasons[0] and "leakproof lint" in r.reasons[0]


def test_like_is_never_authored(tmp_path):
    d = json.loads(EXAMPLE.read_text())
    d["rules"][0]["using"] = {"cmp": [{"col": "owner_id"}, "like", {"const": "a%"}]}
    (tmp_path / "s.json").write_text(json.dumps(d))
    with pytest.raises(SpecError) as ei:
        load_spec(tmp_path / "s.json")
    e = ei.value.errors[0]
    assert e.path == "$.rules[0].using.cmp[1]"
    assert "like" in e.message and "opaque" in e.fix and "leakproof" in e.fix


def test_user_function_is_never_inlined():
    r, sink = do("((level < 1) OR is_member(tenant_id, auth.uid()))")
    assert r.kind == "partial"
    assert r.pred == {"or": [{"cmp": [{"col": "level"}, "<", {"const": 1}]}, {"opaque": "op1"}]}
    assert sink.blocks["op1"]["sql"] == "is_member(tenant_id, auth.uid())"
    assert sink.blocks["op1"]["columns"] == ["tenant_id"] and sink.blocks["op1"]["context"] == ["uid"]
    assert "never inlined" in r.reasons[0] and "D34" in r.reasons[0]
    # the whole expression unreadable: one block, the policy's text
    r, sink = do("is_member(tenant_id, auth.uid())")
    assert r.kind == "opaque" and r.pred == {"opaque": "op1"} and sink.blocks["op1"]["sql"].startswith("is_member")


def test_negated_membership_stays_opaque():
    r, sink = do(f"(NOT {MEMBER})")
    assert r.kind == "partial" and r.pred == {"not": {"opaque": "op1"}}
    assert "anti-join" in r.reasons[0]
    assert sink.blocks["op1"]["relations"] == ["member_of"] and sink.blocks["op1"]["context"] == ["uid"]


def test_membership_needs_a_key_pair_and_a_governed_table():
    env = env_of()
    text = "(tenant_id IN ( SELECT docs.tenant_id\n   FROM docs\n  WHERE (docs.owner_id = auth.uid())))"
    r, _ = do(text, env=env, table="memberships")
    assert r.kind == "opaque" and "not a primary key or unique constraint of docs" in r.reasons[0]
    env.governed.discard("memberships")
    r, _ = do(MEMBER, env=env)
    assert r.kind == "opaque" and "observed" in r.reasons[0]


def test_proposed_relation_is_reused_by_pair():
    env = env_of()
    del env.relations["member_of"]
    env.invalidate()
    r, sink = do(f"({MEMBER} AND (level = ANY (ARRAY( SELECT memberships.level FROM memberships WHERE "
                 f"(memberships.user_id = auth.uid())))))", env=env)
    assert env.relations["memberships_user_id_tenant_id"] == ("memberships", "user_id", "tenant_id")
    assert r.pred["and"][0]["member"]["relation"] == "memberships_user_id_tenant_id"
    assert r.kind == "partial"  # (user_id, level) is not a key of memberships


def test_context_type_must_match_the_declaration():
    r, _ = do("(level <= ( SELECT ((auth.jwt() ->> 'clearance'::text))::text AS text))")
    assert r.kind == "opaque" and "declared int" in r.reasons[0]
    r, _ = do("(level <= ( SELECT ((auth.jwt() ->> 'other'::text))::integer AS int4))")
    assert r.kind == "opaque" and "not a declared context value" in r.reasons[0]


def test_other_adapters():
    env = Env.of_spec(specs.session_spec())
    text = ("(user_id = ( SELECT (NULLIF(current_setting('app.uid'::text, true), ''::text))::uuid AS \"nullif\"))")
    r, _ = do(text, env=env, table="memberships")
    assert r.pred == {"cmp": [{"col": "user_id"}, "=", {"ctx": "uid"}]} and not r.hints
    r, _ = do("(user_id = (current_setting('app.uid'::text, true))::uuid)", env=env, table="memberships")
    assert r.pred == {"cmp": [{"col": "user_id"}, "=", {"ctx": "uid"}]} and r.hints
    env = Env.of_spec(specs.pg_roles_spec())
    r, _ = do('(user_id = ( SELECT (CURRENT_USER)::text AS "current_user"))', env=env, table="memberships")
    assert r.pred == {"cmp": [{"col": "user_id"}, "=", {"ctx": "uid"}]} and not r.hints
    r, _ = do("(user_id = CURRENT_USER)", env=env, table="memberships")
    assert r.pred == {"cmp": [{"col": "user_id"}, "=", {"ctx": "uid"}]} and r.hints


def test_unparseable_text_is_one_block():
    r, sink = do("(CASE WHEN level > 1 THEN true ELSE false END)")
    assert r.kind == "opaque" and r.pred == {"opaque": "op1"}
    assert sink.blocks["op1"]["columns"] == ["level"]
    with pytest.raises(Unreadable):
        parse("(level >")
