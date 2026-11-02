"""Per-template specs for the differential and mutation tests.  Each variant of the example spec
isolates one template and the data shape its mutation needs (NULLs, column lists, bounds...)."""
from __future__ import annotations

import copy
import json

from mls.spec import check_spec
from mls.spec.model import Spec
from tests.conftest import EXAMPLE


def base() -> dict:
    return json.loads(EXAMPLE.read_text())


def make(d: dict) -> Spec:
    spec = Spec.model_validate(d)
    check_spec(spec)
    return spec


def policy_spec() -> Spec:
    return make(base())


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


def pg_roles_spec() -> Spec:
    d = base()
    d["identity"] = {"adapter": "pg_roles", "context": {"uid": {"source": "current_user", "type": "text"}}}
    for t in d["tables"].values():
        for c in t["columns"].values():
            if c["type"] == "uuid":
                c["type"] = "text"
    # no clearance claim with pg_roles: drop the level comparisons
    def strip(p):
        if isinstance(p, dict):
            if "and" in p:
                items = [strip(x) for x in p["and"] if not ("cmp" in x and x["cmp"][2] == {"ctx": "clearance"})]
                return items[0] if len(items) == 1 else {"and": items}
            return {k: strip(v) for k, v in p.items()}
        return p
    for r in d["rules"]:
        for k in ("using", "check"):
            if k in r:
                r[k] = strip(r[k])
    return make(d)


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
