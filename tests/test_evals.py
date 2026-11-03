"""The evaluation corpus (D6): every seed and synthetic case grades its reference spec as
expected; the seed generator is deterministic; synthetic cases are solver-graded mutants."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from mls.evals import case_files, load_case, run_cases
from mls.llm.grade import expected_status
from tests.conftest import ROOT

CASES = ROOT / "evals" / "cases"


def test_corpus_size_and_shape():
    files = case_files([CASES])
    seed = [f for f in files if load_case(f).get("origin", "seed") == "seed"]
    synthetic = [f for f in files if load_case(f).get("origin") == "synthetic"]
    assert len(seed) >= 30 and len(synthetic) >= 50
    for f in files:
        c = load_case(f)
        assert c["id"] == f.stem and c["requirements"]
        assert c.get("expected_verdicts") or c.get("expected_check_errors")
        assert expected_status(c) in ("proved", "escalated")
        if c.get("origin") == "synthetic":
            assert c["base_case"] and c["mutation"] and c["expected_loop"]["status"] == "proved"
            assert isinstance(c["reference_spec"], dict) and c["start_rules"] and c["mutant_verdicts"]
            assert c["mutant_verdicts"] != c["expected_verdicts"]  # the mutation is detected


@pytest.mark.parametrize("path", case_files([CASES]), ids=lambda p: p.stem)
def test_case_grades_as_expected(path):
    r = run_cases([path])[0]
    assert r.ok, r.message


def test_seed_generator_is_deterministic(tmp_path):
    """Regenerating the seed cases into a scratch directory reproduces the committed files."""
    src = (ROOT / "evals" / "make_cases.py").read_text()
    src = src.replace('ROOT = Path(__file__).resolve().parent.parent', f'ROOT = Path({str(ROOT)!r})')
    src = src.replace('OUT = ROOT / "evals" / "cases"', f'OUT = Path({str(tmp_path)!r})')
    script = tmp_path / "make_cases.py"
    script.write_text(src)
    (tmp_path / "good_tenant_docs.json").write_text((CASES / "good_tenant_docs.json").read_text())
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
    for f in tmp_path.glob("*.json"):
        if f.name == "good_tenant_docs.json":
            continue
        assert json.loads(f.read_text()) == json.loads((CASES / f.name).read_text()), f.name
        spec = tmp_path / "specs" / f"{f.stem}.mls.json"
        assert json.loads(spec.read_text()) == json.loads((CASES / "specs" / spec.name).read_text()), spec.name
