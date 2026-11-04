"""Shared test vectors for the Lean transcription (D43).

Generates `lean/MLS/Vectors.lean`: concrete predicates, rows, contexts, relation facts and
hierarchy rows with (a) the truth value the Python evaluator gives (`pg/model.py:eval_pred`),
(b) the pair the Python encoder emits under the matching valuation (`logic/tvl.py`), and (c)
the statement outcomes of the oracle (`pg/model.py:Oracle`) against random policy sets.  Each
vector is a `#guard` on the Lean transcription (`MLS.Pred.eval`, `MLS.Pred.tf`, `MLS.Rules`),
so the Lean build fails when a transcription and its original disagree.  The file is
committed; `tests/lean/test_vectors.py` checks it is what this module generates.

    uv run python -m tests.lean.vectors      # regenerate
"""
from __future__ import annotations

import random
from pathlib import Path

from pydantic import TypeAdapter

from mls.logic.tvl import Encoder
from mls.pg.model import Database, Oracle, eval_pred
from mls.spec.model import Pred, Spec
from tests.logic.test_tvl import SPEC as BASE, evaluate

OUT = Path(__file__).resolve().parents[2] / "lean" / "MLS" / "Vectors.lean"
SEED = 20261104
N_PRED, N_GROUP, N_OUTCOME = 160, 60, 120

UUIDS = ["00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"]
UUID_ID = {s: i + 1 for i, s in enumerate(UUIDS)}

BASE_JSON = BASE.model_dump(mode="json", exclude_none=True)

# the outcome vectors add one check and one transition rule to the base spec
CHECK = {"id": "C1", "kind": "check", "name": "a_small", "table": "t",
         "predicate": {"or": [{"is_null": {"col": "a"}}, {"cmp": [{"col": "a"}, "<=", {"const": 1}]}]}}
TRANSITION = {"id": "T1", "kind": "transition", "name": "b_fixed", "table": "t",
              "predicate": {"or": [{"is_null": {"old": "b"}}, {"cmp": [{"new": "b"}, "=", {"old": "b"}]}]}}

# a hierarchy spec for in_group
HIER_JSON = {
    "mls_version": "0.1",
    "identity": {"adapter": "supabase", "context": {"uid": {"source": "auth.uid", "type": "uuid"}}},
    "roles": ["app"],
    "tables": {
        "groups": {"columns": {"id": {"type": "int", "nullable": False}, "parent_id": {"type": "int", "nullable": True},
                               "depth": {"type": "int", "nullable": False}}, "primary_key": ["id"]},
        "group_members": {"columns": {"user_id": {"type": "uuid", "nullable": False},
                                      "group_id": {"type": "int", "nullable": False}},
                          "primary_key": ["user_id", "group_id"]},
        "files": {"columns": {"id": {"type": "int", "nullable": False}, "group_id": {"type": "int", "nullable": False}},
                  "primary_key": ["id"]},
    },
    "relations": {"members": {"table": "group_members", "key": "user_id", "value": "group_id"}},
    "hierarchies": {"org": {"table": "groups", "id": "id", "parent": "parent_id", "depth": "depth",
                            "max_depth": 2, "members": "members"}},
}
HIER = Spec.model_validate(HIER_JSON)
IN_GROUP = {"in_group": {"hierarchy": "org", "key": {"ctx": "uid"}, "value": {"col": "group_id"}}}


# ----------------------------------------------------------------------------- Lean rendering
def lint(v: int) -> str:
    return f"({v} : Int)" if v < 0 else str(v)


def lopt(v: int | None) -> str:
    return "none" if v is None else f"some {lint(v)}"


def lstr(s: str) -> str:
    return '"' + s + '"'


OPS = {"=": "Op.eq", "<>": "Op.ne", "<": "Op.lt", "<=": "Op.le", ">": "Op.gt", ">=": "Op.ge"}


def lterm(t: dict) -> str:
    (k, v), = t.items()
    if k == "const":
        return f"(Term.const {lint(v)})"
    return f"(Term.{k} {lstr(v)})"


