"""Inclusion template: composite FK, MATCH SIMPLE, ON DELETE RESTRICT (and ON UPDATE RESTRICT)."""
from __future__ import annotations

from mls.compile.mutations import Mutation
from mls.compile.sql import SqlCompiler, q
from mls.spec.model import InclusionRule


def fk_def(r: InclusionRule, sc: SqlCompiler) -> str:
    """The constraint definition (what follows `add constraint <name>`)."""
    rel = sc.spec.relations[r.relation]
    match = "match full" if Mutation.FK_MATCH_FULL in sc.mutations else "match simple"
    return (f"foreign key ({q(r.columns[0])}, {q(r.columns[1])}) references {q(rel.table)} "
            f"({q(rel.key)}, {q(rel.value)}) {match} on update restrict on delete restrict")


def fk_ddl(r: InclusionRule, sc: SqlCompiler) -> str:
    return f"alter table {q(r.table)} add constraint {q(r.name)} {fk_def(r, sc)}"
