"""CREATE TABLE with primary key and unique constraints (greenfield, D8)."""
from __future__ import annotations

from mls.compile.sql import column_ddl, q
from mls.spec.model import Spec, Table


def pk_name(table: str) -> str:
    return f"{table}_pkey"


def unique_name(table: str, cols: list[str]) -> str:
    return f"{table}_{'_'.join(cols)}_key"


def table_ddl(name: str, t: Table) -> list[str]:
    cols = [column_ddl(c, col) for c, col in t.columns.items()]
    if t.primary_key:
        cols.append(f"constraint {q(pk_name(name))} primary key ({', '.join(q(c) for c in t.primary_key)})")
    for u in t.unique:
        cols.append(f"constraint {q(unique_name(name, u))} unique ({', '.join(q(c) for c in u)})")
    return [f"create table {q(name)} (\n  " + ",\n  ".join(cols) + "\n)"]
