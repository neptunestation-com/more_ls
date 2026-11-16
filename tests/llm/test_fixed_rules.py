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
    """The three passes' answers; the relations pass adds none (the skeleton has `in_project`)."""
    if syntax == "terse":
        return ["relations { }", section("properties", props), section("rules", rules)]
    dump = lambda xs: [x.model_dump(mode="json", by_alias=True, exclude_none=True) for x in xs]  # noqa: E731
    return [{"relations": {}}, {"properties": dump(props)}, {"rules": dump(rules), "notes": "n"}]


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
    assert [x["name"] for x in exchanges] == ["relations", "properties", "rules"]
    for x in exchanges[1:]:  # both passes see them, with the first free id
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
    code, _, got, exchanges = _generate(tmp_path, monkeypatch, syntax, bad + [good[2]])
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


# --- the relations pass completes the skeleton (#28)

IN_PROJECT = "relation in_project = team_members . projects(team_id, id)"


def _bare(tmp_path):
    """The skeleton `mls import --policies discard` writes: without the composed relation."""
    spec, skeleton, written = _parts()
    bare = skeleton.model_copy(update={"relations": {n: r for n, r in skeleton.relations.items() if n != "in_project"}})
    return spec, bare, written


def _generate_on(tmp_path, monkeypatch, skeleton, answers) -> tuple[int, Spec | None, list]:
    skel = tmp_path / "spec.mls"
    skel.write_text(render(skeleton))
    script = tmp_path / "script.json"
    script.write_text(json.dumps(answers))
    monkeypatch.setenv("MLS_LLM_SCRIPT", str(script))
    req = tmp_path / "requirements.txt"
    req.write_text("A member reads, creates and edits the tasks of their team's projects.")
    out, tr = tmp_path / "out.mls", tmp_path / "t.json"
    code = main(["generate", str(req), "--skeleton", str(skel), "--out", str(out), "--transcript", str(tr)])
    return code, (load_spec(out) if out.exists() else None), json.loads(tr.read_text())["exchanges"]


def test_the_relations_pass_adds_the_composed_relation(tmp_path, monkeypatch, capsys):
    """On the bare takeover skeleton the relations pass writes `in_project`; the other two passes
    are shown it, the approval list states it in English, and `--out` is the example."""
    spec, bare, written = _bare(tmp_path)
    answers = [f"```mls\nrelations {{\n  {IN_PROJECT}\n}}\n```",
               section("properties", spec.properties), section("rules", written)]
    code, got, exchanges = _generate_on(tmp_path, monkeypatch, bare, answers)
    out = capsys.readouterr().out
    assert code == 0 and "status: proved after 0 repair round(s)" in out
    assert ("# Relations added to the schema (for approval)\n- in_project: team_members, then projects "
            "(team_id → id): a user's in_project are the id of every projects row whose team_id is among "
            "that user's team_members.") in out
    assert out.index("# Relations added") < out.index("# Properties (for approval)")
    assert IN_PROJECT not in exchanges[0]["system"]
    assert all(IN_PROJECT in x["system"] for x in exchanges[1:])
    assert list(got.relations) == ["team_members", "in_project"] and got.relations == spec.relations
    key = lambda r: int(r.id[1:])  # noqa: E731
    assert sorted(got.rules, key=key) == spec.rules and got.properties == spec.properties


def test_a_relation_the_checker_refuses_is_repaired(tmp_path, monkeypatch, capsys):
    """A hop through a column that is not a key is refused at its path, with the line quoted,
    and goes back to the relations pass; restating the schema's relation is not an error,
    changing it is."""
    spec, bare, written = _bare(tmp_path)
    wrong = "relation in_project = team_members . projects(id, team_id)"  # team_id is not a key of projects
    changed = "relation team_members = team_members(team_id, user_id)"
    restated = "relation team_members = team_members(user_id, team_id)"
    answers = [f"relations {{\n  {wrong}\n}}",
               f"relations {{\n  {IN_PROJECT}\n  {changed}\n}}",
               f"relations {{\n  {restated}\n  {IN_PROJECT}\n}}",
               section("properties", spec.properties), section("rules", written)]
    code, got, exchanges = _generate_on(tmp_path, monkeypatch, bare, answers)
    out = capsys.readouterr().out
    assert code == 0 and "status: proved after 2 repair round(s)" in out
    assert [x["name"] for x in exchanges] == ["relations"] + ["repair:relations"] * 2 + ["properties", "rules"]
    hop = exchanges[1]["user"]
    assert "$.relations.in_project.through[0].value (2:" in hop and wrong in hop
    clash = exchanges[2]["user"]
    assert "relation 'team_members' is already in the schema" in clash and "$.relations.team_members (3:" in clash
    assert got.relations == spec.relations


def test_relation_errors_left_after_the_budget_are_invalid(tmp_path, monkeypatch, capsys):
    spec, bare, written = _bare(tmp_path)
    wrong = "relations {\n  relation in_project = team_members . projects(id, team_id)\n}"
    code, got, exchanges = _generate_on(tmp_path, monkeypatch, bare, [wrong] * 4)
    out = capsys.readouterr().out
    assert code == 1 and "status: invalid after 3 repair round(s)" in out and "$.relations.in_project" in out
    assert got is None and [x["name"] for x in exchanges] == ["relations"] + ["repair:relations"] * 3
