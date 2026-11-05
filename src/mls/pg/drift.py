"""Drift detection (D7, D8, D13, D45): compile the spec into a shadow schema on the same server, take
normalized catalog snapshots of the shadow and of the live schema, and compare them.  Everything
runs in one transaction that is rolled back, so the server is left untouched.

Reported: missing objects, objects that differ (expression, validation state, roles, privileges),
foreign objects on MLS-owned tables (policies, triggers, constraints, indexes the spec does not
own), hardening (RLS enabled and forced, app roles without BYPASSRLS/superuser, no SET on
session_replication_role, app role not the owner).  Tables in the schema that the spec does not
mention are listed as notes, not drift.

Table modes (D45): on an `adopted` table a constraint, trigger, index or column the spec does not
declare is `foreign` (listed, not drift: the table was not created by MLS), while an undeclared
policy or privilege is still `unexpected` (it changes what a role can do).  Hardening the spec
states under `assume` is reported as `assumed`, not as `hardening`; an assumption the catalog no
longer needs is a note.  An `observed` table is only checked to exist.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

import psycopg
from psycopg import sql as psql

from mls.compile.internal import INTERNAL_ROLE, needs_internal_role
from mls.compile.plan import Script
from mls.compile.table import table_ddl
from mls.pg.catalog import Snapshot, snapshot
from mls.spec.model import BypassAssumed, OwnerAssumed, RlsAssumed, Spec

FAILING = ("missing", "unexpected", "differs", "hardening")


@dataclass
class DriftItem:
    kind: str        # missing | unexpected | differs | hardening | assumed | foreign
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
        """No drift: `assumed` and `foreign` items are the expected state of an adopted table."""
        return not any(i.kind in FAILING for i in self.items)

    def by_kind(self, kind: str) -> list[DriftItem]:
        return [i for i in self.items if i.kind == kind]


def shadow_snapshot(conn: psycopg.Connection, spec: Spec, script: Script) -> Snapshot:
    """Deploy the script into a throwaway schema inside the current transaction and snapshot it."""
    shadow = "mls_shadow_" + secrets.token_hex(4)
    with conn.cursor() as cur:
        cur.execute(psql.SQL("create schema {}").format(psql.Identifier(shadow)))
        cur.execute(psql.SQL("set local search_path to {}, pg_catalog").format(psql.Identifier(shadow)))
        for name, t in spec.tables.items():
            if t.mode == "adopted":  # the script does not create it: stand in for the existing table
                for s in table_ddl(name, t):
                    cur.execute(s)
        for st in script.statements:
            if st.section == "roles":
                continue  # roles are global; their attributes are compared directly
            cur.execute(st.sql)
    return snapshot(conn, shadow, list(spec.governed()), _roles(spec), _observed(spec))


def _roles(spec: Spec) -> list[str]:
    return list(spec.roles) + ([INTERNAL_ROLE] if needs_internal_role(spec) else [])


def _observed(spec: Spec) -> list[str]:
    return [t for t, tb in spec.tables.items() if tb.mode == "observed"]


def _assumed(spec: Spec, table: str, cls, value: str) -> bool:
    """Whether `table` assumes `cls` with the given value (owner / bypassrls role, or rls mode)."""
    field = next(iter(cls.model_fields))
    return any(isinstance(a, cls) and getattr(a, field) == value for a in spec.tables[table].assume)


def compare(spec: Spec, expected: Snapshot, actual: Snapshot) -> tuple[list[DriftItem], list[str]]:
    items: list[DriftItem] = []
    notes: list[str] = []
    for t, exp in expected["tables"].items():
        mode = spec.tables[t].mode
        extra = "unexpected" if mode == "owned" else "foreign"
        act = actual["tables"].get(t)
        if act is None:
            items.append(DriftItem("missing", f"table {t}" + (" (adopted: it must exist)" if mode == "adopted" else "")))
            continue
        for c, e in exp["columns"].items():
            a = act["columns"].get(c)
            if a is None:
                items.append(DriftItem("missing", f"column {t}.{c}"))
            elif a != e:
                items.append(DriftItem("differs", f"column {t}.{c}", str(e), str(a)))
        for c in act["columns"]:
            if c not in exp["columns"]:
                items.append(DriftItem(extra, f"column {t}.{c}"))
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
                    items.append(DriftItem("unexpected" if group == "policies" else extra, f"{label} {name} on {t}"))
        exp_acl, act_acl = set(exp["acl"]), set(act["acl"])
        if act["owner"] in spec.roles:  # an owner holds every privilege: its grants are not comparable
            exp_acl = {g for g in exp_acl if g[0] != act["owner"]}
            act_acl = {g for g in act_acl if g[0] != act["owner"]}
        if exp_acl != act_acl:
            missing = sorted(exp_acl - act_acl)
            extra_acl = sorted(act_acl - exp_acl)
            for g in missing:
                items.append(DriftItem("missing", f"privilege {g[1]} on {t}{'.' + g[2] if g[2] else ''} for {g[0]}"))
            for g in extra_acl:
                items.append(DriftItem("unexpected", f"privilege {g[1]} on {t}{'.' + g[2] if g[2] else ''} for {g[0]}"))
        if not act["rls"]:
            items.append(DriftItem("hardening", f"table {t}: row level security is not enabled"))
        if not act["force"]:
            if mode == "adopted" and _assumed(spec, t, RlsAssumed, "not_forced"):
                items.append(DriftItem("assumed", f"table {t}: row level security is not forced; the owner "
                                                  f"{act['owner']} bypasses the policies and is trusted"))
            else:
                items.append(DriftItem("hardening", f"table {t}: row level security is not forced"))
        elif mode == "adopted" and _assumed(spec, t, RlsAssumed, "not_forced"):
            notes.append(f"table '{t}': row level security is forced now; the assumption {{\"rls\": \"not_forced\"}} "
                         f"is no longer needed")
        if act["owner"] in spec.roles:
            if mode == "adopted" and _assumed(spec, t, OwnerAssumed, act["owner"]):
                items.append(DriftItem("assumed", f"table {t}: owner {act['owner']} is an app role and is trusted "
                                                  f"(it holds every privilege; its grants are not compared)"))
            else:
                items.append(DriftItem("hardening", f"table {t}: owned by app role {act['owner']}"))
        for a in spec.tables[t].assume:
            if isinstance(a, OwnerAssumed) and a.owner != act["owner"]:
                notes.append(f"table '{t}': the owner is {act['owner']} now, not {a.owner}; the assumption "
                             f"{{\"owner\": \"{a.owner}\"}} is no longer needed")
    for t, present in actual.get("observed", {}).items():
        if not present:
            items.append(DriftItem("missing", f"table {t} (observed)"))
    for name, e in expected["functions"].items():
        a = actual["functions"].get(name)
        if a is None:
            items.append(DriftItem("missing", f"function {name}"))
        elif a != e:
            items.append(DriftItem("differs", f"function {name}", str(e), str(a)))
    adopted = [t for t, tb in spec.governed().items() if tb.mode == "adopted"]
    for r in _roles(spec):
        a = actual["roles"].get(r)
        if a is None:
            items.append(DriftItem("missing", f"role {r}"))
            continue
        if a["bypassrls"]:
            assuming = [t for t in adopted if _assumed(spec, t, BypassAssumed, r)]
            if r in spec.roles and assuming and len(assuming) == len(spec.governed()):
                for t in assuming:
                    items.append(DriftItem("assumed", f"role {r} has BYPASSRLS and is trusted on {t}"))
            else:
                items.append(DriftItem("hardening", f"role {r} has BYPASSRLS"))
        else:
            for t in adopted:
                if _assumed(spec, t, BypassAssumed, r):
                    notes.append(f"role '{r}' has no BYPASSRLS now; the assumption {{\"bypassrls\": \"{r}\"}} on "
                                 f"table '{t}' is no longer needed")
        if a["super"]:
            items.append(DriftItem("hardening", f"role {r} is a superuser"))
        if a["can_set_replication_role"]:
            items.append(DriftItem("hardening", f"role {r} may SET session_replication_role"))
        if spec.identity.adapter == "session_setting" and r in spec.roles:  # D47
            for fn in a["app_setters"]:
                items.append(DriftItem("hardening", f"role {r} may execute {fn}, which sets app.*"))
            for d in a["app_defaults"]:
                items.append(DriftItem("hardening", f"role {r} has a default for {d.split('=')[0]} (ALTER ROLE ... SET)"))
        if r == INTERNAL_ROLE:
            if a["login"]:
                items.append(DriftItem("hardening", f"role {r} can log in"))
            for mbr in a["members"]:
                if mbr in spec.roles:
                    items.append(DriftItem("hardening", f"app role {mbr} is a member of {r}"))
    return items, notes


def drift(conn: psycopg.Connection, spec: Spec, script: Script, schema: str = "public") -> DriftResult:
    with conn.transaction(force_rollback=True):  # nests as a savepoint inside a caller's transaction
        expected = shadow_snapshot(conn, spec, script)
        actual = snapshot(conn, schema, list(spec.governed()), _roles(spec), _observed(spec))
    items, notes = compare(spec, expected, actual)
    notes += [f"table '{t}' in schema '{schema}' is not in the spec" for t in actual["other_tables"]]
    return DriftResult(items, notes)
