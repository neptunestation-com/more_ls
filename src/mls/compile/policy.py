"""Policy template: CREATE POLICY ... AS mode FOR command TO role USING (..) WITH CHECK (..).
WITH CHECK is always emitted explicitly for UPDATE/ALL (equal to USING when the spec omits it).

Policy-set replacement (D51): an adopted table may already carry policies the spec does not
declare, and a permissive one would keep granting what the proofs deny.  Before the spec's
policies are created, every policy on the table is dropped, by name read from pg_policy at run
time; a notice names each one the spec does not declare."""
from __future__ import annotations

from mls.compile.mutations import Mutation
from mls.compile.sql import SqlCompiler, q
from mls.props.effective import policy_expr
from mls.spec.model import PolicyRule


def policy_ddl(p: PolicyRule, sc: SqlCompiler) -> str | None:
    mode = p.mode
    if Mutation.POLICY_ALL_RESTRICTIVE in sc.mutations:
        mode = "restrictive"
    if Mutation.POLICY_DROP_RESTRICTIVE in sc.mutations and p.mode == "restrictive":
        return None
    s = f"create policy {q(p.name)} on {q(p.table)} as {mode} for {p.command} to {q(p.role)}"
    if p.command in ("select", "delete", "update", "all"):
        s += f"\n  using ({sc.pred(p.using, p.table)})"
    if p.command in ("insert", "update", "all"):
        s += f"\n  with check ({sc.pred(policy_expr(p, 'check'), p.table)})"
    return s


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def replace_policies_ddl(table: str, declared: list[str], sc: SqlCompiler) -> str:
    names = ", ".join(_lit(n) for n in sorted(declared))
    only = f" and polname = any (array[{names}]::name[])" if Mutation.REPLACE_KEEPS_FOREIGN in sc.mutations else ""
    return (f"do $mls$ declare p record; begin\n"
            f"  for p in select polname from pg_policy where polrelid = {_lit(q(table))}::regclass{only} order by polname loop\n"
            f"    if p.polname <> all (array[{names}]::name[]) then\n"
            f"      raise notice 'MLS drops policy % on {table}: the spec does not declare it', p.polname;\n"
            f"    end if;\n"
            f"    execute format('drop policy %I on %I', p.polname, {_lit(table)});\n"
            f"  end loop;\n"
            f"end $mls$")
