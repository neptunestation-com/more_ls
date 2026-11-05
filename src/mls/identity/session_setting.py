"""session_setting adapter (D47, amends D1): context values are `current_setting('app.<name>')`,
typed by the spec, read once per statement as an InitPlan.  A missing or empty setting is NULL,
so every comparison with it is UNKNOWN and the row is denied: no context set, denied.

D1 rejected this as identity because any client with a direct connection can SET it; the
rejection stands for a direct connection, and the adapter states the trust assumption below in
every proof report, `explain` and diff.  The D13 hardening for it is a drift check (pg/drift.py):
no app role may execute a function that sets the namespace, and no app role has an
`ALTER ROLE ... SET app.*` default."""
from __future__ import annotations

import re
import uuid
from typing import Any

from mls.identity.base import SQL_TYPE, Adapter
from mls.spec.model import ColType, Spec

NAMESPACE = "app"
SOURCE = re.compile(r"^app\.[a-z_][a-z0-9_]*$")

ASSUMPTION = ("Clients cannot execute SET on app.*; the setting is established by a trusted path "
              "(a pooler, a SET LOCAL inside a SECURITY DEFINER entry point, or the application's "
              "connection layer) that no untrusted client reaches.")


class SessionSettingAdapter(Adapter):
    name = "session_setting"
    assumption = ASSUMPTION

    def _ctx_sql(self, name: str, source: str, type_: ColType) -> str:
        # nullif: a setting that was set and then reset reads as '' rather than NULL
        return f"(select nullif(current_setting('{source}', true), '')::{SQL_TYPE[type_]})"

    def user_context_name(self) -> str | None:
        """No source is distinguished as the user: the user context is the one the policies use
        as a membership (or in_group) key, when there is exactly one."""
        from mls.props.effective import in_groups_in, members_in
        from mls.spec.model import CtxRef

        keys: set[str] = set()
        for pol in self.spec.policies():
            for e in (pol.using, pol.check):
                for m in members_in(e, []):
                    if isinstance(m.key, CtxRef):
                        keys.add(m.key.ctx)
                for g in in_groups_in(e, []):
                    if isinstance(g.key, CtxRef):
                        keys.add(g.key.ctx)
        keys &= set(self.spec.identity.context)
        return next(iter(keys)) if len(keys) == 1 else None

    # --- harness: plain Postgres is the real environment; the trusted path is `set_config(..., true)`
    # inside the transaction, as the pg_roles harness sets the role
    def harness_setup_sql(self, spec: Spec) -> list[str]:
        return []

    def harness_context_sql(self, role: str, ctx: dict[str, Any]) -> list[str]:
        out = []
        for n, cv in self.spec.identity.context.items():
            if ctx.get(n) is None:
                continue  # no setting: the policies see NULL
            v = str(ctx[n]).replace("'", "''")
            if cv.type == "bool":
                v = "true" if ctx[n] else "false"
            out.append(f"select set_config('{cv.source}', '{v}', true)")
        out.append(f"set local role {role}")
        return out

    def harness_user_values(self, n: int) -> list[Any]:
        user = self.user_context_name()
        type_ = self.spec.identity.context[user].type if user else "uuid"
        if type_ == "uuid":
            return [str(uuid.UUID(int=i + 1)) for i in range(n)]
        if type_ == "int":
            return list(range(1, n + 1))
        return [f"u{i + 1}" for i in range(n)]
