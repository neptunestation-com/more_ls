"""Development artefacts never reach users: no decision number (D45), issue number (#27) or
milestone in what the tool prints (every string literal of `src/mls` that is not a docstring:
output, errors, CLI help, generated SQL, the LLM's prompts) or in the user documentation (`docs/`
outside `docs/design/`, README, the explainer page, the Claude Code skills, which are prompts too).  Comments and docstrings may cite them, and
so may `docs/design/`, which is for contributors."""
from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.conftest import ROOT

PATTERN = re.compile(r"\bD\d{1,2}\b|(?<![\w&])#\d{1,3}(?![\w-])|\bMilestones?\b|\b[Aa]mended\b")
USER_DOCS = sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md", ROOT / "index.html"] \
    + sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md"))


def _docstrings(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


def test_no_development_artefact_in_tool_output():
    found = []
    for path in sorted((ROOT / "src" / "mls").rglob("*.py")):
        tree = ast.parse(path.read_text())
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                found += [f"{path.relative_to(ROOT)}:{node.lineno}: {m.group()} in {node.value.strip()[:80]!r}"
                          for m in PATTERN.finditer(node.value)]
    assert not found, "say what it means instead of citing it:\n" + "\n".join(found)


def test_no_development_artefact_in_user_docs():
    found = [f"{path.relative_to(ROOT)}:{i}: {m.group()} in {line.strip()[:80]!r}"
             for path in USER_DOCS
             for i, line in enumerate(path.read_text().splitlines(), 1)
             for m in PATTERN.finditer(line)]
    assert not found, "user docs state the current behaviour, without its history:\n" + "\n".join(found)


def test_the_check_sees_an_artefact():
    assert PATTERN.search("(an expansion of a selector rule, D48)")
    assert PATTERN.search('"Needs" is checked (#27)')
    assert not PATTERN.search("uuid#0") and not PATTERN.search("color: #3ECF8E") and not PATTERN.search("§11")
    assert not PATTERN.search("1. [Why MLS](#1-why-mls)")  # a fragment link, not an issue
