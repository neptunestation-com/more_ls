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
                     'as $$ begin return new; end $$', "differs: trigger tenant_immutable"),
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
