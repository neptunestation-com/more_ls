"""Policy template: CREATE POLICY ... AS mode FOR command TO role USING (..) WITH CHECK (..).
WITH CHECK is always emitted explicitly for UPDATE/ALL (equal to USING when the spec omits it)."""
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
