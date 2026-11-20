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

    def relation(self, table, key, value, where=None):
        for n, trip in self.env.relations.items():
            if trip == (table, key, value) and self.env.conditions.get(n) == where:
                return n
        name = f"{table}_{key}_{value}" + ("_where" if where else "")
        if where is not None:
            self.env.conditions[name] = where
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
    assert "never inlined" in r.reasons[0]
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
    assert r.kind == "opaque" and "(owner_id, tenant_id) is not unique in docs" in r.reasons[0]
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


def test_varchar_and_bigint_shapes():
    """D53: a varchar column read as text deparses with casts on the column and its constants;
    a bigint constant beyond 2³¹ deparses as a cast string, a bigint context as ::bigint."""
    env = env_of(status=("text", False), big=("bigint", False))
    r, _ = do("((status)::text = 'open'::text)", env=env)
    assert r.pred == {"cmp": [{"col": "status"}, "=", {"const": "open"}]}
    text = "((status)::text = ANY ((ARRAY['open'::character varying, 'closed'::character varying])::text[]))"
    r, _ = do(strip_check(f"CHECK ({text})"), scope="check", env=env)
    assert r.pred == {"in": [{"col": "status"}, [{"const": "open"}, {"const": "closed"}]]}
    r, _ = do("(big <> '3000000000'::bigint)", env=env)
    assert r.pred == {"cmp": [{"col": "big"}, "<>", {"const": 3000000000}]}
    # the text cast of a column that is not text is not dropped
    r, _ = do("((level)::text = '1'::text)", env=env)
    assert r.kind == "opaque"


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


# ----------------------------------------------------------------------------- composed relations (D56)
NESTED_ARRAY = ("(project_id = ANY (ARRAY( SELECT projects.id\n   FROM projects\n  WHERE (projects.team_id = ANY "
                "(ARRAY( SELECT team_members.team_id\n           FROM team_members\n          WHERE "
                "(team_members.user_id = ( SELECT auth.uid() AS uid))))))))")
NESTED_ARRAY_2 = ("(task_id = ANY (ARRAY( SELECT tasks.id\n   FROM tasks\n  WHERE (tasks.project_id = ANY (ARRAY( "
                  "SELECT projects.id\n           FROM projects\n          WHERE (projects.team_id = ANY (ARRAY( SELECT "
                  "team_members.team_id\n                   FROM team_members\n                  WHERE "
                  "(team_members.user_id = ( SELECT auth.uid() AS uid)))))))))))")
NESTED_IN = ("(project_id IN ( SELECT projects.id\n   FROM projects\n  WHERE (projects.team_id IN ( SELECT "
             "team_members.team_id\n           FROM team_members\n          WHERE (team_members.user_id = auth.uid())))))")


def composed_env() -> Env:
    env = Env.of_spec(specs.composed_spec())
    env.composed = {}  # what the import starts from: no composed relation declared yet
    env.invalidate()
    return env


@pytest.mark.parametrize("text, table, hops", [
    (NESTED_ARRAY, "tasks", [("projects", "team_id", "id")]),
    (NESTED_IN, "tasks", [("projects", "team_id", "id")]),
    (NESTED_ARRAY_2, "comments", [("projects", "team_id", "id"), ("tasks", "project_id", "id")]),
])
def test_nested_memberships_lift_to_a_composed_relation(text, table, hops):
    env = composed_env()
    r, _ = do(text, env=env, table=table)
    assert r.kind == "lifted", r.reasons
    name = r.pred["member"]["relation"]
    assert env.composed[name] == ("member_of", tuple(hops))
    assert r.pred["member"]["key"] == {"ctx": "uid"}
    # the same chain read again is the same relation
    r2, _ = do(text, env=env, table=table)
    assert r2.pred["member"]["relation"] == name and len(env.composed) == 1


def test_a_nested_membership_through_a_column_that_is_not_a_key_stays_opaque():
    env = composed_env()
    env.keys["projects"] = [frozenset({"id", "team_id"})]
    r, sink = do(NESTED_ARRAY, env=env, table="tasks")
    assert r.kind == "opaque" and env.composed == {}
    assert any("projects.id, which is not the primary key or a unique column of projects" in x for x in r.reasons), r.reasons
    env = composed_env()
    env.tables["projects"]["team_id"] = ("bigint", True)
    r, _ = do(NESTED_ARRAY, env=env, table="tasks")
    assert r.kind == "opaque" and any("must be NOT NULL" in x for x in r.reasons), r.reasons


OWNED_PROJECT = ("(EXISTS ( SELECT 1\n   FROM projects\n  WHERE ((projects.id = tasks.project_id) AND "
                 "(projects.owner_id = ( SELECT auth.uid() AS uid)))))")
JOINED_OWNER = ("(EXISTS ( SELECT 1\n   FROM (tasks\n     JOIN projects ON ((projects.id = tasks.project_id)))\n"
                "  WHERE ((tasks.id = comments.task_id) AND (projects.owner_id = ( SELECT auth.uid() AS uid)))))")
