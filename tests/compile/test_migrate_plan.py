"""Migration plans (no Postgres): phases, ordering, NOT VALID / VALIDATE, renames, refusals,
destructive gating, lock annotations."""
from __future__ import annotations

import json

import pytest

from mls.compile.migrate import LOCKS, Migration, migrate
from mls.errors import CompileError
from tests.templates import specs


def kinds(m: Migration, phase: str) -> list[str]:
    return [s.kind for p in m.phases if p.name == phase for s in p.statements]


def sqls(m: Migration) -> list[str]:
    return [s.sql for p in m.phases for s in p.statements]


def test_phases_and_ordering():
    m = migrate(specs.v_b(), specs.v_c_level())
    names = [p.name for p in m.phases]
    assert names == ["schema", "constraints", "indexes", "validate", "swap", "cleanup", "destructive"]
    assert [p.transactional for p in m.phases] == [True, True, False, False, True, False, True]
    assert kinds(m, "schema")[:2] == ["create table", "create table"]  # owner guard, then the new table
    assert "add column" in kinds(m, "schema")
    assert kinds(m, "constraints").count("add check not valid") == 3 and "replace function" in kinds(m, "constraints")
    assert kinds(m, "indexes") == ["create index concurrently"]
    assert kinds(m, "validate") == ["validate constraint", "validate constraint"]
    assert kinds(m, "swap")[:2] == ["drop constraint", "rename constraint"]
    assert "create policy" in kinds(m, "swap") and "grant" in kinds(m, "swap")
    assert kinds(m, "cleanup") == [] and kinds(m, "destructive") == []
    s = sqls(m)
    assert any('add constraint "level_range_mls_new"' in x and "not valid" in x for x in s)
    assert any('add constraint "max_level_range"' in x and "not valid" not in x for x in s)  # new table: empty
    assert any('rename constraint "level_range_mls_new" to "level_range"' in x for x in s)
    text = m.sql()
    assert "begin;" in text and "commit;" in text and "-- lock: ShareUpdateExclusiveLock on docs" in text
    assert "preflight" in text


def test_destructive_is_gated():
    old, new = specs.v_c_level(), specs.v_b()
    m = migrate(old, new)
    assert kinds(m, "destructive") == []
    assert any("destructive statements omitted" in n and 'drop table "tenant_settings"' in n for n in m.notes)
    m2 = migrate(old, new, allow_destructive=True)
    assert kinds(m2, "destructive") == ["drop column", "drop table"]
    assert "drop policy" in kinds(m2, "swap") and "drop index concurrently" in kinds(m2, "cleanup")
    assert "revoke" in kinds(m2, "swap")


def test_trigger_add_and_remove():
    m = migrate(specs.v_a(), specs.v_b())
    assert kinds(m, "constraints") == ["create function", "create trigger"]
    m = migrate(specs.v_b(), specs.v_a())
    assert kinds(m, "swap") == ["drop trigger", "drop function"]


def test_nullability_and_unique():
    m = migrate(specs.v_c(), specs.v_d())
    k = kinds(m, "schema")
    assert "set not null" in k and "drop not null" in k and "add unique" in k
    assert "add fk not valid" in kinds(m, "constraints")
    assert any("drop constraint" == x for x in kinds(migrate(specs.v_d(), specs.v_c()), "swap"))


def test_noop_and_refusals():
    assert migrate(specs.v_c(), specs.v_c()).empty
    d = specs.base()
    d["tables"]["docs"]["columns"]["level"]["type"] = "text"
    for r in d["rules"]:
        for k in ("using", "check", "predicate"):
            if k in r:
                r[k] = {"lit": True}
    d["properties"] = []
    with pytest.raises(CompileError) as e:
        migrate(specs.v_b(), specs.make(d))
    err = e.value.errors[0]
    assert err.path == "$.tables.docs.columns.level.type" and "data conversions" in err.message and err.fix
    d = specs.base()
    d["tables"]["docs"]["primary_key"] = ["id", "tenant_id"]
    with pytest.raises(CompileError) as e:
        migrate(specs.v_b(), specs.make(d))
    assert e.value.errors[0].path == "$.tables.docs.primary_key"


def test_every_kind_has_a_documented_lock():
    for pair in (("a", "b"), ("b", "c"), ("c", "d"), ("d", "a")):
        m = migrate(getattr(specs, f"v_{pair[0]}")(), getattr(specs, f"v_{pair[1]}")(), allow_destructive=True)
        for p in m.phases:
            for s in p.statements:
                assert s.kind in LOCKS
        json.dumps(m.to_dict())


def test_hierarchy_and_cardinality_migration():
    """Adding the hierarchy and the limit creates the internal role, the SECURITY DEFINER helpers
    (owner, EXECUTE grants), the triggers and the template constraints (NOT VALID, then validated);
    removing them drops the helpers only after the policies that reference them."""
    m = migrate(specs.org_flat_spec(), specs.org_base_spec())
    s = sqls(m)
    assert kinds(m, "schema").count("create role") == 3 and any('create role "mls_internal"' in x for x in s)
    assert any('grant "mls_internal" to current_user' in x for x in s)
    c = kinds(m, "constraints")
    assert c.count("add fk not valid") == 3 and c.count("add check not valid") == 1
    assert c.count("create function") == 4 and c.count("create trigger") == 3  # reach, depth, count, parent-immutable
    assert any('owner to "mls_internal"' in x for x in s) and any('grant execute on function "mls_reach_org"' in x for x in s)
    assert kinds(m, "validate") == ["validate constraint"] * 4
    assert kinds(m, "destructive") == [] and "drop function" not in kinds(m, "swap")
    back = migrate(specs.org_base_spec(), specs.org_flat_spec(), allow_destructive=True)
    sw = kinds(back, "swap")
    assert sw.count("drop function") == 3 + 1  # reach, depth, count + the implicit parent-immutable trigger
    assert sw.index("drop function") > sw.index("create policy") and sw.count("drop trigger") == 3
    assert any('drop policy "mls_internal_groups"' in x for x in sqls(back))
    assert LOCKS["alter function"] is None and LOCKS["drop function"] is None


def test_cardinality_change_replaces_the_trigger_function():
    m = migrate(specs.org_base_spec(), specs.cardinality_spec())
    assert kinds(m, "constraints") == ["replace function"]
    assert any('"mls_card_files_files_per_tenant"' in x and "> 2" in x for x in sqls(m))