def lpred(p: dict) -> str:
    (k, v), = p.items()
    if k == "lit":
        return f"(Pred.lit {'true' if v else 'false'})"
    if k == "and":
        return "(andChain [" + ", ".join(lpred(q) for q in v) + "])"
    if k == "or":
        return "(orChain [" + ", ".join(lpred(q) for q in v) + "])"
    if k == "not":
        return f"(Pred.not {lpred(v)})"
    if k == "is_null":
        return f"(Pred.isNull {lterm(v)})"
    if k == "cmp":
        l, op, r = v
        return f"(Pred.cmp {lterm(l)} {OPS[op]} {lterm(r)})"
    if k == "member":
        return f"(Pred.member {lstr(v['relation'])} {lterm(v['key'])} {lterm(v['value'])})"
    if k == "in_group":
        return f"(Pred.inGroup {lstr(v['hierarchy'])} {lterm(v['key'])} {lterm(v['value'])})"
    if k == "now_between":
        lo = "none" if v.get("from") is None else f"(some {lterm(v['from'])})"
        hi = "none" if v.get("until") is None else f"(some {lterm(v['until'])})"
        return f"(Pred.nowBetween {lo} {hi})"
    raise ValueError(p)


def lcols(images: dict[str, dict[str, int | None]]) -> str:
    items = [f"({lstr(img)}, {lstr(c)}, {lopt(v)})" for img, row in images.items() for c, v in row.items()]
    return "[" + ", ".join(items) + "]"


def lints(d: dict[str, int]) -> str:
    return "[" + ", ".join(f"({lstr(k)}, {lint(v)})" for k, v in d.items()) + "]"


def lfacts(rel: str, facts: list[tuple[int, int]]) -> str:
    return "[" + ", ".join(f"({lstr(rel)}, {lint(a)}, {lint(b)})" for a, b in sorted(facts)) + "]"


def lforest(h: str, nodes: list[tuple[int, int | None]]) -> str:
    return "[(" + lstr(h) + ", [" + ", ".join(f"({lint(i)}, {lopt(p)})" for i, p in nodes) + "])]"


def ltv(v: bool | None) -> str:
    return "TV.tt" if v is True else ("TV.ff" if v is False else "TV.unk")


def lpair(t: bool, f: bool) -> str:
    return f"(⟨{str(t).lower()}, {str(f).lower()}⟩ : Pair)"


def loutcome(o: str) -> str:
    return {"filtered": "Outcome.filtered", "ok": "Outcome.ok", "error": "Outcome.error"}[o]


# ----------------------------------------------------------------------------- random data
INT_TERMS = [{"col": "a"}, {"col": "b"}, {"col": "g"}, {"ctx": "clr"}]


def rand_term(r: random.Random, old_new: bool = False) -> dict:
    if old_new:
        return r.choice([{"old": "a"}, {"new": "a"}, {"old": "b"}, {"new": "b"}, {"const": r.randint(-2, 2)}])
    return r.choice(INT_TERMS + [{"const": r.randint(-2, 2)}])


def rand_atom(r: random.Random, old_new: bool = False) -> dict:
    if old_new:
        k = r.choice(["cmp", "cmp", "is_null", "lit"])
        if k == "cmp":
            return {"cmp": [rand_term(r, True), r.choice(list(OPS)), rand_term(r, True)]}
        if k == "is_null":
            return {"is_null": r.choice([{"old": "a"}, {"new": "a"}, {"old": "b"}, {"new": "b"}])}
        return {"lit": r.random() < 0.5}
    k = r.choice(["cmp", "cmp", "cmp", "member", "member_col", "is_null", "now", "now2", "uuid", "lit"])
    if k == "cmp":
        return {"cmp": [rand_term(r), r.choice(list(OPS)), rand_term(r)]}
    if k == "member":
        return {"member": {"relation": "rel", "key": {"ctx": "uid"}, "value": {"col": "g"}}}
    if k == "member_col":
        return {"member": {"relation": "rel", "key": {"col": "u"}, "value": {"col": "g"}}}
    if k == "is_null":
        return {"is_null": r.choice([{"col": "a"}, {"col": "b"}, {"col": "u"}, {"col": "ts"}])}
    if k == "now":
        return {"now_between": {"from": {"col": "ts"}, "until": None}}
    if k == "now2":
        return {"now_between": {"from": None, "until": {"col": "ts"}}}
    if k == "uuid":
        return {"cmp": [{"col": "u"}, r.choice(["=", "<>"]), {"ctx": "uid"}]}
    return {"lit": r.random() < 0.5}


