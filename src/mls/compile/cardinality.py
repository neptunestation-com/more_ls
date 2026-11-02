"""Cardinality template (D4): at most N rows of T per value of `group`.

  * FK  mls_card_<name>_parent: group -> parent(pk), RESTRICT both ways (the parent row must exist
    to be locked)
  * trigger <name> (BEFORE INSERT OR UPDATE OF group, SECURITY DEFINER as mls_internal):
      lock the parent row FOR UPDATE, count rows of T with the same group, raise check_violation
      if count + 1 > N.  NULL groups are not counted; an UPDATE that keeps the group is skipped.
The parent-row lock serializes writers per group; under READ COMMITTED the count after the lock
sees every committed row, so the bound holds (tests/templates/test_cardinality.py runs parallel
writers; the CARDINALITY_NO_LOCK mutation overshoots).  Under REPEATABLE READ the count would use
a stale snapshot: the proof report states the READ COMMITTED assumption.
"""
from __future__ import annotations

from mls.compile.internal import INTERNAL_ROLE
from mls.compile.mutations import Mutation
from mls.compile.sql import SqlCompiler, q
from mls.spec.model import CardinalityRule, Spec


def card_fn_name(r: CardinalityRule) -> str:
    return f"mls_card_{r.table}_{r.name}"


def parent_fk_def(spec: Spec, r: CardinalityRule) -> str:
    pk = spec.tables[r.parent].primary_key[0]
    return (f"foreign key ({q(r.group)}) references {q(r.parent)} ({q(pk)}) "
            f"on update restrict on delete restrict")


def card_fn_ddl(spec: Spec, r: CardinalityRule, mutations: frozenset[Mutation] = frozenset(),
                replace: bool = False) -> str:
    pk = spec.tables[r.parent].primary_key[0]
    verb = "create or replace function" if replace else "create function"
    lock = "" if Mutation.CARDINALITY_NO_LOCK in mutations else \
        f"  perform 1 from {q(r.parent)} where {q(pk)} = new.{q(r.group)} for update;\n"
    test = f"n > {r.max}" if Mutation.CARDINALITY_OFF_BY_ONE in mutations else f"n + 1 > {r.max}"
    return (
        f"{verb} {q(card_fn_name(r))}() returns trigger language plpgsql "
        f"security definer set search_path from current as $mls$\n"
        f"declare n bigint;\n"
        f"begin\n"
        f"  if new.{q(r.group)} is null then return new; end if;\n"
        f"  if tg_op = 'UPDATE' and new.{q(r.group)} is not distinct from old.{q(r.group)} then return new; end if;\n"
        f"{lock}"
        f"  select count(*) into n from {q(r.table)} where {q(r.group)} = new.{q(r.group)};\n"
        f"  if {test} then\n"
        f"    raise exception 'MLS cardinality {r.name} on {r.table}: more than {r.max} rows for "
        f"{r.group} = %', new.{q(r.group)} using errcode = 'check_violation';\n"
        f"  end if;\n"
        f"  return new;\n"
        f"end $mls$")


def card_trigger_ddl(r: CardinalityRule) -> str:
    return (f"create trigger {q(r.name)} before insert or update of {q(r.group)} on {q(r.table)} "
            f"for each row execute function {q(card_fn_name(r))}()")


def cardinality_ddl(spec: Spec, r: CardinalityRule, sc: SqlCompiler) -> list[str]:
    return [
        f"alter table {q(r.table)} add constraint {q(f'mls_card_{r.name}_parent')} {parent_fk_def(spec, r)}",
        card_fn_ddl(spec, r, sc.mutations),
        f"alter function {q(card_fn_name(r))}() owner to {q(INTERNAL_ROLE)}",
        card_trigger_ddl(r),
    ]
