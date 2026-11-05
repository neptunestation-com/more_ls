"""`mls status --dsn` on real Postgres: the live catalog adds the RLS state, the missing tables
and the tables the spec does not mention."""
from __future__ import annotations

import json

import psycopg
import pytest

from mls.cli import main
from mls.compile import compile_spec
from mls.pg.harness import Harness
from tests.conftest import EXAMPLE
from tests.templates import specs

pytestmark = pytest.mark.pg


@pytest.fixture(scope="module")
def deployed(pg_dsn):
    spec = specs.observed_spec()
    h = Harness(spec, compile_spec(spec), pg_dsn, "mls_status")
    h.deploy()
    with h.conn.cursor() as cur:
        cur.execute('create table "audit" (id int primary key)')  # the observed table exists
        cur.execute('create table "scratch" (id int primary key)')  # a table the spec does not mention
    yield h
    h.close()


def test_status_reads_the_live_catalog(deployed, tmp_path, capsys):
    dsn = psycopg.conninfo.make_conninfo(deployed.dsn, dbname=deployed.dbname)
    d = json.loads(EXAMPLE.read_text())
    d["tables"]["audit"] = {"columns": {"id": {"type": "int", "nullable": False}}, "mode": "observed"}
    p = tmp_path / "s.json"
    p.write_text(json.dumps(d))
    assert main(["status", str(p), "--dsn", dsn]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-3:] == ["audit: ungoverned (observed: the spec does not govern it)",
                        "scratch: ungoverned (not in the spec)",
                        "status: 2 ungoverned, 3 proved"]
    # RLS switched off behind the spec's back: ungoverned, exit 2
    with deployed.conn.cursor() as cur:
        cur.execute('alter table "docs" disable row level security')
    try:
        assert main(["status", str(p), "--dsn", dsn, "--json"]) == 2
        j = json.loads(capsys.readouterr().out)
        assert j["ok"] is False
        docs = next(t for t in j["tables"] if t["table"] == "docs")
        assert docs["status"] == "ungoverned" and docs["detail"].startswith("row level security is not enabled")
    finally:
        with deployed.conn.cursor() as cur:
            cur.execute('alter table "docs" enable row level security')
    # a governed table the database lacks
    d["tables"]["files"] = {"columns": {"id": {"type": "int", "nullable": False}}, "primary_key": ["id"]}
    p.write_text(json.dumps(d))
    assert main(["status", str(p), "--dsn", dsn]) == 2
    assert "files: ungoverned (missing from the database)" in capsys.readouterr().out
