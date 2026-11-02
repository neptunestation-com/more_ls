"""A small solver-agnostic term language: quantifier-free / EPR + linear integer arithmetic +
uninterpreted functions (invariant 6).  Backends: Z3 (logic/z3_backend.py) and SMT-LIB2 text
(logic/smtlib.py) for cvc5 cross-checks.

Only what the encodings need: Int/Bool sorts, variables, integer and boolean literals,
uninterpreted functions, boolean connectives, comparisons, addition, and universal quantifiers.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Union


class Sort(str, Enum):
    INT = "Int"
    BOOL = "Bool"


@dataclass(frozen=True)
class FunDecl:
    name: str
    arg_sorts: tuple[Sort, ...]
    ret: Sort

    def __call__(self, *args: "Term") -> "App":
        assert len(args) == len(self.arg_sorts), (self.name, args)
        return App(self, tuple(args))


@dataclass(frozen=True)
class Var:
    name: str
    sort: Sort


@dataclass(frozen=True)
class IntVal:
    value: int


@dataclass(frozen=True)
class BoolVal:
    value: bool


@dataclass(frozen=True)
class App:
    fn: FunDecl
    args: tuple["Term", ...]


@dataclass(frozen=True)
class Not:
    arg: "Term"


@dataclass(frozen=True)
class And:
    args: tuple["Term", ...]


@dataclass(frozen=True)
class Or:
    args: tuple["Term", ...]


@dataclass(frozen=True)
class Implies:
    a: "Term"
    b: "Term"


@dataclass(frozen=True)
class Iff:
    a: "Term"
    b: "Term"


@dataclass(frozen=True)
class Eq:
    a: "Term"
    b: "Term"


@dataclass(frozen=True)
class Lt:
    a: "Term"
    b: "Term"


@dataclass(frozen=True)
class Le:
    a: "Term"
    b: "Term"


@dataclass(frozen=True)
class Add:
    a: "Term"
    b: "Term"


@dataclass(frozen=True)
class Forall:
    vars: tuple[Var, ...]
    body: "Term"


Term = Union[Var, IntVal, BoolVal, App, Not, And, Or, Implies, Iff, Eq, Lt, Le, Add, Forall]

TRUE = BoolVal(True)
FALSE = BoolVal(False)


# ----------------------------------------------------------------------------- smart constructors
def and_(*args: Term) -> Term:
    flat: list[Term] = []
    for a in args:
        if isinstance(a, And):
            flat.extend(a.args)
        elif a == TRUE:
            continue
        elif a == FALSE:
            return FALSE
        else:
            flat.append(a)
    if not flat:
        return TRUE
    if len(flat) == 1:
        return flat[0]
    return And(tuple(flat))


def or_(*args: Term) -> Term:
    flat: list[Term] = []
    for a in args:
        if isinstance(a, Or):
            flat.extend(a.args)
        elif a == FALSE:
            continue
        elif a == TRUE:
            return TRUE
        else:
            flat.append(a)
    if not flat:
        return FALSE
    if len(flat) == 1:
        return flat[0]
    return Or(tuple(flat))


def not_(a: Term) -> Term:
    if a == TRUE:
        return FALSE
    if a == FALSE:
        return TRUE
    if isinstance(a, Not):
        return a.arg
    return Not(a)


def implies(a: Term, b: Term) -> Term:
    if a == TRUE:
        return b
    if a == FALSE or b == TRUE:
        return TRUE
    return Implies(a, b)


def iff(a: Term, b: Term) -> Term:
    return Iff(a, b)


def eq(a: Term, b: Term) -> Term:
    return Eq(a, b)


def lt(a: Term, b: Term) -> Term:
    return Lt(a, b)


def le(a: Term, b: Term) -> Term:
    return Le(a, b)


def gt(a: Term, b: Term) -> Term:
    return Lt(b, a)


def ge(a: Term, b: Term) -> Term:
    return Le(b, a)


def add(a: Term, b: Term) -> Term:
    return Add(a, b)


def forall(vars: Iterable[Var], body: Term) -> Term:
    vs = tuple(vars)
    if not vs:
        return body
    return Forall(vs, body)


def int_var(name: str) -> Var:
    return Var(name, Sort.INT)


def bool_var(name: str) -> Var:
    return Var(name, Sort.BOOL)


def fun(name: str, *arg_sorts: Sort, ret: Sort = Sort.BOOL) -> FunDecl:
    return FunDecl(name, tuple(arg_sorts), ret)


# ----------------------------------------------------------------------------- traversal
def collect(term: Term, vars: dict[str, Var], funs: dict[str, FunDecl], bound: frozenset[str] = frozenset()) -> None:
    """Collect free variables and function symbols."""
    if isinstance(term, Var):
        if term.name not in bound:
            prev = vars.get(term.name)
            if prev is not None and prev.sort != term.sort:
                raise ValueError(f"variable {term.name} used with two sorts")
            vars[term.name] = term
    elif isinstance(term, (IntVal, BoolVal)):
        pass
    elif isinstance(term, App):
        prev = funs.get(term.fn.name)
        if prev is not None and prev != term.fn:
            raise ValueError(f"function {term.fn.name} declared twice with different signatures")
        funs[term.fn.name] = term.fn
        for a in term.args:
            collect(a, vars, funs, bound)
    elif isinstance(term, Not):
        collect(term.arg, vars, funs, bound)
    elif isinstance(term, (And, Or)):
        for a in term.args:
            collect(a, vars, funs, bound)
    elif isinstance(term, (Implies, Iff, Eq, Lt, Le, Add)):
        collect(term.a, vars, funs, bound)
        collect(term.b, vars, funs, bound)
    elif isinstance(term, Forall):
        collect(term.body, vars, funs, bound | {v.name for v in term.vars})
    else:
        raise TypeError(term)


def has_quantifier(term: Term) -> bool:
    if isinstance(term, Forall):
        return True
    if isinstance(term, App):
        return any(has_quantifier(a) for a in term.args)
    if isinstance(term, Not):
        return has_quantifier(term.arg)
    if isinstance(term, (And, Or)):
        return any(has_quantifier(a) for a in term.args)
    if isinstance(term, (Implies, Iff, Eq, Lt, Le, Add)):
        return has_quantifier(term.a) or has_quantifier(term.b)
    return False
