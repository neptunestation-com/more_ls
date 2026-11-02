"""Opaque blocks (D3): raw SQL with a declared signature, wrapped and named.
Context values are referenced in the SQL as {ctx:name}; columns by their bare names."""
from __future__ import annotations

import re

from mls.identity.base import Adapter
from mls.spec.model import Opaque

_CTX = re.compile(r"\{ctx:([a-z_][a-z0-9_]*)\}")


def opaque_sql(o: Opaque, adapter: Adapter, invert: bool = False) -> str:
    body = _CTX.sub(lambda m: adapter.ctx_sql(m.group(1)), o.sql)
    body = f"({body})"
    return f"(not {body})" if invert else body
