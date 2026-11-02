"""Inclusion template: composite FK, MATCH SIMPLE, ON DELETE RESTRICT (and ON UPDATE RESTRICT)."""
from __future__ import annotations

from mls.compile.mutations import Mutation
from mls.compile.sql import SqlCompiler, q
from mls.spec.model import InclusionRule


def fk_ddl(r: InclusionRule, sc: SqlCompiler) -> str:
    rel = sc.spec.relations[r.relation]
    match = "match full" if Mutation.FK_MATCH_FULL in sc.mutations else "match simple"
    return (f"alter table {q(r.table)} add constraint {q(r.name)} foreign key "
            f"({q(r.columns[0])}, {q(r.columns[1])}) references {q(rel.table)} "
            f"({q(rel.key)}, {q(rel.value)}) {match} on update restrict on delete restrict")
