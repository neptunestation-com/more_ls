"""Differential test harness: deploy compiled DDL to a real Postgres, load generated data, run
SELECT / INSERT / UPDATE / DELETE statements as app roles under generated contexts, and compare
the three-way outcomes (filtered / ok / error) with the concrete model (pg/model.py).

The database state is restored after every statement (savepoints), so the oracle evaluates every
statement against the same loaded dataset.
"""
from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql as psql

from mls.compile.column import privileges
from mls.compile.plan import Script
from mls.identity import Adapter, get_adapter
from mls.pg.model import Database, Oracle, Row, holds
from mls.props.effective import simple_fks
from mls.spec.model import CONTEXT_NOW, Spec

Outcome = str  # "filtered" | "ok" | "error"
EXPECTED_SQLSTATES = ("23", "42501", "22")  # integrity, RLS/privilege, data exceptions


@dataclass
class Dataset:
    tables: dict[str, list[Row]]
    users: list[Any]
    now: dt.datetime

    def database(self, spec: Spec) -> Database:
        rels = {}
        for name, rel in spec.relations.items():
            rels[name] = {(r[rel.key], r[rel.value]) for r in self.tables.get(rel.table, [])}
        return Database(rels, {t: list(rows) for t, rows in self.tables.items()}, spec=spec)


@dataclass
class Mismatch:
    op: str
    table: str
    role: str
    ctx: dict
    detail: dict
    pg: Any
    model: Any

    def __str__(self) -> str:
        return (f"{self.op} {self.table} as {self.role} ctx={self.ctx} {self.detail}: "
                f"pg={self.pg} model={self.model}")


@dataclass
class DiffResult:
    statements: int = 0
    outcomes: dict[str, int] = field(default_factory=lambda: {"filtered": 0, "ok": 0, "error": 0})
    mismatches: list[Mismatch] = field(default_factory=list)
    visible_rows: int = 0  # rows returned by SELECTs (a run where nothing is visible proves little)

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def summary(self) -> str:
        return (f"{self.statements} statements, outcomes {self.outcomes}, {self.visible_rows} rows "
                f"visible, {len(self.mismatches)} mismatches")


# ----------------------------------------------------------------------------- value generation
class Chooser:
    """What the generators need from a source of randomness (random.Random satisfies it).
    Tests let hypothesis choose the seed and the shape (row count, NULL rate) and shrink those."""

    def random(self) -> float: ...
    def choice(self, seq): ...


class ValueGen:
    """Random values per column type from small pools so that collisions (and NULLs) are frequent."""

    def __init__(self, spec: Spec, adapter: Adapter, rnd: Chooser, n_users: int = 6,
                 n_ints: int = 5, now: dt.datetime | None = None, null_p: float = 0.3):
        self.spec = spec
        self.rnd = rnd
        self.users = adapter.harness_user_values(n_users)
        self.ints = list(range(-1, n_ints))
        self.ids = list(range(0, 40))  # single-column int primary keys (trees and parents need many rows)
        self.texts = ["a", "b", "c"]
        self.now = now or dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.null_p = null_p
        self.facts: dict[str, list[tuple[Any, Any]]] = {}  # relation -> known pairs (FK-aware rows)
        self.rows: dict[str, list[Row]] = {}                  # table -> rows generated so far
        self.user_cols = user_columns(spec, adapter)
        self.user_ctx = adapter.user_context_name()

    def value(self, type_: str, nullable: bool, null_p: float | None = None) -> Any:
        if nullable and self.rnd.random() < (self.null_p if null_p is None else null_p):
            return None
        if type_ == "int":
            return self.rnd.choice(self.ints)
        if type_ == "uuid":
            return self.rnd.choice(self.users)
        if type_ == "text":
            return self.rnd.choice(self.texts)
        if type_ == "bool":
            return self.rnd.random() < 0.5
        if type_ == "timestamp":
            return self.now + dt.timedelta(days=self.rnd.choice([-3, -2, -1, 1, 2, 3]))
        raise ValueError(type_)

    def row(self, table: str) -> Row:
        t = self.spec.tables[table]
        r = {c: (self.value("uuid", col.nullable) if (table, c) in self.user_cols
                 else self.value(col.type, col.nullable)) for c, col in t.columns.items()}
        if t.primary_key and len(t.primary_key) == 1 and t.columns[t.primary_key[0]].type == "int":
            r[t.primary_key[0]] = self.rnd.choice(self.ids)
        # most rows reference an existing relation pair, so FK-constrained tables fill up
        for inc in self.spec.inclusions():
            pairs = self.facts.get(inc.relation)
            if inc.table == table and pairs and self.rnd.random() < 0.7:
                k, v = self.rnd.choice(pairs)
                r[inc.columns[0]], r[inc.columns[1]] = k, v
        crowded = {c.group for c in self.spec.cardinalities() if c.table == table}
        for fk in simple_fks(self.spec):  # ... and an existing parent row (or NULL / none when allowed)
            if fk.table != table:
                continue
            parents = self.rows.get(fk.parent, [])
            if parents and self.rnd.random() < 0.85:
                # counted groups crowd into a few parents so cardinality limits are actually reached
                pool = parents[:2] if fk.column in crowded and self.rnd.random() < 0.6 else parents
                r[fk.column] = self.rnd.choice(pool)[fk.parent_column]
        for h in self.spec.hierarchies.values():  # roots are rarer than children: deeper trees
            if h.table == table and r[h.parent] is not None and self.rnd.random() < 0.15:
                r[h.parent] = None
        return r

    def context(self, role: str) -> dict[str, Any]:
        ctx: dict[str, Any] = {CONTEXT_NOW: self.now}
        for name, cv in self.spec.identity.context.items():
            if name == self.user_ctx:
                ctx[name] = self.rnd.choice(self.users)
            else:
                ctx[name] = self.value(cv.type, False)
        return ctx