JOINED_TEAM = ("(EXISTS ( SELECT 1\n   FROM ((tasks t\n     JOIN projects p ON ((p.id = t.project_id)))\n"
               "     JOIN team_members tm ON ((tm.team_id = p.team_id)))\n"
               "  WHERE ((t.id = comments.task_id) AND (tm.user_id = ( SELECT auth.uid() AS uid)))))")


def owner_env() -> Env:
    env = composed_env()
    env.tables["projects"]["owner_id"] = ("uuid", False)
    env.invalidate()
    return env


def test_the_owner_of_the_parent_row_is_a_relation():
    """D60: the correlated EXISTS on the parent row's owner reads (owner_id, id) of projects,
    unique because id is the primary key."""
    env = owner_env()
    r, _ = do(OWNED_PROJECT, env=env, table="tasks")
    assert r.kind == "lifted", r.reasons
    name = r.pred["member"]["relation"]
    assert env.relations[name] == ("projects", "owner_id", "id")
    assert r.pred["member"]["key"] == {"ctx": "uid"} and r.pred["member"]["value"] == {"col": "project_id"}


@pytest.mark.parametrize("text, base, hops", [
    (JOINED_OWNER, ("projects", "owner_id", "id"), [("tasks", "project_id", "id")]),
    (JOINED_TEAM, ("team_members", "user_id", "team_id"), [("projects", "team_id", "id"), ("tasks", "project_id", "id")]),
])
def test_an_exists_over_a_join_lifts_to_a_composed_relation(text, base, hops):
    """D60: an EXISTS over inner joins that step from the key's table to the row's through keys is
    the composed relation of that chain, whatever order the joins are written in."""
    env = owner_env()
    r, _ = do(text, env=env, table="comments")
    assert r.kind == "lifted", r.reasons
    name = r.pred["member"]["relation"]
    of, chain = env.composed[name]
    assert env.relations[of] == base and list(chain) == hops
    assert r.pred["member"]["key"] == {"ctx": "uid"} and r.pred["member"]["value"] == {"col": "task_id"}


@pytest.mark.parametrize("text, reason", [
    (JOINED_OWNER.replace("(tasks.id = comments.task_id) AND ", ""), "without both a key filter and a correlation"),
    (JOINED_OWNER.replace("(projects.owner_id = ( SELECT auth.uid() AS uid))",
                          "(projects.owner_id = ( SELECT auth.uid() AS uid)) AND (tasks.title = 'x'::text)"),
     "filters a table other than the key's"),
    (JOINED_OWNER.replace("(projects.owner_id = ( SELECT auth.uid() AS uid))",
                          "(projects.owner_id = ( SELECT auth.uid() AS uid)) AND (projects.name = comments.body)"),
     "comments.body"),
    (JOINED_OWNER.replace("JOIN projects", "LEFT JOIN projects"), "other than an inner join"),
    (JOINED_TEAM.replace("(tm.team_id = p.team_id)", "(tm.team_id = t.project_id)"),
     "not one chain from the key's table to the row's"),
])
def test_a_joined_exists_outside_the_chain_shape_stays_opaque(text, reason):
    r, _ = do(text, env=owner_env(), table="comments")
    assert r.kind == "opaque" and any(reason in x for x in r.reasons), r.reasons


# ----------------------------------------------------------------------------- relation conditions (D61)
ADMIN_EXISTS = ("(EXISTS ( SELECT 1\n   FROM team_members\n  WHERE ((team_members.team_id = projects.team_id) AND "
                "(team_members.user_id = ( SELECT auth.uid() AS uid)) AND (team_members.role = ANY "
                "(ARRAY['lead'::text, 'owner'::text])))))")
ADMIN_ARRAY = ("(team_id = ANY (ARRAY( SELECT team_members.team_id\n   FROM team_members\n  WHERE "
               "((team_members.user_id = ( SELECT auth.uid() AS uid)) AND (team_members.role = ANY "
               "(ARRAY['lead'::text, 'owner'::text]))))))")
ADMIN_JOIN = ("(EXISTS ( SELECT 1\n   FROM (projects p\n     JOIN team_members tm ON ((tm.team_id = p.team_id)))\n"
              "  WHERE ((p.id = tasks.project_id) AND (tm.user_id = ( SELECT auth.uid() AS uid)) AND "
              "(tm.role = ANY (ARRAY['lead'::text, 'owner'::text])))))")
LEAD_OR_OWNER = {"in": [{"col": "role"}, [{"const": "lead"}, {"const": "owner"}]]}


def role_env() -> Env:
    env = owner_env()
    env.tables["team_members"]["role"] = ("text", True)
    env.invalidate()
    return env


