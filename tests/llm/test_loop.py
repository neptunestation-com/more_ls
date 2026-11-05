"""The LLM loop without a network: scripted answers exercise the two passes, structural and
check-error repair, counterexample-driven repair, escalation, and the grader."""
from __future__ import annotations

import copy
import json

import pytest

from mls.llm.client import LlmError, ScriptedClient
from mls.llm.grade import expected_status, grade, portable_properties, summarize
from mls.llm.loop import generate
from mls.llm.mutate import grade_mutants, mutants
from mls.llm.schemas import properties_schema, rules_schema, skeleton_of
from mls.spec import check_spec
from mls.spec.model import Spec
from tests.conftest import EXAMPLE, ROOT


def base() -> dict:
    return json.loads(EXAMPLE.read_text())


def parts(d: dict) -> tuple[dict, list, list]:
    return skeleton_of(d), d["rules"], d["properties"]


REQ = json.loads((ROOT / "evals" / "cases" / "good_tenant_docs.json").read_text())["requirements"]


def test_schemas_are_objects_with_defs():
    for sch, key in ((properties_schema(), "properties"), (rules_schema(), "rules")):
        assert sch["type"] == "object" and key in sch["properties"] and "notes" in sch["properties"]
        assert sch["additionalProperties"] is False and sch["$defs"]
    sk = skeleton_of(base())
    assert set(sk) >= {"mls_version", "identity", "roles", "tables", "relations"} and "rules" not in sk


def test_prose_around_json_is_tolerated_and_bad_answer_keeps_previous(example_spec):
    sk, rules, props = parts(base())
    talk = "I kept R1 as is.\n\n" + json.dumps({"rules": rules, "notes": "n"}) + "\n\nThat is all."
    client = ScriptedClient([{"properties": props, "notes": ""}, talk])
    assert generate(REQ, sk, client, max_repairs=1, syntax="json").status == "proved"
    # an unusable answer: the repair request still quotes the previous rules
    client = ScriptedClient([{"properties": props}, {"rules": rules[:-1]}, "sorry {", {"rules": rules}])
    res = generate(REQ, sk, client, max_repairs=3, syntax="json")
    assert res.status == "proved" and res.repair_rounds == 2
    assert "R1" in client.exchanges[3].request.user and "is not valid JSON" in client.exchanges[3].request.user


def test_fenced_json_answer_is_accepted(example_spec):
    sk, rules, props = parts(base())
    fence = lambda d: "```json\n" + json.dumps(d, indent=1) + "\n```"  # noqa: E731
    client = ScriptedClient([fence({"properties": props, "notes": ""}), fence({"rules": rules, "notes": ""})])
    res = generate(REQ, sk, client, max_repairs=1, syntax="json")
    assert res.status == "proved" and res.repair_rounds == 0


def test_happy_path_is_proved_in_zero_rounds(example_spec):
    sk, rules, props = parts(base())
    client = ScriptedClient([{"properties": props, "notes": ""}, {"rules": rules, "notes": "all clear"}])
    res = generate(REQ, sk, client, max_repairs=2, syntax="json")
    assert res.status == "proved" and res.repair_rounds == 0 and not res.escalations
    assert set(res.verdicts.values()) == {"PROVED"}
    assert [x.request.name for x in client.exchanges] == ["properties", "rules"]
    # pass 2 never sees pass 1 (D16)
    assert "tenant_isolation" not in client.exchanges[1].request.user
    assert REQ.strip() in client.exchanges[1].request.user
    assert res.notes == {"rules": "all clear"}
    case = {"id": "x", "expected_verdicts": {p["id"]: "PROVED" for p in props}}
    g = grade(case, res, example_spec)
    assert g.correct and g.equivalent and g.reference_properties_hold and g.outcome_match


def test_check_errors_are_fed_back_with_path_and_fix():
    sk, rules, props = parts(base())
    bad = copy.deepcopy(rules)
    bad[0]["using"]["and"][1]["cmp"][0] = {"col": "lvl"}
    client = ScriptedClient([{"properties": props}, {"rules": bad}, {"rules": rules}])
    res = generate(REQ, sk, client, max_repairs=2, syntax="json")
    assert res.status == "proved" and res.repair_rounds == 1
    repair = client.exchanges[2].request
    assert repair.name == "repair:rules"
    assert "$.rules[0].using.and[1].cmp[0]" in repair.user and "unknown column 'lvl'" in repair.user
    assert "fix:" in repair.user and '"lvl"' in repair.user  # the previous answer is quoted


