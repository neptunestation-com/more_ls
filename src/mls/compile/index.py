"""Generated indexes (D14): one composite index per conjunctive group of a permissive policy
(membership columns first, then comparison columns), an index on every FK's referencing columns,
and a key-leading index on every relation table."""
from __future__ import annotations

from mls.compile.sql import q
from mls.spec.model import (
    And, ColRef, Cmp, Member, NowBetween, Or, Pred, Spec, Not, IsNull,
)


def _groups(p: Pred) -> list[list[str]]:
    """Index column groups for a predicate: a shallow DNF over atoms."""
    if isinstance(p, Member) and isinstance(p.member.value, ColRef):
        return [[p.member.value.col]]
    if isinstance(p, Cmp) and isinstance(p.cmp[0], ColRef):
        return [[p.cmp[0].col]]
    if isinstance(p, NowBetween):
        cols = [t.col for t in (p.now_between.from_, p.now_between.until)
                if isinstance(t, ColRef)]
        return [cols] if cols else []
    if isinstance(p, Or):
        out = []
        for a in p.or_:
            out.extend(_groups(a))
        return out
    if isinstance(p, And):
        # cross product of disjuncts of each conjunct; members first
        acc: list[list[str]] = [[]]
        for a in p.and_:
            gs = _groups(a) or [[]]
            acc = [x + y for x in acc for y in gs]
        out = []
        for g in acc:
            # order: membership columns first (they come from Member atoms); keep stable order
            seen: list[str] = []
            for c in g:
                if c not in seen:
                    seen.append(c)
            if seen:
                out.append(seen)
        return out
    return []  # Not, IsNull, Opaque, Lit: not index-driven


def _member_cols(p: Pred, out: set[str]) -> set[str]:
    if isinstance(p, Member) and isinstance(p.member.value, ColRef):
        out.add(p.member.value.col)
    elif isinstance(p, And):
        for a in p.and_:
            _member_cols(a, out)
    elif isinstance(p, Or):
        for a in p.or_:
            _member_cols(a, out)
    elif isinstance(p, Not):
        _member_cols(p.not_, out)
    return out


def index_ddl(spec: Spec) -> list[str]:
    wanted: dict[str, list[tuple[str, ...]]] = {}

    def want(table: str, cols: list[str]) -> None:
        key = tuple(cols)
        lst = wanted.setdefault(table, [])
        if key not in lst and not any(existing[:len(key)] == key for existing in lst):
            lst.append(key)

    for p in spec.policies():
        if p.mode != "permissive":
            continue
        mcols = _member_cols(p.using, set()) if p.using else set()
        for e in (p.using, p.check):
            if e is None:
                continue
            for g in _groups(e):
                ordered = [c for c in g if c in mcols] + [c for c in g if c not in mcols]
                want(p.table, ordered)
    for i in spec.inclusions():
        want(i.table, list(i.columns))
    for rel in spec.relations.values():
        t = spec.tables[rel.table]
        keysets = ([tuple(t.primary_key)] if t.primary_key else []) + [tuple(u) for u in t.unique]
        if not any(ks[0] == rel.key for ks in keysets):
            want(rel.table, [rel.key, rel.value])
    out = []
    for table, groups in wanted.items():
        t = spec.tables[table]
        for cols in groups:
            if t.primary_key and tuple(t.primary_key)[:len(cols)] == cols:
                continue
            if any(tuple(u)[:len(cols)] == cols for u in t.unique):
                continue
            name = f"mls_ix_{table}_{'_'.join(cols)}"
            out.append(f"create index {q(name)} on {q(table)} ({', '.join(q(c) for c in cols)})")
    return out
