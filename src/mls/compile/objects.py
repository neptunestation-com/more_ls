"""The compiled objects of a spec, by name: the unit of comparison for `migrate` and `drift`.

Everything here is produced by the same template functions `compile_spec` uses, so a migration
or a drift report talks about exactly the objects a fresh deploy would create.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from mls.compile.cardinality import card_fn_ddl, card_fn_name, card_trigger_ddl
from mls.compile.cardinality import parent_fk_def as card_parent_fk_def
from mls.compile.check import check_def
from mls.compile.column import privileges
from mls.compile.fk import fk_def
from mls.compile.hierarchy import (
    depth_check_def, depth_fn_ddl, depth_fn_name, depth_trigger_ddl, member_fk_def, parent_fk_def,
    reach_fn_ddl, reach_fn_name, reach_fn_sig,
)
from mls.compile.index import index_specs
from mls.compile.internal import INTERNAL_ROLE, function_grant_ddl, internal_grants, internal_policies
from mls.compile.mutations import Mutation
from mls.compile.policy import policy_ddl
from mls.compile.sql import SqlCompiler, q
from mls.compile.table import pk_name, unique_name
from mls.compile.transition import transition_fn_ddl, transition_fn_name, transition_trigger_ddl
from mls.identity import get_adapter
from mls.identity.base import SQL_TYPE
from mls.props.effective import all_policies, all_transitions
from mls.spec.model import Spec


@dataclass(frozen=True)
class ColumnObj:
    type: str       # SQL type
    notnull: bool


@dataclass
class TableObj:
    name: str
    columns: dict[str, ColumnObj]
    pk: tuple[str, ...] | None
    uniques: dict[str, tuple[str, ...]]   # constraint name -> columns
    mode: str = "owned"                   # owned | adopted (observed tables are not objects)


@dataclass(frozen=True)
class ConstraintObj:
    table: str
    name: str
    kind: str          # check | fk
    definition: str    # what follows `add constraint <name>`
    rule_id: str


@dataclass(frozen=True)
class FunctionObj:
    name: str
    signature: str     # "name"(argtypes)
    ddl: str           # create function ...
    owner: str | None  # mls_internal for the SECURITY DEFINER helpers
    execute: tuple[str, ...]  # roles granted EXECUTE (public revoked when non-empty)
    rule_id: str

    def replace_ddl(self) -> str:
        return "create or replace function" + self.ddl[len("create function"):]

    def create_ddl(self) -> list[str]:
        out = [self.ddl]
        if self.owner:
            out.append(f"alter function {self.signature} owner to {q(self.owner)}")
        if self.execute:
            out += function_grant_ddl(self.signature, list(self.execute))
        return out


@dataclass(frozen=True)
class TriggerObj:
    table: str
    name: str
    fn: str            # function name (a FunctionObj)
    trigger_ddl: str
    rule_id: str


@dataclass(frozen=True)
class PolicyObj:
    table: str
    name: str
    ddl: str
    rule_id: str


@dataclass(frozen=True)
class IndexObj:
    table: str
    name: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class GrantObj:
    role: str
    table: str
    command: str
    columns: tuple[str, ...] | None   # None = whole table

    def sql(self, verb: str = "grant") -> str:
        cols = "" if self.columns is None else f" ({', '.join(q(c) for c in self.columns)})"
        prep = "to" if verb == "grant" else "from"
        return f"{verb} {self.command}{cols} on {q(self.table)} {prep} {q(self.role)}"


@dataclass
class Objects:
    roles: list[str]
    tables: dict[str, TableObj]
    constraints: dict[tuple[str, str], ConstraintObj] = field(default_factory=dict)
    functions: dict[str, FunctionObj] = field(default_factory=dict)
    triggers: dict[tuple[str, str], TriggerObj] = field(default_factory=dict)
    internal_role: bool = False
    policies: dict[tuple[str, str], PolicyObj] = field(default_factory=dict)
    indexes: dict[tuple[str, str], IndexObj] = field(default_factory=dict)
    grants: set[GrantObj] = field(default_factory=set)


def objects(spec: Spec, mutations: frozenset[Mutation] = frozenset()) -> Objects:
    sc = SqlCompiler(spec, get_adapter(spec), mutations)
    tables = {}
    for name, t in spec.governed().items():
        tables[name] = TableObj(
            name, {c: ColumnObj(SQL_TYPE[col.type], not col.nullable) for c, col in t.columns.items()},
            tuple(t.primary_key) if t.primary_key else None,
            {unique_name(name, u): tuple(u) for u in t.unique}, t.mode)
    o = Objects(list(spec.roles), tables)
    for r in spec.checks():
        o.constraints[(r.table, r.name)] = ConstraintObj(r.table, r.name, "check", check_def(r, sc), r.id)
    for r in spec.inclusions():
        o.constraints[(r.table, r.name)] = ConstraintObj(r.table, r.name, "fk", fk_def(r, sc), r.id)
    for r in all_transitions(spec):
        fn = transition_fn_name(r)
        o.functions[fn] = FunctionObj(fn, f"{q(fn)}()", transition_fn_ddl(r, sc), None, (), r.id)
        o.triggers[(r.table, r.name)] = TriggerObj(r.table, r.name, fn, transition_trigger_ddl(r), r.id)
    for name, h in spec.hierarchies.items():
        rid = f"hierarchy:{name}"
        rel = spec.relations[h.members]
        if Mutation.HIERARCHY_NO_DEPTH_CHECK not in mutations:
            o.constraints[(h.table, f"mls_h_{name}_depth_max")] = ConstraintObj(
                h.table, f"mls_h_{name}_depth_max", "check", depth_check_def(h), rid)
        o.constraints[(h.table, f"mls_h_{name}_parent")] = ConstraintObj(
            h.table, f"mls_h_{name}_parent", "fk", parent_fk_def(h), rid)
        o.constraints[(rel.table, f"mls_h_{name}_member")] = ConstraintObj(
            rel.table, f"mls_h_{name}_member", "fk", member_fk_def(spec, h), rid)
        dfn, rfn = depth_fn_name(name), reach_fn_name(name)
        o.functions[dfn] = FunctionObj(dfn, f"{q(dfn)}()", depth_fn_ddl(spec, name), INTERNAL_ROLE, (), rid)
        o.triggers[(h.table, dfn)] = TriggerObj(h.table, dfn, dfn, depth_trigger_ddl(spec, name), rid)
        o.functions[rfn] = FunctionObj(rfn, reach_fn_sig(spec, name), reach_fn_ddl(spec, name, mutations),
                                       INTERNAL_ROLE, tuple(spec.roles), rid)
    for r in spec.cardinalities():
        o.constraints[(r.table, f"mls_card_{r.name}_parent")] = ConstraintObj(
            r.table, f"mls_card_{r.name}_parent", "fk", card_parent_fk_def(spec, r), r.id)
        fn = card_fn_name(r)
        o.functions[fn] = FunctionObj(fn, f"{q(fn)}()", card_fn_ddl(spec, r, mutations), INTERNAL_ROLE, (), r.id)
        o.triggers[(r.table, r.name)] = TriggerObj(r.table, r.name, fn, card_trigger_ddl(r), r.id)
    o.internal_role = bool(spec.hierarchies or spec.cardinalities())
    for cmd, table in internal_grants(spec):
        o.grants.add(GrantObj(INTERNAL_ROLE, table, cmd, None))
    for p in all_policies(spec) + internal_policies(spec):
        ddl = policy_ddl(p, sc)
        if ddl is not None:
            o.policies[(p.table, p.name)] = PolicyObj(p.table, p.name, ddl, p.id)
    if Mutation.INDEX_NONE not in mutations:
        for name, table, cols in index_specs(spec):
            o.indexes[(table, name)] = IndexObj(table, name, cols)
    for (role, table), g in privileges(spec).items():
        for cmd, cols in g.items():
            whole = cols == "*" or Mutation.COLUMN_TABLE_GRANT in mutations
            o.grants.add(GrantObj(role, table, cmd, None if whole else tuple(sorted(cols))))
    return o
