"""The takeover of the user guide (docs/GUIDE.md §3) is replayed on the Supabase starter
(`tests/brownfield/starter_schema.sql` with `starter_data.sql`) on the `supabase/postgres` image,
as `postgres`: every command it shows runs and prints the lines it shows (`...` marks an
elision; `mls generate` replays the example's rules and properties as the model's answer through
`MLS_LLM_SCRIPT`, and must write the example), every spec line it quotes is a line of `examples/starter_takeover.mls`, the skeleton
the import writes is that example without the lines the guide adds, and every note it quotes
is one the import prints.  Between the confirmation and the rollback the differential test runs
on the deployed database (the schema's own foreign keys respected, `pg/catalog.py:foreign_keys`);
after the rollback, the deploy runs again and drift is clean."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

import psycopg
import pytest

from mls.compile import compile_spec
from mls.pg.catalog import foreign_keys
from mls.pg.harness import Harness, run_differential
from mls.props.effective import simple_fks
from mls.spec import load_checked, load_spec
from mls.terse import section
from tests.conftest import ROOT
from tests.test_guide import blocks, shown, split

GUIDE = ROOT / "docs" / "GUIDE.md"
EXAMPLE = ROOT / "examples" / "starter_takeover.mls"
BROWNFIELD = ROOT / "tests" / "brownfield"
DB = "mls_takeover"
ROLLBACK = "### Step 13: rollback"

# (the command's first words, its exit code), in the order the guide shows them
EXPECTED = [("pg_dump", 0), ("uv run mls import", 0), ("uv run mls import", 0), ("psql", 0), ("psql", 0),
            ("uv run mls explain", 0), ("uv run mls generate", 0), ("uv run mls prove", 0), ("uv run mls diff", 2), ("uv run mls drift", 1),
            ("uv run mls preflight", 0), ("uv run mls compile", 0), ("psql", 0), ("uv run mls drift", 0),
            ("uv run mls status", 0),
            ("uv run mls compile", 0), ("psql", 0), ("psql", 0), ("uv run mls drift", 0)]


def takeover() -> str:
    text = GUIDE.read_text()
    return text[text.index("## 3. Taking over the schema you have"):text.index("## 4. ")]


@pytest.fixture(scope="module")
def supabase():
    if os.environ.get("MLS_SKIP_PG"):
        pytest.skip("MLS_SKIP_PG set")
    from mls.pg.server import supabase_container

    with supabase_container() as server:
        yield server


@pytest.fixture
def project(supabase):
    """The project's database: the starter schema and its data, created and loaded as `postgres`."""
    with psycopg.connect(supabase.dsn, autocommit=True) as admin:
        admin.execute(f'drop database if exists "{DB}" with (force)')
        admin.execute(f'create database "{DB}"')
    supabase.prepare(DB)
    dsn = psycopg.conninfo.make_conninfo(supabase.dsn, dbname=DB)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute((BROWNFIELD / "starter_schema.sql").read_text())
        conn.execute((BROWNFIELD / "starter_data.sql").read_text())
    yield dsn
    with psycopg.connect(supabase.dsn, autocommit=True) as admin:
        admin.execute(f'drop database if exists "{DB}" with (force)')


