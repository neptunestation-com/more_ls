from __future__ import annotations

import json

from mls.cli import main
from tests.conftest import EXAMPLE, ROOT


def test_check_ok(capsys):
    assert main(["check", str(EXAMPLE)]) == 0
    assert "OK" in capsys.readouterr().out


def test_check_errors_json(tmp_path, capsys):
    d = json.loads(EXAMPLE.read_text())
    d["rules"][0]["using"]["and"][1]["cmp"][0] = {"col": "nope"}
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(d))
    assert main(["check", str(p), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["errors"][0]["path"] == "$.rules[0].using.and[1].cmp[0]"


def test_prove_and_compile(tmp_path, capsys):
    assert main(["prove", str(EXAMPLE), "--format", "json", "--out", str(tmp_path / "r.json")]) == 0
    assert json.loads((tmp_path / "r.json").read_text())["summary"]["PROVED"] == 6
    assert main(["compile", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "create policy \"member_read\"" in out and "force row level security" in out


def test_schema_and_lint(capsys):
    assert main(["schema"]) == 0
    assert "$defs" in json.loads(capsys.readouterr().out)
    assert main(["lint", str(EXAMPLE)]) == 0


def test_eval_cases(capsys):
    assert main(["eval", str(ROOT / "evals" / "cases")]) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out and out.count("ok:") >= 3