def rand_pred(r: random.Random, depth: int = 0, old_new: bool = False) -> dict:
    if depth >= 3 or r.random() < 0.4:
        return rand_atom(r, old_new)
    k = r.choice(["and", "or", "not"])
    if k == "not":
        return {"not": rand_pred(r, depth + 1, old_new)}
    return {k: [rand_pred(r, depth + 1, old_new) for _ in range(r.randint(1, 3))]}


def rand_row(r: random.Random) -> dict:
    return {"a": r.choice([None, -1, 0, 1, 2]), "b": r.choice([None, -1, 0, 1, 2]), "g": r.randint(0, 2),
            "u": r.choice([None] + UUIDS), "ts": r.choice([None, -1, 0, 1])}


def rand_ctx(r: random.Random) -> dict:
    return {"clr": r.randint(-1, 2), "uid": r.choice(UUIDS), "now": 0}


def rand_facts(r: random.Random) -> list[tuple[str, int]]:
    return sorted({(r.choice(UUIDS), r.randint(0, 2)) for _ in range(r.randint(0, 4))})


def row_images(row: dict) -> dict[str, int | None]:
    return {c: (UUID_ID[v] if isinstance(v, str) else v) for c, v in row.items()}


def smt_env(images: dict[str, dict], ctx: dict) -> dict:
    env = {}
    for img, row in images.items():
        for c, v in row.items():
            env[f"{img}.{c}"] = v if v is not None else 0
            env[f"{img}.{c}.null"] = v is None
    for k, v in ctx.items():
        env[f"ctx.{k}"] = UUID_ID[v] if isinstance(v, str) else v
    return env


# ----------------------------------------------------------------------------- the vectors
def pred_vectors(r: random.Random) -> list[str]:
    out = []
    for i in range(N_PRED):
        p = rand_pred(r)
        row, ctx, facts = rand_row(r), rand_ctx(r), rand_facts(r)
        pred = TypeAdapter(Pred).validate_python(p)
        concrete = eval_pred(pred, row, ctx, Database({"rel": set(facts)}))
        enc = Encoder(BASE)
        T, F = enc.tf(pred, "r", "t")
        images = {"r": row_images(row)}
        rels = {"rel.rel": lambda k, v, fs=facts: any(UUID_ID[fk] == k and fv == v for fk, fv in fs)}
        t, f = evaluate(T, env := smt_env(images, ctx), rels), evaluate(F, env, rels)
        assert not (t and f) and concrete == (True if t else (False if f else None))
        ictx = {k: (UUID_ID[v] if isinstance(v, str) else v) for k, v in ctx.items()}
        ifacts = [(UUID_ID[k], v) for k, v in facts]
        out.append(f"-- P{i}\n"
                   f"def pv{i} : Pred := {lpred(p)}\n"
                   f"def pc{i} : Cols := {lcols(images)}\n"
                   f"def px{i} : Ctx := {lints(ictx)}\n"
                   f"def pf{i} : Facts := {lfacts('rel', ifacts)}\n"
                   f"#guard Pred.eval SB (Vec.env pc{i} px{i} pf{i} []) \"r\" pv{i} = {ltv(concrete)}\n"
                   f"#guard Pred.tf SB (Vec.val pc{i} px{i} pf{i} []) \"r\" pv{i} = {lpair(t, f)}\n")
    return out


