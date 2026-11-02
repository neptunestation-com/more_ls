"""The MLS spec model (pydantic v2).  The JSON Schema is exported from these models (D15).

JSON shape (D-open-detail "exact JSON shape": explicit, no silent defaults):

  {"mls_version": "0.1",
   "identity": {"adapter": "supabase", "context": {"uid": {"source": "auth.uid", "type": "uuid"}}},
   "roles": ["authenticated"],
   "tables": {name: {"columns": {col: {"type": T, "nullable": bool}}, "primary_key": [..], "unique": [[..]]}},
   "relations": {name: {"table": t, "key": col, "value": col}},
   "opaques": {name: {"table": t, "columns": [..], "context": [..], "sql": "..."}},
   "rules": [ {"id", "kind", "name", "table", ...} ],
   "properties": [ {"id", "template", ...} ]}

Predicates are single-key objects:  {"and": [...]}, {"or": [...]}, {"not": p}, {"lit": true},
  {"cmp": [term, op, term]}, {"member": {"relation", "key": term, "value": term}},
  {"now_between": {"from": term|null, "until": term|null}}, {"is_null": term}, {"opaque": name}.
Terms are single-key objects: {"col": c}, {"ctx": name}, {"const": v}, {"old": c}, {"new": c}.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, model_validator

ColType = Literal["int", "text", "bool", "timestamp", "uuid"]
Command = Literal["select", "insert", "update", "delete", "all"]
CmpOp = Literal["=", "<>", "<", "<=", ">", ">="]

CONTEXT_NOW = "now"  # built-in context value: transaction start time (SEMANTICS §2)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------------------- terms
class ColRef(Strict):
    col: str


class CtxRef(Strict):
    ctx: str


class Const(Strict):
    const: Union[bool, int, str]  # bool must precede int so True is not coerced to 1


class OldRef(Strict):
    old: str


class NewRef(Strict):
    new: str


def _single_key(v: Any) -> str:
    if isinstance(v, dict):
        if len(v) != 1:
            return "?"
        return next(iter(v))
    for tag in ("col", "ctx", "const", "old", "new", "cmp", "member", "now_between",
                "is_null", "opaque", "and", "or", "not", "lit"):
        if hasattr(v, tag) or hasattr(v, tag + "_"):
            return tag
    return "?"


Term = Annotated[
    Union[
        Annotated[ColRef, Tag("col")],
        Annotated[CtxRef, Tag("ctx")],
        Annotated[Const, Tag("const")],
        Annotated[OldRef, Tag("old")],
        Annotated[NewRef, Tag("new")],
    ],
    Discriminator(_single_key, custom_error_type="invalid_term",
                  custom_error_message="a term is one of {col}, {ctx}, {const}, {old}, {new}"),
]


# ----------------------------------------------------------------------------- predicates
class Cmp(Strict):
    cmp: tuple[Term, CmpOp, Term]


class MemberBody(Strict):
    relation: str
    key: Term
    value: Term


class Member(Strict):
    member: MemberBody


class NowBetweenBody(Strict):
    from_: Term | None = Field(alias="from")
    until: Term | None


class NowBetween(Strict):
    now_between: NowBetweenBody


class IsNull(Strict):
    is_null: Term


class OpaqueRef(Strict):
    opaque: str


class And(Strict):
    and_: list["Pred"] = Field(alias="and", min_length=1)


class Or(Strict):
    or_: list["Pred"] = Field(alias="or", min_length=1)


class Not(Strict):
    not_: "Pred" = Field(alias="not")


class Lit(Strict):
    lit: bool


Pred = Annotated[
    Union[
        Annotated[Cmp, Tag("cmp")],
        Annotated[Member, Tag("member")],
        Annotated[NowBetween, Tag("now_between")],
        Annotated[IsNull, Tag("is_null")],
        Annotated[OpaqueRef, Tag("opaque")],
        Annotated[And, Tag("and")],
        Annotated[Or, Tag("or")],
        Annotated[Not, Tag("not")],
        Annotated[Lit, Tag("lit")],
    ],
    Discriminator(_single_key, custom_error_type="invalid_predicate",
                  custom_error_message="a predicate is one of {cmp}, {member}, {now_between}, "
                                       "{is_null}, {opaque}, {and}, {or}, {not}, {lit}"),
]

And.model_rebuild()
Or.model_rebuild()
Not.model_rebuild()


# ----------------------------------------------------------------------------- schema
class Column(Strict):
    type: ColType
    nullable: bool


class Table(Strict):
    columns: dict[str, Column]
    primary_key: list[str] | None = None
    unique: list[list[str]] = Field(default_factory=list)


class Relation(Strict):
    """A membership relation: the (key, value) projection of a table."""
    table: str
    key: str
    value: str


class ContextVar(Strict):
    """A context value.  `source` grammar depends on the adapter:
    supabase: 'auth.uid' | 'jwt.claims.<name>';  pg_roles: 'current_user'."""
    source: str
    type: ColType


class Identity(Strict):
    adapter: Literal["supabase", "pg_roles"]
    context: dict[str, ContextVar] = Field(default_factory=dict)


class Opaque(Strict):
    """Raw SQL with a declared signature (D3).  Modeled as an uninterpreted predicate over
    the declared columns (of `table`) and context values."""
    table: str
    columns: list[str] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)  # relations the SQL reads (state-dependent)
    sql: str


# ----------------------------------------------------------------------------- rules
class RuleBase(Strict):
    id: str
    name: str
    table: str
    requirement: str | None = None


class PolicyRule(RuleBase):
    kind: Literal["policy"]
    role: str
    command: Command
    mode: Literal["permissive", "restrictive"]
    using: Pred | None = None
    check: Pred | None = None


class CheckRule(RuleBase):
    kind: Literal["check"]
    predicate: Pred


class InclusionRule(RuleBase):
    kind: Literal["inclusion"]
    columns: list[str] = Field(min_length=2, max_length=2)
    relation: str
    on_delete: Literal["restrict"]


class TransitionRule(RuleBase):
    kind: Literal["transition"]
    predicate: Pred


class ColumnRule(RuleBase):
    kind: Literal["column_rule"]
    name: str | None = None
    role: str
    columns: list[str] = Field(min_length=1)
    access: Literal["read", "write"]


Rule = Annotated[
    Union[PolicyRule, CheckRule, InclusionRule, TransitionRule, ColumnRule],
    Field(discriminator="kind"),
]


# ----------------------------------------------------------------------------- properties
class PropBase(Strict):
    id: str
    description: str | None = None


class TenantIsolation(PropBase):
    """Every row a role can SELECT / INSERT / UPDATE (new row) / DELETE on `table` has
    `column` in relation(key) for the user `key` (a context value)."""
    template: Literal["tenant_isolation"]
    table: str
    column: str
    relation: str
    key: Term
    commands: list[Literal["select", "insert", "update", "delete"]]


class ColumnImmutable(PropBase):
    """No UPDATE admitted by the rules changes `column` (NULL-aware equality)."""
    template: Literal["column_immutable"]
    table: str
    column: str


class MonotoneIn(PropBase):
    """Shrinking `relation` never grants access (every table, role, command)."""
    template: Literal["monotone_in"]
    relation: str


class WriteSubsetRead(PropBase):
    """Every row a role can INSERT or UPDATE (new image) on `table` it can also SELECT."""
    template: Literal["write_subset_read"]
    table: str


class LeastPrivilege(PropBase):
    """No role gets a privilege on a table beyond what is declared here (propositional)."""
    template: Literal["least_privilege"]
    allowed: dict[str, dict[str, list[Literal["select", "insert", "update", "delete"]]]]


class InvariantRef(Strict):
    rule: str | None = None
    table: str | None = None
    predicate: Pred | None = None

    @model_validator(mode="after")
    def _one_form(self) -> "InvariantRef":
        if self.rule is not None and (self.table is not None or self.predicate is not None):
            raise ValueError("an invariant is either {rule: id} or {table, predicate}, not both")
        if self.rule is None and (self.table is None or self.predicate is None):
            raise ValueError("an invariant is either {rule: id} or {table, predicate}")
        return self


class InvariantPreserved(PropBase):
    """Inductive: every compiled transition on every table the invariants mention preserves them."""
    template: Literal["invariant_preserved"]
    invariants: list[InvariantRef] = Field(min_length=1)


class Custom(PropBase):
    """For `role` and `command` on `table`: effective(row) -> goal(row).
    `which` selects the row image for update: 'old' (USING) or 'new' (WITH CHECK)."""
    template: Literal["custom"]
    table: str
    role: str
    command: Literal["select", "insert", "update", "delete"]
    which: Literal["old", "new"] = "new"
    goal: Pred


Property = Annotated[
    Union[TenantIsolation, ColumnImmutable, MonotoneIn, WriteSubsetRead, LeastPrivilege,
          InvariantPreserved, Custom],
    Field(discriminator="template"),
]


# ----------------------------------------------------------------------------- spec
class Spec(Strict):
    comment: str | None = Field(default=None, alias="$comment")
    mls_version: Literal["0.1"]
    identity: Identity
    roles: list[str] = Field(min_length=1)
    tables: dict[str, Table]
    relations: dict[str, Relation] = Field(default_factory=dict)
    opaques: dict[str, Opaque] = Field(default_factory=dict)
    rules: list[Rule] = Field(default_factory=list)
    properties: list[Property] = Field(default_factory=list)

    # convenience accessors (no validation here; see spec/check.py)
    def rule(self, rule_id: str) -> Rule:
        for r in self.rules:
            if r.id == rule_id:
                return r
        raise KeyError(rule_id)

    def policies(self) -> list[PolicyRule]:
        return [r for r in self.rules if isinstance(r, PolicyRule)]

    def checks(self) -> list[CheckRule]:
        return [r for r in self.rules if isinstance(r, CheckRule)]

    def inclusions(self) -> list[InclusionRule]:
        return [r for r in self.rules if isinstance(r, InclusionRule)]

    def transitions(self) -> list[TransitionRule]:
        return [r for r in self.rules if isinstance(r, TransitionRule)]

    def column_rules(self) -> list[ColumnRule]:
        return [r for r in self.rules if isinstance(r, ColumnRule)]

    def context_type(self, name: str) -> ColType | None:
        if name == CONTEXT_NOW:
            return "timestamp"
        cv = self.identity.context.get(name)
        return cv.type if cv else None


def load_spec(path: str | Path) -> Spec:
    """Parse and validate a spec file (schema level only; run check_spec for types/fragment)."""
    from mls.spec.check import pydantic_errors_to_mls
    from mls.errors import SpecError
    from pydantic import ValidationError

    data = json.loads(Path(path).read_text())
    try:
        return Spec.model_validate(data)
    except ValidationError as e:
        raise SpecError(pydantic_errors_to_mls(e)) from None


def spec_json_schema() -> dict:
    return Spec.model_json_schema()