def user_columns(spec: Spec, adapter: Adapter) -> set[tuple[str, str]]:
    """Columns that hold user identities: relation keys (and the columns inclusions map onto
    them) and columns compared with the user context value.  They draw from the adapter's user
    pool so that memberships actually match the session user."""
    from mls.props.effective import members_in
    from mls.spec.model import And, ColRef, Cmp, CtxRef, Not, Or, Pred

    user_ctx = adapter.user_context_name()
    out: set[tuple[str, str]] = set()
    rel_keys: set[str] = set()

    def cmps(p: Pred | None, acc: list[Cmp]) -> list[Cmp]:
        if isinstance(p, Cmp):
            acc.append(p)
        elif isinstance(p, And):
            for a in p.and_:
                cmps(a, acc)
        elif isinstance(p, Or):
            for a in p.or_:
                cmps(a, acc)
        elif isinstance(p, Not):
            cmps(p.not_, acc)
        return acc

    from mls.props.effective import in_groups_in

    for pol in spec.policies():
        for e in (pol.using, pol.check):
            for m in members_in(e, []):
                if isinstance(m.key, CtxRef) and m.key.ctx == user_ctx:
                    rel_keys.add(m.relation)
            for g in in_groups_in(e, []):
                if isinstance(g.key, CtxRef) and g.key.ctx == user_ctx:
                    rel_keys.add(spec.hierarchies[g.hierarchy].members)
            for c in cmps(e, []):
                l, _, r = c.cmp
                if isinstance(r, CtxRef) and r.ctx == user_ctx and isinstance(l, ColRef):
                    out.add((pol.table, l.col))
    for rname in rel_keys:
        rel = spec.relations[rname]
        out.add((rel.table, rel.key))
        for inc in spec.inclusions():
            if inc.relation == rname:
                out.add((inc.table, inc.columns[0]))
    return out


def table_order(spec: Spec) -> list[str]:
    """Tables ordered so that every FK target precedes its referencing table."""
    deps = {t: set() for t in spec.governed()}
    for inc in spec.inclusions():
        target = spec.relations[inc.relation].table
        if target != inc.table:
            deps[inc.table].add(target)
    for fk in simple_fks(spec):
        if fk.parent != fk.table:
            deps[fk.table].add(fk.parent)
    order: list[str] = []
    while len(order) < len(deps):
        progress = False
        for t in deps:
            if t in order:
                continue
            if deps[t] <= set(order):
                order.append(t)
                progress = True
        if not progress:
            raise ValueError("cyclic inclusion dependencies")
    return order


def generate_dataset(spec: Spec, adapter: Adapter, rnd: Chooser, rows_per_table: int = 25,
                     gen: ValueGen | None = None) -> Dataset:
    """Rows that satisfy PKs, uniques, CHECKs and FKs (so the owner can load them)."""
    gen = gen or ValueGen(spec, adapter, rnd)
    oracle = Oracle(spec)
    tables: dict[str, list[Row]] = {}
    ds = Dataset(tables, gen.users, gen.now)
    for table in table_order(spec):
        rows: list[Row] = []
        tables[table] = rows          # self-referencing tables see the rows generated so far
        gen.rows[table] = rows
        for _ in range(rows_per_table * 6):
            if len(rows) >= rows_per_table:
                break
            r = oracle.stored_ok(table, gen.row(table), ds.database(spec))
            if r is None:
                continue
            rows.append(r)
        for rname, rel in spec.relations.items():
            if rel.table == table:
                gen.facts[rname] = [(r[rel.key], r[rel.value]) for r in rows]
    return ds