class Shell:
    """Runs a command of the guide as a reader would: `uv run mls` in a process of its own (the
    solver's counterexamples depend on what the process solved before), `psql -c` and `psql -f`
    over psycopg with the notices printed as psql prints them, `pg_dump` in the container."""

    def __init__(self, supabase, dsn: str, cwd, env: dict[str, str] | None = None):
        self.supabase, self.dsn, self.cwd, self.env = supabase, dsn, cwd, env or {}

    def run(self, command: str) -> tuple[int, str]:
        args = [self.dsn if a == "$DATABASE_URL" else a for a in shlex.split(command)]
        if args[:3] == ["uv", "run", "mls"]:
            r = subprocess.run([sys.executable, "-c", "import sys; from mls.cli import main; sys.exit(main(sys.argv[1:]))",
                                *args[3:]], capture_output=True, text=True, cwd=self.cwd,
                               env={**os.environ, **self.env})
            return r.returncode, r.stdout + r.stderr
        if args[0] == "psql":
            assert args[1] == self.dsn, command
            return 0, self._psql(args[2:])
        assert args[0] == "pg_dump", command
        inside = psycopg.conninfo.make_conninfo("postgresql://postgres:postgres@localhost:5432", dbname=DB)
        code, out = self.supabase.container.exec([*args[:-1], inside])
        return code, out.decode()

    def _psql(self, args: list[str]) -> str:
        notices: list[str] = []
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.add_notice_handler(lambda d: notices.append(f"NOTICE:  {d.message_primary}"))
            if args[0] == "-c":
                conn.execute(args[1])
            else:
                assert args[:4] == ["--single-transaction", "-v", "ON_ERROR_STOP=1", "-f"], args
                with conn.transaction():
                    conn.execute((self.cwd / args[4]).read_text())
        return "\n".join(notices)


def _commands(text: str) -> list[tuple[str, list[str]]]:
    return [c for b in blocks(text) for c in split(b)[1]]


def _fragments(text: str) -> list[list[str]]:
    return [f for f, cs in map(split, blocks(text)) if f and not cs]


def _policies(dsn: str) -> set[tuple[str, str]]:
    with psycopg.connect(dsn) as conn:
        return set(conn.execute("select tablename, policyname from pg_policies where schemaname = 'public'").fetchall())


def _requirements(text: str) -> str:
    """The sentences step 5 writes to requirements.txt."""
    step5 = text[text.index("### Step 5:"):text.index("### Step 6:")] + "(end of step 5)"  # closes its last block
    [sentences] = [f for f in _fragments(step5) if f[0].startswith("Anyone")]
    return "\n".join(sentences) + "\n"


def _answer(tmp_path) -> str:
    """The model's answer `mls generate` replays: the example's properties, then its rules without
    the two the import read (the loop appends those)."""
    spec = load_spec(EXAMPLE)
    rules = [r for r in spec.rules if r.id not in ("R1", "R2")]
    script = tmp_path / "answer.json"
    script.write_text(json.dumps([section("properties", spec.properties), section("rules", rules)]))
    return str(script)


def test_the_example_is_the_skeleton_with_the_guide_lines():
    """No Docker: every spec line the guide quotes is in the example, and the example is what the
    guide says: the skeleton with the composed relation, the rules R3-R13 and the properties."""
    example = EXAMPLE.read_text()
    load_checked(EXAMPLE)
    spec_lines = [line.strip() for f in _fragments(takeover())
                  if f[0].startswith(("relation ", "rules {", "R", "P"))
                  for line in f if line.strip() not in ("rules {", "}")]
    assert len(spec_lines) == 1 + 3 + 11 + 9
    for line in spec_lines:
        assert line in example, f"the guide quotes {line!r}, which is not in {EXAMPLE.name}"


