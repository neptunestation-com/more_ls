"""The internal role (D34): `mls_internal` owns the SECURITY DEFINER helper functions the
hierarchy and cardinality templates need (the reachable-group set, the depth trigger, the
counting trigger).  Those helpers read tables under FORCE RLS, so for every table a helper reads
the compiler emits a permissive policy `mls_internal_<table>` (`using (true)`) and the matching
grant for `mls_internal` only.  The role has NOLOGIN, no BYPASSRLS, and no members other than the
deployer (needed to transfer function ownership); drift verifies that no app role is a member.
"""
from __future__ import annotations

from mls.compile.hardening import APP_ROLE_ATTRIBUTES, role_attributes_ddl
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
        role_attributes_ddl(INTERNAL_ROLE, {"rolcanlogin": "nologin", **APP_ROLE_ATTRIBUTES, "rolinherit": "noinherit"}),
        # the deployer must be a member to transfer ownership of the helper functions; by name,
        # because `grant ... to current_user` crashes the Supabase image's server (D55, FINDINGS)
        f"do $mls$ begin execute format('grant %I to %I', '{INTERNAL_ROLE}', current_user); end $mls$",
    ]


def function_owner_ddl(sig: str, owner: str = INTERNAL_ROLE) -> str:
    """Transfer a helper function to its owner.  ALTER ... OWNER requires the new owner to have
    CREATE on the function's schema, which a superuser deployer never notices and which no other
    deployer has to give on PG 15+ (public's CREATE belongs to its owner only).  So CREATE is
    granted for the transfer and revoked after it, unless the owner already had it (D55)."""
    return (f"do $mls$ declare g boolean := not has_schema_privilege({_lit(owner)}, current_schema(), 'CREATE'); begin\n"
            f"  if g then execute format('grant create on schema %I to %I', current_schema(), {_lit(owner)}); end if;\n"
            f"  alter function {sig} owner to {q(owner)};\n"
            f"  if g then execute format('revoke create on schema %I from %I', current_schema(), {_lit(owner)}); end if;\n"
            f"end $mls$")


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


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
