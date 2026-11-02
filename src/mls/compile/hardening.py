"""Bypass-path hardening (D13): app roles are not owners and have no BYPASSRLS; RLS is enabled and
FORCED on every table; TRUNCATE is never granted; app roles cannot SET session_replication_role."""
from __future__ import annotations

from mls.compile.mutations import Mutation
from mls.compile.sql import q
from mls.spec.model import Spec


def preamble_ddl(spec: Spec) -> list[str]:
    roles = ", ".join(f"'{r}'" for r in [*spec.roles, "mls_internal"])
    return [
        f"do $mls$ begin\n"
        f"  if current_user = any (array[{roles}]) then\n"
        f"    raise exception 'MLS DDL must run as the owner role, not an app role';\n"
        f"  end if;\n"
        f"end $mls$",
    ]


def role_ddl(spec: Spec) -> list[str]:
    out = []
    for r in spec.roles:
        out.append(f"do $mls$ begin\n"
                   f"  if not exists (select 1 from pg_roles where rolname = '{r}') then\n"
                   f"    create role {q(r)} nologin;\n"
                   f"  end if;\n"
                   f"end $mls$")
        out.append(f"alter role {q(r)} nobypassrls nosuperuser nocreatedb nocreaterole")
        out.append(f"revoke set on parameter session_replication_role from {q(r)}")
    return out


def table_hardening_ddl(spec: Spec, mutations: frozenset[Mutation] = frozenset()) -> list[str]:
    out = []
    for t in spec.tables:
        out.append(f"revoke all on {q(t)} from public")
        for r in spec.roles:
            out.append(f"revoke all on {q(t)} from {q(r)}")
        out.append(f"alter table {q(t)} enable row level security")
        if Mutation.HARDENING_NO_FORCE not in mutations:
            out.append(f"alter table {q(t)} force row level security")
    return out
