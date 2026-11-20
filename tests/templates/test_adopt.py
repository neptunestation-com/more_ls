"""Milestone 5 (D45) on real Postgres: an adopted table owned by an app role proves and drifts with
the assumption stated; `check --dsn` verifies adopted tables against the catalog; `mls import` of
the deployed example round-trips to a spec with no opaque block, clean drift and an empty
behaviour diff (Milestone 5's first acceptance criterion); the hand-written Supabase-style
fixture imports to a spec that checks, its policies lifted where the lifter reads them and opaque
blocks carrying their signature where it does not."""
from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest

from mls.cli import main
from mls.compile import compile_spec
from mls.errors import SpecError
from mls.pg.adopt import import_schema, verify_adopted
from mls.pg.drift import drift
from mls.pg.harness import Harness, run_differential
from mls.props.diff import behaviour_diff
from mls.props.report import to_markdown
from mls.props.runner import run_all
from mls.spec import check_spec
from mls.spec.model import Spec
from tests.templates import specs

pytestmark = pytest.mark.pg

ASSUME = [{"owner": "authenticated"}, {"rls": "not_forced"}]
FIXTURE = Path(__file__).resolve().parent.parent / "brownfield" / "supabase_schema.sql"


@pytest.fixture(scope="module")
def adopted(pg_dsn):
    spec = specs.adopted_spec(ASSUME)
    h = Harness(spec, compile_spec(spec), pg_dsn, "mls_adopt")
    h.deploy()
    with h.conn.cursor() as cur:
        cur.execute('alter table "docs" owner to "authenticated"')  # the brownfield state the spec assumes
    yield spec, h
    h.close()


def test_assumed_hardening_is_reported_as_assumed(adopted):
    spec, h = adopted
    res = drift(h.conn, spec, compile_spec(spec))
    assert res.ok, [str(i) for i in res.items]
    kinds = {i.kind for i in res.items}
    assert kinds == {"assumed"}, [str(i) for i in res.items]
    assert any("owner authenticated is an app role and is trusted" in i.object for i in res.items)
    assert any("row level security is not forced" in i.object for i in res.items)
    assert "assumed: owner `authenticated` of adopted table `docs` is trusted" in to_markdown(spec, run_all(spec))


def test_without_the_assumption_it_is_hardening_drift(adopted):
    spec, h = adopted
    bare = specs.adopted_spec()
    res = drift(h.conn, bare, compile_spec(bare))
    assert not res.ok
    assert {i.kind for i in res.items} == {"hardening"}
    assert any("owned by app role authenticated" in i.object for i in res.items)


def test_stale_assumption_is_a_note(adopted):
    spec, h = adopted
    script = compile_spec(spec)
    with h.conn.transaction(force_rollback=True):
        with h.conn.cursor() as cur:
            cur.execute('alter table "docs" owner to current_user')
            cur.execute('alter table "docs" force row level security')
            for g in script.by_section("grants"):  # ownership transfer folds the old owner's grants into the new one
                cur.execute(g)
        res = drift(h.conn, spec, script)
    assert res.ok and res.items == [], [str(i) for i in res.items]
    assert any("no longer needed" in n and "owner" in n for n in res.notes)
    assert any("no longer needed" in n and "not_forced" in n for n in res.notes)


def test_adopted_differential_and_mutation(pg_dsn):
    spec = specs.adopted_spec()
    res = run_differential(spec, compile_spec(spec), pg_dsn, "mls_adopt_diff", seed=1, rows_per_table=30,
                           n_contexts=16, ops_per_context=25)
    assert res.ok, res.summary()
    from mls.compile import Mutation

    bad = run_differential(spec, compile_spec(spec, {Mutation.POLICY_ALL_RESTRICTIVE}), pg_dsn, "mls_adopt_mut",
                           seed=1, rows_per_table=30, n_contexts=16, ops_per_context=25)
    assert not bad.ok