def group_vectors(r: random.Random) -> list[str]:
    out = []
    pred = TypeAdapter(Pred).validate_python(IN_GROUP)
    for i in range(N_GROUP):
        n = r.randint(1, 5)
        nodes: list[tuple[int, int | None]] = []
        depth: dict[int, int] = {}
        for gid in range(n):
            parent = None if gid == 0 or r.random() < 0.3 else r.randint(0, gid - 1)
            nodes.append((gid, parent))
            depth[gid] = 0 if parent is None else depth[parent] + 1
        r.shuffle(nodes)
        # the members FK (D35): facts name stored groups; the row's group may be unknown (n)
        facts = sorted({(r.choice(UUIDS), r.randint(0, n - 1)) for _ in range(r.randint(0, 3))})
        row = {"id": 1, "group_id": r.randint(0, n)}
        ctx = {"uid": r.choice(UUIDS), "now": 0}
        db = Database({"members": set(facts)},
                      {"groups": [{"id": g, "parent_id": p, "depth": depth[g]} for g, p in nodes]}, spec=HIER)
        concrete = eval_pred(pred, row, ctx, db)
        enc = Encoder(HIER)
        T, F = enc.tf(pred, "r", "files")
        images = {"r": dict(row)}
        parent = dict(nodes)
        rels = {"rel.members": lambda k, v, fs=facts: any(UUID_ID[fk] == k and fv == v for fk, fv in fs),
                "hier.org.has_parent": lambda x, pa=parent: x in pa and pa[x] is not None,
                "hier.org.parent": lambda x, pa=parent: pa[x] if x in pa and pa[x] is not None else 0}
        t, f = evaluate(T, env := smt_env(images, ctx), rels), evaluate(F, env, rels)
        assert not (t and f) and concrete == (True if t else (False if f else None))
        ictx = {k: (UUID_ID[v] if isinstance(v, str) else v) for k, v in ctx.items()}
        ifacts = [(UUID_ID[k], v) for k, v in facts]
        out.append(f"-- G{i}\n"
                   f"def gc{i} : Cols := {lcols(images)}\n"
                   f"def gx{i} : Ctx := {lints(ictx)}\n"
                   f"def gf{i} : Facts := {lfacts('members', ifacts)}\n"
                   f"def gh{i} : Hiers := {lforest('org', nodes)}\n"
                   f"#guard Pred.eval SH (Vec.env gc{i} gx{i} gf{i} gh{i}) \"r\" GP = {ltv(concrete)}\n"
                   f"#guard Pred.tf SH (Vec.val gc{i} gx{i} gf{i} gh{i}) \"r\" GP = {lpair(t, f)}\n")
    return out


def rand_policies(r: random.Random, cmd: str) -> list[dict]:
    """Permissive policies for `cmd` (or all) and for select — so the oracle's grants are table
    level and the outcome depends on the modelled layers only — plus restrictive ones."""
    out = []

    def policy(command: str, mode: str) -> dict:
        p = {"id": f"R{len(out) + 1}", "kind": "policy", "name": f"p{len(out) + 1}", "table": "t",
             "role": "app", "command": command, "mode": mode}
        if command in ("select", "delete"):
            p["using"] = rand_pred(r, 1)
        elif command == "insert":
            p["check"] = rand_pred(r, 1)
        else:
            p["using"] = rand_pred(r, 1)
            if r.random() < 0.6:
                p["check"] = rand_pred(r, 1)
        return p

    out.append(policy(r.choice([cmd, "all"]), "permissive"))
    if cmd != "select":
        out.append(policy("select", "permissive"))
    for _ in range(r.randint(0, 2)):
        out.append(policy(r.choice([cmd, "all", "select", "insert", "update", "delete"]),
                          r.choice(["permissive", "restrictive"])))
    r.shuffle(out)
    return out


def lpolicy(p: dict) -> str:
    def opt(k):
        return "none" if p.get(k) is None else f"(some {lpred(p[k])})"
    return f"Vec.policy Command.{p['command']} Mode.{p['mode']} {opt('using')} {opt('check')}"


