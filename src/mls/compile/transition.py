"""Transition template: one fixed BEFORE UPDATE row trigger, IF NOT ((p) IS TRUE) THEN RAISE."""
from __future__ import annotations

from mls.compile.mutations import Mutation
from mls.compile.sql import SqlCompiler, q
from mls.spec.model import TransitionRule


def transition_fn_name(r: TransitionRule) -> str:
    return f"mls_tr_{r.table}_{r.name}"


def transition_ddl(r: TransitionRule, sc: SqlCompiler) -> list[str]:
    body = sc.pred(r.predicate, r.table)
    test = body if Mutation.TRANSITION_NAIVE in sc.mutations else f"({body} is true)"
    fn = transition_fn_name(r)
    return [
        f"create function {q(fn)}() returns trigger language plpgsql as $mls$\n"
        f"begin\n"
        f"  if not {test} then\n"
        f"    raise exception 'MLS transition {r.name} on {r.table} violated' "
        f"using errcode = 'check_violation';\n"
        f"  end if;\n"
        f"  return new;\n"
        f"end $mls$",
        f"create trigger {q(r.name)} before update on {q(r.table)} for each row "
        f"execute function {q(fn)}()",
    ]