def test_observed_table_must_exist(adopted):
    spec, h = adopted
    obs = specs.observed_spec()
    res = drift(h.conn, obs, compile_spec(obs))
    assert any(i.kind == "missing" and "table audit (observed)" in i.object for i in res.items)
    with h.conn.transaction(force_rollback=True):
        with h.conn.cursor() as cur:
            cur.execute('create table "audit" (id integer)')
        res = drift(h.conn, obs, compile_spec(obs))
    assert not any("audit" in i.object for i in res.items), [str(i) for i in res.items]


def test_check_dsn_verifies_adopted_tables(adopted, tmp_path, capsys):
    spec, h = adopted
    assert verify_adopted(h.conn, spec) == []
    wrong = spec.model_copy(deep=True)
    wrong.tables["docs"].columns["note"] = wrong.tables["docs"].columns["level"]  # declared int, text in the catalog
    with h.conn.transaction(force_rollback=True):
        with h.conn.cursor() as cur:
            cur.execute('alter table "docs" drop column "archived" cascade')
            cur.execute('alter table "docs" add column "note" text')
        errs = verify_adopted(h.conn, wrong)
    paths = {e.path: e for e in errs}
    assert "adopted table 'docs' has no column 'archived'" in paths["$.tables.docs.columns.archived"].message
    assert "text in the catalog, declared int" in paths["$.tables.docs.columns.note.type"].message
    # the same table declared owned, while an app role owns it: refused with the fix
    owned = specs.policy_spec()
    errs = verify_adopted(h.conn, owned)
    assert len(errs) == 1 and errs[0].path == "$.tables.docs.mode"
    assert '{"owner": "authenticated"}' in errs[0].fix
    dsn = psycopg.conninfo.make_conninfo(h.dsn, dbname=h.dbname)
    p = tmp_path / "adopted.json"
    d = json.loads(specs.EXAMPLE.read_text())
    d["tables"]["docs"]["mode"] = "adopted"
    d["tables"]["docs"]["assume"] = ASSUME
    p.write_text(json.dumps(d))
    assert main(["check", str(p), "--dsn", dsn]) == 0
    assert "OK" in capsys.readouterr().out
    assert main(["check", str(specs.EXAMPLE), "--dsn", dsn, "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["errors"][0]["path"] == "$.tables.docs.mode"


# ----------------------------------------------------------------------------- mls import
def test_import_of_the_deployed_example_round_trips(pg_dsn, example_spec, tmp_path, capsys):
    """Acceptance (ROADMAP M5): import the database `mls compile examples/tenant_docs.mls.json`
    deploys; the spec has 0 opaque blocks, its drift is clean and `behaviour_diff` against the
    source is empty."""
    h = Harness(example_spec, compile_spec(example_spec), pg_dsn, "mls_import_rt")
    h.deploy()
    try:
        res = import_schema(h.conn)
        imported = check_spec(Spec.model_validate(res.spec))
        assert imported.identity.adapter == "supabase"
        assert imported.identity.context["uid"].source == "auth.uid"
        assert imported.identity.context["clearance"].model_dump() == {"source": "jwt.claims.clearance", "type": "int"}
        assert imported.roles == ["authenticated"]
        assert {t: tb.mode for t, tb in imported.tables.items()} == {"docs": "adopted", "memberships": "adopted",
                                                                     "admins": "adopted"}
        assert imported.tables["docs"].columns == example_spec.tables["docs"].columns
        assert imported.tables["docs"].primary_key == ["id"]
        # the relations get their names back from the compiler's relation-visibility policies
        assert {n: (r.table, r.key, r.value) for n, r in imported.relations.items()} == {
            "member_of": ("memberships", "user_id", "tenant_id"), "admin_of": ("admins", "user_id", "tenant_id")}
        assert imported.opaques == {}, res.notes
        src = {(r.kind, r.name) for r in example_spec.rules}
        assert src == {(r.kind, r.name) for r in imported.rules}, res.notes
        by_name = {r.name: r for r in imported.rules}
        for r in example_spec.rules:
            assert by_name[r.name].model_dump(exclude={"id", "requirement"}) == r.model_dump(exclude={"id", "requirement"})
        assert any("relation-visibility policy" in n for n in res.notes)
        assert any("relation grant" in n for n in res.notes)
        d = drift(h.conn, imported, compile_spec(imported))
        assert d.ok and d.items == [], [str(i) for i in d.items]
        bad = [c for c in behaviour_diff(example_spec, imported) if c.verdict != "PROVED"]
        assert bad == [], [str(c) for c in bad]
        # the CLI: --out writes the spec, the notes go to stderr, and `check --dsn` accepts it
        dsn = psycopg.conninfo.make_conninfo(h.dsn, dbname=h.dbname)
        out = tmp_path / "imported.json"
        assert main(["import", "--dsn", dsn, "--out", str(out)]) == 0
        assert "note:" in capsys.readouterr().err
        assert main(["check", str(out), "--dsn", dsn]) == 0
        assert main(["drift", str(out), "--dsn", dsn]) == 0
        assert "drift: none" in capsys.readouterr().out
    finally:
        h.close()


@pytest.fixture(scope="module")
def brownfield(pg_dsn):
    dbname = "mls_brownfield"
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        admin.execute(f'drop database if exists "{dbname}"')
        admin.execute(f'create database "{dbname}"')
    conn = psycopg.connect(psycopg.conninfo.make_conninfo(pg_dsn, dbname=dbname), autocommit=True)
    conn.execute(FIXTURE.read_text())
    yield conn
    conn.close()
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        admin.execute(f'drop database if exists "{dbname}"')


def test_import_of_the_brownfield_fixture(brownfield):
    """Acceptance (ROADMAP M5): the hand-written Supabase-style schema imports to a spec that
    checks; the policies the lifter reads are in the fragment, every other one is an opaque
    block whose signature names the columns and context it reads; a proof over a block is
    conditional on it."""
    res = import_schema(brownfield)
    spec = check_spec(Spec.model_validate(res.spec))
    assert spec.identity.adapter == "supabase" and set(spec.identity.context) == {"uid", "role"}
    assert spec.identity.context["role"].model_dump() == {"source": "jwt.claims.role", "type": "text"}
    assert spec.roles == ["anon", "authenticated"]
    modes = {t: tb.mode for t, tb in spec.tables.items()}
    assert modes == {"profiles": "adopted", "projects": "adopted", "project_members": "adopted",
                     "tasks": "adopted", "audit_log": "observed"}
    # bigint is modelled since D53 (the starter fixture of #20 keeps an unmodelled jsonb column)
    assert spec.tables["profiles"].columns["credits"].type == "bigint"
    assert spec.tables["tasks"].columns["id"].type == "bigint"
    assert spec.tables["profiles"].columns["created_at"].type == "timestamp"
    assert spec.tables["tasks"].primary_key == ["id"]
    # the lifted policies (bare auth.uid(), IN (subquery), EXISTS, a claim, a boolean column)
    by_name = {p.name: p for p in spec.policies()}
    pred = lambda p: p.model_dump(mode="json", by_alias=True, exclude_none=True)  # noqa: E731
    member = {"member": {"relation": "project_members", "key": {"ctx": "uid"}, "value": {"col": "project_id"}}}
    assert pred(by_name["tasks_of_my_projects"].using) == member
    assert pred(by_name["members_see_their_projects"].using) == {
        "member": {"relation": "project_members", "key": {"ctx": "uid"}, "value": {"col": "id"}}}
    assert pred(by_name["assignee_updates"].using) == {"and": [
        {"cmp": [{"col": "assignee"}, "=", {"ctx": "uid"}]}, {"cmp": [{"ctx": "role"}, "=", {"const": "editor"}]}]}
    assert pred(by_name["no_deletes_of_done_tasks"].using) == {"not": {"cmp": [{"col": "done"}, "=", {"const": True}]}}
    assert by_name["no_deletes_of_done_tasks"].mode == "restrictive"
    assert pred(by_name["users_update_own_profile"].using) == {"cmp": [{"col": "id"}, "=", {"ctx": "uid"}]}
    assert by_name["users_update_own_profile"].check is None  # equal to using: one predicate, not two
    two = [p for n, p in by_name.items() if n.startswith("members_see_memberships")]
    assert len(two) == 2 and {p.role for p in two} == {"anon", "authenticated"}
    assert pred(two[0].using) == pred(two[1].using) == {"cmp": [{"col": "user_id"}, "=", {"ctx": "uid"}]}
    assert spec.relations["project_members"].model_dump() == {"table": "project_members", "key": "user_id",
                                                               "value": "project_id"}
    # what stays opaque: (owner, id) is not a key of projects, so the lookup is not a relation
    assert by_name["insert_into_own_projects"].command == "insert" and by_name["insert_into_own_projects"].using is None
    o = spec.opaques[by_name["insert_into_own_projects"].check.opaque]
    assert o.columns == ["project_id"] and o.context == ["uid"] and "projects" in o.sql
    assert list(spec.opaques) == [by_name["insert_into_own_projects"].check.opaque]
    assert any("insert into own projects" in n and "not a primary key or unique constraint of projects" in n
               for n in res.notes)
    assert any(n.startswith("lint:") and "auth.uid()" in n and "index-friendly" in n for n in res.notes)
    assert any(n.startswith("lint:") and "correlated EXISTS" in n for n in res.notes)
    # the CHECKs over NOT NULL columns are check rules; the trigger is listed
    checks = {r.name: pred(r.predicate) for r in spec.checks()}
    assert checks == {"projects_name_check": {"cmp": [{"col": "name"}, "<>", {"const": ""}]},
                      "tasks_priority_check": {"and": [{"cmp": [{"col": "priority"}, ">=", {"const": 0}]},
                                                       {"cmp": [{"col": "priority"}, "<=", {"const": 5}]}]}}
    assert any("tasks_priority_check" in n and "drift reports the definition as differs" in n for n in res.notes)
    # late binding (D48): a selector over the imported spec expands to every adopted table with the
    # column, the membership table included, where reading its own relation is refused (Postgres:
    # infinite recursion in policy); narrowed by name, the selector still filters by the column
    d = dict(res.spec)
    s1 = {"id": "S1", "kind": "policy", "name": "project_rows", "role": "authenticated",
          "command": "select", "mode": "permissive", "using": member,
          "applies_to": {"has_column": ["project_id", "uuid"]}}
    d["rules"] = d["rules"] + [s1]
    with pytest.raises(SpecError) as ei:
        check_spec(Spec.model_validate(d))
    (e,) = ei.value.errors
    assert e.path == f"$.rules[{len(d['rules']) - 1}].using.member.relation"
    assert "a policy on 'project_members' cannot read 'project_members'" in e.message
    d["rules"][-1] = {**s1, "applies_to": {"and": [{"has_column": ["project_id", "uuid"]},
                                                   {"named": ["projects", "tasks"]}]}}
    sel = check_spec(Spec.model_validate(d))
    assert [r.table for r in sel.expansions("S1")] == ["tasks"]
    assert not any(r.kind == "inclusion" for r in spec.rules)  # single-column FKs are not inclusions
    cr = [r for r in spec.rules if r.kind == "column_rule"]
    assert [(r.table, r.role, r.access, r.columns) for r in cr] == [("profiles", "anon", "read", ["display_name", "id"])]
    assert any("is not an inclusion" in n for n in res.notes)
    assert any("has insert on project_members but no permissive insert policy" in n for n in res.notes)
    assert any("audit_log has row level security disabled: observed" in n for n in res.notes)
    assert any('"tasks of my projects" on tasks is imported as rule tasks_of_my_projects' in n for n in res.notes)
    assert any("trigger tasks_touch" in n and "never inlined" in n for n in res.notes)
    # proofs over the lifted policies are unconditional; one over the block is conditional on it
    d = dict(res.spec)
    d["properties"] = [
        {"id": "P1", "template": "write_subset_read", "table": "profiles"},
        {"id": "P2", "template": "tenant_isolation", "table": "tasks", "column": "project_id",
         "relation": "project_members", "key": {"ctx": "uid"}, "commands": ["select"]},
        # the insert policy is the opaque block: the written row is not shown readable
        {"id": "P3", "template": "write_subset_read", "table": "tasks"},
        {"id": "P4", "template": "custom", "table": "tasks", "role": "authenticated", "command": "insert",
         "goal": {"opaque": by_name["insert_into_own_projects"].check.opaque}},
    ]
    proved = check_spec(Spec.model_validate(d))
    results = {r.id: r for r in run_all(proved)}
    assert results["P1"].verdict == "PROVED" and results["P1"].conditional_on == []
    assert results["P2"].verdict == "PROVED" and results["P2"].conditional_on == []
    assert results["P3"].verdict == "REFUTED"
    assert results["P4"].verdict == "PROVED" and results["P4"].conditional_on == ["insert_into_own_projects_check"]
    md = to_markdown(proved, list(results.values()))
    assert "3 proved (1 conditional on opaque blocks), 1 refuted" in md
    for r in results.values():
        for name in r.conditional_on:
            assert f"opaque block '{name}' on" in md  # the assumptions list it with its SQL
        if r.conditional_on:
            assert f"conditional on opaque blocks: {', '.join(r.conditional_on)}" in md
    # the hardening state is recorded as assumptions, so drift reports it as assumed, not as hardening;
    # what fails is only what the notes announced: renamed policies and the grants without a policy
    assert all(tb.assume == [] or tb.mode == "adopted" for tb in spec.tables.values())
    assert [a.model_dump() for a in spec.tables["tasks"].assume] == [{"rls": "not_forced"}]
    assert any("table tasks: assumed row level security is not forced" in n for n in res.notes)
    dd = drift(brownfield, spec, compile_spec(spec))
    assert dd.by_kind("assumed") and not dd.by_kind("hardening"), [str(i) for i in dd.items]
    failing = [i for i in dd.items if i.kind not in ("foreign", "assumed")]
    assert failing
    for i in failing:  # renamed policies, the grants without a policy, the indexes and the CHECK form a deploy adds
        assert (i.kind in ("missing", "unexpected") and ("policy" in i.object or "privilege" in i.object)) \
            or (i.kind == "missing" and "index mls_ix_" in i.object) \
            or (i.kind == "differs" and "constraint" in i.object and "IS TRUE" in i.expected), str(i)
    assert any("privilege INSERT on project_members for authenticated" in i.object for i in failing)
    assert any("has delete on tasks but no permissive delete policy" in n for n in res.notes)


def test_import_needs_an_app_role(brownfield):
    with pytest.raises(SpecError) as ei:
        import_schema(brownfield, roles=[])
    assert ei.value.errors[0].path == "$.roles" and "--role" in ei.value.errors[0].fix


TYPES_SQL = """
do $$ begin if not exists (select from pg_roles where rolname = 'app') then create role app; end if; end $$;
create table team_members (user_id bigint not null, team_id bigint not null, primary key (user_id, team_id));
create table tickets (
  id bigint generated by default as identity primary key,
  team_id bigint not null,
  status varchar(20) not null,
  title character varying,
  check (status in ('open', 'closed')),
  foreign key (user_id, team_id) references team_members (user_id, team_id),
  user_id bigint not null
);
alter table team_members enable row level security;
alter table tickets enable row level security;
create policy member_read on tickets for select to app using (
  team_id = any (array(select team_id from team_members
                       where user_id = (select nullif(current_setting('app.uid', true), '')::bigint))));
"""
BIG = 2**40 + 7


@pytest.fixture
def typed(pg_dsn):
    dbname = "mls_types"
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        admin.execute(f'drop database if exists "{dbname}"')
        admin.execute(f'create database "{dbname}"')
    conn = psycopg.connect(psycopg.conninfo.make_conninfo(pg_dsn, dbname=dbname), autocommit=True)
    conn.execute(TYPES_SQL)
    yield conn
    conn.close()
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        admin.execute(f'drop database if exists "{dbname}"')


def test_bigint_keys_and_varchar_columns_import_deploy_and_drift(typed):
    """D53: `bigint` identity keys import as bigint, `varchar` columns as text on the adopted
    table; the imported spec deploys over the schema, drift is then clean (the CHECK over the
    varchar column included), and a key above 2³¹ passes through the compiled policy."""
    res = import_schema(typed)
    spec = check_spec(Spec.model_validate(res.spec))
    t = spec.tables["tickets"]
    assert t.mode == "adopted"
    assert {c: col.type for c, col in t.columns.items()} == {
        "id": "bigint", "team_id": "bigint", "status": "text", "title": "text", "user_id": "bigint"}
    assert spec.identity.context["uid"].type == "bigint"
    assert res.spec["opaques"] == {}, res.notes
    check = next(r for r in spec.rules if r.kind == "check")  # the CHECK over the varchar column is read
    assert check.name == "tickets_status_check"
    verify_adopted(typed, spec)  # the catalog's varchar is the spec's text
    script = compile_spec(spec)
    with typed.transaction():
        with typed.cursor() as cur:
            # the deploy creates the CHECK it imported and does not replace the live one yet (#20)
            cur.execute("alter table tickets drop constraint tickets_status_check")
            for st in script.statements:
                cur.execute(st.sql)
    d = drift(typed, spec, script)
    assert d.ok, [str(i) for i in d.items]
    assert not [i for i in d.items if i.kind in ("differs", "missing")], [str(i) for i in d.items]
    with typed.transaction(force_rollback=True):
        with typed.cursor() as cur:
            cur.execute("insert into team_members values (%s, %s), (%s, 1)", (BIG, BIG + 1, BIG + 2))
            cur.execute("insert into tickets (id, team_id, status, user_id) values (%s, %s, 'open', %s), "
                        "(1, 1, 'open', %s)", (BIG + 3, BIG + 1, BIG, BIG + 2))
            cur.execute("select set_config('app.uid', %s, true)", (str(BIG),))
            cur.execute("set local role app")
            cur.execute("select id from tickets")
            assert [r[0] for r in cur.fetchall()] == [BIG + 3]


def test_discard_import_of_the_brownfield_fixture(brownfield):
    """D52 on plain Postgres: every table adopted (the one without RLS too), no policy, column rule
    or opaque block, each discarded policy listed with its text; `keep` stays the default."""
    res = import_schema(brownfield, policies="discard")
    spec = check_spec(Spec.model_validate(res.spec))
    assert all(t.mode == "adopted" for t in spec.tables.values())
    assert "audit_log" in spec.tables
    assert spec.policies() == [] and spec.column_rules() == [] and spec.opaques == {}
    discarded = [n for n in res.notes if " discarded (--policies discard" in n]
    assert len(discarded) == 9 and any("restrictive for delete to authenticated using ((NOT done))" in n
                                       for n in discarded), discarded
    assert spec.identity.adapter == "supabase"  # still read from the discarded policies' text
    assert import_schema(brownfield).spec["tables"]["audit_log"]["mode"] == "observed"


def test_enum_labels_are_the_catalogs(pg_dsn):
    """D59: drift's shadow creates its own copy of each enum type, so a deployed enum spec drifts
    clean; a label added to the live type (or a column the spec calls text) is drift and a
    `check --dsn` error naming the catalog's labels."""
    spec = specs.enum_spec()
    h = Harness(spec, compile_spec(spec), pg_dsn, "mls_enum")
    h.deploy()
    try:
        h.conn.execute('alter table "docs" force row level security')  # the owner's step (the compiler never forces it)
        script = compile_spec(spec)
        res = drift(h.conn, spec, script)
        assert res.ok and res.items == [], [str(i) for i in res.items]
        assert verify_adopted(h.conn, spec) == []
        with h.conn.transaction(force_rollback=True):
            h.conn.execute("""alter type "DocStatus" add value 'Gone'""")
            res = drift(h.conn, spec, script)
            assert [(i.kind, i.object) for i in res.items] == [("differs", "enum type DocStatus")]
            errs = verify_adopted(h.conn, spec)
            assert [e.path for e in errs] == ["$.enums.DocStatus"]
            assert "'Draft', 'In review', \"it's\", 'Published', 'Gone' in the catalog" in errs[0].message
        d = json.loads(spec.model_dump_json(by_alias=True, exclude_none=True))
        d["tables"]["docs"]["columns"]["status"] = {"type": "text", "nullable": True}
        d["enums"] = {}
        errs = verify_adopted(h.conn, Spec.model_validate(d))
        assert [(e.path, e.fix) for e in errs] == [("$.tables.docs.columns.status", "declare it as enum DocStatus")]
    finally:
        h.close()