def outcome_vectors(r: random.Random) -> list[str]:
    out = []
    for i in range(N_OUTCOME):
        cmd = r.choice(["select", "insert", "update", "delete"])
        policies = rand_policies(r, cmd)
        spec = Spec.model_validate({**BASE_JSON, "rules": policies + [CHECK, TRANSITION]})
        oracle = Oracle(spec)
        old, new, ctx, facts = rand_row(r), rand_row(r), rand_ctx(r), rand_facts(r)
        db = Database({"rel": set(facts)}, {"t": [old]})
        reads = r.random() < 0.5
        if cmd == "select":
            res = oracle.visible("app", old, ctx, db, "t")
            images = {"r": row_images(old)}
            expect = f"= {str(res).lower()}"
        elif cmd == "insert":
            res = oracle.insert("app", "t", new, ctx, db)
            images = {"new": row_images(new)}
            expect = f"= {loutcome(res)}"
        elif cmd == "update":
            res = oracle.update("app", "t", old, new, ctx, db, ["a", "b"], ["g"] if reads else [])
            images = {"old": row_images(old), "new": row_images(new)}
            expect = f"= {loutcome(res)}"
        else:
            res = oracle.delete("app", "t", old, ctx, db, ["g"] if reads else [])
            images = {"old": row_images(old)}
            expect = f"= {loutcome(res)}"
        ictx = {k: (UUID_ID[v] if isinstance(v, str) else v) for k, v in ctx.items()}
        ifacts = [(UUID_ID[k], v) for k, v in facts]
        rules = ("{ policies := [" + ", ".join(lpolicy(p) for p in policies) + "], "
                 f"constraints := [{lpred(CHECK['predicate'])}], transitions := [{lpred(TRANSITION['predicate'])}] " + "}")
        if cmd == "select":
            guard = f"#guard Rules.visible or{i} SB \"t\" \"app\" (Vec.env oc{i} ox{i} of{i} []) {expect}"
        elif cmd == "insert":
            guard = f"#guard Rules.insert or{i} SB \"t\" \"app\" (Vec.env oc{i} ox{i} of{i} []) {expect}"
        elif cmd == "update":
            guard = f"#guard Rules.update or{i} SB \"t\" \"app\" {str(reads).lower()} (Vec.env oc{i} ox{i} of{i} []) {expect}"
        else:
            guard = f"#guard Rules.delete or{i} SB \"t\" \"app\" {str(reads).lower()} (Vec.env oc{i} ox{i} of{i} []) {expect}"
        out.append(f"-- O{i} ({cmd})\n"
                   f"def or{i} : Rules := {rules}\n"
                   f"def oc{i} : Cols := {lcols(images)}\n"
                   f"def ox{i} : Ctx := {lints(ictx)}\n"
                   f"def of{i} : Facts := {lfacts('rel', ifacts)}\n"
                   f"{guard}\n")
    return out


HEADER = '''import MLS.Harness

/-!
# Shared test vectors (D43) — GENERATED by `tests/lean/vectors.py`; do not edit.

Every `#guard` compares a Lean transcription with what the Python original computed on the
same data: `Pred.eval` with `pg/model.py:eval_pred`, `Pred.tf` with `logic/tvl.py:Encoder.tf`
under the harness valuation, `Rules.visible/insert/update/delete` with `pg/model.py:Oracle`.
-/
namespace MLS

open Vec (Cols Ctx Facts Hiers)

/-- `tests/logic/test_tvl.py:SPEC`: nullable a, b, u, ts; g NOT NULL. -/
def SB : Schema := Vec.schema ["a", "b", "u", "ts"] []

/-- The hierarchy spec of the generator: `org` over `groups`, members relation `members`,
`max_depth` 2. -/
def SH : Schema := Vec.schema ["parent_id"] [("org", "members", 2)]

/-- `in_group(org, uid, group_id)` -/
def GP : Pred := Pred.inGroup "org" (Term.ctx "uid") (Term.col "group_id")

'''


def generate() -> str:
    r = random.Random(SEED)
    parts = [HEADER, "/-! ## Predicates: evaluator and encoder -/\n\n"]
    parts += pred_vectors(r)
    parts.append("\n/-! ## `in_group`: the walk and the unrolling -/\n\n")
    parts += group_vectors(r)
    parts.append("\n/-! ## Statement outcomes -/\n\n")
    parts += outcome_vectors(r)
    parts.append("\nend MLS\n")
    return "\n".join(parts)


def main() -> None:
    OUT.write_text(generate())
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
