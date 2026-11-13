"""`mls import --policies discard` (D52) of the Supabase starter (`tests/brownfield/starter_schema.sql`)
on the `supabase/postgres` image: a skeleton that checks, every table adopted, the membership table
a relation, the readable CHECKs rules, every discarded policy in the notes; the skeleton deploys
over the schema as `postgres` (the existing CHECKs are compared and replaced, the old policies
dropped), drift reports the view and the SECURITY DEFINER function as bypass paths (D54) and is
clean once their fixes are applied, and `mls generate --skeleton` takes it (scripted, no API call)."""
from __future__ import annotations

import json
import os

import psycopg
import pytest

from mls.cli import main
from mls.compile import compile_spec
from mls.pg.adopt import import_schema
from mls.pg.drift import drift
from mls.spec import check_spec, load_spec
from mls.spec.model import Spec
from tests.conftest import ROOT

pytestmark = [pytest.mark.pg, pytest.mark.supabase]

STARTER = ROOT / "tests" / "brownfield" / "starter_schema.sql"
DB = "mls_starter"


@pytest.fixture(scope="module")
def supabase():
    if os.environ.get("MLS_SKIP_PG"):
        pytest.skip("MLS_SKIP_PG set")
    from mls.pg.server import supabase_container

    with supabase_container() as server:
        yield server


@pytest.fixture
def starter(supabase):
    """A project database with the starter schema, created and loaded as `postgres`."""
    with psycopg.connect(supabase.dsn, autocommit=True) as admin:
        admin.execute(f'drop database if exists "{DB}" with (force)')
        admin.execute(f'create database "{DB}"')
    supabase.prepare(DB)
    dsn = psycopg.conninfo.make_conninfo(supabase.dsn, dbname=DB)
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute(STARTER.read_text())
    yield conn, dsn
    conn.close()
    with psycopg.connect(supabase.dsn, autocommit=True) as admin:
        admin.execute(f'drop database if exists "{DB}" with (force)')


def test_discard_import_of_the_starter_is_a_skeleton(starter):
    conn, _ = starter
    res = import_schema(conn, policies="discard")
    spec = check_spec(Spec.model_validate(res.spec))
    assert {t: tb.mode for t, tb in spec.tables.items()} == dict.fromkeys(
        ["profiles", "projects", "tasks", "team_members", "teams"], "adopted")
    assert spec.identity.adapter == "supabase" and set(spec.identity.context) == {"uid"}
    assert spec.roles == ["anon", "authenticated"]  # service_role is the platform's trusted role (D55)
    rel = spec.relations["team_members"]
    assert (rel.table, rel.key, rel.value) == ("team_members", "user_id", "team_id")
    assert spec.policies() == [] and spec.column_rules() == [] and spec.opaques == {}
    assert {r.name for r in spec.checks()} == {"team_members_role_check", "tasks_status_check"}
    assert spec.tables["tasks"].columns["status"].type == "text"
    assert spec.tables["tasks"].columns["project_id"].type == "bigint"
    assert "settings" not in spec.tables["projects"].columns  # jsonb: left out, noted
    notes = "\n".join(res.notes)
    assert 'policy Public profiles are viewable by everyone. on profiles discarded' in notes
    assert "policy Users can update own profile. on profiles discarded" in notes and "auth.uid()" in notes
    assert "table team_members has row level security disabled today: the deploy enables it" in notes
    assert "relation team_members proposed: (user_id, team_id) of team_members (membership table" in notes
    assert "column projects.settings has type jsonb" in notes
    assert sorted(n.split(" without")[0] for n in res.notes if n.startswith("bypass path: ")) == [
        "bypass path: function is_team_member(bigint) reaches table team_members",
        "bypass path: view project_overview reaches table projects",
        "bypass path: view project_overview reaches table tasks",
        "bypass path: view project_overview reaches table teams"]
    assert ("(fix: revoke execute on function is_team_member(bigint) from public, anon, authenticated; or assume "
            "{\"bypass\": \"function is_team_member(bigint)\"} on team_members)") in notes


