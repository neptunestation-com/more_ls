"""Typechecker + fragment checker for MLS specs.

Every error names the JSON path of the offending element and states the allowed alternative
(invariant 7).  `check_spec` returns nothing on success and raises SpecError otherwise.

Scopes (SEMANTICS §2-3):
  policy      USING / WITH CHECK: cols of the table, ctx, const; member(key=ctx|const, value=col);
              now_between; is_null; opaque.  Negated member is rejected (anti-join).
  check       CHECK constraint: cols, const, is_null, opaque without context inputs.
  transition  BEFORE UPDATE trigger: old/new refs and const only.
  invariant   property goals / invariants: cols, const, member (key may be a column), is_null.
  goal        custom property goals: like policy plus member with a column key.
  in_group follows the rules of member everywhere (key is context, value a NOT NULL column).
"""
from __future__ import annotations

import re
import uuid as _uuid
from dataclasses import dataclass

from pydantic import ValidationError

from mls.errors import ErrorSink, MlsError, SpecError
from mls.spec.model import (
    CONTEXT_NOW, And, CardinalityRule, CheckRule, ColRef, ColType, ColumnRule, Cmp, Const, CtxRef, In,
    Custom, InclusionRule, InGroup, InvariantPreserved, IsNull, Lit, Member, MonotoneIn, MustAllow, NewRef, Not,
    NowBetween, OldRef, OpaqueRef, Or, PolicyRule, Spec, TenantIsolation, ColumnImmutable,
    WriteSubsetRead, LeastPrivilege, TransitionRule,
)

IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
ORDERED_TYPES = {"int", "timestamp"}


def _json_path(loc: tuple) -> str:
    """Pydantic locations carry discriminator tags ('rules', 0, 'policy', 'using', 'and', 'and', 1):
    a tag repeats the field name for single-key unions, and follows the index for rules/properties."""
    parts: list = []
    prev = None
    for i, seg in enumerate(loc):
        if isinstance(seg, str) and seg == prev:
            continue
        if isinstance(seg, str) and isinstance(prev, int) and i >= 2 and loc[i - 2] in ("rules", "properties"):
            prev = seg
            continue
        parts.append(seg)
        prev = seg
    return "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in parts)


def pydantic_errors_to_mls(e: ValidationError) -> list[MlsError]:
    return [MlsError(path=_json_path(err["loc"]), message=err["msg"], code="schema") for err in e.errors()]


@dataclass(frozen=True)
class Scope:
    name: str
    table: str
    cols: bool = False
    ctx: bool = False
    old_new: bool = False
    member: bool = False
    member_col_key: bool = False
    now_between: bool = False
    opaque: bool = False
    opaque_ctx: bool = False
    reject_negated_member: bool = False
    is_policy: bool = False


def policy_scope(table: str) -> Scope:
    return Scope("policy", table, cols=True, ctx=True, member=True, now_between=True, opaque=True,
                 opaque_ctx=True, reject_negated_member=True, is_policy=True)


def check_scope(table: str) -> Scope:
    return Scope("check", table, cols=True, opaque=True)


def transition_scope(table: str) -> Scope:
    return Scope("transition", table, old_new=True)


def invariant_scope(table: str) -> Scope:
    return Scope("invariant", table, cols=True, member=True, member_col_key=True)


def goal_scope(table: str) -> Scope:
    return Scope("goal", table, cols=True, ctx=True, member=True, member_col_key=True,
                 now_between=True, opaque=True, opaque_ctx=True)


def must_allow_scope(table: str, command: str) -> Scope:
    """The condition of a must_allow property: a goal predicate that, for update, may also name
    the old and new images."""
    return Scope("must_allow condition", table, cols=True, ctx=True, member=True, member_col_key=True,
                 now_between=True, opaque=True, opaque_ctx=True, old_new=(command == "update"))


