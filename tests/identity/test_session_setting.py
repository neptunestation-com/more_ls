"""The session_setting adapter (D47) without Postgres: SQL shape, source grammar, the trust
assumption in the proof report, explain and diff, the harness hooks."""
from __future__ import annotations

import json

from mls import english
from mls.compile import compile_spec
from mls.identity import get_adapter
from mls.identity.session_setting import ASSUMPTION
from mls.props.diff import DiffResult, to_json, to_markdown
from mls.props.report import assumptions
from mls.spec.check import Checker
from mls.spec.model import Spec
from tests.templates import specs


def test_context_sql_is_a_typed_initplan_with_nullif():
    spec = specs.session_spec()
    a = get_adapter(spec)
    assert a.name == "session_setting"
    assert a.ctx_sql("uid") == "(select nullif(current_setting('app.uid', true), '')::uuid)"
    assert a.ctx_sql("clearance") == "(select nullif(current_setting('app.clearance', true), '')::integer)"
    assert a.ctx_sql("now") == "(select now())"
    script = compile_spec(spec)
    assert "nullif(current_setting('app.uid', true), '')::uuid" in script.sql()
    assert f"identity (session_setting): {ASSUMPTION}" in script.assumptions


def test_source_grammar():
    d = json.loads(specs.SESSION.read_text())
    d["identity"]["context"]["uid"]["source"] = "auth.uid"
    errs = Checker(Spec.model_validate(d)).run()
    e = next(e for e in errs if e.path == "$.identity.context.uid.source")
    assert "unknown session_setting source 'auth.uid'" in e.message and "app.<name>" in e.fix
    d["identity"]["context"]["uid"]["source"] = "app.Uid"
    assert any(e.path == "$.identity.context.uid.source" for e in Checker(Spec.model_validate(d)).run())
    d["identity"]["context"]["uid"]["source"] = "app.user_id"
    assert not Checker(Spec.model_validate(d)).run()


def test_assumption_is_stated_in_report_explain_and_diff():
    spec = specs.session_spec()
    assert f"identity adapter 'session_setting': {ASSUMPTION}" in assumptions(spec, [])
    text = english.spec_summary(spec)
    assert ASSUMPTION in text and "no context set: denied" in text
    md = to_markdown(spec, spec, DiffResult([], [], [], []))
    assert "## Assumptions" in md and ASSUMPTION in md
    assert ASSUMPTION in " ".join(to_json(spec, spec, DiffResult([], [], [], []))["assumptions"])
    # the other adapters state theirs the same way (and nothing about missing settings)
    sup = english.spec_summary(specs.policy_spec())
    assert "identity adapter 'supabase'" in sup and "no context set" not in sup


def test_harness_hooks():
    spec = specs.session_spec()
    a = get_adapter(spec)
    assert a.user_context_name() == "uid"  # the membership key
    assert a.harness_setup_sql(spec) == []
    u = a.harness_user_values(2)
    assert u == ["00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"]
    stmts = a.harness_context_sql("authenticated", {"uid": u[0], "clearance": 2, "now": None})
    assert stmts == ["select set_config('app.uid', '00000000-0000-0000-0000-000000000001', true)",
                     "select set_config('app.clearance', '2', true)", "set local role authenticated"]
    assert a.harness_context_sql("authenticated", {}) == ["set local role authenticated"]
