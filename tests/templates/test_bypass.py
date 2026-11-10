"""D54 on real Postgres: bypass paths over a governed table.  Found: a view that is not
`security_invoker` (directly, or over an invoker view), a materialized view, a SECURITY DEFINER
function a superuser owns whose body names the table, and one the table's owner owns while row
level security is not forced.  Not found: an invoker view, a view no app role may use, a function
that is not SECURITY DEFINER, one whose body does not name the table, one whose EXECUTE is
revoked, one whose owner the policies bind.  Drift reports the found ones as `hardening` with the
fix, `assumed` when the table assumes them (a stale assumption is a note); `mls status --dsn`
fails on them; `mls import` lists them."""
from __future__ import annotations

import json

import psycopg
import pytest

from mls.cli import main
from mls.compile import compile_spec
from mls.pg.adopt import import_schema
from mls.pg.catalog import bypass_paths
from mls.pg.drift import drift
from mls.pg.harness import Harness
from mls.props.report import to_markdown
from mls.props.runner import run_all
from tests.templates import specs

pytestmark = pytest.mark.pg

DB = "mls_bypass"
FOUND = {"view doc_titles": "view", "view outer_titles": "view", "view doc_counts": "materialized view",
         "function count_docs()": "function"}
OBJECTS = """
create view doc_titles as select id, tenant_id from docs;
create view inner_titles with (security_invoker = true) as select id from docs;
create view outer_titles as select id from inner_titles;
create materialized view doc_counts as select tenant_id, count(*) from docs group by tenant_id;
create view invoker_titles with (security_invoker = true) as select id from docs;
create view hidden_titles as select id from docs;
grant select on doc_titles, inner_titles, outer_titles, doc_counts, invoker_titles to authenticated;

create function count_docs() returns bigint language sql security definer as $$ select count(*) from docs $$;
create function count_docs_invoker() returns bigint language sql as $$ select count(*) from docs $$;
create function unrelated() returns int language sql security definer as $$ select 1 $$;
create function count_docs_revoked() returns bigint language sql security definer as $$ select count(*) from docs $$;
revoke execute on function count_docs_revoked() from public;
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'mls_plain') then create role mls_plain nologin; end if;
end $$;
create function count_docs_plain() returns bigint language sql security definer as $$ select count(*) from docs $$;
alter function count_docs_plain() owner to mls_plain;
"""


@pytest.fixture(scope="module")
def bypassed(pg_dsn):
    spec = specs.adopted_spec([{"rls": "not_forced"}])
    h = Harness(spec, compile_spec(spec), pg_dsn, DB)
    h.deploy()
    h.conn.execute(OBJECTS)
    yield spec, h, psycopg.conninfo.make_conninfo(pg_dsn, dbname=DB)
    h.close()


def _found(conn) -> dict[str, str]:
    paths = bypass_paths(conn, "public", ["docs", "memberships", "admins"], ["authenticated"])
    assert paths["memberships"] == [] and paths["admins"] == []
    assert all(f["roles"] == ["authenticated"] for f in paths["docs"])
    return {f["path"]: f["kind"] for f in paths["docs"]}


def test_the_bypass_paths_and_only_those_are_found(bypassed):
    _, h, _ = bypassed
    assert _found(h.conn) == FOUND


def test_a_fixed_path_is_no_longer_found(bypassed):
    _, h, _ = bypassed
    with h.conn.transaction(force_rollback=True):
        h.conn.execute("alter view doc_titles set (security_invoker = true)")
        h.conn.execute("revoke execute on function count_docs() from public")
        assert set(_found(h.conn)) == set(FOUND) - {"view doc_titles", "function count_docs()"}


def test_the_table_owner_bypasses_unless_row_level_security_is_forced(bypassed):
    _, h, _ = bypassed
    with h.conn.transaction(force_rollback=True):
        h.conn.execute("alter table docs owner to mls_plain")
        assert _found(h.conn)["function count_docs_plain()"] == "function"
        h.conn.execute("alter table docs force row level security")
        assert "function count_docs_plain()" not in _found(h.conn)


def test_drift_reports_them_as_hardening_with_the_fix(bypassed):
    spec, h, _ = bypassed
    res = drift(h.conn, spec, compile_spec(spec))
    assert not res.ok
    hard = sorted(i.object for i in res.by_kind("hardening"))
    assert len(hard) == len(FOUND) and all(" reaches table docs without its policies" in o for o in hard), hard
    assert ("view doc_titles reaches table docs without its policies: it runs as its owner (it is not "
            "security_invoker), and authenticated may use it (fix: alter view doc_titles set (security_invoker = "
            "true), or revoke its privileges from authenticated; or assume {\"bypass\": \"view doc_titles\"} on "
            "docs)") in hard
    assert any(o.startswith("function count_docs() reaches table docs without its policies: it is SECURITY "
                            "DEFINER, owned by ") and "fix: revoke execute on function count_docs() from public, "
                                                      "authenticated" in o for o in hard), hard


def test_assumed_paths_are_assumed_and_stated_in_the_proof_report(bypassed):
    _, h, _ = bypassed
    assume = [{"rls": "not_forced"}] + [{"bypass": p} for p in FOUND] + [{"bypass": "view gone"}]
    spec = specs.adopted_spec(assume)
    res = drift(h.conn, spec, compile_spec(spec))
    assert res.ok, [str(i) for i in res.items]
    assert len([i for i in res.by_kind("assumed") if "reaches table docs" in i.object]) == len(FOUND)
    assert any("view gone is not a bypass path now" in n for n in res.notes), res.notes
    report = to_markdown(spec, run_all(spec))
    assert "assumed: view doc_titles runs as its owner and reads `docs` without its policies" in report


def test_status_fails_on_an_unassumed_path_and_import_lists_them(bypassed, tmp_path, capsys):
    spec, h, dsn = bypassed
    p = tmp_path / "s.json"
    p.write_text(json.dumps(spec.model_dump(mode="json", by_alias=True, exclude_none=True)))
    assert main(["status", str(p), "--dsn", dsn]) == 2
    out = capsys.readouterr().out
    assert ("docs: proved (P1, P2, P3, P4, P5, P6, P7, P8, P9, P10); bypassed by view doc_counts, view doc_titles, "
            "view outer_titles, function count_docs()") in out, out
    assert out.strip().endswith("status: 3 proved; 1 bypassed (D54: see mls drift)")
    notes = import_schema(h.conn).notes
    assert {n.split(" reaches")[0] for n in notes if n.startswith("bypass path: ")} == {
        f"bypass path: {p}" for p in FOUND}
