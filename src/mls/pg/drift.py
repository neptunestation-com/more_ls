"""Drift detection (D7, D8, D13): compile the spec into a shadow schema on the same server, take
normalized catalog snapshots of the shadow and of the live schema, and compare them.  Everything
runs in one transaction that is rolled back, so the server is left untouched.

Reported: missing objects, objects that differ (expression, validation state, roles, privileges),
foreign objects on MLS-owned tables (policies, triggers, constraints, indexes the spec does not
own), hardening (RLS enabled and forced, app roles without BYPASSRLS/superuser, no SET on
session_replication_role, app role not the owner).  Tables in the schema that the spec does not
mention are listed as notes, not drift.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

import psycopg
from psycopg import sql as psql

from mls.compile.internal import INTERNAL_ROLE, needs_internal_role
from mls.compile.plan import Script
from mls.pg.catalog import Snapshot, snapshot
from mls.spec.model import Spec


@dataclass
class DriftItem:
    kind: str        # missing | unexpected | differs | hardening
    object: str
    expected: str | None = None
    actual: str | None = None

    def __str__(self) -> str:
        s = f"{self.kind}: {self.object}"
        if self.expected is not None or self.actual is not None:
            s += f" (expected {self.expected!r}, found {self.actual!r})"
        return s


@dataclass
class DriftResult:
    items: list[DriftItem]
    notes: list[str]

    @property
    def ok(self) -> bool:
        return not self.items


def shadow_snapshot(conn: psycopg.Connection, spec: Spec, script: Script) -> Snapshot:
    """Deploy the script into a throwaway schema inside the current transaction and snapshot it."""
    shadow = "mls_shadow_" + secrets.token_hex(4)
    with conn.cursor() as cur:
        cur.execute(psql.SQL("create schema {}").format(psql.Identifier(shadow)))
        cur.execute(psql.SQL("set local search_path to {}, pg_catalog").format(psql.Identifier(shadow)))
        for st in script.statements:
            if st.section == "roles":
                continue  # roles are global; their attributes are compared directly
            cur.execute(st.sql)
    return snapshot(conn, shadow, list(spec.tables), _roles(spec))


def _roles(spec: Spec) -> list[str]:
    return list(spec.roles) + ([INTERNAL_ROLE] if needs_internal_role(spec) else [])


def compare(spec: Spec, expected: Snapshot, actual: Snapshot) -> list[DriftItem]:
    items: list[DriftItem] = []
    for t, exp in expected["tables"].items():
        act = actual["tables"].get(t)
        if act is None:
            items.append(DriftItem("missing", f"table {t}"))
            continue
        for c, e in exp["columns"].items():
            a = act["columns"].get(c)
            if a is None:
                items.append(DriftItem("missing", f"column {t}.{c}"))
            elif a != e:
                items.append(DriftItem("differs", f"column {t}.{c}", str(e), str(a)))
        for c in act["columns"]:
            if c not in exp["columns"]:
                items.append(DriftItem("unexpected", f"column {t}.{c}"))
        for group, label in (("constraints", "constraint"), ("policies", "policy"),
                             ("triggers", "trigger"), ("indexes", "index")):
            for name, e in exp[group].items():
                a = act[group].get(name)
                if a is None:
                    items.append(DriftItem("missing", f"{label} {name} on {t}"))
                elif a != e:
                    items.append(DriftItem("differs", f"{label} {name} on {t}", str(e), str(a)))
            for name in act[group]:
                if name not in exp[group]:
                    items.append(DriftItem("unexpected", f"{label} {name} on {t}"))
        if exp["acl"] != act["acl"]:
            missing = sorted(set(exp["acl"]) - set(act["acl"]))
            extra = sorted(set(act["acl"]) - set(exp["acl"]))
            for g in missing:
                items.append(DriftItem("missing", f"privilege {g[1]} on {t}{'.' + g[2] if g[2] else ''} for {g[0]}"))
            for g in extra:
                items.append(DriftItem("unexpected", f"privilege {g[1]} on {t}{'.' + g[2] if g[2] else ''} for {g[0]}"))
        if not act["rls"]:
            items.append(DriftItem("hardening", f"table {t}: row level security is not enabled"))
        if not act["force"]:
            items.append(DriftItem("hardening", f"table {t}: row level security is not forced"))
        if act["owner"] in spec.roles:
            items.append(DriftItem("hardening", f"table {t}: owned by app role {act['owner']}"))
    for name, e in expected["functions"].items():
        a = actual["functions"].get(name)
        if a is None:
            items.append(DriftItem("missing", f"function {name}"))
        elif a != e:
            items.append(DriftItem("differs", f"function {name}", str(e), str(a)))
    for r in _roles(spec):
        a = actual["roles"].get(r)
        if a is None:
            items.append(DriftItem("missing", f"role {r}"))
            continue
        if a["bypassrls"]:
            items.append(DriftItem("hardening", f"role {r} has BYPASSRLS"))
        if a["super"]:
            items.append(DriftItem("hardening", f"role {r} is a superuser"))
        if a["can_set_replication_role"]:
            items.append(DriftItem("hardening", f"role {r} may SET session_replication_role"))
        if r == INTERNAL_ROLE:
            if a["login"]:
                items.append(DriftItem("hardening", f"role {r} can log in"))
            for mbr in a["members"]:
                if mbr in spec.roles:
                    items.append(DriftItem("hardening", f"app role {mbr} is a member of {r}"))
    return items


def drift(conn: psycopg.Connection, spec: Spec, script: Script, schema: str = "public") -> DriftResult:
    with conn.transaction(force_rollback=True):  # nests as a savepoint inside a caller's transaction
        expected = shadow_snapshot(conn, spec, script)
        actual = snapshot(conn, schema, list(spec.tables), _roles(spec))
    items = compare(spec, expected, actual)
    notes = [f"table '{t}' in schema '{schema}' is not in the spec" for t in actual["other_tables"]]
    return DriftResult(items, notes)