# ----------------------------------------------------------------------------- Postgres side
def _ident(name: str) -> psql.Identifier:
    return psql.Identifier(name)


def classify_error(e: psycopg.Error) -> Outcome:
    code = e.sqlstate or ""
    if any(code.startswith(p) for p in EXPECTED_SQLSTATES):
        return "error"
    raise e


class LoadError(Exception):
    pass


class Harness:
    def __init__(self, spec: Spec, script: Script, dsn: str, dbname: str,
                 opaque_impl: dict[str, Any] | None = None):
        self.spec = spec
        self.script = script
        self.dsn = dsn
        self.dbname = dbname
        self.adapter = get_adapter(spec)
        self.oracle = Oracle(spec)
        self.opaque_impl = opaque_impl or {}
        self.conn: psycopg.Connection | None = None

    def db(self, ds: Dataset) -> Database:
        d = ds.database(self.spec)
        d.opaque_eval = self.opaque_impl
        return d

    # --- lifecycle
    def deploy(self) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as admin:
            admin.execute(psql.SQL("drop database if exists {}").format(_ident(self.dbname)))
            admin.execute(psql.SQL("create database {}").format(_ident(self.dbname)))
        self.conn = psycopg.connect(psycopg.conninfo.make_conninfo(self.dsn, dbname=self.dbname),
                                    autocommit=True)
        with self.conn.cursor() as cur:
            # roles first, then the adapter shim (it grants to the roles), then the rest
            for s in self.script.statements:
                if s.section in ("preamble", "roles"):
                    cur.execute(s.sql)
            for s in self.adapter.harness_setup_sql(self.spec):
                cur.execute(s)
            for s in self.pre_existing_sql():
                cur.execute(s)
            for s in self.script.statements:
                if s.section not in ("preamble", "roles"):
                    cur.execute(s.sql)

    def pre_existing_sql(self) -> list[str]:
        """What the brownfield database holds before the script runs: the adopted tables (D45),
        created from their declared columns as a stand-in for the real ones, and on each the kind
        of policies a hand-written attempt leaves behind (D51): a permissive one that admits
        everything, under a name that is not an identifier; a restrictive one that refuses every
        insert; and one under the name of a policy the spec declares.  The compiled script never
        creates the tables and must replace the policies."""
        from mls.compile.sql import q
        from mls.compile.table import table_ddl
        from mls.props.effective import all_policies

        out = []
        for name, t in self.spec.tables.items():
            if t.mode != "adopted":
                continue
            out += table_ddl(name, t)
            out.append(f'create policy "Enable access for all users" on {q(name)} as permissive for all '
                       f"to public using (true) with check (true)")
            out.append(f"create policy legacy_no_inserts on {q(name)} as restrictive for insert to public "
                       f"with check (false)")
            declared = sorted(p.name for p in all_policies(self.spec) if p.table == name)
            if declared:
                out.append(f"create policy {q(declared[0])} on {q(name)} as permissive for select "
                           f"to public using (true)")
        return out

    def attach(self) -> None:
        """Connect to an already deployed database (e.g. after a migration) without redeploying."""
        self.conn = psycopg.connect(psycopg.conninfo.make_conninfo(self.dsn, dbname=self.dbname),
                                    autocommit=True)

    def close(self, drop: bool = True) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        if drop:
            with psycopg.connect(self.dsn, autocommit=True) as admin:
                admin.execute(psql.SQL("drop database if exists {}").format(_ident(self.dbname)))

    def load(self, ds: Dataset) -> None:
        assert self.conn is not None
        with self.conn.cursor() as cur:
            for t in reversed(table_order(self.spec)):
                cur.execute(psql.SQL("delete from {}").format(_ident(t)))
            for t in table_order(self.spec):
                for r in ds.tables.get(t, []):
                    try:
                        self._insert(cur, t, r)
                    except psycopg.Error as e:
                        raise LoadError(f"owner could not load a row the model accepts: {t} {r}: {e}") from e

    def _insert(self, cur, table: str, row: Row) -> None:
        cols = list(row)
        cur.execute(psql.SQL("insert into {} ({}) values ({})").format(
            _ident(table), psql.SQL(", ").join(map(_ident, cols)),
            psql.SQL(", ").join(psql.Placeholder() for _ in cols)), [row[c] for c in cols])

    # --- one statement under a context, state restored afterwards
    def _begin_ctx(self, cur, role: str, ctx: dict) -> None:
        cur.execute("begin")
        for s in self.adapter.harness_context_sql(role, ctx):
            cur.execute(s)

    def _granted_cols(self, role: str, table: str, cmd: str) -> list[str]:
        """Columns the statement touches: the granted ones, or every column when nothing is granted
        (the statement then errors, which the oracle predicts)."""
        g = privileges(self.spec).get((role, table), {}).get(cmd)
        if g is None or g == "*":
            return list(self.spec.tables[table].columns)
        return sorted(g)

    def pk_cols(self, table: str) -> list[str]:
        t = self.spec.tables[table]
        if t.primary_key:
            return list(t.primary_key)
        if t.unique:
            return list(t.unique[0])
        return list(t.columns)

    def run_select(self, cur, role: str, ctx: dict, table: str, ds: Dataset, res: DiffResult,
                   cols: list[str] | None = None) -> None:
        cols = cols or self._granted_cols(role, table, "select")
        self._begin_ctx(cur, role, ctx)
        try:
            cur.execute(psql.SQL("select {} from {}").format(psql.SQL(", ").join(map(_ident, cols)), _ident(table)))
            pg = sorted(tuple(_norm(v) for v in r) for r in cur.fetchall())
            got = "ok"
        except psycopg.Error as e:
            got = classify_error(e)
            pg = None
        finally:
            cur.execute("rollback")
        db = self.db(ds)
        want = self.oracle.select_outcome(role, table, cols)
        res.statements += 1
        res.outcomes[got] += 1
        if got != want:
            res.mismatches.append(Mismatch("select", table, role, ctx, {"cols": cols}, got, want))
            return
        if got == "ok":
            model = sorted(tuple(_norm(r[c]) for c in cols) for r in ds.tables[table]
                           if self.oracle.visible(role, r, ctx, db, table))
            res.visible_rows += len(pg)
            if pg != model:
                res.mismatches.append(Mismatch("select", table, role, ctx, {"cols": cols},
                                               {"pg_only": _diff(pg, model), "model_only": _diff(model, pg)},
                                               "visible rows differ"))

    def run_insert(self, cur, role: str, ctx: dict, table: str, new: Row, ds: Dataset, res: DiffResult,
                   probe: bool = False) -> None:
        cols = list(self.spec.tables[table].columns) if probe else self._granted_cols(role, table, "insert")
        row = {c: new[c] for c in cols}
        self._begin_ctx(cur, role, ctx)
        try:
            self._insert(cur, table, row)
            got = "ok"
        except psycopg.Error as e:
            got = classify_error(e)
        finally:
            cur.execute("rollback")
        full = {c: row.get(c) for c in self.spec.tables[table].columns}
        want = self.oracle.insert(role, table, full, ctx, self.db(ds), cols)
        self._record(res, "insert", table, role, ctx, {"row": row}, got, want)

    def run_update(self, cur, role: str, ctx: dict, table: str, old: Row, new: Row, ds: Dataset, res: DiffResult,
                   probe: bool = False) -> None:
        cols = list(self.spec.tables[table].columns) if probe else self._granted_cols(role, table, "update")
        pk = self.pk_cols(table)
        set_cols = [c for c in cols if new[c] != old[c]] or cols[:1]
        self._begin_ctx(cur, role, ctx)
        try:
            cur.execute(psql.SQL("update {} set {} where {}").format(
                _ident(table),
                psql.SQL(", ").join(psql.SQL("{} = {}").format(_ident(c), psql.Placeholder()) for c in set_cols),
                psql.SQL(" and ").join(psql.SQL("{} = {}").format(_ident(c), psql.Placeholder()) for c in pk)),
                [new[c] for c in set_cols] + [old[c] for c in pk])
            got = "ok" if cur.rowcount == 1 else "filtered"
        except psycopg.Error as e:
            got = classify_error(e)
        finally:
            cur.execute("rollback")
        new_full = dict(old)
        new_full.update({c: new[c] for c in set_cols})
        want = self.oracle.update(role, table, old, new_full, ctx, self.db(ds), set_cols, pk)
        self._record(res, "update", table, role, ctx, {"old": old, "set": {c: new[c] for c in set_cols}}, got, want)

    def run_delete(self, cur, role: str, ctx: dict, table: str, old: Row, ds: Dataset, res: DiffResult) -> None:
        pk = self.pk_cols(table)
        self._begin_ctx(cur, role, ctx)
        try:
            cur.execute(psql.SQL("delete from {} where {}").format(
                _ident(table),
                psql.SQL(" and ").join(psql.SQL("{} = {}").format(_ident(c), psql.Placeholder()) for c in pk)),
                [old[c] for c in pk])
            got = "ok" if cur.rowcount == 1 else "filtered"
        except psycopg.Error as e:
            got = classify_error(e)
        finally:
            cur.execute("rollback")
        want = self.oracle.delete(role, table, old, ctx, self.db(ds), pk)
        self._record(res, "delete", table, role, ctx, {"old": old}, got, want)

    def _record(self, res, op, table, role, ctx, detail, got, want) -> None:
        res.statements += 1
        res.outcomes[got] += 1
        if got != want:
            res.mismatches.append(Mismatch(op, table, role, ctx, detail, got, want))

    # --- the full differential run
    def differential(self, ds: Dataset, rnd: random.Random, n_contexts: int = 12, ops_per_context: int = 20,
                     gen: ValueGen | None = None) -> DiffResult:
        assert self.conn is not None
        gen = gen or ValueGen(self.spec, self.adapter, rnd)
        res = DiffResult()
        with self.conn.cursor() as cur:
            for _ in range(n_contexts):
                role = rnd.choice(self.spec.roles)
                ctx = gen.context(role)
                for table in self.spec.governed():
                    self.run_select(cur, role, ctx, table, ds, res)
                    all_cols = list(self.spec.tables[table].columns)
                    if self._granted_cols(role, table, "select") != all_cols:
                        self.run_select(cur, role, ctx, table, ds, res, all_cols)  # probe column grants
                # statements go mostly to tables the role can write, and mostly to rows it can see
                writable = [t for t in self.spec.governed()
                            if any(c in privileges(self.spec).get((role, t), {}) for c in ("insert", "update", "delete"))]
                db = self.db(ds)
                for _ in range(ops_per_context):
                    table = rnd.choice(writable) if writable and rnd.random() < 0.8 else rnd.choice(list(self.spec.governed()))
                    rows = ds.tables.get(table, [])
                    visible = [r for r in rows if self.oracle.visible(role, r, ctx, db, table)]
                    pick = (rnd.choice(visible) if visible and rnd.random() < 0.7 else rnd.choice(rows)) if rows else None
                    op = rnd.choice(["insert", "update", "update", "delete"])
                    probe = rnd.random() < 0.2  # sometimes touch every column to probe column grants
                    if op == "insert":
                        new = gen.row(table) if pick is None or rnd.random() < 0.5 else _mutate(rnd, gen, table, pick)
                        self.run_insert(cur, role, ctx, table, new, ds, res, probe)
                    elif pick is None:
                        continue
                    elif op == "update":
                        self.run_update(cur, role, ctx, table, pick, _mutate(rnd, gen, table, pick), ds, res, probe)
                    else:
                        self.run_delete(cur, role, ctx, table, pick, ds, res)
        return res


