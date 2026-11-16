"""The skeleton's own rules are kept (#26): `mls generate --skeleton` on the takeover skeleton
(`examples/starter_takeover.mls` without the rules and properties the guide adds) keeps the two
CHECKs `mls import` read, R1 and R2, when the scripted answer leaves them out, in both syntaxes;
both passes are shown them; a rule that takes a fixed rule's id or name is a structural error at
the answer's own path, repaired like any other.  No API call: `MLS_LLM_SCRIPT` replays answers."""
from __future__ import annotations

import json

import pytest

from mls.cli import main
from mls.spec import load_spec
from mls.spec.model import Spec
from mls.terse import render, section
from tests.conftest import ROOT

EXAMPLE = ROOT / "examples" / "starter_takeover.mls"
FIXED = ["R1", "R2"]


def _parts():
    spec = load_spec(EXAMPLE)
    fixed = [r for r in spec.rules if r.id in FIXED]
    written = [r for r in spec.rules if r.id not in FIXED]
    skeleton = spec.model_copy(update={"rules": fixed, "properties": [], "comment": None})
    return spec, skeleton, written


def _answers(syntax: str, props, rules) -> list:
    if syntax == "terse":
        return [section("properties", props), section("rules", rules)]
    dump = lambda xs: [x.model_dump(mode="json", by_alias=True, exclude_none=True) for x in xs]  # noqa: E731
    return [{"properties": dump(props)}, {"rules": dump(rules), "notes": "n"}]


def _generate(tmp_path, monkeypatch, syntax: str, answers: list) -> tuple[int, str, Spec | None, list]:
    spec, skeleton, _ = _parts()
    skel = tmp_path / "spec.mls"
    skel.write_text(render(skeleton))
    script = tmp_path / "script.json"
    script.write_text(json.dumps(answers))
    monkeypatch.setenv("MLS_LLM_SCRIPT", str(script))
    req = tmp_path / "requirements.txt"
    req.write_text("A member reads, creates and edits the tasks of their team's projects.")
    out, tr = tmp_path / "out.mls", tmp_path / "t.json"
    code = main(["generate", str(req), "--skeleton", str(skel), "--syntax", syntax, "--out", str(out),
                 "--transcript", str(tr)])
    exchanges = json.loads(tr.read_text())["exchanges"]
    return code, out, (load_spec(out) if out.exists() else None), exchanges


@pytest.mark.parametrize("syntax", ["terse", "json"])
def test_the_imported_checks_are_kept_when_the_answer_leaves_them_out(tmp_path, monkeypatch, capsys, syntax):
    spec, _, written = _parts()
    code, _, got, exchanges = _generate(tmp_path, monkeypatch, syntax, _answers(syntax, spec.properties, written))
    assert code == 0 and "status: proved after 0 repair round(s)" in capsys.readouterr().out
    assert got.rules == written + [r for r in spec.rules if r.id in FIXED]  # the fixed rules after the model's
    assert got.properties == spec.properties
    for x in exchanges:  # both passes see them, with the first free id
        assert "# Rules the spec already has (read from the database)" in x["system"]
        assert "number your rules from R3" in x["system"]
        assert "tasks_status_check" in x["system"] and "team_members_role_check" in x["system"]


@pytest.mark.parametrize("syntax", ["terse", "json"])
def test_a_rule_that_takes_a_fixed_id_or_name_is_repaired(tmp_path, monkeypatch, capsys, syntax):
    spec, _, written = _parts()
    r1 = next(r for r in spec.rules if r.id == "R1")
    clash = [written[0].model_copy(update={"id": "R1"}),  # a fixed rule's id
             r1.model_copy(update={"id": "R99"})]  # a fixed rule's name on its table, under another id
    bad = _answers(syntax, spec.properties, clash + written[1:])
    good = _answers(syntax, spec.properties, written)
    code, _, got, exchanges = _generate(tmp_path, monkeypatch, syntax, bad + [good[1]])
    out = capsys.readouterr().out
    assert code == 0 and "status: proved after 1 repair round(s)" in out
    repair = exchanges[-1]["user"]
    assert "rule id 'R1' is taken by a rule the spec already has" in repair and "number your rules from R3" in repair
    assert "rule name 'tasks_status_check' on 'tasks' is taken by rule R1" in repair
    assert {r.id for r in got.rules} == {r.id for r in spec.rules}


def test_a_verbatim_restatement_is_not_a_duplicate(tmp_path, monkeypatch, capsys):
    spec, _, _ = _parts()
    code, _, got, _ = _generate(tmp_path, monkeypatch, "terse", _answers("terse", spec.properties, spec.rules))
    assert code == 0 and got.rules == spec.rules
