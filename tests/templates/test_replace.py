"""Policy-set replacement on adopted tables (D51): the deploy leaves exactly the spec's policies,
names every policy it drops that the spec does not declare, and those are exactly the policies
`drift` calls unexpected before the deploy.  The differential test and the mutation that keeps
foreign policies are in test_templates.py (`adopted_replace`)."""
from __future__ import annotations

import pytest

from mls.compile import compile_spec
from mls.compile.plan import Script
from mls.pg.drift import drift
from mls.pg.harness import Harness
from mls.props.effective import all_policies
from tests.templates import specs

pytestmark = pytest.mark.pg


@pytest.fixture
def brownfield(pg_dsn):
    """The adopted tables with their hand-written policies (Harness.pre_existing_sql), before the
    script runs: only the roles section is deployed."""
    spec = specs.adopted_spec([{"rls": "not_forced"}])
    script = compile_spec(spec)
    roles = Script([s for s in script.statements if s.section in ("preamble", "roles")])
    h = Harness(spec, roles, pg_dsn, "mls_replace")
    h.deploy()
    yield spec, script, h
    h.close()


def policies_on(h: Harness, table: str) -> set[str]:
    with h.conn.cursor() as cur:
        cur.execute("select polname from pg_policy where polrelid = %s::regclass", (f'"{table}"',))
        return {r[0] for r in cur.fetchall()}


def run(h: Harness, script: Script) -> list[str]:
    notices: list[str] = []
    h.conn.add_notice_handler(lambda d: notices.append(d.message_primary))
    with h.conn.cursor() as cur:
        for s in script.statements:
            if s.section not in ("preamble", "roles"):
                cur.execute(s.sql)
    return notices


def test_the_deploy_replaces_the_policy_set(brownfield):
    spec, script, h = brownfield
    declared = {p.name for p in all_policies(spec) if p.table == "docs"}
    before = policies_on(h, "docs")
    foreign = before - declared
    assert foreign == {"Enable access for all users", "legacy_no_inserts"} and before & declared

    unexpected = {i.object.removeprefix("policy ").removesuffix(" on docs")
                  for i in drift(h.conn, spec, script).by_kind("unexpected") if i.object.startswith("policy ")}
    assert unexpected == foreign, "drift before the deploy lists the policies it will drop"

    notices = [n for n in run(h, script) if n.startswith("MLS drops")]
    assert sorted(notices) == sorted(f"MLS drops policy {n} on docs: the spec does not declare it" for n in foreign)
    assert policies_on(h, "docs") == declared
    res = drift(h.conn, spec, script)
    assert res.ok, [str(i) for i in res.items]


def test_the_policies_of_an_adopted_table_deploy_again(brownfield):
    """A second run of the adopted table's policy statements replaces the policies the first
    created, silently (owned tables are created once; their policies are migrated, not redeployed)."""
    spec, script, h = brownfield
    run(h, script)
    policies = Script([s for s in script.statements if s.section == "policies"
                       and (s.rule_id == "replace:docs" or ' on "docs" ' in s.sql)])
    assert run(h, policies) == []
    assert drift(h.conn, spec, script).ok


# ----------------------------------------------------------------------------- constraints (D52)
def constraint(h: Harness, table: str, name: str) -> tuple[int, str, bool] | None:
    with h.conn.cursor() as cur:
        cur.execute("select oid, pg_get_constraintdef(oid), convalidated from pg_constraint "
                    "where conrelid = %s::regclass and conname = %s", (f'"{table}"', name))
        return cur.fetchone()


def test_an_existing_constraint_is_kept_when_it_matches_and_replaced_when_it_differs(brownfield):
    """The seeded `level_range` is `check (true)`: replaced, with a notice.  The FK `owner_in_tenant`
    is written by hand as the spec writes it after the tables exist: kept, the same constraint."""
    spec, script, h = brownfield
    tables = Script([s for s in script.statements if s.section == "tables"])
    run(h, tables)
    with h.conn.cursor() as cur:
        cur.execute('alter table "docs" add constraint "owner_in_tenant" foreign key ("owner_id", "tenant_id") '
                    'references "memberships" ("user_id", "tenant_id") on update restrict on delete restrict')
    fk_before = constraint(h, "docs", "owner_in_tenant")
    rest = Script([s for s in script.statements if s.section not in ("tables",)])
    notices = run(h, rest)
    assert [n for n in notices if n.startswith("MLS replaces")] == [
        "MLS replaces constraint level_range on docs: CHECK (true) (the spec: "
        "CHECK ((((level >= 0) AND (level <= 3)) IS TRUE)))"]
    assert constraint(h, "docs", "owner_in_tenant") == fk_before  # kept: same oid
    assert constraint(h, "docs", "level_range")[1:] == ("CHECK ((((level >= 0) AND (level <= 3)) IS TRUE))", True)
    assert constraint(h, "docs", "mls_probe") is None
    res = drift(h.conn, spec, script)
    assert res.ok, [str(i) for i in res.items]


def test_a_matching_constraint_that_is_not_valid_is_replaced(brownfield):
    spec, script, h = brownfield
    with h.conn.cursor() as cur:
        cur.execute('alter table "docs" drop constraint "level_range"')
        cur.execute('alter table "docs" add constraint "level_range" check (((level >= 0) and (level <= 3)) is true) '
                    'not valid')
    notices = run(h, script)
    assert any(n.startswith("MLS replaces constraint level_range on docs") and "IS TRUE)) NOT VALID (the spec" in n
               for n in notices), notices
    assert constraint(h, "docs", "level_range")[2] is True


def test_a_replacement_that_the_rows_violate_fails_the_deploy(brownfield):
    spec, script, h = brownfield
    with h.conn.cursor() as cur:
        cur.execute('insert into "docs" (id, tenant_id, level) values (1, 1, 9)')  # fine under check (true)
    with pytest.raises(Exception, match="level_range"):
        with h.conn.transaction():
            run(h, script)
