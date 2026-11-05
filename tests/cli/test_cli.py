from __future__ import annotations

import json

from mls.cli import main
from tests.conftest import EXAMPLE, ORG_TREE, ROOT


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
    assert json.loads((tmp_path / "r.json").read_text())["summary"]["PROVED"] == 10
    assert main(["compile", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "create policy \"member_read\"" in out and "force row level security" in out


def test_explain_terse(capsys):
    assert main(["explain", str(ORG_TREE), "--terse"]) == 0
    out = capsys.readouterr().out
    assert "R1 policy files: authenticated select permissive using in_group(org, @uid, group_id)\n" in out
    assert main(["explain", str(ORG_TREE)]) == 0
    assert "# What the rules say" in capsys.readouterr().out


def test_schema_and_lint(capsys):
    assert main(["schema"]) == 0
    assert "$defs" in json.loads(capsys.readouterr().out)
    assert main(["lint", str(EXAMPLE)]) == 0


def test_eval_cases(capsys):
    assert main(["eval", str(ROOT / "evals" / "cases")]) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out and out.count("ok:") >= 3


def test_explain_diff_migrate(tmp_path, capsys):
    from tests.templates.specs import V2

    assert main(["explain", str(EXAMPLE)]) == 0
    assert "# What the rules say" in capsys.readouterr().out
    assert main(["diff", str(EXAMPLE), str(EXAMPLE)]) == 0
    assert main(["diff", str(EXAMPLE), str(V2), "--format", "json", "--out", str(tmp_path / "d.json")]) == 2
    assert json.loads((tmp_path / "d.json").read_text())["summary"]["access_changes"] == 2
    assert main(["migrate", str(EXAMPLE), str(V2)]) == 0
    out = capsys.readouterr().out
    assert "phase 4: validate" in out
    # v2's policy on owner_id needs no index of its own: v1's inclusion index (owner_id, tenant_id) covers it
    assert "mls_ix_docs_owner_id" not in out
    assert main(["migrate", str(V2), str(EXAMPLE), "--format", "json"]) == 0
    assert "destructive statements omitted" in json.loads(capsys.readouterr().out)["notes"][0]


def test_org_tree_example(tmp_path, capsys):
    assert main(["check", str(ORG_TREE)]) == 0
    assert main(["prove", str(ORG_TREE), "--format", "json", "--out", str(tmp_path / "r.json")]) == 0
    r = json.loads((tmp_path / "r.json").read_text())
    assert r["summary"]["PROVED"] == 7 and any("READ COMMITTED" in a for a in r["assumptions"])
    assert main(["compile", str(ORG_TREE)]) == 0
    out = capsys.readouterr().out
    assert 'create function "mls_reach_org"' in out and 'create role "mls_internal"' in out
    assert main(["explain", str(ORG_TREE)]) == 0
    assert "# Group hierarchies" in capsys.readouterr().out
