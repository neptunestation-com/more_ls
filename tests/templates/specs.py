"""Per-template specs for the differential and mutation tests.  Each variant of the example spec
isolates one template and the data shape its mutation needs (NULLs, column lists, bounds...)."""
from __future__ import annotations

import json

from mls.spec import check_spec
from mls.spec.model import Spec
from tests.conftest import EXAMPLE


def base() -> dict:
    return json.loads(EXAMPLE.read_text())


def make(d: dict) -> Spec:
    spec = Spec.model_validate(d)
    spec = check_spec(spec)
    return spec


def policy_spec() -> Spec:
    return make(base())


def in_spec() -> Spec:
    """A policy with `in` (expanded to an IN list): members see levels 2 and 3 whatever their
    clearance.  The mutation drops the last constant, hiding level 3 from low-clearance members.
    The admin policy (R2) and its lower bound go: in the generated data every member pair is
    also an admin pair, so with R2 the `in` policy would never decide anything."""
    d = base()
    d["rules"] = [r for r in d["rules"] if r["id"] != "R2"]
    d["properties"] = [p for p in d["properties"] if p["id"] != "P8"]
    d["rules"].append({"id": "R10", "kind": "policy", "name": "member_read_high", "table": "docs",
                       "role": "authenticated", "command": "select", "mode": "permissive",
                       "using": {"and": [{"member": {"relation": "member_of", "key": {"ctx": "uid"},
                                                     "value": {"col": "tenant_id"}}},
                                         {"in": [{"col": "level"}, [{"const": 2}, {"const": 3}]]}]}})
    return make(d)


def check_nullable_spec() -> Spec:
    d = base()
    d["tables"]["docs"]["columns"]["flag"] = {"type": "int", "nullable": True}
    d["rules"].append({"id": "C1", "kind": "check", "name": "flag_bit", "table": "docs",
                       "predicate": {"or": [{"cmp": [{"col": "flag"}, "=", {"const": 0}]},
                                            {"cmp": [{"col": "flag"}, "=", {"const": 1}]}]}})
    return make(d)


def transition_nullable_spec() -> Spec:
    d = base()
    d["rules"].append({"id": "T1", "kind": "transition", "name": "owner_immutable", "table": "docs",
                       "predicate": {"cmp": [{"new": "owner_id"}, "=", {"old": "owner_id"}]}})
    return make(d)


def fk_spec() -> Spec:
    return make(base())


def column_rule_spec() -> Spec:
    d = base()
    d["rules"].append({"id": "C1", "kind": "column_rule", "role": "authenticated", "table": "docs",
                       "columns": ["id", "tenant_id", "level"], "access": "read"})
    d["rules"].append({"id": "C2", "kind": "column_rule", "role": "authenticated", "table": "docs",
                       "columns": ["id", "tenant_id", "level", "owner_id"], "access": "write"})
    d["properties"] = []
    return make(d)


def time_spec() -> Spec:
    d = base()
    d["tables"]["docs"]["columns"]["valid_from"] = {"type": "timestamp", "nullable": True}
    d["tables"]["docs"]["columns"]["valid_until"] = {"type": "timestamp", "nullable": True}
    d["rules"].append({"id": "TB", "kind": "policy", "name": "currently_valid", "table": "docs",
                       "role": "authenticated", "command": "all", "mode": "restrictive",
                       "using": {"now_between": {"from": {"col": "valid_from"}, "until": {"col": "valid_until"}}}})
    return make(d)


def opaque_spec() -> Spec:
    d = base()
    d["opaques"] = {"low_or_cleared": {"table": "docs", "columns": ["level"], "context": ["clearance"],
                                       "sql": "level < 1 or {ctx:clearance} >= 3"}}
    d["rules"].append({"id": "OP", "kind": "policy", "name": "opaque_gate", "table": "docs",
                       "role": "authenticated", "command": "all", "mode": "restrictive",
                       "using": {"opaque": "low_or_cleared"}})
    return make(d)


def opaque_impl():
    return {"low_or_cleared": lambda row, ctx: row["level"] < 1 or ctx["clearance"] >= 3}


PG_ROLES = EXAMPLE.parent / "pg_roles_docs.mls.json"
SESSION = EXAMPLE.parent / "session_docs.mls.json"


def session_spec() -> Spec:
    """The example on the session_setting adapter (D47): the harness sets app.uid and
    app.clearance with set_config inside the transaction."""
    return make(json.loads(SESSION.read_text()))


def pg_roles_spec() -> Spec:
    """The pg_roles rendering of the example (`examples/pg_roles_docs.mls.json`): same tables and
    rule shape (two permissive SELECT policies on docs, so the all-restrictive mutation is
    detectable), identity from `current_user`, no clearance claim."""
    return make(json.loads(PG_ROLES.read_text()))


# ----------------------------------------------------------------------------- versions (M2)
V2 = EXAMPLE.parent / "tenant_docs_v2.mls.json"


def v_a() -> Spec:
    """The example without the transition rule (no trigger)."""
    d = base()
    d["rules"] = [r for r in d["rules"] if r["id"] != "R9"]
    d["properties"] = [p for p in d["properties"] if p["id"] != "P2"]
    return make(d)


def v_b() -> Spec:
    return make(base())


def v_c() -> Spec:
    return make(json.loads(V2.read_text()))


