"""The compiled objects of a spec, by name: the unit of comparison for `migrate` and `drift`.

Everything here is produced by the same template functions `compile_spec` uses, so a migration
or a drift report talks about exactly the objects a fresh deploy would create.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from mls.compile.check import check_def
from mls.compile.column import privileges
from mls.compile.fk import fk_def
from mls.compile.index import index_specs
from mls.compile.mutations import Mutation
from mls.compile.policy import policy_ddl
from mls.compile.sql import SqlCompiler, q
from mls.compile.table import pk_name, unique_name
from mls.compile.transition import transition_fn_ddl, transition_fn_name, transition_trigger_ddl
from mls.identity import get_adapter
from mls.identity.base import SQL_TYPE
from mls.props.effective import all_policies
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


@dataclass(frozen=True)
class ConstraintObj:
    table: str
    name: str
    kind: str          # check | fk
    definition: str    # what follows `add constraint <name>`
    rule_id: str


@dataclass(frozen=True)
class TriggerObj:
    table: str
    name: str
    fn: str
    fn_ddl: str        # create function ...
    trigger_ddl: str
    rule_id: str

    def replace_fn_ddl(self) -> str:
        return "create or replace function" + self.fn_ddl[len("create function"):]


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
    triggers: dict[tuple[str, str], TriggerObj] = field(default_factory=dict)
    policies: dict[tuple[str, str], PolicyObj] = field(default_factory=dict)
    indexes: dict[tuple[str, str], IndexObj] = field(default_factory=dict)
    grants: set[GrantObj] = field(default_factory=set)


def objects(spec: Spec, mutations: frozenset[Mutation] = frozenset()) -> Objects:
    sc = SqlCompiler(spec, get_adapter(spec), mutations)
    tables = {}
    for name, t in spec.tables.items():
        tables[name] = TableObj(
            name, {c: ColumnObj(SQL_TYPE[col.type], not col.nullable) for c, col in t.columns.items()},
            tuple(t.primary_key) if t.primary_key else None,
            {unique_name(name, u): tuple(u) for u in t.unique})
    o = Objects(list(spec.roles), tables)
    for r in spec.checks():
        o.constraints[(r.table, r.name)] = ConstraintObj(r.table, r.name, "check", check_def(r, sc), r.id)
    for r in spec.inclusions():
        o.constraints[(r.table, r.name)] = ConstraintObj(r.table, r.name, "fk", fk_def(r, sc), r.id)
    for r in spec.transitions():
        o.triggers[(r.table, r.name)] = TriggerObj(r.table, r.name, transition_fn_name(r),
                                                   transition_fn_ddl(r, sc), transition_trigger_ddl(r), r.id)
    for p in all_policies(spec):
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
