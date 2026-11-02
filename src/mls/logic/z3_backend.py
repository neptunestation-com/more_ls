"""Z3 backend for the term language."""
from __future__ import annotations

import z3

from mls.logic.term import (
    Add, And, App, BoolVal, Eq, Forall, FunDecl, Iff, Implies, IntVal, Le, Lt, Not, Or, Sort, Term,
    Var,
)


def _sort(s: Sort):
    return z3.IntSort() if s == Sort.INT else z3.BoolSort()


class Z3Translator:
    def __init__(self):
        self.funs: dict[str, z3.FuncDeclRef] = {}
        self.vars: dict[str, z3.ExprRef] = {}

    def fun(self, f: FunDecl) -> z3.FuncDeclRef:
        d = self.funs.get(f.name)
        if d is None:
            d = z3.Function(f.name, *[_sort(s) for s in f.arg_sorts], _sort(f.ret))
            self.funs[f.name] = d
        return d

    def var(self, v: Var) -> z3.ExprRef:
        e = self.vars.get(v.name)
        if e is None:
            e = z3.Int(v.name) if v.sort == Sort.INT else z3.Bool(v.name)
            self.vars[v.name] = e
        return e

    def tr(self, t: Term, bound: dict[str, z3.ExprRef] | None = None):
        bound = bound or {}
        if isinstance(t, Var):
            if t.name in bound:
                return bound[t.name]
            return self.var(t)
        if isinstance(t, IntVal):
            return z3.IntVal(t.value)
        if isinstance(t, BoolVal):
            return z3.BoolVal(t.value)
        if isinstance(t, App):
            d = self.fun(t.fn)
            return d(*[self.tr(a, bound) for a in t.args]) if t.args else d()
        if isinstance(t, Not):
            return z3.Not(self.tr(t.arg, bound))
        if isinstance(t, And):
            return z3.And(*[self.tr(a, bound) for a in t.args])
        if isinstance(t, Or):
            return z3.Or(*[self.tr(a, bound) for a in t.args])
        if isinstance(t, Implies):
            return z3.Implies(self.tr(t.a, bound), self.tr(t.b, bound))
        if isinstance(t, (Iff, Eq)):
            return self.tr(t.a, bound) == self.tr(t.b, bound)
        if isinstance(t, Lt):
            return self.tr(t.a, bound) < self.tr(t.b, bound)
        if isinstance(t, Le):
            return self.tr(t.a, bound) <= self.tr(t.b, bound)
        if isinstance(t, Add):
            return self.tr(t.a, bound) + self.tr(t.b, bound)
        if isinstance(t, Forall):
            inner = dict(bound)
            zs = []
            for v in t.vars:
                zv = z3.Const(f"{v.name}!b{len(inner)}", _sort(v.sort))
                inner[v.name] = zv
                zs.append(zv)
            return z3.ForAll(zs, self.tr(t.body, inner))
        raise TypeError(t)


class Z3Model:
    """Thin wrapper: evaluate variables and function applications in a Z3 model."""

    def __init__(self, tr: Z3Translator, model: z3.ModelRef):
        self.tr = tr
        self.model = model

    def var(self, name: str, sort: Sort):
        e = self.tr.vars.get(name)
        if e is None:
            return None
        v = self.model.eval(e, model_completion=True)
        return v.as_long() if sort == Sort.INT else z3.is_true(v)

    def app(self, f: FunDecl, *args: int | bool):
        d = self.tr.fun(f)
        zargs = [z3.IntVal(a) if s == Sort.INT else z3.BoolVal(a) for a, s in zip(args, f.arg_sorts)]
        v = self.model.eval(d(*zargs), model_completion=True)
        return v.as_long() if f.ret == Sort.INT else z3.is_true(v)

    def int_values(self) -> set[int]:
        """Every integer that appears as a variable value (the universe of the counterexample)."""
        out = set()
        for d in self.model.decls():
            if d.arity() == 0:
                v = self.model[d]
                if z3.is_int_value(v):
                    out.add(v.as_long())
        return out


def z3_check(assertions: list[Term], timeout_ms: int = 20000) -> tuple[str, Z3Model | None]:
    tr = Z3Translator()
    s = z3.Solver()
    s.set("timeout", timeout_ms)
    for a in assertions:
        s.add(tr.tr(a))
    r = s.check()
    if r == z3.unsat:
        return "unsat", None
    if r == z3.sat:
        return "sat", Z3Model(tr, s.model())
    return "unknown", None
