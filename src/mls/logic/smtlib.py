"""SMT-LIB2 printer for the term language (used for cvc5 cross-checks and for proof dumps)."""
from __future__ import annotations

from mls.logic.term import (
    Add, And, App, BoolVal, Eq, Forall, FunDecl, Iff, Implies, IntVal, Le, Lt, Not, Or, Term, Var,
    collect,
)


def _sym(name: str) -> str:
    """Quote symbols that are not simple SMT-LIB symbols."""
    ok = all(c.isalnum() or c in "_-+*/<>=~!@$%^&?." for c in name) and not name[0].isdigit()
    return name if ok else f"|{name}|"


def term_to_smt(t: Term) -> str:
    if isinstance(t, Var):
        return _sym(t.name)
    if isinstance(t, IntVal):
        return str(t.value) if t.value >= 0 else f"(- {-t.value})"
    if isinstance(t, BoolVal):
        return "true" if t.value else "false"
    if isinstance(t, App):
        if not t.args:
            return _sym(t.fn.name)
        return f"({_sym(t.fn.name)} {' '.join(term_to_smt(a) for a in t.args)})"
    if isinstance(t, Not):
        return f"(not {term_to_smt(t.arg)})"
    if isinstance(t, And):
        return f"(and {' '.join(term_to_smt(a) for a in t.args)})"
    if isinstance(t, Or):
        return f"(or {' '.join(term_to_smt(a) for a in t.args)})"
    if isinstance(t, Implies):
        return f"(=> {term_to_smt(t.a)} {term_to_smt(t.b)})"
    if isinstance(t, Iff):
        return f"(= {term_to_smt(t.a)} {term_to_smt(t.b)})"
    if isinstance(t, Eq):
        return f"(= {term_to_smt(t.a)} {term_to_smt(t.b)})"
    if isinstance(t, Lt):
        return f"(< {term_to_smt(t.a)} {term_to_smt(t.b)})"
    if isinstance(t, Le):
        return f"(<= {term_to_smt(t.a)} {term_to_smt(t.b)})"
    if isinstance(t, Add):
        return f"(+ {term_to_smt(t.a)} {term_to_smt(t.b)})"
    if isinstance(t, Forall):
        binders = " ".join(f"({_sym(v.name)} {v.sort.value})" for v in t.vars)
        return f"(forall ({binders}) {term_to_smt(t.body)})"
    raise TypeError(t)


def problem_to_smt(assertions: list[Term], logic: str = "ALL", check_sat: bool = True) -> str:
    vars: dict[str, Var] = {}
    funs: dict[str, FunDecl] = {}
    for a in assertions:
        collect(a, vars, funs)
    lines = [f"(set-logic {logic})"]
    for f in sorted(funs.values(), key=lambda f: f.name):
        args = " ".join(s.value for s in f.arg_sorts)
        lines.append(f"(declare-fun {_sym(f.name)} ({args}) {f.ret.value})")
    for v in sorted(vars.values(), key=lambda v: v.name):
        lines.append(f"(declare-const {_sym(v.name)} {v.sort.value})")
    for a in assertions:
        lines.append(f"(assert {term_to_smt(a)})")
    if check_sat:
        lines.append("(check-sat)")
    return "\n".join(lines) + "\n"
