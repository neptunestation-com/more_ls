"""Hierarchy template (D5): a bounded tree with a compiler-maintained depth column.

Per hierarchy h over table T(id, parent, depth):
  * CHECK  mls_h_<h>_depth_max:   ((depth >= 0 and depth <= d) is true)
  * FK     mls_h_<h>_parent:      parent -> T(id), RESTRICT both ways
  * FK     mls_h_<h>_member:      members.value -> T(id), RESTRICT both ways
  * trigger mls_h_<h>_depth (BEFORE INSERT OR UPDATE, SECURITY DEFINER as mls_internal): sets
    depth := 0 for roots, parent.depth + 1 otherwise (the stored depth is never user-controlled)
  * the parent column is immutable: an implicit transition rule (props/effective.py), compiled by
    the transition template
  * function mls_reach_<h>(k) returns id[]: the groups of k and everything below them within d
    levels, as a recursive CTE, SECURITY DEFINER as mls_internal, STABLE.  Policies compile the
    in_group atom to  value = any((select mls_reach_<h>(key)))  — an uncorrelated InitPlan, so
    the atom is index-driven on the value column (D14).
Cycles are impossible: a node's depth is fixed at insert from an existing parent and parents
never change, so depth strictly decreases along every parent chain.
"""
from __future__ import annotations

from mls.compile.internal import INTERNAL_ROLE, function_grant_ddl
from mls.compile.mutations import Mutation
from mls.compile.sql import SqlCompiler, q
from mls.identity.base import SQL_TYPE
from mls.spec.model import Hierarchy, Spec


def reach_fn_name(h: str) -> str:
    return f"mls_reach_{h}"


def depth_fn_name(h: str) -> str:
    return f"mls_h_{h}_depth"


def reach_fn_sig(spec: Spec, name: str) -> str:
    h = spec.hierarchies[name]
    rel = spec.relations[h.members]
    kt = SQL_TYPE[spec.tables[rel.table].columns[rel.key].type]
    return f"{q(reach_fn_name(name))}({kt})"


def depth_check_def(h: Hierarchy, mutations: frozenset[Mutation] = frozenset()) -> str:
    return f"check ((({q(h.depth)} >= 0) and ({q(h.depth)} <= {h.max_depth})) is true)"


def parent_fk_def(h: Hierarchy) -> str:
    return (f"foreign key ({q(h.parent)}) references {q(h.table)} ({q(h.id)}) "
            f"on update restrict on delete restrict")


def member_fk_def(spec: Spec, h: Hierarchy) -> str:
    rel = spec.relations[h.members]
    return (f"foreign key ({q(rel.value)}) references {q(h.table)} ({q(h.id)}) "
            f"on update restrict on delete restrict")


def reach_fn_ddl(spec: Spec, name: str, mutations: frozenset[Mutation] = frozenset(),
                 replace: bool = False) -> str:
    h = spec.hierarchies[name]
    rel = spec.relations[h.members]
    kt = SQL_TYPE[spec.tables[rel.table].columns[rel.key].type]
    it = SQL_TYPE[spec.tables[h.table].columns[h.id].type]
    depth = h.max_depth - 1 if Mutation.HIERARCHY_SHALLOW in mutations else h.max_depth
    verb = "create or replace function" if replace else "create function"
    return (
        f"{verb} {q(reach_fn_name(name))}(k {kt}) returns {it}[] language sql stable "
        f"security definer set search_path from current as $mls$\n"
        f"  with recursive r(id, lvl) as (\n"
        f"    select g.{q(h.id)}, 0 from {q(h.table)} g\n"
        f"      where g.{q(h.id)} = any (array(select {q(rel.value)} from {q(rel.table)} where {q(rel.key)} = k))\n"
        f"    union all\n"
        f"    select c.{q(h.id)}, r.lvl + 1 from {q(h.table)} c join r on c.{q(h.parent)} = r.id\n"
        f"      where r.lvl < {depth})\n"
        f"  select coalesce(array_agg(id), '{{}}') from r\n"
        f"$mls$")


def depth_fn_ddl(spec: Spec, name: str, replace: bool = False) -> str:
    h = spec.hierarchies[name]
    verb = "create or replace function" if replace else "create function"
    return (
        f"{verb} {q(depth_fn_name(name))}() returns trigger language plpgsql "
        f"security definer set search_path from current as $mls$\n"
        f"declare d integer;\n"
        f"begin\n"
        f"  if new.{q(h.parent)} is null then\n"
        f"    new.{q(h.depth)} := 0;\n"
        f"  else\n"
        f"    select {q(h.depth)} + 1 into d from {q(h.table)} where {q(h.id)} = new.{q(h.parent)};\n"
        f"    if d is null then\n"
        f"      raise exception 'MLS hierarchy {name}: parent % does not exist', new.{q(h.parent)} "
        f"using errcode = 'foreign_key_violation';\n"
        f"    end if;\n"
        f"    new.{q(h.depth)} := d;\n"
        f"  end if;\n"
        f"  return new;\n"
        f"end $mls$")


def depth_trigger_ddl(spec: Spec, name: str) -> str:
    h = spec.hierarchies[name]
    return (f"create trigger {q(depth_fn_name(name))} before insert or update on {q(h.table)} "
            f"for each row execute function {q(depth_fn_name(name))}()")


def function_owner_ddl(sig: str) -> str:
    return f"alter function {sig} owner to {q(INTERNAL_ROLE)}"


def hierarchy_ddl(spec: Spec, name: str, sc: SqlCompiler) -> list[str]:
    h = spec.hierarchies[name]
    rel = spec.relations[h.members]
    out = []
    if Mutation.HIERARCHY_NO_DEPTH_CHECK not in sc.mutations:
        out.append(f"alter table {q(h.table)} add constraint {q(f'mls_h_{name}_depth_max')} {depth_check_def(h)}")
    out.append(f"alter table {q(h.table)} add constraint {q(f'mls_h_{name}_parent')} {parent_fk_def(h)}")
    out.append(f"alter table {q(rel.table)} add constraint {q(f'mls_h_{name}_member')} {member_fk_def(spec, h)}")
    out.append(depth_fn_ddl(spec, name))
    out.append(function_owner_ddl(f"{q(depth_fn_name(name))}()"))
    out.append(depth_trigger_ddl(spec, name))
    sig = reach_fn_sig(spec, name)
    out.append(reach_fn_ddl(spec, name, sc.mutations))
    out.append(function_owner_ddl(sig))
    out += function_grant_ddl(sig, list(spec.roles))
    return out
