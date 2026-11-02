"""The SMT encoding and the concrete evaluator agree on three-valued semantics."""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from mls.logic.term import (
    Add, And, App, BoolVal, Eq, Forall, Iff, Implies, IntVal, Le, Lt, Not, Or, Sort, Term, Var,
)
from mls.logic.tvl import Encoder
from mls.pg.model import Database, eval_pred
from mls.spec.model import Spec

SPEC = Spec.model_validate({
    "mls_version": "0.1",
    "identity": {"adapter": "supabase", "context": {"uid": {"source": "auth.uid", "type": "uuid"},
                                                    "clr": {"source": "jwt.claims.clr", "type": "int"}}},
    "roles": ["app"],
    "tables": {"t": {"columns": {"a": {"type": "int", "nullable": True}, "b": {"type": "int", "nullable": True},
                                 "g": {"type": "int", "nullable": False}, "u": {"type": "uuid", "nullable": True},
                                 "ts": {"type": "timestamp", "nullable": True}}},
               "m": {"columns": {"k": {"type": "uuid", "nullable": False}, "v": {"type": "int", "nullable": False}},
                     "primary_key": ["k", "v"]}},
    "relations": {"rel": {"table": "m", "key": "k", "value": "v"}},
})


def evaluate(t: Term, env: dict, rels: dict):
    ev = lambda x: evaluate(x, env, rels)  # noqa: E731
    if isinstance(t, Var):
        return env[t.name]
    if isinstance(t, (IntVal, BoolVal)):
        return t.value
    if isinstance(t, App):
        return rels[t.fn.name](*[ev(a) for a in t.args])
    if isinstance(t, Not):
        return not ev(t.arg)
    if isinstance(t, And):
        return all(ev(a) for a in t.args)
    if isinstance(t, Or):
        return any(ev(a) for a in t.args)
    if isinstance(t, Implies):
        return (not ev(t.a)) or ev(t.b)
    if isinstance(t, (Iff, Eq)):
        return ev(t.a) == ev(t.b)
    if isinstance(t, Lt):
        return ev(t.a) < ev(t.b)
    if isinstance(t, Le):
        return ev(t.a) <= ev(t.b)
    if isinstance(t, Add):
        return ev(t.a) + ev(t.b)
    raise TypeError(t)


int_term = st.one_of(st.just({"col": "a"}), st.just({"col": "b"}), st.just({"col": "g"}),
                     st.just({"ctx": "clr"}), st.integers(-2, 2).map(lambda v: {"const": v}))
atoms = st.one_of(
    st.tuples(int_term, st.sampled_from(["=", "<>", "<", "<=", ">", ">="]), int_term).map(lambda x: {"cmp": list(x)}),
    st.just({"member": {"relation": "rel", "key": {"ctx": "uid"}, "value": {"col": "g"}}}),
    st.just({"member": {"relation": "rel", "key": {"col": "u"}, "value": {"col": "g"}}}),
    st.just({"is_null": {"col": "a"}}),
    st.just({"now_between": {"from": {"col": "ts"}, "until": None}}),
    st.booleans().map(lambda b: {"lit": b}),
)
preds = st.recursive(atoms, lambda s: st.one_of(
    st.lists(s, min_size=1, max_size=3).map(lambda l: {"and": l}),
    st.lists(s, min_size=1, max_size=3).map(lambda l: {"or": l}),
    s.map(lambda p: {"not": p})), max_leaves=6)

UUIDS = ["00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"]


@settings(max_examples=300, deadline=None)
@given(p=preds, a=st.one_of(st.none(), st.integers(-2, 2)), b=st.one_of(st.none(), st.integers(-2, 2)),
       g=st.integers(0, 2), u=st.one_of(st.none(), st.sampled_from(UUIDS)), clr=st.integers(-1, 2),
       uid=st.sampled_from(UUIDS), ts=st.one_of(st.none(), st.integers(-1, 1)),
       facts=st.frozensets(st.tuples(st.sampled_from(UUIDS), st.integers(0, 2)), max_size=4))
def test_encoding_matches_evaluator(p, a, b, g, u, clr, uid, ts, facts):
    from mls.spec.model import And as SAnd, Pred
    from pydantic import TypeAdapter

    pred = TypeAdapter(Pred).validate_python(p)
    enc = Encoder(SPEC)
    T, F = enc.tf(pred, "r", "t")
    uuid_id = {s: i + 1 for i, s in enumerate(UUIDS)}
    env = {"r.a": a if a is not None else 0, "r.a.null": a is None,
           "r.b": b if b is not None else 0, "r.b.null": b is None,
           "r.g": g, "r.u": uuid_id[u] if u else 0, "r.u.null": u is None,
           "r.ts": ts if ts is not None else 0, "r.ts.null": ts is None,
           "ctx.clr": clr, "ctx.uid": uuid_id[uid], "ctx.now": 0}
    rels = {"rel.rel": lambda k, v: any(uuid_id[fk] == k and fv == v for fk, fv in facts)}
    t, f = evaluate(T, env, rels), evaluate(F, env, rels)
    assert not (t and f)
    row = {"a": a, "b": b, "g": g, "u": u, "ts": ts}
    ctx = {"clr": clr, "uid": uid, "now": 0}
    concrete = eval_pred(pred, row, ctx, Database({"rel": set(facts)}))
    expected = True if t else (False if f else None)
    assert concrete == expected, (p, row, ctx, facts, t, f, concrete)