class Checker:
    def __init__(self, spec: Spec):
        self.spec = spec
        self.sink = ErrorSink()

    # ------------------------------------------------------------------ helpers
    def err(self, path: str, msg: str, fix: str = "", code: str = "error") -> None:
        self.sink.add(path, msg, fix, code)

    def col_type(self, table: str, col: str) -> ColType | None:
        t = self.spec.tables.get(table)
        if t is None or col not in t.columns:
            return None
        return t.columns[col].type

    def col_nullable(self, table: str, col: str) -> bool:
        return self.spec.tables[table].columns[col].nullable

    # ------------------------------------------------------------------ terms
    def term_type(self, term, scope: Scope, path: str) -> ColType | None:
        if isinstance(term, ColRef):
            if not scope.cols:
                self.err(path, f"a column reference is not allowed in a {scope.name} predicate",
                         "use {\"old\": col} / {\"new\": col} in transitions" if scope.old_new else
                         "remove the column reference")
                return None
            ty = self.col_type(scope.table, term.col)
            if ty is None:
                self.err(path, f"unknown column '{term.col}' of table '{scope.table}'",
                         f"one of: {', '.join(self.spec.tables[scope.table].columns)}")
            return ty
        if isinstance(term, (OldRef, NewRef)):
            if not scope.old_new:
                self.err(path, f"old/new references are only allowed in transition predicates and in the "
                               f"condition of a must_allow property on update",
                         "use {\"col\": name} here")
                return None
            col = term.old if isinstance(term, OldRef) else term.new
            ty = self.col_type(scope.table, col)
            if ty is None:
                self.err(path, f"unknown column '{col}' of table '{scope.table}'",
                         f"one of: {', '.join(self.spec.tables[scope.table].columns)}")
            return ty
        if isinstance(term, CtxRef):
            if not scope.ctx:
                self.err(path, f"context values are not allowed in a {scope.name} predicate "
                               f"(it must be a function of the row alone)",
                         "move the condition into a policy, or state it without context")
                return None
            ty = self.spec.context_type(term.ctx)
            if ty is None:
                self.err(path, f"unknown context value '{term.ctx}'",
                         f"declare it under identity.context or use one of: "
                         f"{', '.join([*self.spec.identity.context, CONTEXT_NOW])}")
            return ty
        if isinstance(term, Const):
            v = term.const
            if isinstance(v, bool):
                return "bool"
            if isinstance(v, int):
                return "int"
            return "text"  # str; uuid handled by the comparison rule
        self.err(path, "unknown term")
        return None

    def const_ok_for(self, term, ty: ColType, path: str) -> bool:
        """Constants: int<->int, bool<->bool, str<->text|uuid (uuid-shaped). No timestamp constants."""
        if not isinstance(term, Const):
            return True
        v = term.const
        if ty == "uuid":
            if isinstance(v, str):
                try:
                    _uuid.UUID(v)
                    return True
                except ValueError:
                    self.err(path, f"'{v}' is not a UUID literal", "use a canonical UUID string")
                    return False
            self.err(path, f"a uuid column needs a UUID string constant, got {v!r}")
            return False
        if ty == "timestamp":
            self.err(path, "timestamp constants are not supported in v1",
                     "compare with {\"ctx\": \"now\"} or use now_between")
            return False
        return True

    # ------------------------------------------------------------------ predicates
    def _cmp(self, l, op, r, scope: Scope, path: str, lpath: str, rpath: str) -> None:
        """The checks of a comparison; `path` names the comparison, `lpath`/`rpath` its operands
        (an expanded `in` reports under the `in` paths)."""
        lt = self.term_type(l, scope, lpath)
        rt = self.term_type(r, scope, rpath)
        if scope.is_policy:
            # sargable shape: column op (ctx | const), or (ctx|const) op (ctx|const)
            lhs_ok = isinstance(l, ColRef) or isinstance(l, (CtxRef, Const))
            rhs_ok = isinstance(r, (CtxRef, Const))
            if not (lhs_ok and rhs_ok):
                self.err(path, "comparisons in policies must be  column op (context | "
                                        "constant)  so they compile to an index condition",
                         "put the column on the left and a context value or constant on the right")
        elif scope.name == "check":
            if isinstance(l, Const) and isinstance(r, Const):
                self.err(path, "comparing two constants is meaningless", "")
        if lt is None or rt is None:
            return
        if isinstance(r, Const) and not isinstance(l, Const):
            if not self.const_ok_for(r, lt, rpath):
                return
        if isinstance(l, Const) and not isinstance(r, Const):
            if not self.const_ok_for(l, rt, lpath):
                return
        if lt != rt and not ({lt, rt} == {"uuid", "text"} and (isinstance(l, Const) or isinstance(r, Const))):
            self.err(path, f"type mismatch: {lt} {op} {rt}",
                     "compare values of the same type")
            return
        ty = lt if lt != "text" else rt
        if op not in ("=", "<>") and ty not in ORDERED_TYPES:
            self.err(path, f"operator {op} is not allowed on {ty} values "
                                    f"(v1 compares {ty} for equality only)", "use = or <>")
        self.const_ok_for(l, ty, lpath)
        self.const_ok_for(r, ty, rpath)

    def pred(self, p, scope: Scope, path: str, negated: bool = False) -> None:
        if isinstance(p, Lit):
            return
        if isinstance(p, And):
            for i, a in enumerate(p.and_):
                self.pred(a, scope, f"{path}.and[{i}]", negated)
            return
        if isinstance(p, Or):
            for i, a in enumerate(p.or_):
                self.pred(a, scope, f"{path}.or[{i}]", negated)
            return
        if isinstance(p, Not):
            self.pred(p.not_, scope, f"{path}.not", not negated)
            return
        if isinstance(p, IsNull):
            ty = self.term_type(p.is_null, scope, f"{path}.is_null")
            if isinstance(p.is_null, (Const, CtxRef)):
                self.err(f"{path}.is_null", "is_null applies to a column (constants and context "
                                            "values are never NULL)", "use {\"col\": name}")
            return
        if isinstance(p, Cmp):
            l, op, r = p.cmp
            self._cmp(l, op, r, scope, f"{path}.cmp", f"{path}.cmp[0]", f"{path}.cmp[2]")
            return
        if isinstance(p, In):
            t, consts = p.in_
            if isinstance(t, Const):
                self.err(f"{path}.in[0]", "the left side of 'in' must be a column, a context value or "
                                          "an image column, not a constant", "use {\"col\": name}")
                return
            if not consts:
                self.err(f"{path}.in[1]", "'in' needs at least one constant (an empty list is never true)",
                         "list the allowed values, or use {\"lit\": false}")
                return
            # the expansion is  t = c1 or t = c2 ...  (spec/expand.py); each disjunct is checked
            # as the comparison it becomes, under the path of the constant it comes from
            for i, c in enumerate(consts):
                self._cmp(t, "=", c, scope, f"{path}.in", f"{path}.in[0]", f"{path}.in[1][{i}]")
            return
        if isinstance(p, Member):
            if not scope.member:
                self.err(path, f"membership is not allowed in a {scope.name} predicate",
                         "express the relationship as an inclusion rule (foreign key)")
                return
            m = p.member
            rel = self.spec.relations.get(m.relation)
            if rel is None:
                self.err(f"{path}.member.relation", f"unknown relation '{m.relation}'",
                         f"one of: {', '.join(self.spec.relations)}")
                return
            if negated and scope.reject_negated_member:
                self.err(path, "negated membership in a policy is an anti-join and cannot be "
                               "index-driven", "restate positively, or wrap it in an opaque block")
            kt = self.term_type(m.key, scope, f"{path}.member.key")
            vt = self.term_type(m.value, scope, f"{path}.member.value")
            if isinstance(m.key, ColRef) and not scope.member_col_key:
                self.err(f"{path}.member.key", "the membership key must be a context value or "
                                               "constant (a column key is a per-row correlated "
                                               "subquery)", "use {\"ctx\": name}")
            if not isinstance(m.value, (ColRef, OldRef, NewRef)):
                self.err(f"{path}.member.value", "the membership value must be a column",
                         "use {\"col\": name}")
            elif isinstance(m.value, ColRef) and self.col_type(scope.table, m.value.col) is not None \
                    and self.col_nullable(scope.table, m.value.col) and scope.is_policy:
                self.err(f"{path}.member.value", f"membership value column '{m.value.col}' must be "
                                                 f"NOT NULL in a policy (NULL makes the atom UNKNOWN "
                                                 f"and the row invisible)",
                         "declare the column NOT NULL, or guard with {\"is_null\": ...} in an or")
            rkt = self.col_type(rel.table, rel.key)
            rvt = self.col_type(rel.table, rel.value)
            if kt is not None and rkt is not None and kt != rkt:
                self.err(f"{path}.member.key", f"key type {kt} does not match relation key "
                                               f"{rel.table}.{rel.key}: {rkt}")
            if vt is not None and rvt is not None and vt != rvt:
                self.err(f"{path}.member.value", f"value type {vt} does not match relation value "
                                                 f"{rel.table}.{rel.value}: {rvt}")
            return
        if isinstance(p, InGroup):
            if not scope.member:
                self.err(path, f"in_group is not allowed in a {scope.name} predicate", "")
                return
            g = p.in_group
            h = self.spec.hierarchies.get(g.hierarchy)
            if h is None:
                self.err(f"{path}.in_group.hierarchy", f"unknown hierarchy '{g.hierarchy}'",
                         f"one of: {', '.join(self.spec.hierarchies) or 'none'}")
                return
            if negated and scope.reject_negated_member:
                self.err(path, "negated in_group in a policy is an anti-join and cannot be "
                               "index-driven", "restate positively, or wrap it in an opaque block")
            kt = self.term_type(g.key, scope, f"{path}.in_group.key")
            vt = self.term_type(g.value, scope, f"{path}.in_group.value")
            if isinstance(g.key, ColRef) and not scope.member_col_key:
                self.err(f"{path}.in_group.key", "the in_group key must be a context value or "
                                                 "constant", "use {\"ctx\": name}")
            if not isinstance(g.value, (ColRef, OldRef, NewRef)):
                self.err(f"{path}.in_group.value", "the in_group value must be a column", "use {\"col\": name}")
            elif isinstance(g.value, ColRef) and self.col_type(scope.table, g.value.col) is not None \
                    and self.col_nullable(scope.table, g.value.col) and scope.is_policy:
                self.err(f"{path}.in_group.value", f"in_group value column '{g.value.col}' must be "
                                                   f"NOT NULL in a policy (NULL makes the atom UNKNOWN "
                                                   f"and the row invisible)",
                         "declare the column NOT NULL, or guard with {\"is_null\": ...} in an or")
            rel = self.spec.relations.get(h.members)
            if rel is not None and rel.table in self.spec.tables:
                rkt = self.col_type(rel.table, rel.key)
                if kt is not None and rkt is not None and kt != rkt:
                    self.err(f"{path}.in_group.key", f"key type {kt} does not match the members "
                                                     f"relation key {rel.table}.{rel.key}: {rkt}")
            it = self.col_type(h.table, h.id)
            if vt is not None and it is not None and vt != it:
                self.err(f"{path}.in_group.value", f"value type {vt} does not match the hierarchy id "
                                                   f"{h.table}.{h.id}: {it}")
            return
        if isinstance(p, NowBetween):
            if not scope.now_between:
                self.err(path, f"now_between is not allowed in a {scope.name} predicate "
                               f"(now() is not immutable)", "move the condition into a policy")
                return
            nb = p.now_between
            if nb.from_ is None and nb.until is None:
                self.err(path, "now_between needs at least one bound", "")
            for name, t in (("from", nb.from_), ("until", nb.until)):
                if t is None:
                    continue
                ty = self.term_type(t, scope, f"{path}.now_between.{name}")
                if not isinstance(t, ColRef):
                    self.err(f"{path}.now_between.{name}", "bounds must be columns", "use {\"col\": name}")
                elif ty is not None and ty != "timestamp":
                    self.err(f"{path}.now_between.{name}", f"bound column must be a timestamp, got {ty}")
            return
        if isinstance(p, OpaqueRef):
            if not scope.opaque:
                self.err(path, f"opaque blocks are not allowed in a {scope.name} predicate", "")
                return
            o = self.spec.opaques.get(p.opaque)
            if o is None:
                self.err(f"{path}.opaque", f"unknown opaque block '{p.opaque}'",
                         f"declare it under opaques; known: {', '.join(self.spec.opaques) or 'none'}")
                return
            if o.table != scope.table:
                self.err(f"{path}.opaque", f"opaque block '{p.opaque}' is declared for table "
                                           f"'{o.table}', not '{scope.table}'")
            if o.context and not scope.opaque_ctx:
                self.err(f"{path}.opaque", f"opaque block '{p.opaque}' reads context values and "
                                           f"cannot be used in a {scope.name} predicate")
            return
        self.err(path, f"unknown predicate node {type(p).__name__}")

    # ------------------------------------------------------------------ top level
    def run(self) -> list[MlsError]:
        s = self.spec
        self.check_identity()
        for i, r in enumerate(s.roles):
            if not IDENT.match(r):
                self.err(f"$.roles[{i}]", f"'{r}' is not a valid role identifier",
                         "lower-case letters, digits and underscores")
        if len(set(s.roles)) != len(s.roles):
            self.err("$.roles", "duplicate role names")
        self.check_tables()
        self.check_relations()
        self.check_hierarchies()
        self.check_opaques()
        self.check_rules()
        self.check_properties()
        return self.sink.errors

    def check_identity(self) -> None:
        ident = self.spec.identity
        for name, cv in ident.context.items():
            path = f"$.identity.context.{name}"
            if name == CONTEXT_NOW:
                self.err(path, "'now' is a built-in context value", "remove it")
            if not IDENT.match(name):
                self.err(path, f"'{name}' is not a valid context name")
            if ident.adapter == "supabase":
                if cv.source == "auth.uid":
                    if cv.type != "uuid":
                        self.err(path + ".type", "auth.uid is a uuid", "set type to \"uuid\"")
                elif cv.source.startswith("jwt.claims."):
                    claim = cv.source[len("jwt.claims."):]
                    if not claim:
                        self.err(path + ".source", "empty claim name")
                else:
                    self.err(path + ".source", f"unknown supabase source '{cv.source}'",
                             "use 'auth.uid' or 'jwt.claims.<name>'")
            else:  # pg_roles
                if cv.source != "current_user":
                    self.err(path + ".source", f"unknown pg_roles source '{cv.source}'",
                             "use 'current_user'")
                elif cv.type != "text":
                    self.err(path + ".type", "current_user is text", "set type to \"text\"")

    def check_tables(self) -> None:
        for tname, t in self.spec.tables.items():
            path = f"$.tables.{tname}"
            if not IDENT.match(tname):
                self.err(path, f"'{tname}' is not a valid table identifier")
            if not t.columns:
                self.err(path + ".columns", "a table needs at least one column")
            for cname in t.columns:
                if not IDENT.match(cname):
                    self.err(f"{path}.columns.{cname}", f"'{cname}' is not a valid column identifier")
            if t.primary_key is not None:
                for i, c in enumerate(t.primary_key):
                    if c not in t.columns:
                        self.err(f"{path}.primary_key[{i}]", f"unknown column '{c}'")
                    elif t.columns[c].nullable:
                        self.err(f"{path}.primary_key[{i}]", f"primary key column '{c}' must be NOT NULL",
                                 "set nullable to false")
            for i, u in enumerate(t.unique):
                for j, c in enumerate(u):
                    if c not in t.columns:
                        self.err(f"{path}.unique[{i}][{j}]", f"unknown column '{c}'")

    def key_sets(self, table: str) -> list[frozenset[str]]:
        t = self.spec.tables[table]
        out = []
        if t.primary_key:
            out.append(frozenset(t.primary_key))
        out.extend(frozenset(u) for u in t.unique)
        return out

    def check_relations(self) -> None:
        for rname, rel in self.spec.relations.items():
            path = f"$.relations.{rname}"
            if not IDENT.match(rname):
                self.err(path, f"'{rname}' is not a valid relation identifier")
            if rel.table not in self.spec.tables:
                self.err(path + ".table", f"unknown table '{rel.table}'",
                         f"one of: {', '.join(self.spec.tables)}")
                continue
            ok = True
            for fld in ("key", "value"):
                c = getattr(rel, fld)
                if c not in self.spec.tables[rel.table].columns:
                    self.err(f"{path}.{fld}", f"unknown column '{c}' of table '{rel.table}'")
                    ok = False
                elif self.spec.tables[rel.table].columns[c].nullable:
                    self.err(f"{path}.{fld}", f"relation column '{c}' must be NOT NULL",
                             "set nullable to false")
            if not ok:
                continue
            if rel.key == rel.value:
                self.err(path, "key and value must be different columns")
            if frozenset((rel.key, rel.value)) not in self.key_sets(rel.table):
                self.err(path, f"({rel.key}, {rel.value}) must be a primary key or unique "
                               f"constraint of '{rel.table}' (a relation is a set of pairs and "
                               f"inclusion rules compile to foreign keys that reference it)",
                         f"add [\"{rel.key}\", \"{rel.value}\"] to tables.{rel.table}.unique")

    def check_hierarchies(self) -> None:
        for hname, h in self.spec.hierarchies.items():
            path = f"$.hierarchies.{hname}"
            if not IDENT.match(hname):
                self.err(path, f"'{hname}' is not a valid identifier")
            t = self.spec.tables.get(h.table)
            if t is None:
                self.err(path + ".table", f"unknown table '{h.table}'", f"one of: {', '.join(self.spec.tables)}")
                continue
            ok = True
            for fld in ("id", "parent", "depth"):
                if getattr(h, fld) not in t.columns:
                    self.err(f"{path}.{fld}", f"unknown column '{getattr(h, fld)}' of table '{h.table}'")
                    ok = False
            if not ok:
                continue
            if len({h.id, h.parent, h.depth}) != 3:
                self.err(path, "id, parent and depth must be three different columns")
            if t.primary_key != [h.id]:
                self.err(f"{path}.id", f"'{h.id}' must be the single-column primary key of '{h.table}' "
                                       f"(parents reference it)", f"set tables.{h.table}.primary_key to [\"{h.id}\"]")
            if not t.columns[h.parent].nullable:
                self.err(f"{path}.parent", f"'{h.parent}' must be nullable (roots have no parent)",
                         "set nullable to true")
            if t.columns[h.parent].type != t.columns[h.id].type:
                self.err(f"{path}.parent", f"'{h.parent}' ({t.columns[h.parent].type}) must have the "
                                           f"type of '{h.id}' ({t.columns[h.id].type})")
            if t.columns[h.depth].type != "int" or t.columns[h.depth].nullable:
                self.err(f"{path}.depth", f"'{h.depth}' must be an int NOT NULL column (the compiler "
                                          f"maintains it)", "declare {\"type\": \"int\", \"nullable\": false}")
            rel = self.spec.relations.get(h.members)
            if rel is None:
                self.err(f"{path}.members", f"unknown relation '{h.members}'",
                         f"one of: {', '.join(self.spec.relations) or 'none'}")
            elif rel.table in self.spec.tables and self.col_type(rel.table, rel.value) is not None \
                    and self.col_type(rel.table, rel.value) != t.columns[h.id].type:
                self.err(f"{path}.members", f"the value of relation '{h.members}' ({rel.table}.{rel.value}: "
                                            f"{self.col_type(rel.table, rel.value)}) must have the type of "
                                            f"'{h.id}' ({t.columns[h.id].type})")

    def check_opaques(self) -> None:
        for oname, o in self.spec.opaques.items():
            path = f"$.opaques.{oname}"
            if not IDENT.match(oname):
                self.err(path, f"'{oname}' is not a valid identifier")
            if o.table not in self.spec.tables:
                self.err(path + ".table", f"unknown table '{o.table}'")
                continue
            for i, c in enumerate(o.columns):
                if c not in self.spec.tables[o.table].columns:
                    self.err(f"{path}.columns[{i}]", f"unknown column '{c}' of table '{o.table}'")
            for i, c in enumerate(o.context):
                if self.spec.context_type(c) is None:
                    self.err(f"{path}.context[{i}]", f"unknown context value '{c}'")
            for i, r in enumerate(o.relations):
                if r not in self.spec.relations:
                    self.err(f"{path}.relations[{i}]", f"unknown relation '{r}'")
            if not o.sql.strip():
                self.err(path + ".sql", "empty SQL")

    def check_rules(self) -> None:
        ids: set[str] = set()
        names: set[tuple[str, str]] = set()
        colrules: set[tuple[str, str, str]] = set()
        for i, r in enumerate(self.spec.rules):
            path = f"$.rules[{i}]"
            if r.id in ids:
                self.err(path + ".id", f"duplicate rule id '{r.id}'")
            ids.add(r.id)
            if r.name is not None and not IDENT.match(r.name):
                self.err(path + ".name", f"'{r.name}' is not a valid identifier")
            if r.table not in self.spec.tables:
                self.err(path + ".table", f"unknown table '{r.table}'",
                         f"one of: {', '.join(self.spec.tables)}")
                continue
            if r.kind != "column_rule":
                if (r.table, r.name) in names:
                    self.err(path + ".name", f"duplicate rule name '{r.name}' on table '{r.table}'")
                names.add((r.table, r.name))
            if isinstance(r, PolicyRule):
                self.check_policy(r, path)
            elif isinstance(r, CheckRule):
                self.pred(r.predicate, check_scope(r.table), path + ".predicate")
            elif isinstance(r, InclusionRule):
                self.check_inclusion(r, path)
            elif isinstance(r, TransitionRule):
                self.pred(r.predicate, transition_scope(r.table), path + ".predicate")
            elif isinstance(r, CardinalityRule):
                self.check_cardinality(r, path)
            elif isinstance(r, ColumnRule):
                if r.role not in self.spec.roles:
                    self.err(path + ".role", f"unknown role '{r.role}'", f"one of: {', '.join(self.spec.roles)}")
                for j, c in enumerate(r.columns):
                    if c not in self.spec.tables[r.table].columns:
                        self.err(f"{path}.columns[{j}]", f"unknown column '{c}' of table '{r.table}'")
                k = (r.role, r.table, r.access)
                if k in colrules:
                    self.err(path, f"a second {r.access} column rule for role '{r.role}' on "
                                   f"'{r.table}'", "merge the column lists into one rule")
                colrules.add(k)

    def check_policy(self, r: PolicyRule, path: str) -> None:
        if r.role not in self.spec.roles:
            self.err(path + ".role", f"unknown role '{r.role}'", f"one of: {', '.join(self.spec.roles)}")
        cmd = r.command
        if cmd in ("select", "delete"):
            if r.using is None:
                self.err(path, f"a {cmd} policy needs 'using'")
            if r.check is not None:
                self.err(path + ".check", f"a {cmd} policy has no 'check' (no new row)", "remove it")
        elif cmd == "insert":
            if r.check is None:
                self.err(path, "an insert policy needs 'check'")
            if r.using is not None:
                self.err(path + ".using", "an insert policy has no 'using' (no existing row)", "remove it")
        else:  # update, all
            if r.using is None:
                self.err(path, f"an {cmd} policy needs 'using' ('check' defaults to 'using' "
                               f"when omitted, as in Postgres)")
        scope = policy_scope(r.table)
        if r.using is not None:
            self.pred(r.using, scope, path + ".using")
        if r.check is not None:
            self.pred(r.check, scope, path + ".check")

    def check_cardinality(self, r: CardinalityRule, path: str) -> None:
        t = self.spec.tables[r.table]
        if r.group not in t.columns:
            self.err(path + ".group", f"unknown column '{r.group}' of table '{r.table}'")
            return
        parent = self.spec.tables.get(r.parent)
        if parent is None:
            self.err(path + ".parent", f"unknown table '{r.parent}'",
                     "the parent table whose row is locked while counting (D4)")
            return
        if r.parent == r.table:
            self.err(path + ".parent", "the parent table must be a different table")
            return
        if not parent.primary_key or len(parent.primary_key) != 1:
            self.err(path + ".parent", f"'{r.parent}' needs a single-column primary key that '{r.group}' "
                                       f"references")
            return
        pk = parent.primary_key[0]
        if parent.columns[pk].type != t.columns[r.group].type:
            self.err(path + ".group", f"type of {r.table}.{r.group} ({t.columns[r.group].type}) does not "
                                      f"match {r.parent}.{pk} ({parent.columns[pk].type})")

    def check_inclusion(self, r: InclusionRule, path: str) -> None:
        rel = self.spec.relations.get(r.relation)
        if rel is None:
            self.err(path + ".relation", f"unknown relation '{r.relation}'",
                     f"one of: {', '.join(self.spec.relations)}")
            return
        t = self.spec.tables[r.table]
        ok = True
        for j, c in enumerate(r.columns):
            if c not in t.columns:
                self.err(f"{path}.columns[{j}]", f"unknown column '{c}' of table '{r.table}'")
                ok = False
        if not ok or rel.table not in self.spec.tables:
            return
        if r.columns[0] == r.columns[1]:
            self.err(path + ".columns", "the two columns must differ")
        for j, (c, rc) in enumerate(zip(r.columns, (rel.key, rel.value))):
            ct, rt = self.col_type(r.table, c), self.col_type(rel.table, rc)
            if ct is not None and rt is not None and ct != rt:
                self.err(f"{path}.columns[{j}]", f"type of {r.table}.{c} ({ct}) does not match "
                                                 f"{rel.table}.{rc} ({rt})")

    def check_properties(self) -> None:
        ids: set[str] = set()
        for i, p in enumerate(self.spec.properties):
            path = f"$.properties[{i}]"
            if p.id in ids:
                self.err(path + ".id", f"duplicate property id '{p.id}'")
            ids.add(p.id)
            if isinstance(p, (TenantIsolation, ColumnImmutable, WriteSubsetRead, Custom, MustAllow)):
                if p.table not in self.spec.tables:
                    self.err(path + ".table", f"unknown table '{p.table}'")
                    continue
            if isinstance(p, TenantIsolation):
                if p.column not in self.spec.tables[p.table].columns:
                    self.err(path + ".column", f"unknown column '{p.column}'")
                if p.relation not in self.spec.relations:
                    self.err(path + ".relation", f"unknown relation '{p.relation}'")
                self.term_type(p.key, policy_scope(p.table), path + ".key")
                if not isinstance(p.key, (CtxRef, Const)):
                    self.err(path + ".key", "the key must be a context value", "use {\"ctx\": name}")
            elif isinstance(p, ColumnImmutable):
                if p.column not in self.spec.tables[p.table].columns:
                    self.err(path + ".column", f"unknown column '{p.column}'")
            elif isinstance(p, MonotoneIn):
                if p.relation not in self.spec.relations:
                    self.err(path + ".relation", f"unknown relation '{p.relation}'")
            elif isinstance(p, LeastPrivilege):
                for role, tabs in p.allowed.items():
                    if role not in self.spec.roles:
                        self.err(f"{path}.allowed.{role}", f"unknown role '{role}'")
                    for t in tabs:
                        if t not in self.spec.tables:
                            self.err(f"{path}.allowed.{role}.{t}", f"unknown table '{t}'")
            elif isinstance(p, InvariantPreserved):
                for j, inv in enumerate(p.invariants):
                    ipath = f"{path}.invariants[{j}]"
                    if inv.rule is not None:
                        try:
                            rule = self.spec.rule(inv.rule)
                        except KeyError:
                            self.err(ipath + ".rule", f"unknown rule '{inv.rule}'")
                            continue
                        if not isinstance(rule, (CheckRule, InclusionRule, CardinalityRule)):
                            self.err(ipath + ".rule", f"rule '{inv.rule}' is a {rule.kind}; only "
                                                      f"check, inclusion and cardinality rules are invariants")
                    else:
                        if inv.table not in self.spec.tables:
                            self.err(ipath + ".table", f"unknown table '{inv.table}'")
                            continue
                        self.pred(inv.predicate, invariant_scope(inv.table), ipath + ".predicate")
            elif isinstance(p, Custom):
                if p.role not in self.spec.roles:
                    self.err(path + ".role", f"unknown role '{p.role}'")
                self.pred(p.goal, goal_scope(p.table), path + ".goal")
            elif isinstance(p, MustAllow):
                if p.role not in self.spec.roles:
                    self.err(path + ".role", f"unknown role '{p.role}'")
                self.pred(p.when, must_allow_scope(p.table, p.command), path + ".when")


def check_spec(spec: Spec) -> Spec:
    """Typecheck and fragment-check `spec`, then return it with its sugar expanded
    (spec/expand.py).  Every consumer must use the returned spec."""
    from mls.spec.expand import expand_spec

    errors = Checker(spec).run()
    if errors:
        raise SpecError(errors)
    return expand_spec(spec)
