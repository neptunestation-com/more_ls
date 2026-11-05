"""`mls status` (Milestone 5): one table of each status, the output pinned."""
from __future__ import annotations

import json

from mls.cli import main
from mls.props.runner import run_all
from mls.spec import check_spec
from mls.spec.model import Spec
from mls.status import failing, table_statuses, to_json, to_text
from tests.conftest import EXAMPLE

OWNER = {"cmp": [{"col": "owner_id"}, "=", {"ctx": "uid"}]}


def t(**cols):
    return {"columns": {"id": {"type": "int", "nullable": False},
                        **{c: {"type": ty, "nullable": False} for c, ty in cols.items()}}, "primary_key": ["id"]}


def fixture() -> dict:
    """The example (docs proved) plus: `files` governed by an opaque block (opaque), `notes` with a
    rule and no property (unproved), `logs` whose lower bound fails (refuted), `audit` observed
    (ungoverned)."""
    d = json.loads(EXAMPLE.read_text())
    d["tables"]["files"] = t(owner_id="uuid")
    d["tables"]["notes"] = t(owner_id="uuid")
    d["tables"]["logs"] = t(owner_id="uuid")
    d["tables"]["audit"] = {**t(), "mode": "observed"}
    d["opaques"] = {"file_gate": {"table": "files", "columns": ["owner_id"], "context": ["uid"],
                                  "sql": "owner_id = {ctx:uid}"}}
    d["rules"] += [
        {"id": "F1", "kind": "policy", "name": "file_access", "table": "files", "role": "authenticated",
         "command": "all", "mode": "permissive", "using": {"opaque": "file_gate"}, "check": {"opaque": "file_gate"}},
        {"id": "N1", "kind": "policy", "name": "note_read", "table": "notes", "role": "authenticated",
         "command": "select", "mode": "permissive", "using": OWNER},
        {"id": "L1", "kind": "policy", "name": "log_read", "table": "logs", "role": "authenticated",
         "command": "select", "mode": "permissive", "using": OWNER},
    ]
    d["properties"] = [p for p in d["properties"] if p["template"] != "least_privilege"] + [
        {"id": "PF", "template": "write_subset_read", "description": "what is written to files can be read back",
         "table": "files"},
        {"id": "PL", "template": "must_allow", "description": "everyone reads every log", "table": "logs",
         "role": "authenticated", "command": "select", "when": {"lit": True}},
    ]
    return d


def test_one_table_of_each_status(tmp_path, capsys):
    spec = check_spec(Spec.model_validate(fixture()))
    st = table_statuses(spec, run_all(spec))
    assert to_text(st) == "\n".join([
        "memberships: unproved (no property mentions it)",
        "admins: proved (P4)",
        "docs: proved (P1, P2, P3, P4, P5, P7, P8, P9, P10)",
        "files: opaque (conditional on file_gate)",
        "notes: unproved (no property mentions it)",
        "logs: refuted (not proved: PL)",
        "audit: ungoverned (observed: the spec does not govern it)",
        "status: 1 ungoverned, 1 refuted, 1 opaque, 2 unproved, 2 proved",
    ])
    assert [s.table for s in failing(st)] == ["logs"]
    files = next(s for s in st if s.table == "files")
    assert files.opaques == ["file_gate"] and files.properties == ["PF"]
    # the CLI: text and --json, exit 2 because of the refuted table
    p = tmp_path / "s.json"
    p.write_text(json.dumps(fixture()))
    assert main(["status", str(p)]) == 2
    assert capsys.readouterr().out.strip() == to_text(st)
    assert main(["status", str(p), "--json"]) == 2
    j = json.loads(capsys.readouterr().out)
    assert j == to_json(st) and j["ok"] is False
    assert j["tables"][3] == {"table": "files", "status": "opaque", "detail": "conditional on file_gate",
                              "opaques": ["file_gate"], "properties": ["PF"]}


def test_the_example_is_all_proved(capsys):
    assert main(["status", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert out.endswith("status: 3 proved\n")
    assert "docs: proved (P1, P2, P3, P4, P5, P6, P7, P8, P9, P10)" in out


def test_a_rule_reading_an_opaque_block_makes_the_table_opaque_even_without_a_proof_about_it():
    """A proved property about reads and writes says nothing about an opaque delete gate: the
    table is opaque, not proved."""
    d = fixture()
    d["rules"].append({"id": "F2", "kind": "policy", "name": "file_delete", "table": "files",
                       "role": "authenticated", "command": "delete", "mode": "permissive",
                       "using": {"opaque": "file_gate"}})
    f1 = next(r for r in d["rules"] if r["id"] == "F1")
    f1["using"] = f1["check"] = OWNER  # F1 in the fragment: PF is proved unconditionally
    spec = check_spec(Spec.model_validate(d))
    st = {s.table: s for s in table_statuses(spec, run_all(spec))}
    assert st["files"].status == "opaque" and st["files"].opaques == ["file_gate"]
    assert st["files"].properties == ["PF"]


def test_global_properties_are_attributed_per_table():
    """least_privilege ranges over every table; monotone_in over the tables with policies."""
    spec = check_spec(Spec.model_validate(json.loads(EXAMPLE.read_text())))
    st = {s.table: s for s in table_statuses(spec, run_all(spec))}
    assert st["memberships"].properties == ["P6"]
    assert "P3" in st["docs"].properties and "P3" not in st["memberships"].properties
