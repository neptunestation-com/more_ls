"""Z3 verdicts are reproduced by cvc5 from the SMT-LIB2 text (D19)."""
from __future__ import annotations

import pytest

from mls.logic.cvc5_backend import cvc5_available, cvc5_check
from mls.logic.smtlib import problem_to_smt
from mls.logic.solve import solve
from mls.logic.term import Sort, forall, fun, implies, int_var
from mls.props.obligations import obligations_for


def test_smtlib_roundtrip_declarations():
    r = fun("rel.m", Sort.INT, Sort.INT)
    x, y = int_var("a"), int_var("b")
    text = problem_to_smt([forall([x, y], implies(r(x, y), r(y, x))), r(int_var("ctx.uid"), int_var("r.t"))])
    assert "(declare-fun rel.m (Int Int) Bool)" in text
    assert "(declare-const ctx.uid Int)" in text
    assert text.strip().endswith("(check-sat)")


@pytest.mark.cvc5
@pytest.mark.skipif(not cvc5_available(), reason="cvc5 not installed")
def test_cvc5_agrees_with_z3_on_example(example_spec):
    n = 0
    for prop in example_spec.properties:
        for ob in obligations_for(example_spec, prop):
            if ob.propositional:
                continue
            res = solve(ob.assumptions, ob.goal)
            other = cvc5_check(res.assertions)
            assert other == {"proved": "unsat", "refuted": "sat"}[res.status], ob.name
            n += 1
    assert n > 10
