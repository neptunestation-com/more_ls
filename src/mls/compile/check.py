"""CHECK template: CHECK ((p) IS TRUE)  (SEMANTICS §4: CHECK passes on UNKNOWN, RLS denies)."""
from __future__ import annotations

from mls.compile.mutations import Mutation
from mls.compile.sql import SqlCompiler, q
from mls.spec.model import CheckRule


def check_ddl(r: CheckRule, sc: SqlCompiler) -> str:
    body = sc.pred(r.predicate, r.table)
    expr = body if Mutation.CHECK_NAIVE in sc.mutations else f"{body} is true"
    return f"alter table {q(r.table)} add constraint {q(r.name)} check ({expr})"
