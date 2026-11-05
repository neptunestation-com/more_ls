from __future__ import annotations

import json

from mls.cli import main
from tests.conftest import EXAMPLE, ROOT


def test_generate_and_eval_llm_with_a_script(tmp_path, capsys, monkeypatch):
    d = json.loads(EXAMPLE.read_text())
    script = tmp_path / "script.json"
    script.write_text(json.dumps([{"properties": d["properties"]}, {"rules": d["rules"], "notes": "n"}]))
    monkeypatch.setenv("MLS_LLM_SCRIPT", str(script))
    req = tmp_path / "req.txt"
    req.write_text(json.loads((ROOT / "evals" / "cases" / "good_tenant_docs.json").read_text())["requirements"])
    out, tr = tmp_path / "out.json", tmp_path / "t.json"
    assert main(["generate", str(req), "--skeleton", str(EXAMPLE), "--syntax", "json", "--out", str(out),
                 "--transcript", str(tr)]) == 0
    text = capsys.readouterr().out
    assert "status: proved after 0 repair round(s)" in text and "# Properties (for approval)" in text
    assert "P1 (PROVED): " in text and "notes from the rules pass: n" in text
    assert json.loads(out.read_text())["rules"] == d["rules"]
    t = json.loads(tr.read_text())
    assert len(t["exchanges"]) == 2
    assert t["spec_terse"][0] == "mls 0.1" and "rules {" in t["spec_terse"]
    assert any(line.startswith("  R1 policy member_read on docs: authenticated select") for line in t["spec_terse"])
    assert main(["eval", str(ROOT / "evals" / "cases" / "good_tenant_docs.json"), "--llm", "--syntax", "json", "--json",
                 "--transcripts", str(tmp_path / "tr")]) == 0
    s = capsys.readouterr().out
    assert '"spec_correctness_rate": 1.0' in s and (tmp_path / "tr" / "good_tenant_docs.json").exists()
    t = json.loads((tmp_path / "tr" / "good_tenant_docs.json").read_text())
    assert any(line.startswith("  P1 tenant_isolation docs:") for line in t["spec_terse"])


def test_generate_and_eval_with_the_terse_syntax(tmp_path, capsys, monkeypatch):
    """`--syntax terse` on both commands (D50, #16): the script answers in terse, `--out x.mls`
    writes the terse spec, the transcript keeps `spec_terse`, the eval summary says the syntax
    and counts the tokens."""
    from mls.spec import load_spec
    from mls.terse import section

    spec = load_spec(EXAMPLE)
    script = tmp_path / "script.json"
    script.write_text(json.dumps(["```mls\n" + section("properties", spec.properties) + "\n```",
                                  section("rules", spec.rules) + "\nnote: all clear"]))
    monkeypatch.setenv("MLS_LLM_SCRIPT", str(script))
    req = tmp_path / "req.txt"
    req.write_text(json.loads((ROOT / "evals" / "cases" / "good_tenant_docs.json").read_text())["requirements"])
    out, tr = tmp_path / "out.mls", tmp_path / "t.json"
    assert main(["generate", str(req), "--skeleton", str(EXAMPLE), "--syntax", "terse", "--out", str(out),
                 "--transcript", str(tr)]) == 0
    text = capsys.readouterr().out
    assert "status: proved after 0 repair round(s)" in text and "notes from the rules pass: note: all clear" in text
    assert load_spec(out) == spec and out.read_text() == EXAMPLE.with_suffix("").read_text()
    t = json.loads(tr.read_text())
    assert t["result"]["syntax"] == "terse" and "# Output syntax" in t["exchanges"][0]["system"]
    assert any(line.startswith("  R1 policy member_read on docs:") for line in t["spec_terse"])
    assert main(["eval", str(ROOT / "evals" / "cases" / "good_tenant_docs.json"), "--llm", "--syntax", "terse",
                 "--json"]) == 0
    out_text = capsys.readouterr().out
    assert out_text.startswith("ok: good_tenant_docs")
    s = json.loads(out_text[out_text.index("{"):])
    assert s["syntax"] == "terse" and s["spec_correctness_rate"] == 1.0
    assert s["tokens"] == {"api_calls": 2, "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}


def test_eval_mutate_writes_synthetic_cases(tmp_path, capsys):
    out = tmp_path / "syn"
    assert main(["eval", str(ROOT / "evals" / "cases" / "good_tenant_docs.json"), "--mutate", str(out)]) == 0
    text = capsys.readouterr().out
    assert "mutation score" in text
    files = sorted(out.glob("*.json"))
    assert files
    c = json.loads(files[0].read_text())
    assert c["origin"] == "synthetic" and c["expected_loop"] == {"status": "proved"} and c["start_rules"]
    assert main(["eval", str(out)]) == 0  # base reference as expected, mutant rules as recorded
    # without --repair a synthetic case is skipped; with it the loop starts from the mutant rules
    import os
    script = tmp_path / "s.json"
    script.write_text(json.dumps([{"properties": c["reference_spec"]["properties"]}, {"rules": c["reference_spec"]["rules"]}]))
    os.environ["MLS_LLM_SCRIPT"] = str(script)
    try:
        assert main(["eval", str(files[0]), "--llm", "--syntax", "json"]) == 0
        assert "skip: " in capsys.readouterr().out
        assert main(["eval", str(files[0]), "--llm", "--repair", "--syntax", "json", "--json"]) == 0
        s = capsys.readouterr().out
        assert '"spec_correctness_rate": 1.0' in s and '"mean_repair_rounds": 1.0' in s
    finally:
        del os.environ["MLS_LLM_SCRIPT"]
