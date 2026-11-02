"""Pre-flight counts agree with the concrete model (NULLs fail `IS TRUE` checks), and new
columns are evaluated as NULL."""
from __future__ import annotations

import random

import pytest

from mls.compile import compile_spec
from mls.pg.harness import Harness, ValueGen, generate_dataset
from mls.pg.model import holds
from mls.pg.preflight import preflight
from mls.props.effective import inclusion_pred
from mls.spec.model import CONTEXT_NOW
from tests.templates import specs

pytestmark = pytest.mark.pg


def test_preflight_counts_match_model(pg_dsn):
    old, new = specs.v_b(), specs.v_tight()
    h = Harness(old, compile_spec(old), pg_dsn, "mls_preflight")
    h.deploy()
    try:
        rnd = random.Random(5)
        gen = ValueGen(old, h.adapter, rnd, null_p=0.5)
        ds = generate_dataset(old, h.adapter, rnd, 40, gen)
        h.load(ds)
        res = preflight(h.conn, new, old)
        by_rule = {v.rule_id: v for v in res.violations}
        db = ds.database(old)
        ctx = {CONTEXT_NOW: ds.now}
        for rid in ("R6", "R14"):
            pred = new.rule(rid).predicate
            expect = sum(1 for r in ds.tables["docs"] if not holds(pred, r, ctx, db))
            got = by_rule[rid].count if rid in by_rule else 0
            assert got == expect, (rid, got, expect)
        assert by_rule["R14"].count > 0 and len(by_rule["R14"].samples) == min(5, by_rule["R14"].count)
        assert any(s["archived"] is None for s in by_rule["R14"].samples)  # NULL fails IS TRUE
        assert preflight(h.conn, old, old).ok  # nothing new: nothing to check
    finally:
        h.close()


def test_preflight_new_columns_and_fks(pg_dsn):
    """A check on a column that does not exist yet sees NULL; an FK to a new relation table
    counts every non-null pair; a new NOT NULL column counts every row."""
    old = specs.v_b()
    d = specs.base()
    d["tables"]["docs"]["columns"]["title"] = {"type": "text", "nullable": True}
    d["tables"]["docs"]["columns"]["created_by"] = {"type": "uuid", "nullable": False}
    d["tables"]["groups"] = {"columns": {"user_id": {"type": "uuid", "nullable": False},
                                         "tenant_id": {"type": "int", "nullable": False}},
                             "primary_key": ["user_id", "tenant_id"]}
    d["relations"]["group_of"] = {"table": "groups", "key": "user_id", "value": "tenant_id"}
    d["rules"] += [
        {"id": "N1", "kind": "check", "name": "title_present", "table": "docs",
         "predicate": {"not": {"is_null": {"col": "title"}}}},
        {"id": "N2", "kind": "inclusion", "name": "owner_in_group", "table": "docs",
         "columns": ["owner_id", "tenant_id"], "relation": "group_of", "on_delete": "restrict"}]
    new = specs.make(d)
    h = Harness(old, compile_spec(old), pg_dsn, "mls_preflight2")
    h.deploy()
    try:
        rnd = random.Random(7)
        gen = ValueGen(old, h.adapter, rnd, null_p=0.3)
        ds = generate_dataset(old, h.adapter, rnd, 30, gen)
        h.load(ds)
        res = preflight(h.conn, new, old)
        by_rule = {v.rule_id: v for v in res.violations}
        n_docs = len(ds.tables["docs"])
        assert by_rule["N1"].count == n_docs                      # every row has title NULL
        owners = sum(1 for r in ds.tables["docs"] if r["owner_id"] is not None)
        assert by_rule["N2"].count == owners                      # groups is empty (does not exist yet)
        assert by_rule["schema"].count == n_docs and "created_by" in by_rule["schema"].what
        assert any("groups" in n for n in res.notes)
    finally:
        h.close()
