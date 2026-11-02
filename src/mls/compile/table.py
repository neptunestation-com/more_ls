"""CREATE TABLE with primary key and unique constraints (greenfield, D8)."""
from __future__ import annotations

from mls.compile.sql import column_ddl, q
from mls.spec.model import Spec, Table


def table_ddl(name: str, t: Table) -> list[str]:
    cols = [column_ddl(c, col) for c, col in t.columns.items()]
    if t.primary_key:
        cols.append(f"primary key ({', '.join(q(c) for c in t.primary_key)})")
    for u in t.unique:
        cols.append(f"unique ({', '.join(q(c) for c in u)})")
    return [f"create table {q(name)} (\n  " + ",\n  ".join(cols) + "\n)"]
