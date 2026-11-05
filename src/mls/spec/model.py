"""The MLS spec model (pydantic v2).  The JSON Schema is exported from these models (D15).

JSON shape (D-open-detail "exact JSON shape": explicit, no silent defaults):

  {"mls_version": "0.1",
   "identity": {"adapter": "supabase", "context": {"uid": {"source": "auth.uid", "type": "uuid"}}},
   "roles": ["authenticated"],
   "tables": {name: {"columns": {col: {"type": T, "nullable": bool}}, "primary_key": [..], "unique": [[..]],
                     "mode": "owned" | "adopted" | "observed", "assume": [..], "tags": [..]}},
   "relations": {name: {"table": t, "key": col, "value": col}},
   "hierarchies": {name: {"table": t, "id": col, "parent": col, "depth": col, "max_depth": d, "members": rel}},
   "opaques": {name: {"table": t, "columns": [..], "context": [..], "sql": "..."}},
   "rules": [ {"id", "kind", "name", "table", ...} ],
   "properties": [ {"id", "template", ...} ]}

Predicates are single-key objects:  {"and": [...]}, {"or": [...]}, {"not": p}, {"lit": true},
  {"cmp": [term, op, term]}, {"in": [term, [const, ...]]} (sugar: expanded by the checker into
  an "or" of "=" comparisons, spec/expand.py), {"member": {"relation", "key": term, "value": term}},
  {"in_group": {"hierarchy", "key": term, "value": term}},
  {"now_between": {"from": term|null, "until": term|null}}, {"is_null": term}, {"opaque": name}.
Terms are single-key objects: {"col": c}, {"ctx": name}, {"const": v}, {"old": c}, {"new": c}.
"""
from __future__ import annotations

import json

from mls.errors import MlsError as MlsErrorRef
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
    for tag in ("col", "ctx", "const", "old", "new", "cmp", "in", "member", "in_group", "now_between",
                "is_null", "opaque", "and", "or", "not", "lit", "owner", "bypassrls", "rls"):
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


class In(Strict):
    """`term` equals one of the constants: sugar for `{"or": [{"cmp": [term, "=", c]}, ...]}`.
    The checker expands it before anything else sees the spec; the compiler renders the
    expanded shape as an IN list."""
    in_: tuple[Term, list[Const]] = Field(alias="in")


class MemberBody(Strict):
    relation: str
    key: Term
    value: Term


class Member(Strict):
    member: MemberBody


class NowBetweenBody(Strict):
    from_: Term | None = Field(default=None, alias="from")
    until: Term | None = None


class NowBetween(Strict):
    now_between: NowBetweenBody


class InGroupBody(Strict):
    hierarchy: str
    key: Term
    value: Term


class InGroup(Strict):
    """`value` is a group of hierarchy `hierarchy` that `key` is a member of, or lies below one
    (within the hierarchy's max_depth levels)."""
    in_group: InGroupBody


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
        Annotated[In, Tag("in")],
        Annotated[Member, Tag("member")],
        Annotated[InGroup, Tag("in_group")],
        Annotated[NowBetween, Tag("now_between")],
        Annotated[IsNull, Tag("is_null")],
        Annotated[OpaqueRef, Tag("opaque")],
        Annotated[And, Tag("and")],
        Annotated[Or, Tag("or")],
        Annotated[Not, Tag("not")],
        Annotated[Lit, Tag("lit")],
    ],
    Discriminator(_single_key, custom_error_type="invalid_predicate",
                  custom_error_message="a predicate is one of {cmp}, {in}, {member}, {in_group}, "
                                       "{now_between}, {is_null}, {opaque}, {and}, {or}, {not}, {lit}"),
]

And.model_rebuild()
Or.model_rebuild()
Not.model_rebuild()


# ----------------------------------------------------------------------------- schema
class Column(Strict):
    type: ColType
    nullable: bool


TableMode = Literal["owned", "adopted", "observed"]


class OwnerAssumed(Strict):
    """The table's owner is the app role named here and is trusted (D45; D13 hardening it replaces:
    app roles are not table owners)."""
    owner: str


class BypassAssumed(Strict):
    """The app role named here has BYPASSRLS, reads and writes the table unfiltered, and is trusted."""
    bypassrls: str


class RlsAssumed(Strict):
    """`not_forced`: row level security is not forced on the table; its owner bypasses every policy
    and is trusted.  (No other value: a table without row level security *enabled* is `observed`.)"""
    rls: str