def test_structural_errors_are_repaired_per_pass():
    sk, rules, props = parts(base())
    bad_props = copy.deepcopy(props)
    bad_props[0]["template"] = "tenant_isolaton"
    client = ScriptedClient(['not json', {"properties": bad_props}, {"properties": props}, {"rules": rules}])
    res = generate(REQ, sk, client, max_repairs=3, syntax="json")
    assert res.status == "proved" and res.repair_rounds == 2
    names = [x.request.name for x in client.exchanges]
    assert names == ["properties", "repair:properties", "repair:properties", "rules"]
    assert "not valid JSON" in client.exchanges[1].request.user
    assert "$.properties[0]" in client.exchanges[2].request.user


def test_counterexample_drives_repair_and_escalation():
    """The buggy owner_read rule set (evals/cases/buggy_owner_read) violates P1; the feedback is
    the property and the counterexample in English; the fix is the example's rule set."""
    sk, rules, _ = parts(base())
    b = json.loads((ROOT / "evals" / "cases" / "specs" / "buggy_owner_read.mls.json").read_text())
    props, buggy = b["properties"], b["rules"]
    fixed = rules  # the example's rules (with the owner FK, R7) satisfy the buggy case's properties
    client = ScriptedClient([{"properties": props}, {"rules": buggy}, {"rules": fixed}])
    res = generate(REQ, sk, client, max_repairs=2, syntax="json")
    assert res.status == "proved" and res.repair_rounds == 1, res.errors
    fb = client.exchanges[2].request.user
    assert "P1" in fb and "REFUTED" in fb and "counterexample: the current user is uuid#" in fb
    from mls import english
    spec_b = Spec.model_validate({**sk, "rules": buggy, "properties": props})
    assert english.prop(spec_b, spec_b.properties[0]) in fb  # the property in English, not MLS
    assert '"template"' not in fb
    # never fixed: escalation with the scenario after max_repairs
    client = ScriptedClient([{"properties": props}, {"rules": buggy, "notes": "owners must read their docs"},
                             {"rules": buggy}, {"rules": buggy}])
    res = generate(REQ, sk, client, max_repairs=2, syntax="json")
    # the rules pass stood by its rules: escalated at once, the rest of the budget unspent
    assert res.status == "escalated" and res.repair_rounds == 1
    assert res.steps[-1].stage == "stop" and len(client.responses) == 1
    assert [e.property_id for e in res.escalations] == ["P1"]
    e = res.escalations[0]
    assert e.verdict == "REFUTED" and e.scenario.startswith("the current user is uuid#")
    assert e.notes == "owners must read their docs"
    assert "the current user" in str(e)
    case = {"id": "buggy", "expected_verdicts": {"P1": "REFUTED"}}
    assert expected_status(case) == "proved"  # the requirements are self-consistent (D38)
    g = grade(case, res, Spec.model_validate(base()))
    assert not g.outcome_match and not g.correct and g.escalated and not g.all_proved
    case["expected_loop"] = {"status": "escalated"}
    g = grade(case, res, Spec.model_validate(base()))
    assert g.outcome_match and g.correct


def test_last_checked_rules_are_escalated_when_the_final_repair_does_not_check(example_spec):
    sk, rules, _ = parts(base())
    b = json.loads((ROOT / "evals" / "cases" / "specs" / "buggy_owner_read.mls.json").read_text())
    props, buggy = b["properties"], b["rules"]
    broken = copy.deepcopy(buggy)
    broken[0]["using"] = {"member": {"relation": "member_of", "key": {"col": "owner_id"}, "value": {"col": "tenant_id"}}}
    client = ScriptedClient([{"properties": props}, {"rules": buggy}, {"rules": broken}])
    res = generate(REQ, sk, client, max_repairs=1, syntax="json")
    assert res.status == "escalated" and res.rules == buggy and res.errors  # the errors of the last repair
    assert res.steps[-1].stage == "stop" and [e.property_id for e in res.escalations] == ["P1"]


# ----------------------------------------------------------------------------- terse (D50, #16)
def terse_parts() -> tuple[dict, str, str]:
    """The skeleton, and the example's two sections in the terse syntax."""
    from mls.spec import load_spec
    from mls.terse import section

    spec = load_spec(EXAMPLE)
    return skeleton_of(base()), section("rules", spec.rules), section("properties", spec.properties)


