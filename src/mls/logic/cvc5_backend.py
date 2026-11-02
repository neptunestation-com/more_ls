"""cvc5 cross-check: feed the SMT-LIB2 text of a problem to cvc5 (Python package) and return
sat / unsat / unknown.  Used in CI to confirm Z3's verdicts (D19)."""
from __future__ import annotations

import re

from mls.logic.smtlib import problem_to_smt
from mls.logic.term import Term


def cvc5_available() -> bool:
    try:
        import cvc5  # noqa: F401
        return True
    except ImportError:
        return False


def _run(smt: str, timeout_ms: int, options: dict[str, str]) -> str:
    import cvc5

    tm = cvc5.TermManager()
    solver = cvc5.Solver(tm)
    solver.setOption("tlimit-per", str(timeout_ms))
    for k, v in options.items():
        solver.setOption(k, v)
    sm = cvc5.SymbolManager(tm)
    parser = cvc5.InputParser(solver, sm)
    parser.setStringInput(cvc5.InputLanguage.SMT_LIB_2_6, smt, "mls")
    result = "unknown"
    while True:
        cmd = parser.nextCommand()
        if cmd.isNull():
            break
        out = cmd.invoke(solver, sm).strip()
        if out in ("sat", "unsat", "unknown"):
            result = out
    return result


def cvc5_check(assertions: list[Term], timeout_ms: int = 20000) -> str:
    smt = problem_to_smt(assertions)
    # EPR problems with satisfiable quantified assumptions need model-based instantiation and
    # finite model finding; the cardinality obligations quantify over Int (LIA + UF), where
    # finite-model-find answers unknown, so those fall back to plain mbqi
    quantifies_int = re.search(r"\(forall \(\([^()]* Int\)", smt) is not None
    order = ([{"mbqi": "true"}, {"mbqi": "true", "finite-model-find": "true"}] if quantifies_int
             else [{"mbqi": "true", "finite-model-find": "true"}, {"mbqi": "true"}])
    result = "unknown"
    for options in order:
        result = _run(smt, timeout_ms, options)
        if result != "unknown":
            break
    return result