Assumption = Annotated[
    Union[
        Annotated[OwnerAssumed, Tag("owner")],
        Annotated[BypassAssumed, Tag("bypassrls")],
        Annotated[RlsAssumed, Tag("rls")],
    ],
    Discriminator(_single_key, custom_error_type="invalid_assumption",
                  custom_error_message="an assumption is one of {owner}, {bypassrls}, {rls}"),
]


class Table(Strict):
    """`mode` (D45): `owned` — MLS creates the table and emits and verifies its hardening;
    `adopted` — the table exists, MLS adds rules to it, the hardening it cannot establish is
    stated under `assume`; `observed` — in the schema, mentioned, not governed: no rule may name
    it.  `tags` are free strings for selectors (D48)."""
    columns: dict[str, Column]
    primary_key: list[str] | None = None
    unique: list[list[str]] = Field(default_factory=list)
    mode: TableMode = "owned"
    assume: list[Assumption] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class Relation(Strict):
    """A membership relation: the (key, value) projection of a table."""
    table: str
    key: str
    value: str


MAX_HIERARCHY_DEPTH = 16  # the solver unrolls reachability this many times at most


class Hierarchy(Strict):
    """A tree over `table` (D5): `id` is the primary key, `parent` a nullable column of the same
    type referencing `id`, `depth` an int NOT NULL column the compiler maintains (root 0, child =
    parent + 1) and bounds by `max_depth`; `parent` is immutable.  `members` is the relation
    (user -> id) that places a user in a group."""
    table: str
    id: str
    parent: str
    depth: str
    max_depth: int = Field(ge=1, le=MAX_HIERARCHY_DEPTH)
    members: str


class ContextVar(Strict):
    """A context value.  `source` grammar depends on the adapter:
    supabase: 'auth.uid' | 'jwt.claims.<name>';  pg_roles: 'current_user';
    session_setting: 'app.<name>' (read with current_setting, D47)."""
    source: str
    type: ColType


class Identity(Strict):
    adapter: Literal["supabase", "pg_roles", "session_setting"]
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
class AppliesTo(Strict):
    """A selector (D48): which tables a rule applies to, in place of `table`.  One key:
    `has_column: [name]` or `[name, type]`, `named: [t, ...]`, `tagged: tag`, `all: true`,
    `and: [...]`, `or: [...]`.  The checker expands the rule into one rule per matching governed
    table before anything else sees the spec (`spec/selectors.py`)."""
    has_column: list[str] | None = Field(default=None, min_length=1, max_length=2)
    named: list[str] | None = Field(default=None, min_length=1)
    tagged: str | None = None
    all: Literal[True] | None = None
    and_: list["AppliesTo"] | None = Field(default=None, alias="and", min_length=1)
    or_: list["AppliesTo"] | None = Field(default=None, alias="or", min_length=1)

    @model_validator(mode="after")
    def _one_key(self) -> "AppliesTo":
        keys = [k for k in ("has_column", "named", "tagged", "all", "and_", "or_") if getattr(self, k) is not None]
        if len(keys) != 1:
            raise ValueError("a selector is exactly one of {has_column}, {named}, {tagged}, {all}, {and}, {or}")
        return self


AppliesTo.model_rebuild()


class RuleBase(Strict):
    """`table` names the table; `applies_to` (D48) selects tables instead, and the checker
    expands the rule into one per matching table, with ids `<id>.<table>` and `from` naming the
    selector rule (an expanded rule carries `table`, `applies_to` and `from` together)."""
    id: str
    name: str
    table: str | None = None
    applies_to: AppliesTo | None = None
    from_: str | None = Field(default=None, alias="from")
    requirement: str | None = None

    @model_validator(mode="after")
    def _table_or_selector(self) -> "RuleBase":
        if self.table is None and self.applies_to is None:
            raise ValueError("a rule needs \"table\" (one table) or \"applies_to\" (a selector, D48)")
        if self.table is not None and self.applies_to is not None and self.from_ is None:
            raise ValueError("\"table\" and \"applies_to\" are exclusive (only an expanded rule, which carries "
                             "\"from\", has both)")
        if self.from_ is not None and self.table is None:
            raise ValueError("an expanded rule (\"from\") names its table")
        return self


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


class CardinalityRule(RuleBase):
    """At most `max` rows of `table` share one value of `group` (D4).  `parent` is the table whose
    primary key `group` references; the compiled trigger locks that parent row before counting,
    which makes the bound hold under READ COMMITTED.  NULL groups are not counted."""
    kind: Literal["cardinality"]
    group: str
    parent: str
    max: int = Field(ge=1)