@pytest.mark.parametrize("text, table", [(ADMIN_EXISTS, "projects"), (ADMIN_ARRAY, "projects")])
def test_a_condition_on_the_relation_table_is_the_relations_condition(text, table):
    """D61: what the subquery's WHERE says about the relation's own rows besides the key and the
    correlation is the relation's condition; read twice, it is one relation, apart from the
    relation without a condition on the same pair."""
    env = role_env()
    r, _ = do(text, env=env, table=table)
    assert r.kind == "lifted", r.reasons
    name = r.pred["member"]["relation"]
    assert name != "member_of" and env.relations[name] == ("team_members", "user_id", "team_id")
    assert env.conditions[name] == LEAD_OR_OWNER
    r2, _ = do(ADMIN_ARRAY if text is ADMIN_EXISTS else ADMIN_EXISTS, env=env, table=table)
    assert r2.pred["member"]["relation"] == name
    r3, _ = do(MEMBER.replace("tenant_id", "team_id").replace("memberships", "team_members"), env=env, table=table)
    assert r3.pred["member"]["relation"] == "member_of"


def test_a_joined_exists_with_a_condition_is_composed_over_the_condition():
    env = role_env()
    r, _ = do(ADMIN_JOIN, env=env, table="tasks")
    assert r.kind == "lifted", r.reasons
    of, chain = env.composed[r.pred["member"]["relation"]]
    assert env.relations[of] == ("team_members", "user_id", "team_id") and env.conditions[of] == LEAD_OR_OWNER
    assert list(chain) == [("projects", "team_id", "id")]


@pytest.mark.parametrize("cond, reason", [
    ("(team_members.user_id <> ( SELECT auth.uid() AS uid))", "context values are not allowed"),
    ("(team_members.role ~~ 'l%'::text)", "LIKE"),
    ("(team_members.team_id IN ( SELECT projects.team_id FROM projects))", "not a relation lookup"),
])
def test_a_condition_outside_the_relation_fragment_keeps_the_whole_membership_opaque(cond, reason):
    text = ADMIN_EXISTS.replace("(team_members.role = ANY (ARRAY['lead'::text, 'owner'::text]))", cond)
    env = role_env()
    r, sink = do(text, env=env, table="projects")
    assert r.kind == "opaque" and any(reason in x for x in r.reasons), r.reasons
    assert env.conditions == {}


def test_a_constant_key_reads_the_rows_in_one_state():
    """D60: with no context value in the subquery, its one constant comparison is the key; with
    two, which one is the key is not determined, and the membership stays opaque."""
    env = role_env()
    text = ("(EXISTS ( SELECT 1\n   FROM tasks\n  WHERE ((tasks.id = comments.task_id) AND (tasks.done = true))))")
    r, _ = do(text, env=env, table="comments")
    assert r.kind == "lifted", r.reasons
    assert env.relations[r.pred["member"]["relation"]] == ("tasks", "done", "id")
    assert r.pred["member"]["key"] == {"const": True}
    r, _ = do(text.replace("(tasks.done = true)", "(tasks.done = true) AND (tasks.title = 'x'::text)"), env=env,
              table="comments")
    assert r.kind == "opaque" and "which one is the key is not determined" in r.reasons[0]


def test_quoted_names_keep_their_case_and_bare_names_fold():
    """Postgres deparses a mixed-case name quoted and folds a bare one (D58): `"isArchived"` is the
    column isArchived, a bare `isArchived` is `isarchived`, another column of the fixture."""
    from tests.templates.specs import mixed_case_spec

    env = Env.of_spec(mixed_case_spec())
    r, _ = do('(NOT ("isArchived" = 1))', env=env, table="Doc")
    assert (r.kind, r.pred) == ("lifted", {"not": {"cmp": [{"col": "isArchived"}, "=", {"const": 1}]}})
    r, _ = do('("ownerId" IS NULL)', env=env, table="Doc")
    assert r.pred == {"is_null": {"col": "ownerId"}}
    r, _ = do("(isArchived = 1)", env=env, table="Doc")
    assert r.pred == {"cmp": [{"col": "isarchived"}, "=", {"const": 1}]}


def test_enum_label_shapes():
    """D59: Postgres deparses a label compared with an enum column as a cast to the type, quoted
    when its name is mixed-case; an IN list as = ANY over an array of such casts, NOT IN as <> ALL.
    A cast to a type the spec does not declare, or a label of none, stays opaque."""
    from tests.templates.specs import enum_spec

    env = env_of(enum_spec())
    r, _ = do("""(status = 'Draft'::"DocStatus")""", env=env)
    assert r.pred == {"cmp": [{"col": "status"}, "=", {"const": "Draft"}]}
    r, _ = do("""(status = ANY (ARRAY['Draft'::"DocStatus", 'it''s'::"DocStatus"]))""", env=env)
    assert r.pred == {"in": [{"col": "status"}, [{"const": "Draft"}, {"const": "it's"}]]}
    r, _ = do("""(status <> ALL (ARRAY['Draft'::"DocStatus", 'In review'::"DocStatus"]))""", env=env)
    assert r.pred == {"not": {"in": [{"col": "status"}, [{"const": "Draft"}, {"const": "In review"}]]}}
    r, _ = do("""(status = 'draft'::"DocStatus")""", env=env)
    assert r.kind == "opaque"
    r, _ = do("""(status = 'Draft'::docstatus)""", env=env)
    assert r.kind == "opaque"
