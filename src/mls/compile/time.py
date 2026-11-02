"""now_between(from, until): from <= now < until, NULL bound = open.  Two-valued."""
from __future__ import annotations

from mls.identity.base import Adapter
from mls.spec.model import CONTEXT_NOW


def now_between_sql(from_sql: str | None, until_sql: str | None, adapter: Adapter,
                    closed_null: bool = False) -> str:
    now = adapter.ctx_sql(CONTEXT_NOW)
    parts = []
    if from_sql is not None:
        parts.append(f"{from_sql} <= {now}" if closed_null else f"({from_sql} is null or {from_sql} <= {now})")
    if until_sql is not None:
        parts.append(f"{now} < {until_sql}" if closed_null else f"({until_sql} is null or {now} < {until_sql})")
    return "(" + " and ".join(parts) + ")"
