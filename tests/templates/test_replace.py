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

    notices = run(h, script)
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
