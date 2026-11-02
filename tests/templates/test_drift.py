"""Drift detection against the live catalog (D7, D8, D13): a fresh deploy is clean; every kind
of tampering is reported; foreign objects on MLS tables are reported."""
from __future__ import annotations

import pytest

from mls.compile import Mutation, compile_spec
from mls.pg.drift import drift
from mls.pg.harness import Harness
from tests.templates import specs

pytestmark = pytest.mark.pg

TAMPER = {
    "drop_policy": ('drop policy "not_archived" on "docs"', "missing: policy not_archived"),
    "foreign_policy": ('create policy "backdoor" on "docs" for select to "authenticated" using (true)',
                       "unexpected: policy backdoor"),
    "changed_check": ('alter table "docs" drop constraint "level_range"; '
                      'alter table "docs" add constraint "level_range" check (("level" >= 0) is true)',
                      "differs: constraint level_range"),
    "no_force_rls": ('alter table "docs" no force row level security', "row level security is not forced"),
    "extra_grant": ('grant delete on "docs" to "authenticated"', "unexpected: privilege DELETE on docs"),
    "column_grant": ('revoke select on "docs" from "authenticated"; '
                     'grant select ("id", "tenant_id") on "docs" to "authenticated"',
                     "missing: privilege SELECT on docs for authenticated"),
    "dropped_index": ('drop index "mls_ix_docs_tenant_id_level"', "missing: index mls_ix_docs_tenant_id_level"),
    "foreign_trigger": ('create function noop() returns trigger language plpgsql as $$ begin return new; end $$; '
                        'create trigger extra before insert on "docs" for each row execute function noop()',
                        "unexpected: trigger extra"),
    "trigger_body": ('create or replace function "mls_tr_docs_tenant_immutable"() returns trigger language plpgsql '
                     'as $$ begin return new; end $$', "differs: function mls_tr_docs_tenant_immutable"),
    "bypassrls": ('alter role "authenticated" bypassrls', "role authenticated has BYPASSRLS"),
    "dropped_column": ('alter table "docs" drop column "archived" cascade', "missing: column docs.archived"),
    "nullability": ('alter table "docs" alter column "level" drop not null', "differs: column docs.level"),
}


@pytest.fixture(scope="module")
def deployed(pg_dsn):
    spec = specs.v_b()
    h = Harness(spec, compile_spec(spec), pg_dsn, "mls_drift")
    h.deploy()
    yield spec, h
    h.close()


def test_fresh_deploy_has_no_drift(deployed):
    spec, h = deployed
    res = drift(h.conn, spec, compile_spec(spec))
    assert res.ok, [str(i) for i in res.items]
    assert res.notes == []


def test_drift_leaves_no_trace(deployed):
    spec, h = deployed
    drift(h.conn, spec, compile_spec(spec))
    with h.conn.cursor() as cur:
        cur.execute("select nspname from pg_namespace where nspname like 'mls_shadow_%'")
        assert cur.fetchall() == []


@pytest.mark.parametrize("name", list(TAMPER))
def test_tampering_is_reported(deployed, name):
    spec, h = deployed
    sql, expected = TAMPER[name]
    with h.conn.transaction(force_rollback=True):
        with h.conn.cursor() as cur:
            cur.execute(sql)
        res = drift(h.conn, spec, compile_spec(spec))
    assert any(expected in str(i) for i in res.items), [str(i) for i in res.items]
    assert drift(h.conn, spec, compile_spec(spec)).ok  # the tampering was rolled back


ORG_TAMPER = {
    "internal_login": ('alter role "mls_internal" login', "role mls_internal can log in"),
    "internal_member": ('grant "mls_internal" to "authenticated"', "app role authenticated is a member of mls_internal"),
    "reach_body": ('create or replace function "mls_reach_org"(k uuid) returns integer[] language sql stable '
                   'security definer set search_path from current as $$ select array[]::integer[] $$',
                   "differs: function mls_reach_org"),
    "reach_owner": ('alter function "mls_reach_org"(uuid) owner to current_user', "differs: function mls_reach_org"),
    "reach_execute": ('revoke execute on function "mls_reach_org"(uuid) from "authenticated"',
                      "differs: function mls_reach_org"),
    "depth_trigger": ('drop trigger "mls_h_org_depth" on "groups"', "missing: trigger mls_h_org_depth"),
    "card_trigger": ('drop trigger "files_per_tenant" on "files"', "missing: trigger files_per_tenant"),
    "card_body": ('create or replace function "mls_card_files_files_per_tenant"() returns trigger language plpgsql '
                  'security definer set search_path from current as $$ begin return new; end $$',
                  "differs: function mls_card_files_files_per_tenant"),
    "depth_check": ('alter table "groups" drop constraint "mls_h_org_depth_max"', "missing: constraint mls_h_org_depth_max"),
    "member_fk": ('alter table "group_members" drop constraint "mls_h_org_member"', "missing: constraint mls_h_org_member"),
    "internal_policy": ('drop policy "mls_internal_groups" on "groups"', "missing: policy mls_internal_groups"),
    "parent_immutable": ('drop trigger "mls_h_org_parent_immutable" on "groups"',
                         "missing: trigger mls_h_org_parent_immutable"),
}


@pytest.fixture(scope="module")
def deployed_org(pg_dsn):
    spec = specs.org_base_spec()
    h = Harness(spec, compile_spec(spec), pg_dsn, "mls_drift_org")
    h.deploy()
    yield spec, h
    h.close()


def test_fresh_org_deploy_has_no_drift(deployed_org):
    spec, h = deployed_org
    res = drift(h.conn, spec, compile_spec(spec))
    assert res.ok, [str(i) for i in res.items]


@pytest.mark.parametrize("name", list(ORG_TAMPER))
def test_org_tampering_is_reported(deployed_org, name):
    spec, h = deployed_org
    sql, expected = ORG_TAMPER[name]
    with h.conn.transaction(force_rollback=True):
        with h.conn.cursor() as cur:
            cur.execute(sql)
        res = drift(h.conn, spec, compile_spec(spec))
    assert any(expected in str(i) for i in res.items), [str(i) for i in res.items]
    assert drift(h.conn, spec, compile_spec(spec)).ok


@pytest.mark.parametrize("mutation", [Mutation.HIERARCHY_SHALLOW, Mutation.HIERARCHY_NO_DEPTH_CHECK,
                                      Mutation.CARDINALITY_OFF_BY_ONE, Mutation.CARDINALITY_NO_LOCK])
def test_mutated_org_compile_drifts(deployed_org, mutation):
    """A deploy from a mutated hierarchy / cardinality template drifts from the correct spec."""
    spec, h = deployed_org
    bad = Harness(spec, compile_spec(spec, {mutation}), h.dsn, "mls_drift_org_mut")
    bad.deploy()
    try:
        assert not drift(bad.conn, spec, compile_spec(spec)).ok
    finally:
        bad.close()


def test_mutated_compile_drifts(deployed):
    """A deploy from a mutated compiler (no FORCE RLS) drifts from the correct spec."""
    spec, h = deployed
    bad = Harness(spec, compile_spec(spec, {Mutation.HARDENING_NO_FORCE}), h.dsn, "mls_drift_mut")
    bad.deploy()
    try:
        res = drift(bad.conn, spec, compile_spec(spec))
        assert any(i.kind == "hardening" for i in res.items)
    finally:
        bad.close()
