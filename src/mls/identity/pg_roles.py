"""pg_roles adapter: identity is the Postgres session role (current_user)."""
from __future__ import annotations

from typing import Any

from mls.identity.base import Adapter
from mls.spec.model import ColType, Spec


class PgRolesAdapter(Adapter):
    name = "pg_roles"
    assumption = ("Identity is the Postgres role of the session (current_user); no external "
                  "component is trusted.")

    def _ctx_sql(self, name: str, source: str, type_: ColType) -> str:
        return "(select current_user::text)"

    def user_context_name(self) -> str | None:
        for n, cv in self.spec.identity.context.items():
            if cv.source == "current_user":
                return n
        return None

    def harness_setup_sql(self, spec: Spec) -> list[str]:
        # user roles u1..u16 are members of every app role (the harness picks which one to SET ROLE to)
        out = []
        for u in self.harness_user_values(16):
            out.append(f"do $$ begin if not exists (select 1 from pg_roles where rolname = '{u}') "
                       f"then create role {u} nologin; end if; end $$")
            for r in spec.roles:
                out.append(f"grant {r} to {u}")
        return out

    def harness_context_sql(self, role: str, ctx: dict[str, Any]) -> list[str]:
        user = self.user_context_name()
        if user is None:
            return [f"set local role {role}"]
        return [f"set local role {ctx[user]}"]

    def harness_user_values(self, n: int) -> list[Any]:
        return [f"u{i + 1}" for i in range(n)]
