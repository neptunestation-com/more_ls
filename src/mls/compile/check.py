"""CHECK template: CHECK ((p) IS TRUE)  (SEMANTICS §4: CHECK passes on UNKNOWN, RLS denies)."""
from __future__ import annotations

from mls.compile.mutations import Mutation
from mls.compile.sql import SqlCompiler, q
from mls.spec.model import CheckRule


def check_def(r: CheckRule, sc: SqlCompiler) -> str:
    """The constraint definition (what follows `add constraint <name>`)."""
    body = sc.pred(r.predicate, r.table)
    expr = body if Mutation.CHECK_NAIVE in sc.mutations else f"{body} is true"
    return f"check ({expr})"


def check_ddl(r: CheckRule, sc: SqlCompiler) -> str:
    return f"alter table {q(r.table)} add constraint {q(r.name)} {check_def(r, sc)}"