def test_terse_answers_fenced_bare_and_in_prose(example_spec):
    """The system prompt carries the grammar, three examples and the skeleton in terse (no JSON
    Schema); a fenced section, a bare one and one inside prose are all read; the prose is the
    notes; the result's rules are the JSON shape the grader compares."""
    sk, rules, props = terse_parts()
    fenced = "Here are the properties.\n\n```mls\n" + props + "\n```\n\nI assumed clearance is per user."
    client = ScriptedClient([fenced, rules])
    res = generate(REQ, sk, client, max_repairs=1, syntax="terse")
    assert res.status == "proved" and res.repair_rounds == 0 and res.syntax == "terse"
    assert res.notes == {"properties": "Here are the properties.\n\nI assumed clearance is per user."}
    assert res.rules == base()["rules"] and res.properties == base()["properties"]
    sysp = client.exchanges[1].request.system
    assert "# Output syntax" in sysp and "```mls" in sysp and "table docs owned {" in sysp
    assert "$defs" not in sysp and '"cmp"' not in sysp and client.exchanges[1].request.schema is None
    assert "Answer with one `rules { ... }` section" in sysp
    assert "$comment" not in sysp and "Mirrors prototype" not in sysp  # the author's note stays hidden
    assert res.to_dict()["syntax"] == "terse"
    # in prose, no fence, a brace in a string and a comment on the way
    talk = ("I kept R1.\n\n" + rules.replace("requirement 'REQ-1'", "requirement 'REQ-1 {see}'  # note }", 1)
            + "\n\nThat is all.")
    client = ScriptedClient([props, talk])
    res = generate(REQ, sk, client, max_repairs=1, syntax="terse")
    assert res.status == "proved" and res.rules[0]["requirement"] == "REQ-1 {see}"
    assert res.notes["rules"] == "I kept R1.\n\nThat is all."


def test_terse_syntax_error_is_repaired_with_the_line_quoted(example_spec):
    """A terse answer with a syntax error: the repair request quotes the section as written and
    names line:col with the offending line; the next answer fixes it."""
    sk, rules, props = terse_parts()
    broken = rules.replace("level <= @clearance", "level = = @clearance", 1)
    client = ScriptedClient([props, broken, rules])
    res = generate(REQ, sk, client, max_repairs=2, syntax="terse")
    assert res.status == "proved" and res.repair_rounds == 1
    repair = client.exchanges[2].request
    assert repair.name == "repair:rules" and broken in repair.user  # what the model wrote, verbatim
    line = broken.split("\n")[1]
    assert f"- at $.rules[0] (2:111: {line.strip()}): expected a term" in repair.user
    assert "Return the complete corrected `rules { ... }` section" in repair.user
    # no section at all: the structural error says what to answer with
    client = ScriptedClient([props, "I cannot say.", rules])
    res = generate(REQ, sk, client, max_repairs=2, syntax="terse")
    assert res.status == "proved" and res.repair_rounds == 1
    assert "holds no `rules { ... }` section" in client.exchanges[2].request.user


def test_terse_check_error_points_into_the_answer(example_spec):
    """The checker's error (JSON path) is located in the section the model wrote: the atom's
    line:col and the line; a schema error likewise."""
    sk, rules, props = terse_parts()
    bad = rules.replace("level <= @clearance", "nope <= @clearance", 1)
    client = ScriptedClient([props, bad, rules])
    res = generate(REQ, sk, client, max_repairs=2, syntax="terse")
    assert res.status == "proved" and res.repair_rounds == 1
    fb = client.exchanges[2].request.user
    assert "- at $.rules[0].using.and[1].cmp[0] (2:103: " in fb and "unknown column 'nope'" in fb and "fix: one of" in fb
    bad = rules.replace("R5 cardinality", "R5 cardinality", 1).replace(" max 5", " max 0") if " max 5" in rules else \
        rules.replace("R1 policy member_read on docs: authenticated select permissive",
                      "R1 policy member_read on docs: authenticated select permissive", 1)
    schema_bad = "rules {\n  R1 cardinality c on docs: tenant_id -> memberships max 0\n}"
    client = ScriptedClient([props, schema_bad, rules])
    res = generate(REQ, sk, client, max_repairs=2, syntax="terse")
    assert res.status == "proved"
    fb = client.exchanges[2].request.user
    assert "- at $.rules[0].max (2:3: R1 cardinality" in fb and "greater than or equal to 1" in fb