def _mutate(rnd: Chooser, gen: ValueGen, table: str, row: Row) -> Row:
    new = dict(row)
    t = gen.spec.tables[table]
    for c, col in t.columns.items():
        if rnd.random() < 0.4:
            new[c] = gen.value(col.type, col.nullable)
    return new


def _norm(v: Any) -> Any:
    if isinstance(v, dt.datetime):
        return v.astimezone(dt.timezone.utc).replace(microsecond=0)
    if hasattr(v, "hex") and not isinstance(v, (bytes, str)):  # uuid.UUID
        return str(v)
    return v


def _diff(a, b) -> list:
    bs = set(b)
    return [x for x in a if x not in bs][:5]


def run_differential(spec: Spec, script: Script, dsn: str, dbname: str, seed: int = 0,
                     rows_per_table: int = 25, n_contexts: int = 12, ops_per_context: int = 20,
                     keep: bool = False, opaque_impl: dict[str, Any] | None = None,
                     harness: "Harness | None" = None, rnd: Chooser | None = None,
                     null_p: float = 0.3) -> DiffResult:
    """Deploy (unless a deployed harness is given), load a generated dataset, run the differential.
    A load failure (the owner cannot store a row the model accepts) counts as a mismatch."""
    rnd = rnd or random.Random(seed)
    h = harness or Harness(spec, script, dsn, dbname, opaque_impl)
    try:
        if harness is None:
            h.deploy()
        gen = ValueGen(spec, h.adapter, rnd, null_p=null_p)
        ds = generate_dataset(spec, h.adapter, rnd, rows_per_table, gen)
        try:
            h.load(ds)
        except LoadError as e:
            res = DiffResult()
            res.mismatches.append(Mismatch("load", "*", "owner", {}, {"error": str(e)}, "error", "ok"))
            return res
        return h.differential(ds, rnd, n_contexts, ops_per_context, gen)
    finally:
        if harness is None:
            h.close(drop=not keep)
