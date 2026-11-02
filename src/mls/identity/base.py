"""Identity adapters (D1, SEMANTICS §6): map context names to SQL expressions and state the
trust assumption that every proof report repeats."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from mls.spec.model import CONTEXT_NOW, ColType, Spec

SQL_TYPE = {"int": "integer", "text": "text", "bool": "boolean", "timestamp": "timestamptz", "uuid": "uuid"}


class Adapter(ABC):
    name: str
    assumption: str

    def __init__(self, spec: Spec):
        self.spec = spec

    def ctx_sql(self, name: str) -> str:
        """SQL expression for a context value, wrapped as an InitPlan (constant per statement)."""
        if name == CONTEXT_NOW:
            return "(select now())"
        cv = self.spec.identity.context[name]
        return self._ctx_sql(name, cv.source, cv.type)

    @abstractmethod
    def _ctx_sql(self, name: str, source: str, type_: ColType) -> str: ...

    # --- differential-test harness hooks (plain Postgres stands in for the real environment)
    @abstractmethod
    def harness_setup_sql(self, spec: Spec) -> list[str]:
        """Statements (run once as superuser) that make the adapter's SQL work on plain Postgres."""

    @abstractmethod
    def harness_context_sql(self, role: str, ctx: dict[str, Any]) -> list[str]:
        """Statements to run inside a transaction so the session carries `ctx` for `role`."""

    @abstractmethod
    def harness_user_values(self, n: int) -> list[Any]:
        """n distinct identity values for the user context (uuid strings or role names)."""

    @abstractmethod
    def user_context_name(self) -> str | None:
        """The context name that identifies the user (auth.uid / current_user), if declared."""


def get_adapter(spec: Spec) -> Adapter:
    from mls.identity.pg_roles import PgRolesAdapter
    from mls.identity.supabase import SupabaseAdapter

    return {"supabase": SupabaseAdapter, "pg_roles": PgRolesAdapter}[spec.identity.adapter](spec)
