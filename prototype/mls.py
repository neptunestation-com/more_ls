"""
mls -- MLS ("More LS"): RLS more-or-less.  A tiny DSL for a decidable fragment of
PostgreSQL access and integrity rules.

One AST, three interpretations that must agree:
  1. compile to CREATE POLICY SQL          (sql_pred / create_policy_sql)
  2. concrete SQL three-valued evaluator   (eval_pred)      -- for differential testing
  3. SMT encoding (Z3)                     (Enc / prove)    -- for theorems

The fragment (checked syntactically by check_fragment):
  * comparisons are  <column> op <session-var | constant>
  * membership is    <column> IN (select val_col from rel where key_col = <session-var | constant>)
    where <column> is NOT NULL.  Compiles to an array InitPlan => index condition.
  * and / or / not.  `not` over membership is allowed but flagged: it is an anti-join,
    not index-driven.
No per-row correlated subqueries, no user functions, no recursion.  That is exactly why the
logic stays decidable (quantifier-free + uninterpreted predicates, i.e. EPR-style).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import z3

# ----------------------------------------------------------------- AST
@dataclass(frozen=True)
class Col:   name: str
@dataclass(frozen=True)
class Ctx:   name: str          # session variable: constant per statement, never NULL
@dataclass(frozen=True)
class Const: value: int
@dataclass(frozen=True)
class Old:   name: str          # column of the pre-image (transition predicates only)
@dataclass(frozen=True)
class New:   name: str          # column of the post-image

@dataclass(frozen=True)
class Cmp:    op: str; l: object; r: object            # op in = <> < <= > >=
@dataclass(frozen=True)
class Member: rel: str; key: object; val: object       # val IN rel(key)
@dataclass(frozen=True)
class And:    args: tuple
@dataclass(frozen=True)
class Or:     args: tuple
@dataclass(frozen=True)
class Not:    arg: object
@dataclass(frozen=True)
class Lit:    value: bool
@dataclass(frozen=True)
class IsNull: arg: object                               # two-valued: never unknown

TRUE, FALSE = Lit(True), Lit(False)

def AND(*a):
    a = [x for x in a if x != TRUE]
    if FALSE in a: return FALSE
    return TRUE if not a else a[0] if len(a) == 1 else And(tuple(a))

def OR(*a):
    a = [x for x in a if x != FALSE]
    if TRUE in a: return TRUE
    return FALSE if not a else a[0] if len(a) == 1 else Or(tuple(a))

@dataclass
class Table:
    name: str
    cols: dict            # column -> nullable (bool)

@dataclass
class Rel:                # a membership relation = (key_col, val_col) projection of a table
    name: str
    table: str
    key_col: str
    val_col: str

@dataclass
class Schema:
    tables: dict
    rels: dict
    subsets: list = field(default_factory=list)   # [(sub, sup)] axioms: sub(k,v) -> sup(k,v)

@dataclass
class Policy:
    name: str
    table: str
    cmd: str                      # select | insert | update | delete | all
    kind: str = "permissive"      # permissive | restrictive
    using: Optional[object] = None
    check: Optional[object] = None

# ----------------------------------------------------------------- integrity constructs (non-RLS targets)
@dataclass
class Check:          # single-row invariant  -> CHECK ((p) IS TRUE)
    name: str; table: str; pred: object

@dataclass
class Inclusion:      # (cols) ⊆ rel, MATCH SIMPLE (any NULL passes)  -> FOREIGN KEY ... ON DELETE RESTRICT
    name: str; table: str; cols: tuple; rel: str
    def pred(self, schema):
        t = schema.tables[self.table]
        nulls = [IsNull(Col(c)) for c in self.cols if t.cols[c]]
        return OR(*nulls, Member(self.rel, Col(self.cols[0]), Col(self.cols[1])))

@dataclass
class Transition:     # two-state predicate over (old, new) for UPDATE  -> BEFORE UPDATE trigger
    name: str; table: str; pred: object

# ----------------------------------------------------------------- policy combination (PG semantics)
def effective(policies, table, cmd, which, skip=()):
    """Effective predicate for `cmd` on `table`. which = 'using' (old row) | 'check' (new row).
    Permissive policies OR together, restrictive AND, no permissive => deny.
    WITH CHECK defaults to USING (ALL / UPDATE policies)."""
    ps = [p for p in policies if p.table == table and p.cmd in (cmd, "all") and p.name not in skip]
    def expr(p):
        if which == "using": return p.using
        return p.check if p.check is not None else p.using
    perm = [expr(p) for p in ps if p.kind == "permissive" and expr(p) is not None]
    rest = [expr(p) for p in ps if p.kind == "restrictive" and expr(p) is not None]
    if not perm: return FALSE
    return AND(OR(*perm), *rest)

# ----------------------------------------------------------------- fragment check
def check_fragment(pred, schema, table):
    """Return a list of violations (empty => in the index-driven fragment)."""
    out = []
    t = schema.tables[table]
    def term_ok_rhs(x): return isinstance(x, (Ctx, Const))
    def walk(p, neg=False):
        if isinstance(p, Lit): return
        if isinstance(p, Cmp):
            if not (isinstance(p.l, Col) and term_ok_rhs(p.r)) and not (term_ok_rhs(p.l) and term_ok_rhs(p.r)):
                out.append(f"non-sargable comparison {p}")
        elif isinstance(p, Member):
            if not isinstance(p.val, Col): out.append(f"membership value must be a column: {p}")
            elif t.cols[p.val.name]: out.append(f"membership column {p.val.name} must be NOT NULL")
            if not term_ok_rhs(p.key): out.append(f"correlated membership key (per-row subquery): {p}")
            if neg: out.append(f"negated membership is an anti-join, not index-driven: {p}")
        elif isinstance(p, (And, Or)):
            for a in p.args: walk(a, neg)
        elif isinstance(p, Not):
            walk(p.arg, True)
        else:
            out.append(f"unknown node {p}")
    walk(pred)
    return out

# ----------------------------------------------------------------- SQL compiler
def _sql_term(x):
    if isinstance(x, Col):   return x.name
    if isinstance(x, Ctx):   return f"(select current_setting('app.{x.name}')::int)"
    if isinstance(x, Const): return str(x.value)
    if isinstance(x, Old):   return f"old.{x.name}"
    if isinstance(x, New):   return f"new.{x.name}"
    raise TypeError(x)

_SQLOP = {"=": "=", "<>": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">="}

def sql_pred(p, schema):
    if isinstance(p, Lit):    return "true" if p.value else "false"
    if isinstance(p, Cmp):    return f"{_sql_term(p.l)} {_SQLOP[p.op]} {_sql_term(p.r)}"
    if isinstance(p, Member):
        r = schema.rels[p.rel]
        return (f"{_sql_term(p.val)} = any (array(select {r.val_col} from {r.table} "
                f"where {r.key_col} = {_sql_term(p.key)}))")
    if isinstance(p, And):    return "(" + " and ".join(sql_pred(a, schema) for a in p.args) + ")"
    if isinstance(p, Or):     return "(" + " or ".join(sql_pred(a, schema) for a in p.args) + ")"
    if isinstance(p, Not):    return f"not ({sql_pred(p.arg, schema)})"
    if isinstance(p, IsNull): return f"{_sql_term(p.arg)} is null"
    raise TypeError(p)

def check_sql(c: Check, schema, normalize=True):
    """CHECK passes on UNKNOWN; RLS denies on UNKNOWN.  Normalizing with IS TRUE gives the DSL one
    meaning ('p holds') in both targets.  normalize=False is the naive (wrong) compilation."""
    body = sql_pred(c.pred, schema)
    return f"alter table {c.table} add constraint {c.name} check ({f'({body}) is true' if normalize else body});"

def inclusion_sql(i: Inclusion, schema):
    r = schema.rels[i.rel]
    return (f"alter table {i.table} add constraint {i.name} foreign key ({', '.join(i.cols)}) "
            f"references {r.table}({r.key_col}, {r.val_col}) on delete restrict;")

def transition_sql(tr: Transition, schema):
    fn = f"{tr.table}_{tr.name}_fn"
    return (f"create function {fn}() returns trigger language plpgsql as $$\n"
            f"begin\n"
            f"  if not (({sql_pred(tr.pred, schema)}) is true) then\n"
            f"    raise exception 'transition {tr.name} violated' using errcode = 'check_violation';\n"
            f"  end if;\n"
            f"  return new;\n"
            f"end $$;\n"
            f"create trigger {tr.name} before update on {tr.table} for each row execute function {fn}();")

def create_policy_sql(p: Policy, schema, role="app"):
    s = f"create policy {p.name} on {p.table} as {p.kind} for {p.cmd} to {role}"
    if p.using is not None: s += f" using ({sql_pred(p.using, schema)})"
    if p.check is not None: s += f" with check ({sql_pred(p.check, schema)})"
    return s + ";"

# ----------------------------------------------------------------- concrete 3VL evaluator
def eval_pred(p, row, ctx, rels):
    """SQL three-valued semantics. Returns True / False / None (unknown). Policy passes iff True."""
    def term(x):
        if isinstance(x, Col):   return row[x.name]
        if isinstance(x, Ctx):   return ctx[x.name]
        if isinstance(x, Const): return x.value
        if isinstance(x, Old):   return row["__old"][x.name]
        if isinstance(x, New):   return row["__new"][x.name]
    if isinstance(p, Lit): return p.value
    if isinstance(p, IsNull): return term(p.arg) is None
    if isinstance(p, Cmp):
        l, r = term(p.l), term(p.r)
        if l is None or r is None: return None
        return {"=": l == r, "<>": l != r, "<": l < r, "<=": l <= r, ">": l > r, ">=": l >= r}[p.op]
    if isinstance(p, Member):
        k, v = term(p.key), term(p.val)
        if k is None or v is None: return None
        return (k, v) in rels[p.rel]
    if isinstance(p, And):
        vs = [eval_pred(a, row, ctx, rels) for a in p.args]
        return False if False in vs else (None if None in vs else True)
    if isinstance(p, Or):
        vs = [eval_pred(a, row, ctx, rels) for a in p.args]
        return True if True in vs else (None if None in vs else False)
    if isinstance(p, Not):
        v = eval_pred(p.arg, row, ctx, rels)
        return None if v is None else (not v)

# ----------------------------------------------------------------- SMT encoding
class Enc:
    """Encode predicates as (T, F) pairs of Z3 booleans: T = 'is true', F = 'is false'.
    NULL/unknown is neither.  A policy admits a row iff T holds."""
    def __init__(self, schema, tags=None):
        self.schema = schema
        self.tags = tags or {}          # rel name -> database-copy tag (default 'db')
        self._fn = {}

    def rel_fn(self, name):
        tag = self.tags.get(name, "db")
        key = f"{tag}.{name}"
        if key not in self._fn:
            self._fn[key] = z3.Function(key, z3.IntSort(), z3.IntSort(), z3.BoolSort())
        return self._fn[key]

    def term(self, x, row, table):
        if isinstance(x, Col):
            nullable = self.schema.tables[table].cols[x.name]
            v = z3.Int(f"{row}.{x.name}")
            n = z3.Bool(f"{row}.{x.name}.null") if nullable else z3.BoolVal(False)
            return v, n
        if isinstance(x, Ctx):   return z3.Int(f"ctx.{x.name}"), z3.BoolVal(False)
        if isinstance(x, Const): return z3.IntVal(x.value), z3.BoolVal(False)
        if isinstance(x, (Old, New)):
            return self.term(Col(x.name), "old" if isinstance(x, Old) else "new", table)
        raise TypeError(x)

    def tf(self, p, row, table):
        if isinstance(p, Lit): return z3.BoolVal(p.value), z3.BoolVal(not p.value)
        if isinstance(p, Cmp):
            (lv, ln), (rv, rn) = self.term(p.l, row, table), self.term(p.r, row, table)
            core = {"=": lv == rv, "<>": lv != rv, "<": lv < rv, "<=": lv <= rv,
                    ">": lv > rv, ">=": lv >= rv}[p.op]
            ok = z3.Not(z3.Or(ln, rn))
            return z3.And(ok, core), z3.And(ok, z3.Not(core))
        if isinstance(p, IsNull):
            _, n = self.term(p.arg, row, table)
            return n, z3.Not(n)
        if isinstance(p, Member):
            kv, kn = self.term(p.key, row, table)
            vv, vn = self.term(p.val, row, table)
            ok = z3.Not(z3.Or(kn, vn))
            m = self.rel_fn(p.rel)(kv, vv)
            return z3.And(ok, m), z3.And(ok, z3.Not(m))
        if isinstance(p, And):
            ts, fs = zip(*[self.tf(a, row, table) for a in p.args])
            return z3.And(*ts), z3.Or(*fs)
        if isinstance(p, Or):
            ts, fs = zip(*[self.tf(a, row, table) for a in p.args])
            return z3.Or(*ts), z3.And(*fs)
        if isinstance(p, Not):
            t, f = self.tf(p.arg, row, table)
            return f, t
        raise TypeError(p)

    def T(self, p, row, table): return self.tf(p, row, table)[0]

    def axioms(self):
        u, t = z3.Ints("ax_u ax_t")
        out = []
        for sub, sup in self.schema.subsets:
            out.append(z3.ForAll([u, t], z3.Implies(self.rel_fn(sub)(u, t), self.rel_fn(sup)(u, t))))
        return out

def prove(assumptions, goal):
    """Valid iff assumptions & ~goal is UNSAT.  Returns (True, None) or (False, model)."""
    s = z3.Solver()
    s.set("timeout", 20000)
    s.add(*assumptions)
    s.add(z3.Not(goal))
    r = s.check()
    if r == z3.unsat: return True, None
    if r == z3.sat:   return False, s.model()
    raise RuntimeError("solver returned unknown (timeout)")

def show_model(m, enc=None):
    """Human-readable counterexample: scalar values, plus every relation fact over the
    integers that appear in the model (Z3 relation interpretations have a default, so we
    evaluate them explicitly instead of listing entries)."""
    scalars, funcs, ints = [], [], set()
    for d in sorted(m.decls(), key=lambda d: d.name()):
        v = m[d]
        if isinstance(v, z3.FuncInterp):
            funcs.append(d)
        else:
            if d.name().endswith(".null") and not z3.is_true(v): continue   # hide 'not null'
            scalars.append(f"  {d.name()} = {v}")
            if z3.is_int_value(v): ints.add(v.as_long())
    out = scalars
    for f in funcs:
        facts = [(a, b) for a in sorted(ints) for b in sorted(ints)
                 if z3.is_true(m.eval(f(z3.IntVal(a), z3.IntVal(b)), model_completion=True))]
        out.append(f"  {f.name()}(user, tenant) holds for {facts}")
    return "\n".join(out)