def test_the_skeleton_deploys_over_the_starter_and_drift_is_clean(starter):
    conn, _ = starter
    spec = check_spec(Spec.model_validate(import_schema(conn, policies="discard").spec))
    script = compile_spec(spec)
    notices: list[str] = []
    conn.add_notice_handler(lambda d: notices.append(d.message_primary))
    with conn.transaction():
        with conn.cursor() as cur:
            for st in script.statements:
                cur.execute(st.sql)
    assert sorted(n for n in notices if n.startswith("MLS drops policy")) == [
        "MLS drops policy Public profiles are viewable by everyone. on profiles: the spec does not declare it",
        "MLS drops policy Users can update own profile. on profiles: the spec does not declare it"]
    # the imported CHECKs are the bare form: replaced by `(p) is true` (their columns are NOT NULL)
    assert {n.split(" on ")[0] for n in notices if n.startswith("MLS replaces")} == {
        "MLS replaces constraint team_members_role_check", "MLS replaces constraint tasks_status_check"}
    res = drift(conn, spec, script)
    # the starter's view and SECURITY DEFINER function are its bypass paths (D54) ...
    assert sorted((i.object.split(" without")[0] for i in res.items if i.kind == "hardening")) == [
        "function is_team_member(bigint) reaches table team_members", "view project_overview reaches table projects",
        "view project_overview reaches table tasks", "view project_overview reaches table teams"]
    # ... and with the fixes drift prints applied, drift is clean
    conn.execute("alter view project_overview set (security_invoker = true)")
    conn.execute("revoke execute on function is_team_member(bigint) from public, anon, authenticated")
    res = drift(conn, spec, script)
    assert res.ok, [str(i) for i in res.items]
    with conn.cursor() as cur:
        cur.execute("select relname from pg_class where relname in ('profiles', 'teams', 'team_members', "
                    "'projects', 'tasks') and not relrowsecurity")
        assert cur.fetchall() == []


def test_import_cli_with_observe_and_generate_from_the_skeleton(starter, tmp_path, capsys, monkeypatch):
    _, dsn = starter
    out = tmp_path / "skeleton.json"
    assert main(["import", "--dsn", dsn, "--policies", "discard", "--observe", "profiles", "--out", str(out)]) == 0
    err = capsys.readouterr().err
    assert "note: table profiles is observed (--observe)" in err
    skeleton = json.loads(out.read_text())
    assert skeleton["tables"]["profiles"]["mode"] == "observed"
    assert main(["check", str(out)]) == 0
    capsys.readouterr()
    assert main(["import", "--dsn", dsn, "--observe", "teams"]) == 1  # --observe needs --policies discard
    assert "fix: add --policies discard" in capsys.readouterr().err
    script = tmp_path / "script.json"
    script.write_text(json.dumps([
        "properties {\n  P1 must_allow projects authenticated select: team_members(@uid, team_id) "
        "description 'a member reads the projects of their teams'\n}",
        "rules {\n  R3 policy member_read on projects: authenticated select permissive "
        "using team_members(@uid, team_id)\n}"]))
    monkeypatch.setenv("MLS_LLM_SCRIPT", str(script))
    req = tmp_path / "req.txt"
    req.write_text("A team member reads the projects of their teams.")
    spec_out = tmp_path / "spec.mls"
    assert main(["generate", str(req), "--skeleton", str(out), "--out", str(spec_out)]) == 0
    assert "status: proved" in capsys.readouterr().out
    spec = load_spec(spec_out)
    assert spec.tables["projects"].mode == "adopted" and [p.name for p in spec.policies()] == ["member_read"]
    # the skeleton's imported CHECKs are kept as fixed rules (#26), after the generated policy
    assert [r.id for r in spec.rules] == ["R3", "R1", "R2"]