def v_c_level() -> Spec:
    """v2 plus a permissive policy on `level` alone: the migration from v1 creates an index
    (`mls_ix_docs_level`) concurrently, and the migration back drops it.  (v2's policy on
    `owner_id` needs no index of its own: v1's inclusion index (owner_id, tenant_id) covers it.)"""
    d = json.loads(V2.read_text())
    d["rules"].append({"id": "R16", "kind": "policy", "name": "low_level_read", "table": "docs",
                       "role": "authenticated", "command": "select", "mode": "permissive",
                       "using": {"cmp": [{"col": "level"}, "<=", {"const": 0}]}})
    return make(d)


def v_d() -> Spec:
    """v2 plus: archived becomes NOT NULL, level becomes nullable, a unique (tenant_id, id), an FK."""
    d = json.loads(V2.read_text())
    d["tables"]["docs"]["columns"]["archived"]["nullable"] = False
    d["tables"]["docs"]["columns"]["level"]["nullable"] = True
    d["tables"]["docs"]["unique"] = [["tenant_id", "id"]]
    d["rules"].append({"id": "R15", "kind": "inclusion", "name": "owner_is_admin", "table": "docs",
                       "columns": ["owner_id", "tenant_id"], "relation": "admin_of", "on_delete": "restrict"})
    return make(d)


def v_tight() -> Spec:
    """The example with a stricter level range and a NULL-sensitive check on archived."""
    d = base()
    for r in d["rules"]:
        if r["id"] == "R6":
            r["predicate"] = {"and": [{"cmp": [{"col": "level"}, ">=", {"const": 0}]},
                                      {"cmp": [{"col": "level"}, "<=", {"const": 2}]}]}
    d["rules"].append({"id": "R14", "kind": "check", "name": "archived_bit", "table": "docs",
                       "predicate": {"or": [{"cmp": [{"col": "archived"}, "=", {"const": 0}]},
                                            {"cmp": [{"col": "archived"}, "=", {"const": 1}]}]}})
    return make(d)


# ----------------------------------------------------------------------------- M3
ORG = EXAMPLE.parent / "org_tree.mls.json"


def org_base() -> dict:
    return json.loads(ORG.read_text())


def hierarchy_spec() -> Spec:
    """org_tree where authenticated may also insert and update groups (so the depth bound and the
    parent immutability are exercised by app roles)."""
    d = org_base()
    d["rules"] += [
        {"id": "G1", "kind": "policy", "name": "group_insert", "table": "groups", "role": "authenticated",
         "command": "insert", "mode": "permissive", "check": {"lit": True}},
        {"id": "G2", "kind": "policy", "name": "group_update", "table": "groups", "role": "authenticated",
         "command": "update", "mode": "permissive", "using": {"lit": True}},
        {"id": "G3", "kind": "policy", "name": "member_insert", "table": "group_members", "role": "authenticated",
         "command": "insert", "mode": "permissive", "check": {"lit": True}},
        {"id": "G4", "kind": "policy", "name": "member_delete", "table": "group_members", "role": "authenticated",
         "command": "delete", "mode": "permissive", "using": {"lit": True}}]
    d["properties"] = [p for p in d["properties"] if p["id"] != "P5"]
    return make(d)


def cardinality_spec() -> Spec:
    """org_tree with a tight limit (2 files per tenant) and file deletes."""
    d = org_base()
    for r in d["rules"]:
        if r["id"] == "R5":
            r["max"] = 2
    d["rules"].append({"id": "F1", "kind": "policy", "name": "file_delete", "table": "files",
                       "role": "authenticated", "command": "delete", "mode": "permissive",
                       "using": {"in_group": {"hierarchy": "org", "key": {"ctx": "uid"}, "value": {"col": "group_id"}}}})
    d["properties"] = [p for p in d["properties"] if p["id"] != "P5"]
    return make(d)


def org_base_spec() -> Spec:
    return make(org_base())


def org_flat_spec() -> Spec:
    """org_tree without the hierarchy and the cardinality limit: direct membership only, every
    group a root (so a migration to org_tree finds consistent depths)."""
    d = org_base()
    del d["hierarchies"]
    for r in d["rules"]:
        for k in ("using", "check"):
            if "in_group" in r.get(k, {}):
                ig = r[k]["in_group"]
                r[k] = {"member": {"relation": "group_member", "key": ig["key"], "value": ig["value"]}}
    d["rules"] = [r for r in d["rules"] if r["kind"] != "cardinality"]
    d["rules"].append({"id": "X1", "kind": "check", "name": "groups_are_roots", "table": "groups",
                       "predicate": {"and": [{"is_null": {"col": "parent_id"}},
                                             {"cmp": [{"col": "depth"}, "=", {"const": 0}]}]}})
    d["properties"] = [p for p in d["properties"] if p["id"] in ("P4", "P5")]
    return make(d)


# ----------------------------------------------------------------------------- M5 (brownfield, D45)
def adopted_spec(assume: list[dict] | None = None) -> Spec:
    """The example with `docs` adopted (the harness creates it as the pre-existing table) and the
    optional hardening assumptions."""
    d = base()
    d["tables"]["docs"]["mode"] = "adopted"
    if assume:
        d["tables"]["docs"]["assume"] = assume
    return make(d)


def observed_spec() -> Spec:
    """The example plus an observed table nothing names."""
    d = base()
    d["tables"]["audit"] = {"columns": {"id": {"type": "int", "nullable": False}}, "mode": "observed",
                            "tags": ["legacy"]}
    return make(d)
