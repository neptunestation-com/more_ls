"""cvc5 cross-check: feed the SMT-LIB2 text of a problem to cvc5 (Python package) and return
sat / unsat / unknown.  Used in CI to confirm Z3's verdicts (D19)."""
from __future__ import annotations

from mls.logic.smtlib import problem_to_smt
from mls.logic.term import Term


def cvc5_available() -> bool:
    try:
        import cvc5  # noqa: F401
        return True
    except ImportError:
        return False


def cvc5_check(assertions: list[Term], timeout_ms: int = 20000) -> str:
    import cvc5

    tm = cvc5.TermManager()
    solver = cvc5.Solver(tm)
    solver.setOption("tlimit-per", str(timeout_ms))
    # EPR problems with satisfiable quantified assumptions need model-based instantiation
    solver.setOption("mbqi", "true")
    solver.setOption("finite-model-find", "true")
    sm = cvc5.SymbolManager(tm)
    parser = cvc5.InputParser(solver, sm)
    parser.setStringInput(cvc5.InputLanguage.SMT_LIB_2_6, problem_to_smt(assertions), "mls")
    result = "unknown"
    while True:
        cmd = parser.nextCommand()
        if cmd.isNull():
            break
        out = cmd.invoke(solver, sm).strip()
        if out in ("sat", "unsat", "unknown"):
            result = out
    return result
