"""The tour of the user guide (docs/GUIDE.md §3) is replayed on `examples/team_notes.mls`: each
step's spec is the example with the later steps taken out, every spec fragment the tour quotes
is a line of one of those specs, and every command it shows prints the lines it shows (`...`
marks an elision).  A change to the CLI's output, the solver's counterexamples or the example
that the tour does not follow fails here."""
from __future__ import annotations

import subprocess
import sys

from mls.spec.syntax import parse
from tests.conftest import ROOT

GUIDE = ROOT / "docs" / "GUIDE.md"
NOTES = ROOT / "examples" / "team_notes.mls"

ADMINS = "table admins owned {\n  user_id uuid not null,\n  team_id int not null;\n  primary_key (user_id, team_id)\n}\n\n"
FIXED_R4 = "R4 policy hide_archived on notes: authenticated all restrictive using archived is null or archived <> 1"
BROKEN_R4 = "R4 policy hide_archived on notes: authenticated all restrictive using not archived = 1"


def _drop(text: str, *prefixes: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True) if not line.strip().startswith(prefixes))


def steps() -> dict[str, str]:
    """The spec after each step, from the example: the comment is the example's own, not the tour's."""
    final = _drop(NOTES.read_text(), "comment ")
    assert ADMINS in final and FIXED_R4 in final
    step3 = _drop(final, "R4 ", "P2 ").replace("  body text not null,\n  archived int;", "  body text not null;")
    step2 = _drop(step3, "R3 ")
    step1 = _drop(step2.replace(ADMINS, ""), "R2 ", "relation admin_of ")
    return {"1": step1, "2": step2, "3": step3, "4": final.replace(FIXED_R4, BROKEN_R4), "5": final}


def tour() -> str:
    text = GUIDE.read_text()
    return text[text.index("## 3. A ten-minute tour"):text.index("## 4. ")]


def blocks(text: str) -> list[list[str]]:
    """The indented code blocks, de-indented, with the blank lines inside a block kept."""
    out, cur = [], []
    for line in text.splitlines() + [""]:
        if line.startswith("    ") or (cur and not line.strip()):
            cur.append(line[4:])
        elif cur:
            while cur and not cur[-1].strip():
                cur.pop()
            out.append(cur)
            cur = []
    return out


def split(block: list[str]) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """A block as the fragment before its first `$ command` line and (command, the lines it
    prints) for each command: a quoted fragment and the command run after it share a block."""
    fragment, commands = [], []
    for line in block:
        if line.startswith("$ "):
            commands.append((line[2:], []))
        elif commands:
            commands[-1][1].append(line)
        else:
            fragment.append(line)
    return fragment, commands


def run(command: str, cwd) -> tuple[int, str]:
    """In a process of its own, as a reader runs it: the solver's choice of counterexample
    depends on what the process solved before, so in-process runs print other (valid) ones."""
    args = command.split()
    assert args[:3] == ["uv", "run", "mls"], command
    r = subprocess.run([sys.executable, "-c", "import sys; from mls.cli import main; sys.exit(main(sys.argv[1:]))",
                        *args[3:]], capture_output=True, text=True, cwd=cwd)
    return r.returncode, r.stdout


def shown(lines: list[str], out: str, what: str) -> None:
    for line in lines:
        if line.strip() and line.strip() != "...":
            assert line in out, f"{what}: the guide shows {line!r}, mls prints:\n{out}"


def test_the_example_is_the_tour_finished():
    from mls.spec import load_spec

    assert load_spec(NOTES) == load_spec(NOTES.with_suffix(".mls.json"))
    assert parse(steps()["5"]) == load_spec(NOTES).model_copy(update={"comment": None})


def test_every_fragment_the_tour_quotes_is_part_of_a_step():
    specs = steps()
    first = blocks(tour())[0]
    assert first[0] == "mls 0.1"
    assert parse("\n".join(first)) == parse(specs["1"]), "the step 1 listing is the example without steps 2-4"
    for block in blocks(tour()):
        fragment, _ = split(block)
        if fragment and fragment[0].startswith(("create policy", "# What the rules say")):
            continue
        for line in fragment:
            if line.strip():
                assert any(line.strip() in s for s in specs.values()), f"{line.strip()!r} is in no step"


def test_every_command_of_the_tour_prints_what_the_guide_shows(tmp_path):
    """The commands in tour order, each on the spec of its step; the exit codes are the ones
    the prose states (2 when something is refuted)."""
    specs = steps()
    expected = [("check", "1", 0), ("prove", "1", 0), ("prove", "2", 2), ("prove", "3", 0), ("lint", "3", 0),
                ("prove", "4", 2), ("prove", "5", 0), ("compile", "5", 0), ("explain", "5", 0)]
    commands = [c for b in blocks(tour()) for c in split(b)[1]]
    assert [c.split()[3] for c, _ in commands] == [e[0] for e in expected]
    outputs = {}
    for (command, lines), (verb, step, code) in zip(commands, expected):
        (tmp_path / "notes.mls").write_text(specs[step])
        got, out = run(command, tmp_path)
        assert got == code, f"{command} at step {step} exited {got}:\n{out}"
        shown(lines, out, f"step {step}: {command}")
        outputs[verb] = out
    policies = next(b for b in blocks(tour()) if b[0].startswith("create policy"))
    shown(policies, outputs["compile"], "step 5: the compiled policies")

