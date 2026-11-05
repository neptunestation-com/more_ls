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
    assert "R1 policy group_read on files: authenticated select permissive using in_group(org, @uid, group_id)" in out
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


def test_terse_input_and_fmt(tmp_path, capsys):
    """Every subcommand reads `.mls` by extension (D50); `fmt` converts, and formats."""
    from tests.templates.specs import V2

    mls = EXAMPLE.with_suffix("")  # examples/tenant_docs.mls
    assert main(["check", str(mls)]) == 0 and "OK" in capsys.readouterr().out
    assert main(["prove", str(mls), "--format", "json", "--out", str(tmp_path / "r.json")]) == 0
    assert json.loads((tmp_path / "r.json").read_text())["summary"]["PROVED"] == 10
    assert main(["compile", str(mls)]) == 0 and 'create policy "member_read"' in capsys.readouterr().out
    assert main(["explain", str(mls)]) == 0 and "# What the rules say" in capsys.readouterr().out
    assert main(["diff", str(mls), str(EXAMPLE)]) == 0  # the twins are the same spec
    assert main(["diff", str(mls), str(V2), "--format", "json", "--out", str(tmp_path / "d.json")]) == 2
    capsys.readouterr()
    # fmt: to the other syntax by default, --out writes it, --to terse on .mls is the formatter
    assert main(["fmt", str(EXAMPLE)]) == 0
    assert capsys.readouterr().out == mls.read_text()
    assert main(["fmt", str(mls), "--out", str(tmp_path / "back.mls.json")]) == 0
    assert main(["check", str(tmp_path / "back.mls.json")]) == 0
    capsys.readouterr()
    assert main(["fmt", str(mls), "--to", "terse"]) == 0
    assert capsys.readouterr().out == mls.read_text()
    assert main(["fmt", str(EXAMPLE), "--to", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["rules"][0]["name"] == "member_read"
    bad = tmp_path / "bad.mls"
    bad.write_text("mls 0.1\nroles a\nrules {\n  R1 policy p on t: a select permissive using id = = 1\n}\n")
    assert main(["check", str(bad), "--json"]) == 1
    err = json.loads(capsys.readouterr().out)["errors"][0]
    assert err["code"] == "syntax" and err["span"] == {"file": str(bad), "line": 4, "col": 52,
                                                      "text": "  R1 policy p on t: a select permissive using id = = 1"}
    assert main(["check", str(bad)]) == 1
    assert f"syntax at $.rules[0] ({bad}:4:52): expected a term" in capsys.readouterr().err
    assert main(["fmt", str(bad)]) == 1
    capsys.readouterr()
    # a checker error on terse input: the JSON path, file:line:col and the offending line (#15);
    # the same spec as JSON: the same path and message, no span, the same exit code
    worse = tmp_path / "worse.mls"
    worse.write_text(mls.read_text().replace("level <= @clearance", "nope <= @clearance", 1))
    assert main(["check", str(worse)]) == 1
    err_text = capsys.readouterr().err
    assert f"error at $.rules[0].using.and[1].cmp[0] ({worse}:32:103): unknown column 'nope' of table 'docs'\n" \
           "    R1 policy member_read on docs:" in err_text and "\n  fix: one of: id, tenant_id" in err_text
    assert main(["fmt", str(worse), "--out", str(tmp_path / "worse.mls.json")]) == 0
    assert main(["check", str(tmp_path / "worse.mls.json"), "--json"]) == 1
    err = json.loads(capsys.readouterr().out)["errors"][0]
    assert err["path"] == "$.rules[0].using.and[1].cmp[0]" and err["span"] is None


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