class ColumnRule(RuleBase):
    kind: Literal["column_rule"]
    name: str | None = None
    role: str
    columns: list[str] = Field(min_length=1)
    access: Literal["read", "write"]


Rule = Annotated[
    Union[PolicyRule, CheckRule, InclusionRule, TransitionRule, CardinalityRule, ColumnRule],
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


class MustAllow(PropBase):
    """The lower bound (liveness, D40): every row that satisfies `when` and the stored-row
    constraints is admitted to `role`'s `command` on `table`, all layers together (policies, and
    for a write the constraints on the written image, the transition rules and the column
    privileges).  For update `when` ranges over {"old": c} / {"new": c}; a bare {"col": c} is the
    new image.  A `when` no row can satisfy refutes the property (it would require nothing)."""
    template: Literal["must_allow"]
    table: str
    role: str
    command: Literal["select", "insert", "update", "delete"]
    when: Pred


class RoleSeparation(PropBase):
    """No row is admitted to `command` on `table` for both roles (D49): the English form of the
    `custom` property it already was.  The two roles are evaluated with the same context values
    (the same user acting under either role) and, for update, both the targeted and the written
    row must be admitted to both."""
    template: Literal["role_separation"]
    table: str
    roles: list[str] = Field(min_length=2, max_length=2)
    command: Literal["select", "insert", "update", "delete"]

    @model_validator(mode="after")
    def _distinct(self) -> "RoleSeparation":
        if self.roles[0] == self.roles[1]:
            raise ValueError("role_separation needs two different roles")
        return self


Property = Annotated[
    Union[TenantIsolation, ColumnImmutable, MonotoneIn, WriteSubsetRead, LeastPrivilege,
          InvariantPreserved, Custom, MustAllow, RoleSeparation],
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
    hierarchies: dict[str, Hierarchy] = Field(default_factory=dict)
    opaques: dict[str, Opaque] = Field(default_factory=dict)
    rules: list[Rule] = Field(default_factory=list)
    properties: list[Property] = Field(default_factory=list)

    # convenience accessors (no validation here; see spec/check.py)
    def expansions(self, rule_id: str) -> list[Rule]:
        """The rules a selector rule `rule_id` expanded into (empty when it is not a selector)."""
        return [r for r in self.rules if r.from_ == rule_id]

    def governed(self) -> dict[str, Table]:
        """The tables MLS compiles for: `owned` and `adopted` (D45)."""
        return {n: t for n, t in self.tables.items() if t.mode != "observed"}

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

    def cardinalities(self) -> list[CardinalityRule]:
        return [r for r in self.rules if isinstance(r, CardinalityRule)]

    def column_rules(self) -> list[ColumnRule]:
        return [r for r in self.rules if isinstance(r, ColumnRule)]

    def context_type(self, name: str) -> ColType | None:
        if name == CONTEXT_NOW:
            return "timestamp"
        cv = self.identity.context.get(name)
        return cv.type if cv else None


def is_terse(path: str | Path) -> bool:
    """`.mls` is the terse syntax, anything else (`.mls.json`, `.json`) is JSON (D50)."""
    return Path(path).suffix == ".mls"


def load_spec(path: str | Path) -> Spec:
    """Parse and validate a spec file (schema level only; run check_spec for types/fragment).
    The syntax is chosen by extension: `.mls` is terse (`spec/syntax.py`), otherwise JSON."""
    from mls.spec.check import pydantic_errors_to_mls
    from mls.errors import SpecError
    from pydantic import ValidationError

    text = Path(path).read_text()
    if is_terse(path):
        from mls.spec.syntax import parse

        return parse(text, str(path))
    try:
        return Spec.model_validate(json.loads(text))
    except json.JSONDecodeError as e:
        raise SpecError([MlsErrorRef(path="$", message=f"{path}:{e.lineno}:{e.colno}: not JSON: {e.msg}",
                                     fix="a spec is one JSON object (or terse syntax in a .mls file)",
                                     code="syntax")]) from None
    except ValidationError as e:
        raise SpecError(pydantic_errors_to_mls(e)) from None


def spec_to_json(spec: Spec) -> str:
    """The canonical serialization (D15): the JSON of the model, aliases, no nulls."""
    return json.dumps(spec.model_dump(by_alias=True, exclude_none=True), indent=1) + "\n"


def spec_json_schema() -> dict:
    return Spec.model_json_schema()
