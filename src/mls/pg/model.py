"""The concrete model: a Python interpreter of the spec semantics used as the oracle in
differential tests.  It must agree with SEMANTICS.md and therefore with the SMT encoding.

Values: Python ints, bools, strs (text/uuid), datetimes (timestamp), or None (NULL).
Predicates evaluate to True / False / None (UNKNOWN).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mls.props.effective import (
    effective, inclusion_pred, row_constraints, transition_constraints,
)
from mls.spec.model import (
    CONTEXT_NOW, And, ColRef, ColumnRule, Const, CtxRef, Cmp, InclusionRule, IsNull, Lit, Member,
    NewRef, Not, NowBetween, OldRef, OpaqueRef, Or, Pred, Spec,
)

Row = dict[str, Any]
TV = bool | None


@dataclass
class Database:
    """Relation facts (name -> set of (key, value)) and table rows."""
    relations: dict[str, set[tuple[Any, Any]]]
    tables: dict[str, list[Row]] = field(default_factory=dict)
    opaque_eval: dict[str, Any] = field(default_factory=dict)  # name -> callable(row, ctx) -> TV


def eval_term(t, row: Row, ctx: dict[str, Any], old: Row | None = None, new: Row | None = None):
    if isinstance(t, ColRef):
        return row[t.col]
    if isinstance(t, CtxRef):
        return ctx[t.ctx]
    if isinstance(t, Const):
        return t.const
    if isinstance(t, OldRef):
        return old[t.old]
    if isinstance(t, NewRef):
        return new[t.new]
    raise TypeError(t)


def eval_pred(p: Pred, row: Row, ctx: dict[str, Any], db: Database,
              old: Row | None = None, new: Row | None = None) -> TV:
    def ev(q: Pred) -> TV:
        return eval_pred(q, row, ctx, db, old, new)

    def term(t):
        return eval_term(t, row, ctx, old, new)

    if isinstance(p, Lit):
        return p.lit
    if isinstance(p, And):
        vs = [ev(a) for a in p.and_]
        return False if False in vs else (None if None in vs else True)
    if isinstance(p, Or):
        vs = [ev(a) for a in p.or_]
        return True if True in vs else (None if None in vs else False)
    if isinstance(p, Not):
        v = ev(p.not_)
        return None if v is None else (not v)
    if isinstance(p, IsNull):
        return term(p.is_null) is None
    if isinstance(p, Cmp):
        l, op, r = p.cmp
        lv, rv = term(l), term(r)
        if lv is None or rv is None:
            return None
        return {"=": lv == rv, "<>": lv != rv, "<": lv < rv, "<=": lv <= rv,
                ">": lv > rv, ">=": lv >= rv}[op]
    if isinstance(p, Member):
        k, v = term(p.member.key), term(p.member.value)
        if k is None or v is None:
            return None
        return (k, v) in db.relations[p.member.relation]
    if isinstance(p, NowBetween):
        now = ctx[CONTEXT_NOW]
        ok = True
        if p.now_between.from_ is not None:
            f = term(p.now_between.from_)
            ok = ok and (f is None or f <= now)
        if p.now_between.until is not None:
            u = term(p.now_between.until)
            ok = ok and (u is None or now < u)
        return ok
    if isinstance(p, OpaqueRef):
        return db.opaque_eval[p.opaque](row, ctx)
    raise TypeError(p)


def holds(p: Pred, row: Row, ctx: dict[str, Any], db: Database, old: Row | None = None,
          new: Row | None = None) -> bool:
    """Admission rule: a predicate admits iff it is TRUE (SEMANTICS §1)."""
    return eval_pred(p, row, ctx, db, old, new) is True


# ----------------------------------------------------------------------------- statement outcomes
# Outcomes: "filtered" (row not targeted / not visible), "ok", "error".

class Oracle:
    def __init__(self, spec: Spec):
        self.spec = spec

    def _granted(self, role: str, table: str, cmd: str, cols: list[str] | None = None) -> bool:
        from mls.compile.column import privileges
        privs = privileges(self.spec)
        g = privs.get((role, table), {})
        allowed = g.get(cmd)
        if allowed is None:
            return False
        if allowed == "*":
            return True
        return all(c in allowed for c in (cols or []))

    def visible(self, role: str, row: Row, ctx: dict, db: Database, table: str) -> bool:
        """SELECT: the row is visible iff SELECT-USING is TRUE (privileges are checked per
        statement by select_outcome)."""
        return holds(effective(self.spec, table, role, "select", "using"), row, ctx, db)

    def select_outcome(self, role: str, table: str, cols: list[str]) -> str:
        return "ok" if self._granted(role, table, "select", cols) else "error"

    def _unique_violation(self, table: str, new: Row, db: Database, old: Row | None = None) -> bool:
        """PK / UNIQUE: no other stored row has the same key (NULLs never collide)."""
        t = self.spec.tables[table]
        keysets = ([t.primary_key] if t.primary_key else []) + list(t.unique)
        for ks in keysets:
            key = tuple(new[c] for c in ks)
            if any(v is None for v in key):
                continue
            for r in db.tables.get(table, []):
                if r is old:
                    continue
                if tuple(r[c] for c in ks) == key:
                    return True
        return False

    def insert(self, role: str, table: str, new: Row, ctx: dict, db: Database,
               insert_cols: list[str] | None = None) -> str:
        if not self._granted(role, table, "insert", insert_cols if insert_cols is not None else list(new)):
            return "error"
        if any(new[c] is None and not col.nullable for c, col in self.spec.tables[table].columns.items()):
            return "error"
        if not holds(effective(self.spec, table, role, "insert", "check"), new, ctx, db):
            return "error"
        if not holds(row_constraints(self.spec, table), new, ctx, db):
            return "error"
        if self._unique_violation(table, new, db):
            return "error"
        return "ok"

    def update(self, role: str, table: str, old: Row, new: Row, ctx: dict, db: Database,
               set_cols: list[str], where_cols: list[str]) -> str:
        """UPDATE ... SET set_cols WHERE <where_cols = ...>.  SEMANTICS §5."""
        reads_columns = bool(where_cols)
        if not self._granted(role, table, "update", set_cols):
            return "error"
        if reads_columns and not self._granted(role, table, "select", where_cols):
            return "error"
        sel = effective(self.spec, table, role, "select", "using")
        if not holds(effective(self.spec, table, role, "update", "using"), old, ctx, db):
            return "filtered"
        if reads_columns and not holds(sel, old, ctx, db):
            return "filtered"
        if not holds(effective(self.spec, table, role, "update", "check"), new, ctx, db):
            return "error"
        if reads_columns and not holds(sel, new, ctx, db):
            return "error"
        if any(new[c] is None and not col.nullable for c, col in self.spec.tables[table].columns.items()):
            return "error"
        if not holds(row_constraints(self.spec, table), new, ctx, db):
            return "error"
        if not holds(transition_constraints(self.spec, table), new, ctx, db, old=old, new=new):
            return "error"
        if self._unique_violation(table, new, db, old):
            return "error"
        if self._fk_restrict_violation(table, old, new, db):
            return "error"
        return "ok"

    def delete(self, role: str, table: str, old: Row, ctx: dict, db: Database,
               where_cols: list[str]) -> str:
        reads_columns = bool(where_cols)
        if not self._granted(role, table, "delete"):
            return "error"
        if reads_columns and not self._granted(role, table, "select", where_cols):
            return "error"
        if not holds(effective(self.spec, table, role, "delete", "using"), old, ctx, db):
            return "filtered"
        if reads_columns and not holds(effective(self.spec, table, role, "select", "using"), old, ctx, db):
            return "filtered"
        if self._fk_restrict_violation(table, old, None, db):
            return "error"
        return "ok"

    def _fk_restrict_violation(self, table: str, old: Row, new: Row | None, db: Database) -> bool:
        """ON DELETE/UPDATE RESTRICT: removing a referenced (key, value) pair of a relation
        backed by `table` errors if any row references it."""
        for rname, rel in self.spec.relations.items():
            if rel.table != table:
                continue
            pair = (old[rel.key], old[rel.value])
            if new is not None and (new[rel.key], new[rel.value]) == pair:
                continue
            for inc in self.spec.inclusions():
                if inc.relation != rname:
                    continue
                for r in db.tables.get(inc.table, []):
                    if r is old:
                        continue
                    if (r[inc.columns[0]], r[inc.columns[1]]) == pair:
                        return True
        return False
