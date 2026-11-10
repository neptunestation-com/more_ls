"""The levels of a relation (D56).  A base relation is one level, its table's (key, value); a
composed relation adds one level per hop.  Every consumer that needs a relation's tables reads
them here, so a composed relation is never mistaken for one table.

The compiler also needs the prefixes of a chain (the D21 visibility policy of hop `i` reads the
relation up to hop `i - 1`): `name/i` is the relation `name` cut after its `i`-th hop.  A prefix
name never appears in a spec (relation names are identifiers); it is internal to the derived
policies and their encodings."""
from __future__ import annotations

from dataclasses import dataclass

from mls.spec.model import ComposedRelation, Relation, Spec


@dataclass(frozen=True)
class Level:
    table: str
    key: str
    value: str


def split(name: str) -> tuple[str, int | None]:
    """`r/2` -> (`r`, 2); `r` -> (`r`, None)."""
    if "/" in name:
        base, _, i = name.partition("/")
        return base, int(i)
    return name, None


def prefix(name: str, hops: int) -> str:
    """The relation `name` cut after `hops` hops (0: its base relation)."""
    return name if hops is None else f"{name}/{hops}"


def levels(spec: Spec, name: str) -> list[Level]:
    rname, cut = split(name)
    rel = spec.relations[rname]
    if isinstance(rel, Relation):
        return [Level(rel.table, rel.key, rel.value)]
    assert isinstance(rel, ComposedRelation)
    base = spec.relations[rel.of]
    out = [Level(base.table, base.key, base.value)] + [Level(h.table, h.key, h.value) for h in rel.through]
    return out if cut is None else out[:cut + 1]


def base_name(spec: Spec, name: str) -> str:
    """The base relation a relation (or a prefix of one) starts from."""
    rname, _ = split(name)
    rel = spec.relations[rname]
    return rel.of if isinstance(rel, ComposedRelation) else rname


def is_composed(spec: Spec, name: str) -> bool:
    return len(levels(spec, name)) > 1


def hop_prefix(spec: Spec, name: str, i: int) -> str:
    """The relation whose values are the keys of level `i` (i ≥ 1): the base relation for the
    first hop, else the prefix `name/(i-1)`."""
    return base_name(spec, name) if i == 1 else prefix(split(name)[0], i - 1)


def key_type(spec: Spec, name: str) -> str:
    lv = levels(spec, name)[0]
    return spec.tables[lv.table].columns[lv.key].type


def value_type(spec: Spec, name: str) -> str:
    lv = levels(spec, name)[-1]
    return spec.tables[lv.table].columns[lv.value].type
