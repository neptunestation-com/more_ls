"""The internal role (D34): `mls_internal` owns the SECURITY DEFINER helper functions the
hierarchy and cardinality templates need (the reachable-group set, the depth trigger, the
counting trigger).  Those helpers read tables under FORCE RLS, so for every table a helper reads
the compiler emits a permissive policy `mls_internal_<table>` (`using (true)`) and the matching
grant for `mls_internal` only.  The role has NOLOGIN, no BYPASSRLS, and no members other than the
deployer (needed to transfer function ownership); drift verifies that no app role is a member.
"""
from __future__ import annotations

from mls.compile.sql import q
from mls.spec.model import PolicyRule, Lit, Spec

INTERNAL_ROLE = "mls_internal"


def internal_reads(spec: Spec) -> dict[str, set[str]]:
    """table -> privileges the helpers need on it (select; update for the parent-row lock)."""
    out: dict[str, set[str]] = {}
    for h in spec.hierarchies.values():
        out.setdefault(h.table, set()).add("select")
        out.setdefault(spec.relations[h.members].table, set()).add("select")
    for c in spec.cardinalities():
        out.setdefault(c.table, set()).add("select")
        out.setdefault(c.parent, set()).update({"select", "update"})
    return out


def needs_internal_role(spec: Spec) -> bool:
    return bool(spec.hierarchies or spec.cardinalities())


def internal_role_ddl(spec: Spec) -> list[str]:
    if not needs_internal_role(spec):
        return []
    return [
        f"do $mls$ begin\n"
        f"  if not exists (select 1 from pg_roles where rolname = '{INTERNAL_ROLE}') then\n"
        f"    create role {q(INTERNAL_ROLE)} nologin;\n"
        f"  end if;\n"
        f"end $mls$",
        f"alter role {q(INTERNAL_ROLE)} nologin nobypassrls nosuperuser nocreatedb nocreaterole noinherit",
        # the deployer must be a member to transfer ownership of the helper functions
        f"grant {q(INTERNAL_ROLE)} to current_user",
    ]


def internal_policies(spec: Spec) -> list[PolicyRule]:
    out = []
    for table, privs in sorted(internal_reads(spec).items()):
        cmd = "all" if "update" in privs else "select"
        out.append(PolicyRule(id=f"implicit:internal:{table}", kind="policy", name=f"mls_internal_{table}",
                              table=table, role=INTERNAL_ROLE, command=cmd, mode="permissive",
                              using=Lit(lit=True), check=Lit(lit=True) if cmd == "all" else None))
    return out


def internal_grants(spec: Spec) -> list[tuple[str, str]]:
    """(command, table) grants for mls_internal."""
    return sorted((cmd, table) for table, privs in internal_reads(spec).items() for cmd in privs)


def function_grant_ddl(fn_sig: str, roles: list[str]) -> list[str]:
    out = [f"revoke all on function {fn_sig} from public"]
    out += [f"grant execute on function {fn_sig} to {q(r)}" for r in roles]
    return out
