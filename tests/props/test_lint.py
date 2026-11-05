"""The lint pack (D49): a positive and a negative fixture per lint; findings never touch the
proof verdicts; a finding over an opaque block is conditional."""
from __future__ import annotations

import json

from mls.cli import main
from mls.props.lint import Finding, coverage, lint_spec
from mls.props.report import to_markdown
from mls.props.runner import run_all
from mls.spec import check_spec
from mls.spec.model import Spec
from tests.conftest import EXAMPLE

OWNER = {"cmp": [{"col": "owner_id"}, "=", {"ctx": "uid"}]}
LEVEL0 = {"cmp": [{"col": "level"}, "<=", {"const": 0}]}


def base() -> dict:
    return json.loads(EXAMPLE.read_text())


def make(d: dict) -> Spec:
    return check_spec(Spec.model_validate(d))


def kinds(findings: list[Finding]) -> list[str]:
    return [f.kind for f in findings]


def policy(id, name, command, using, mode="permissive", table="docs", role="authenticated", check=None):
    r = {"id": id, "kind": "policy", "name": name, "table": table, "role": role, "command": command, "mode": mode}
    if using is not None:
        r["using"] = using
    if check is not None:
        r["check"] = check
    return r


def test_the_example_is_clean():
    assert lint_spec(make(base())) == []


# ----------------------------------------------------------------------------- coverage
def test_coverage_dead_column_rule_and_locked_table():
    d = base()
    d["rules"].append({"id": "CR", "kind": "column_rule", "name": "no_user_writes", "table": "admins",
                       "role": "authenticated", "columns": ["user_id"], "access": "write"})
    d["tables"]["locked"] = {"columns": {"id": {"type": "int", "nullable": False}}, "primary_key": ["id"]}
    fs = coverage(make(d))
    assert [(f.kind, f.table, f.rules) for f in fs] == [("coverage", "admins", ["CR"]), ("coverage", "locked", [])]
    assert "no permissive policy lets authenticated insert or update there: the grant is dead" in fs[0].message
    assert "nobody can select, insert, update or delete" in fs[1].message
    # negative: an insert policy makes the write rule live; a policy on the table unlocks it
    d["rules"].append(policy("A2", "admin_insert", "insert", None, table="admins",
                             check={"cmp": [{"col": "user_id"}, "=", {"ctx": "uid"}]}))
    d["rules"].append(policy("L1", "lock_read", "select", {"lit": True}, table="locked"))
    assert coverage(make(d)) == []


def test_coverage_observed_table_a_selector_or_an_isolation_column_would_cover():
    d = base()
    d["tables"]["audit"] = {"columns": {"id": {"type": "int", "nullable": False},
                                        "tenant_id": {"type": "int", "nullable": False}}, "mode": "observed"}
    r2 = next(r for r in d["rules"] if r["id"] == "R2")
    del r2["table"]
    r2["applies_to"] = {"has_column": ["tenant_id", "int"]}
    fs = coverage(make(d))
    assert [(f.kind, f.table) for f in fs] == [("coverage", "audit"), ("coverage", "audit")]
    assert "matches the selector of rule R2 (has column tenant_id of type int)" in fs[0].message
    assert fs[0].rules == ["R2"] and '"adopted"' in fs[0].message
    assert "has column tenant_id, which property P1 names as the tenant column of docs" in fs[1].message
    # negative: a different column, and the table adopted
    d["tables"]["audit"]["columns"] = {"id": {"type": "int", "nullable": False}}
    assert coverage(make(d)) == []
    d["tables"]["audit"] = {"columns": {"id": {"type": "int", "nullable": False},
                                        "tenant_id": {"type": "int", "nullable": False}},
                            "primary_key": ["id"], "mode": "adopted"}
    assert coverage(make(d)) == []  # governed: R2 expands to it, nothing to flag


# ----------------------------------------------------------------------------- contradiction
def test_contradiction():
    d = base()
    never = {"and": [{"cmp": [{"col": "level"}, "<", {"const": 0}]}, {"cmp": [{"col": "level"}, ">", {"const": 0}]}]}
    d["rules"].append(policy("C0", "never", "delete", never))
    fs = [f for f in lint_spec(make(d)) if f.kind == "contradiction"]
    assert len(fs) == 1 and fs[0].rules == ["C0"]
    assert fs[0].message == ("policy C0 (never) never admits a row: its using cannot be TRUE on any row that "
                             "satisfies the constraints of docs")
    # the stored-row constraints count: the example's CHECK (R6) rejects negative levels
    d["rules"][-1]["using"] = {"cmp": [{"col": "level"}, "<", {"const": 0}]}
    fs = lint_spec(make(d))
    assert kinds(fs) == ["contradiction"] and fs[0].rules == ["C0"]
    # negative: a satisfiable using
    d["rules"][-1]["using"] = LEVEL0
    assert kinds(lint_spec(make(d))) == []
    # an insert policy's check that cannot hold
    d["rules"][-1] = policy("C1", "never_insert", "insert", None, check=never)
    fs = lint_spec(make(d))
    assert kinds(fs) == ["contradiction"] and "its check cannot be TRUE" in fs[0].message


