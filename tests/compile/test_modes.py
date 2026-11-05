"""Table modes (D45) in the compiler, the reports and the migration: an adopted table is not
created or forced, an observed one is not touched, and the assumptions are stated everywhere."""
from __future__ import annotations

import json

from mls import english
from mls.compile import compile_spec
from mls.compile.migrate import migrate
from mls.compile.objects import objects
from mls.props.diff import diff, to_json, to_markdown
from mls.props.report import assumptions, to_markdown as report_md
from mls.props.runner import run_all
from tests.templates import specs

ASSUME = [{"owner": "authenticated"}, {"rls": "not_forced"}]


def test_adopted_table_is_not_created_or_forced():
    script = compile_spec(specs.adopted_spec())
    sql = script.sql()
    assert 'create table "docs"' not in sql and 'create table "memberships"' in sql
    assert 'alter table "docs" enable row level security' in sql
    assert 'alter table "docs" force row level security' not in sql
    assert 'alter table "memberships" force row level security' in sql
    assert 'create policy "member_read" on "docs"' in sql and 'add constraint "level_range"' in sql


def test_observed_table_is_not_touched():
    spec = specs.observed_spec()
    sql = compile_spec(spec).sql()
    assert '"audit"' not in sql
    assert "audit" not in objects(spec).tables and "docs" in objects(spec).tables
    assert spec.tables["audit"].tags == ["legacy"]


def test_assumptions_are_stated_in_every_report():
    spec = specs.adopted_spec(ASSUME)
    owner = "assumed: owner `authenticated` of adopted table `docs` is trusted"
    forced = "assumed: row level security is not forced on adopted table `docs`"
    assert any(owner in a for a in compile_spec(spec).assumptions)
    lines = assumptions(spec, [])
    assert any(owner in a for a in lines) and any(forced in a for a in lines)
    assert any("adopted tables" in a and "docs" in a for a in lines)
    md = report_md(spec, run_all(spec))
    assert owner in md
    ex = english.spec_summary(spec)
    assert "# Tables MLS did not create" in ex and owner in ex
    d = diff(specs.policy_spec(), spec)
    assert any(owner in a for a in to_json(specs.policy_spec(), spec, d)["assumptions"])
    assert owner in to_markdown(specs.policy_spec(), spec, d)
    assert not assumptions(specs.policy_spec(), [])[2].startswith("assumed")  # greenfield: none


def test_migrate_does_not_create_an_adopted_table():
    new = specs.adopted_spec()
    m = migrate(specs.v_a(), new)
    assert not any("create table" in s.sql and '"docs"' in s.sql for p in m.phases for s in p.statements)
    m2 = migrate(specs.policy_spec(), new)
    assert any("changes mode from owned to adopted" in n for n in m2.notes)
    # docs appears in the new version as an adopted table: no CREATE TABLE, only its hardening
    d = json.loads(specs.EXAMPLE.read_text())
    d["tables"] = {k: v for k, v in d["tables"].items() if k != "docs"}
    d["rules"] = [r for r in d["rules"] if r["id"] == "R8"]
    d["properties"] = []
    old = specs.make(d)
    d = json.loads(specs.EXAMPLE.read_text())
    d["tables"]["docs"]["mode"] = "adopted"
    d["rules"] = [r for r in d["rules"] if r["id"] in ("R2", "R5", "R6", "R8")]
    d["properties"] = []
    brand_new = specs.make(d)
    m3 = migrate(old, brand_new, allow_destructive=True)
    assert any("adopted: it must already exist" in n for n in m3.notes)
    assert not any("create table" in s.sql for p in m3.phases for s in p.statements)
    assert any('enable row level security' in s.sql and '"docs"' in s.sql for p in m3.phases for s in p.statements)
