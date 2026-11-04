"""Sugar that the checker expands before anything encodes, compiles, renders or proves (ROADMAP
Milestone 5): `in` with a constant list is a disjunction of `=`.  No downstream module sees an
`In` node: the encoder (`logic/tvl.py`), the templates, the English, the terse rendering and the
Lean transcription are untouched by construction."""
from __future__ import annotations

from pydantic import BaseModel

from mls.spec.model import Cmp, ColRef, Const, CtxRef, In, NewRef, OldRef, Or, Pred, Spec


def expand_pred(p: Pred) -> Pred:
    """`{"in": [t, [c1, ..., cn]]}` -> `{"or": [{"cmp": [t, "=", c1]}, ...]}` (a single constant
    is the bare comparison).  The checker has already rejected an empty list."""
    return _expand(p)


def _expand(obj):
    if isinstance(obj, In):
        t, consts = obj.in_
        cmps = [Cmp(cmp=(t, "=", c)) for c in consts]
        return cmps[0] if len(cmps) == 1 else Or(**{"or": cmps})
    if isinstance(obj, BaseModel):
        changed = {}
        for name in type(obj).model_fields:
            old = getattr(obj, name)
            new = _expand(old)
            if new is not old:
                changed[name] = new
        return obj.model_copy(update=changed) if changed else obj
    if isinstance(obj, list):
        items = [_expand(x) for x in obj]
        return items if any(a is not b for a, b in zip(items, obj)) else obj
    if isinstance(obj, tuple):
        items = tuple(_expand(x) for x in obj)
        return items if any(a is not b for a, b in zip(items, obj)) else obj
    return obj


def expand_spec(spec: Spec) -> Spec:
    """The spec with every `in` expanded, wherever a predicate occurs (rules, properties,
    invariants); the same object when there is nothing to expand."""
    return _expand(spec)


def as_in_list(p: Pred) -> tuple[ColRef | CtxRef | OldRef | NewRef, list[Const]] | None:
    """The inverse shape: an `or` of two or more `=` comparisons of one non-constant term with
    constants, which is what an expanded `in` looks like.  The SQL renderer compiles it as an IN
    list (one index condition) and the EXPLAIN check requires that condition."""
    if not isinstance(p, Or) or len(p.or_) < 2:
        return None
    first = p.or_[0]
    if not isinstance(first, Cmp) or isinstance(first.cmp[0], Const):
        return None
    term = first.cmp[0]
    consts: list[Const] = []
    for a in p.or_:
        if not (isinstance(a, Cmp) and a.cmp[1] == "=" and a.cmp[0] == term and isinstance(a.cmp[2], Const)):
            return None
        consts.append(a.cmp[2])
    return term, consts