def test_terse_repair_benchmark_quotes_a_rendered_section(example_spec):
    """With start rules (the repair benchmark) there is no text of the model's: the previous
    answer is rendered from the rules, and the counterexample feedback is unchanged (English)."""
    sk, rules, _ = terse_parts()
    b = json.loads((ROOT / "evals" / "cases" / "specs" / "buggy_owner_read.mls.json").read_text())
    client = ScriptedClient([rules])
    res = generate(REQ, sk, client, max_repairs=2, start_rules=b["rules"], start_properties=b["properties"],
                   syntax="terse")
    assert res.status == "proved" and res.repair_rounds == 1
    fb = client.exchanges[0].request.user
    assert "# Your previous answer (rules)\nrules {\n  R1 policy" in fb and "counterexample: the current user is uuid#" in fb
    assert '"kind"' not in fb


def test_terse_prompt_examples_are_proved_canonical_specs():
    """The three examples in the terse prompt parse, are the renderer's own output, check, and
    prove every property; and they share no table name with the eval corpus (no leak)."""
    from mls.evals import case_files, load_case
    from mls.llm.terse_prompt import EXAMPLES, terse_output_reference
    from mls.props.runner import run_all
    from mls.spec.syntax import parse
    from mls.terse import render

    corpus_tables: set[str] = set()
    for f in case_files([ROOT / "evals" / "cases"]):
        ref = load_case(f)["reference_spec"]
        data = json.loads((f.parent / ref).read_text()) if isinstance(ref, str) else ref
        corpus_tables |= set(data["tables"])
    for text in EXAMPLES:
        spec = parse(text)
        assert render(spec) == text
        spec = check_spec(spec)
        assert all(r.verdict == "PROVED" for r in run_all(spec, 20000)), text[:60]
        assert not (set(spec.tables) & corpus_tables), set(spec.tables) & corpus_tables
    assert terse_output_reference().count("```mls") == 3


def test_invalid_after_budget_and_script_exhaustion():
    sk, rules, props = parts(base())
    bad = copy.deepcopy(rules)
    bad[0]["table"] = "nope"
    client = ScriptedClient([{"properties": props}, {"rules": bad}, {"rules": bad}])
    res = generate(REQ, sk, client, max_repairs=1, syntax="json")
    assert res.status == "invalid" and res.errors and res.repair_rounds == 1
    client = ScriptedClient([{"properties": props}])
    res = generate(REQ, sk, client, max_repairs=1, syntax="json")
    assert res.status == "failed" and "script exhausted" in res.errors[0].message
    with pytest.raises(LlmError):
        ScriptedClient([]).complete(client.exchanges[0].request)


def test_grade_detects_a_different_rule_set(example_spec):
    """Rules that check clean and prove the generated properties but differ from the reference
    are not equivalent: the access diff finds the change."""
    sk, rules, props = parts(base())
    looser = copy.deepcopy(rules)
    for r in looser:
        if r["id"] == "R1":
            r["using"] = r["using"]["and"][0]  # drop the clearance bound
    props_ok = [p for p in props if p["id"] != "P5"]
    client = ScriptedClient([{"properties": props_ok}, {"rules": looser}])
    res = generate(REQ, sk, client, max_repairs=0, syntax="json")
    assert res.status == "proved"
    g = grade({"id": "x", "expected_verdicts": {p["id"]: "PROVED" for p in props}}, res, example_spec)
    assert g.outcome_match and g.equivalent is False and g.net_equivalent is False and not g.correct
    assert any(d.startswith("access: docs authenticated select using granted") for d in g.differences)
    assert "docs authenticated select granted" in g.net_differences
    s = summarize([g])
    assert s["spec_correctness_rate"] == 0 and s["net_equivalence_rate"] == 0 and s["failed"] == ["x"]


