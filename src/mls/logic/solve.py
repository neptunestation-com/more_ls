"""Proof entry point: `assumptions ∧ ¬goal` is UNSAT  ⇔  the goal is valid under the assumptions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mls.logic.smtlib import problem_to_smt
from mls.logic.term import Term, not_
from mls.logic.z3_backend import Z3Model, z3_check


@dataclass
class Model:
    z3: Z3Model

    def var(self, name: str, sort) -> Any:
        return self.z3.var(name, sort)

    def app(self, f, *args) -> Any:
        return self.z3.app(f, *args)

    def int_values(self) -> set[int]:
        return self.z3.int_values()


@dataclass
class SolveResult:
    status: str  # "proved" | "refuted" | "unknown"
    model: Model | None
    assertions: list[Term]

    @property
    def proved(self) -> bool:
        return self.status == "proved"

    def smtlib(self) -> str:
        return problem_to_smt(self.assertions)


def solve(assumptions: list[Term], goal: Term, timeout_ms: int = 20000) -> SolveResult:
    assertions = [*assumptions, not_(goal)]
    status, model = z3_check(assertions, timeout_ms)
    if status == "unsat":
        return SolveResult("proved", None, assertions)
    if status == "sat":
        return SolveResult("refuted", Model(model), assertions)
    return SolveResult("unknown", None, assertions)
