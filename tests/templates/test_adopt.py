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
    assert "credits" not in spec.tables["profiles"].columns and "id" not in spec.tables["tasks"].columns
    assert spec.tables["profiles"].columns["created_at"].type == "timestamp"
    assert spec.tables["tasks"].primary_key is None  # bigserial id is not modelled
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