@pytest.mark.pg
@pytest.mark.supabase
def test_the_takeover_replays_on_the_starter(supabase, project, tmp_path):
    text = takeover()
    shell = Shell(supabase, project, tmp_path, env={"MLS_LLM_SCRIPT": _answer(tmp_path)})
    before, after = _commands(text[:text.index(ROLLBACK)]), _commands(text[text.index(ROLLBACK):])
    assert [(c.split()[0] if c.startswith(("psql", "pg_dump")) else " ".join(c.split()[:4])) for c, _ in before + after] == [
        e for e, _ in EXPECTED]
    codes = iter(code for _, code in EXPECTED)
    old_policies = _policies(project)
    outputs = []

    def replay(commands):
        for command, lines in commands:
            got, out = shell.run(command)
            assert got == next(codes), f"{command} exited {got}:\n{out}"
            shown(lines, out, command)
            outputs.append((command, out))
            if "--policies discard" in command:
                discard_imported(out)
            if " generate " in command:
                generated()

    def discard_imported(out: str) -> None:
        # the notes the guide quotes are the import's, and the skeleton is the example without
        # the lines step 6 adds; then the reader adds the relation and writes the requirements
        for fragment in _fragments(text):
            if fragment[0].startswith("note:"):
                shown(fragment, out, "the notes of the discard import")
        skeleton = (tmp_path / "spec.mls").read_text().splitlines()
        example = EXAMPLE.read_text().splitlines()
        added = [line for line in example if line.strip().startswith(("relation in_project", "R", "P"))
                 and not line.strip().startswith(("R1 ", "R2 "))]
        assert [l for l in skeleton if not l.startswith("comment ")] == [
            l for l in example if l not in added and not l.startswith("comment ")]
        relation = next(l for l in example if l.startswith("relation in_project"))
        at = skeleton.index(next(l for l in skeleton if l.startswith("relation team_members"))) + 1
        (tmp_path / "spec.mls").write_text("\n".join(skeleton[:at] + [relation] + skeleton[at:]) + "\n")
        (tmp_path / "requirements.txt").write_text(_requirements(text))

    def generated() -> None:
        # what generate wrote is the example: the model's rules, then the two imported ones
        got, want = load_spec(tmp_path / "spec.mls"), load_spec(EXAMPLE)
        assert [r.id for r in got.rules] == [f"R{i}" for i in range(3, 14)] + ["R1", "R2"]
        key = lambda r: int(r.id[1:])  # noqa: E731
        assert got.model_copy(update={"comment": None, "rules": sorted(got.rules, key=key)}) == \
            want.model_copy(update={"comment": None})

    replay(before)
    assert (tmp_path / "before.mls").exists() and (tmp_path / "deploy.sql").exists()
    prove = next(out for c, out in outputs if " prove " in c)
    assert "**9 proved, 0 refuted, 0 unknown**" in prove

    # deployed: the old policies are gone, and the database behaves as the model says
    spec = load_checked(EXAMPLE)
    live = _policies(project)
    assert old_policies and not old_policies & live
    assert {p for t, p in live if not p.startswith("mls_rel_")} == {p.name for p in spec.policies()}
    with psycopg.connect(project, autocommit=True) as conn:
        owned = {r.name for r in spec.inclusions()} | {f.name for f in simple_fks(spec)}
        fks = foreign_keys(conn, "public", list(spec.governed()), owned)
    assert {(f["table"], f["column"], f["parent"]) for f in fks} >= {
        ("tasks", "project_id", "projects"), ("projects", "team_id", "teams"), ("profiles", "id", "auth.users")}
    h = Harness(spec, compile_spec(spec), supabase.dsn, DB, foreign=fks)
    h.attach()
    try:
        for seed in (1, 2):
            res = run_differential(spec, h.script, supabase.dsn, DB, harness=h, seed=seed,
                                   rows_per_table=20, n_contexts=12, ops_per_context=25)
            assert res.ok, f"seed {seed}: {res.summary()}\n" + "\n".join(map(str, res.mismatches[:10]))
            assert res.visible_rows > 0, res.summary()
    finally:
        h.close(drop=False)

    replay(after)
    restored = _policies(project)
    assert not restored & live and {t for t, _ in restored} == {"profiles"}

    # the deploy runs again after a rollback (the indexes it creates on adopted tables are
    # `if not exists`), and drift is clean
    for command in ('psql "$DATABASE_URL" --single-transaction -v ON_ERROR_STOP=1 -f deploy.sql',
                    'uv run mls drift spec.mls --dsn "$DATABASE_URL"'):
        got, out = shell.run(command)
        assert got == 0, f"{command} exited {got}:\n{out}"
    assert "drift: none" in out