# ----------------------------------------------------------------------------- subsumption
def test_subsumption_and_equivalence():
    d = base()
    r1 = next(r for r in d["rules"] if r["id"] == "R1")
    d["rules"].append(policy("S1", "member_read_level0", "select", {"and": [r1["using"], LEVEL0]}))
    fs = lint_spec(make(d))
    assert kinds(fs) == ["subsumption"] and fs[0].rules == ["S1", "R1"]
    assert fs[0].message == ("policy S1 (member_read_level0) is subsumed by policy R1 (member_read) on docs: every "
                             "row it admits to authenticated, the other admits too")
    # equivalent policies: reported once, as a pair, and neither is also reported redundant
    d["rules"][-1] = {**r1, "id": "S2", "name": "member_read_again"}
    fs = lint_spec(make(d))
    assert kinds(fs) == ["subsumption"] and sorted(fs[0].rules) == ["R1", "S2"]
    assert "admit the same rows for authenticated: one of them is enough" in fs[0].message
    # negative: a policy neither within nor containing the other (R1 vs R2 in the example)
    assert lint_spec(make(base())) == []
    # an insert policy is compared on its check: within the example's member_insert (R3)
    d["rules"][-1] = policy("S3", "insert_level0", "insert", None, check={"and": [r1["using"], LEVEL0]})
    fs = lint_spec(make(d))
    assert kinds(fs) == ["subsumption"] and fs[0].rules == ["S3", "R3"]


def test_subsumption_is_three_valued():
    """`level < 1 or level >= 1` is a tautology in two-valued logic and UNKNOWN on a NULL level:
    an owner policy is not subsumed by it, because a row with a NULL level that the owner policy
    admits is not admitted by the tautology."""
    d = base()
    d["tables"]["docs"]["columns"]["level"]["nullable"] = True
    d["rules"] = [r for r in d["rules"] if r["id"] not in ("R1", "R6")]  # R1 compares the level; R6 checks it
    d["properties"] = []
    taut = {"or": [{"cmp": [{"col": "level"}, "<", {"const": 1}]}, {"cmp": [{"col": "level"}, ">=", {"const": 1}]}]}
    d["rules"] += [policy("T1", "tautology", "select", taut), policy("T2", "owner_read", "select", OWNER)]
    assert kinds(lint_spec(make(d))) == []
    d["tables"]["docs"]["columns"]["level"]["nullable"] = False
    fs = lint_spec(make(d))
    assert ["T2", "T1"] in [f.rules for f in fs] and kinds(fs) == ["subsumption"] * len(fs)


# ----------------------------------------------------------------------------- redundancy
def test_redundancy():
    d = base()
    # a restrictive policy the permissive ones already imply: R6 (CHECK level >= 0) makes level >= 0 on
    # every stored row, so a restrictive `level >= 0` changes nothing
    d["rules"].append(policy("X1", "nonneg", "select", {"cmp": [{"col": "level"}, ">=", {"const": 0}]},
                             mode="restrictive"))
    fs = lint_spec(make(d))
    assert kinds(fs) == ["redundancy"] and fs[0].rules == ["X1"]
    assert fs[0].message == ("policy X1 (nonneg) on docs is redundant: without it authenticated can select exactly "
                             "the same rows")
    # negative: a restrictive policy that bites
    d["rules"][-1]["using"] = LEVEL0
    assert kinds(lint_spec(make(d))) == []
    # a permissive policy on an `all` command whose other commands matter is not redundant
    d["rules"][-1] = policy("X2", "owner_all", "all", OWNER)
    assert kinds(lint_spec(make(d))) == []


def test_findings_over_an_opaque_block_are_conditional():
    d = base()
    d["opaques"] = {"gate": {"table": "docs", "columns": ["level"], "context": ["clearance"], "sql": "level < 1"}}
    d["rules"] += [policy("O1", "gate_a", "delete", {"opaque": "gate"}),
                   policy("O2", "gate_b", "delete", {"opaque": "gate"})]
    fs = lint_spec(make(d))
    assert kinds(fs) == ["subsumption"] and fs[0].conditional_on == ["gate"]
    assert str(fs[0]).endswith("(conditional on opaque block gate)")


# ----------------------------------------------------------------------------- never a verdict
def test_findings_never_change_the_proof_report(tmp_path, capsys):
    d = base()
    r1 = next(r for r in d["rules"] if r["id"] == "R1")
    d["rules"].append({**r1, "id": "S2", "name": "member_read_again"})
    clean, noisy = make(base()), make(d)
    assert kinds(lint_spec(noisy)) == ["subsumption"]
    assert {r.id: r.verdict for r in run_all(noisy)} == {r.id: r.verdict for r in run_all(clean)}
    report = to_markdown(noisy, run_all(noisy))
    assert "subsum" not in report and "redundan" not in report and "lint" not in report
    p = tmp_path / "s.json"
    p.write_text(json.dumps(d))
    assert main(["prove", str(p)]) == 0
    capsys.readouterr()
    assert main(["lint", str(p)]) == 0  # hygiene: exit 0 even with findings
    out = capsys.readouterr().out
    assert out.startswith("subsumption: policy S2 (member_read_again) and policy R1 (member_read)")
    assert out.endswith("0 warning(s), 1 finding(s)\n")
    assert main(["lint", str(p), "--json"]) == 0
    j = json.loads(capsys.readouterr().out)
    assert j["warnings"] == [] and j["findings"][0]["kind"] == "subsumption" and j["findings"][0]["rules"] == ["S2", "R1"]