def test_same_requirement_at_another_layer_is_net_equivalent(example_spec):
    """The archived rule as a transition rule plus per-command policies instead of one restrictive
    policy on all commands: the integrity diff reports the new transition rule, the net-effect
    diff finds nothing (every role can do exactly the same things)."""
    from mls.props.diff import behaviour_diff, diff
    sk, rules, props = parts(base())
    alt = copy.deepcopy(rules)
    for r in alt:
        if r["id"] == "R5":  # not_archived: restrictive on all commands -> on select only ...
            r["command"] = "select"
    # ... and the write side as a transition rule and per-command restrictive policies
    alt.append({"id": "R11", "kind": "transition", "name": "archived_frozen", "table": "docs",
                "requirement": "x", "predicate": {"not": {"cmp": [{"old": "archived"}, "=", {"const": 1}]}}})
    alt.append({"id": "R12", "kind": "policy", "name": "not_archived_write", "table": "docs", "requirement": "x",
                "role": "authenticated", "command": "insert", "mode": "restrictive",
                "check": {"not": {"cmp": [{"col": "archived"}, "=", {"const": 1}]}}})
    alt.append({"id": "R13", "kind": "policy", "name": "not_archived_update", "table": "docs", "requirement": "x",
                "role": "authenticated", "command": "update", "mode": "restrictive",
                "using": {"not": {"cmp": [{"col": "archived"}, "=", {"const": 1}]}},
                "check": {"not": {"cmp": [{"col": "archived"}, "=", {"const": 1}]}}})
    alt.append({"id": "R14", "kind": "policy", "name": "not_archived_delete", "table": "docs", "requirement": "x",
                "role": "authenticated", "command": "delete", "mode": "restrictive",
                "using": {"not": {"cmp": [{"col": "archived"}, "=", {"const": 1}]}}})
    gen = Spec.model_validate({**sk, "rules": alt, "properties": []})
    gen = check_spec(gen)
    d = diff(example_spec, gen)
    assert d.any_change and d.integrity_changes  # the transition rule shows layer by layer
    assert all(a.verdict == "PROVED" for a in behaviour_diff(example_spec, gen) if a.table == "docs"), [
        (a.command, a.direction) for a in behaviour_diff(example_spec, gen) if a.verdict != "PROVED"]


def test_portable_properties_replace_rule_references(example_spec):
    ps = portable_properties(example_spec)
    inv = next(p for p in ps if p["template"] == "invariant_preserved")
    assert all("rule" not in i and "predicate" in i for i in inv["invariants"])
    data = example_spec.model_dump(by_alias=True, exclude_none=True)
    data["properties"] = ps
    check_spec(Spec.model_validate(data))


def test_mutants_are_solver_graded(example_spec):
    data = base()
    ms = mutants(data)
    names = {m.name for m in ms}
    assert "drop:R1" in names and "drop:R7" in names and "permissive:R5" in names and "loosen_using:R1" in names
    base_verdicts = {p["id"]: "PROVED" for p in data["properties"]}
    graded = grade_mutants(data, base_verdicts)
    by = {m.name: m for m in graded}
    assert by["drop:R8"].detected and by["drop:R8"].verdicts["P1"] == "REFUTED"   # admins ⊆ memberships
    assert by["permissive:R5"].detected                                              # archived rows become visible
    assert by["drop:R7"].detected and by["drop:R7"].verdicts["P4"] == "REFUTED"   # the owner invariant, re-based
    assert any(not m.detected for m in graded)   # coverage gaps are reported, not failed
    for m in graded:
        check_spec(Spec.model_validate(m.data))
        assert all("rule" not in inv for p in m.data["properties"] if p["template"] == "invariant_preserved"
                   for inv in p["invariants"])


def test_lint_findings_are_hints_in_the_repair_prompt():
    """D49: the buggy owner_read rule set carries a legacy duplicate of member_read (a subsumption
    finding); the repair request carries it under Hints after the counterexamples, and a spec
    that proves is never sent back for a lint finding."""
    sk, rules, _ = parts(base())
    b = json.loads((ROOT / "evals" / "cases" / "specs" / "buggy_owner_read.mls.json").read_text())
    props, buggy = b["properties"], b["rules"]
    client = ScriptedClient([{"properties": props}, {"rules": buggy}, {"rules": rules}])
    res = generate(REQ, sk, client, max_repairs=2, syntax="json")
    assert res.status == "proved" and res.repair_rounds == 1, res.errors
    fb = client.exchanges[2].request.user
    assert "# Hints (lint findings" in fb
    assert "- subsumption: policy R11 (legacy_member_read) and policy R1 (member_read) on docs admit the same rows" in fb
    assert fb.index("REFUTED") < fb.index("# Hints")
    # the fixed rules prove: no further round, whatever the lint would say
    noisy = rules + [{**next(r for r in rules if r["id"] == "R1"), "id": "R1b", "name": "member_read_again"}]
    client = ScriptedClient([{"properties": props}, {"rules": noisy}])
    res = generate(REQ, sk, client, max_repairs=2, syntax="json")
    assert res.status == "proved" and res.repair_rounds == 0
