"""Supabase adapter: identity from auth.uid() and auth.jwt() claims, always as InitPlans."""
from __future__ import annotations

import json
import uuid
from typing import Any

from mls.identity.base import SQL_TYPE, Adapter
from mls.spec.model import ColType, Spec


class SupabaseAdapter(Adapter):
    name = "supabase"
    assumption = ("Clients reach the database only through Supabase's API layer, which verifies "
                  "the JWT before setting request.jwt.claims; direct database connections by "
                  "untrusted clients are out of scope.")

    def _ctx_sql(self, name: str, source: str, type_: ColType) -> str:
        if source == "auth.uid":
            return "(select auth.uid())"
        claim = source[len("jwt.claims."):]
        return f"(select (auth.jwt() ->> '{claim}')::{SQL_TYPE[type_]})"

    def user_context_name(self) -> str | None:
        for n, cv in self.spec.identity.context.items():
            if cv.source == "auth.uid":
                return n
        return None

    # Supabase's own definitions (supabase/postgres, migrations 20210809183423 / 20211124212715)
    def harness_setup_sql(self, spec: Spec) -> list[str]:
        return [
            "create schema if not exists auth",
            """create or replace function auth.jwt() returns jsonb language sql stable as $$
                 select coalesce(
                   nullif(current_setting('request.jwt.claim', true), ''),
                   nullif(current_setting('request.jwt.claims', true), '')
                 )::jsonb $$""",
            """create or replace function auth.uid() returns uuid language sql stable as $$
                 select coalesce(
                   nullif(current_setting('request.jwt.claim.sub', true), ''),
                   (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
                 )::uuid $$""",
            *[f"grant usage on schema auth to {r}; grant execute on function auth.jwt(), auth.uid() to {r}"
              for r in spec.roles],
        ]

    def harness_context_sql(self, role: str, ctx: dict[str, Any]) -> list[str]:
        claims: dict[str, Any] = {}
        for n, cv in self.spec.identity.context.items():
            if n not in ctx:
                continue
            if cv.source == "auth.uid":
                claims["sub"] = str(ctx[n])
            else:
                claims[cv.source[len("jwt.claims."):]] = ctx[n]
        j = json.dumps(claims).replace("'", "''")
        return [f"select set_config('request.jwt.claims', '{j}', true)", f"set local role {role}"]

    def harness_user_values(self, n: int) -> list[Any]:
        return [str(uuid.UUID(int=i + 1)) for i in range(n)]
