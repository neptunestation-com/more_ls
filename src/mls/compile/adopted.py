"""Constraints on an adopted table (D52): the table may already hold a constraint under the name
of one of the spec's check or inclusion rules (`mls import` names its rules after the
constraints it read).  `add constraint` would fail on it, so the template compares instead: the
spec's definition is added NOT VALID under a probe name (no scan), Postgres deparses it, the
probe is dropped, and the existing constraint is kept when its deparse is the same (a NOT VALID
one is not), else dropped and added again, which validates the rows, with a notice naming it."""
from __future__ import annotations

from mls.compile.mutations import Mutation
from mls.compile.sql import q

PROBE = "mls_probe"


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def adopted_constraint_ddl(table: str, name: str, definition: str, mutations: frozenset[Mutation]) -> str:
    rel = f"{_lit(q(table))}::regclass"
    add = f"alter table {q(table)} add constraint {q(name)} {definition}"
    if Mutation.ADOPTED_KEEPS_CONSTRAINT in mutations:
        compare = ""
    else:
        compare = (f"  alter table {q(table)} add constraint {PROBE} {definition} not valid;\n"
                   f"  select pg_get_constraintdef(oid) into want from pg_constraint\n"
                   f"    where conrelid = {rel} and conname = '{PROBE}';\n"
                   f"  alter table {q(table)} drop constraint {PROBE};\n"
                   f"  want := regexp_replace(want, ' NOT VALID$', '');\n"
                   f"  if have <> want then\n"
                   f"    raise notice 'MLS replaces constraint % on {table}: % (the spec: %)', {_lit(name)}, have, want;\n"
                   f"    alter table {q(table)} drop constraint {q(name)};\n"
                   f"    {add};\n"
                   f"  end if;\n")
    return (f"do $mls$ declare have text; want text; begin\n"
            f"  select pg_get_constraintdef(oid) into have from pg_constraint\n"
            f"    where conrelid = {rel} and conname = {_lit(name)};\n"
            f"  if have is null then\n"
            f"    {add};\n"
            f"    return;\n"
            f"  end if;\n"
            f"{compare}"
            f"end $mls$")
