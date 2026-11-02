"""Migration correctness (D7): after applying `mls migrate v1 v2` to a database deployed from v1
and loaded with data, (a) the catalog matches a fresh v2 deploy (drift is clean), (b) the v2
differential test passes on the migrated database, and (c) a mutated migration (constraints left
NOT VALID) is caught by drift."""
from __future__ import annotations

import random

import pytest

from mls.compile import Mutation, compile_spec
from mls.compile.migrate import migrate
from mls.pg.apply import apply_migration
from mls.pg.drift import drift
from mls.pg.harness import Harness, ValueGen, generate_dataset, run_differential
from mls.pg.preflight import preflight
from tests.templates import specs

pytestmark = pytest.mark.pg


def deploy_with_data(spec, dsn, dbname, seed=0, rows=30):
    h = Harness(spec, compile_spec(spec), dsn, dbname)
    h.deploy()
    rnd = random.Random(seed)
    gen = ValueGen(spec, h.adapter, rnd)
    h.load(generate_dataset(spec, h.adapter, rnd, rows, gen))
    return h


PAIRS = [("v_a", "v_b"), ("v_b", "v_c"), ("v_c", "v_d"), ("v_d", "v_a"),
         # Milestone 3: adding a hierarchy and a cardinality limit (internal role, SECURITY DEFINER
         # helpers, triggers, template FKs), then changing policies around them and the limit itself
         ("org_flat_spec", "org_base_spec"), ("org_base_spec", "hierarchy_spec"),
         ("hierarchy_spec", "cardinality_spec"), ("cardinality_spec", "org_base_spec")]


@pytest.mark.parametrize("pair", PAIRS, ids=["->".join(p) for p in PAIRS])
def test_migration_reaches_the_new_spec(pg_dsn, pair):
    old = getattr(specs, pair[0])()
    new = getattr(specs, pair[1])()
    h = deploy_with_data(old, pg_dsn, "mls_migrate")
    try:
        assert drift(h.conn, old, compile_spec(old)).ok
        # columns that become NOT NULL may hold NULLs in the generated data: clear those rows first
        with h.conn.cursor() as cur:
            for t, tb in new.tables.items():
                for c, col in tb.columns.items():
                    if t in old.tables and c in old.tables[t].columns and not col.nullable \
                            and old.tables[t].columns[c].nullable:
                        cur.execute(f'delete from "{t}" where "{c}" is null')
        # likewise rows the new constraints reject (the generated data satisfies only the old ones)
        with h.conn.cursor() as cur:
            for v in preflight(h.conn, new, old).violations:
                assert v.cond, str(v)
                cur.execute(f'delete from "{v.table}" t where {v.cond}')
        pf = preflight(h.conn, new, old)
        assert pf.ok, [str(v) for v in pf.violations]
        m = migrate(old, new, allow_destructive=True)
        assert not m.empty
        apply_migration(h.conn, m)
        d = drift(h.conn, new, compile_spec(new))
        assert d.ok, "\n".join(str(i) for i in d.items)
        assert not drift(h.conn, old, compile_spec(old)).ok  # the old spec no longer matches
        h2 = Harness(new, compile_spec(new), pg_dsn, "mls_migrate")
        h2.attach()
        try:
            res = run_differential(new, h2.script, pg_dsn, "mls_migrate", harness=h2, seed=1,
                                   rows_per_table=30, n_contexts=10, ops_per_context=20)
        finally:
            h2.close(drop=False)
        assert res.ok, res.summary() + "\n" + "\n".join(str(x) for x in res.mismatches[:10])
        assert res.visible_rows > 0
    finally:
        h.close()


def test_dropping_a_hierarchy(pg_dsn):
    """Removing the hierarchy and the limit drops the helpers, triggers, template constraints and
    the internal policies; the database then matches a fresh deploy of the flat spec."""
    old, new = specs.org_base_spec(), specs.org_flat_spec()
    h = deploy_with_data(old, pg_dsn, "mls_migrate_flat")
    try:
        with h.conn.cursor() as cur:  # the flat spec allows only root groups (restrict FKs: members first)
            cur.execute('delete from "files"; delete from "group_members"; delete from "groups" where "parent_id" is not null')
        assert preflight(h.conn, new, old).ok
        apply_migration(h.conn, migrate(old, new, allow_destructive=True))
        d = drift(h.conn, new, compile_spec(new))
        assert d.ok, "\n".join(str(i) for i in d.items)
        with h.conn.cursor() as cur:
            cur.execute("select proname from pg_proc where proname like 'mls_reach_%' or proname like 'mls_card_%'")
            assert cur.fetchall() == []
            cur.execute("select polname from pg_policy where polname like 'mls_internal_%'")
            assert cur.fetchall() == []
    finally:
        h.close()


def test_noop_migration_is_empty():
    spec = specs.v_c()
    assert migrate(spec, spec).empty


def test_unvalidated_constraints_are_drift(pg_dsn):
    old, new = specs.v_b(), specs.v_c()
    h = deploy_with_data(old, pg_dsn, "mls_migrate_mut")
    try:
        apply_migration(h.conn, migrate(old, new, mutations={Mutation.MIGRATE_SKIP_VALIDATE}))
        d = drift(h.conn, new, compile_spec(new))
        assert not d.ok
        assert any("validated" in str(i) and "level_range" in i.object for i in d.items), d.items
    finally:
        h.close()
