"""Render a solver model as a concrete scenario (the human-facing counterexample, D10)."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from mls.logic.solve import Model
from mls.logic.term import Sort
from mls.logic.tvl import rel_fun
from mls.props.obligations import Obligation
from mls.spec.model import CONTEXT_NOW, Spec


@dataclass
class Scenario:
    context: dict[str, Any] = field(default_factory=dict)
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)     # row name -> {table, values}
    relations: dict[str, list[list[Any]]] = field(default_factory=dict)  # "rel[@tag]" -> pairs

    def to_dict(self) -> dict:
        return asdict(self)

    def to_markdown(self) -> str:
        lines = []
        if self.context:
            lines.append("- context: " + ", ".join(f"{k} = {v}" for k, v in self.context.items()))
        for name, r in self.rows.items():
            vals = ", ".join(f"{k} = {v}" for k, v in r["values"].items())
            lines.append(f"- {name} ({r['table']}): {vals}")
        for name, pairs in self.relations.items():
            shown = ", ".join(f"({a}, {b})" for a, b in pairs) if pairs else "∅"
            lines.append(f"- {name} = {{{shown}}}")
        return "\n".join(lines)


def _render(spec: Spec, type_: str, v: int | None, consts: dict[int, str], now: int | None) -> Any:
    if v is None:
        return None
    if type_ == "bool":
        return v != 0
    if type_ in ("text", "uuid"):
        if v in consts:
            return consts[v]
        return f"{type_}#{v}"
    if type_ == "timestamp":
        if now is not None:
            d = v - now
            return "now" if d == 0 else (f"now+{d}" if d > 0 else f"now{d}")
        return f"t#{v}"
    return v


def render_scenario(spec: Spec, ob: Obligation, model: Model) -> Scenario:
    consts: dict[int, str] = {}
    for e in ob.encoders:
        for s, i in e.consts.items():
            consts[i] = repr(s)
    sc = Scenario()
    universe: dict[str, set[int]] = {}  # type -> integers that appear with that type
    const_types: dict[str, set[str]] = {}  # encoders may share the interning table
    for e in ob.encoders:
        for value, tys in e.const_types.items():
            const_types.setdefault(value, set()).update(tys)
    for e in ob.encoders:
        for value, i in e.consts.items():
            for ty in const_types.get(value, {"text", "uuid"}):
                universe.setdefault(ty, set()).add(i)
    now = model.var(f"ctx.{CONTEXT_NOW}", Sort.INT)
    for name in [*spec.identity.context, CONTEXT_NOW]:
        v = model.var(f"ctx.{name}", Sort.INT)
        if v is not None:
            ty = spec.context_type(name)
            universe.setdefault(ty, set()).add(v)
            sc.context[name] = _render(spec, ty, v, consts, now)
    for row, table in ob.rows.items():
        values = {}
        for col, c in spec.tables[table].columns.items():
            v = model.var(f"{row}.{col}", Sort.INT)
            if v is None:
                continue  # column not mentioned by the obligation
            if c.nullable and model.var(f"{row}.{col}.null", Sort.BOOL):
                values[col] = None
            else:
                universe.setdefault(c.type, set()).add(v)
                values[col] = _render(spec, c.type, v, consts, now)
        sc.rows[row] = {"table": table, "values": values}
    for rname, tag in ob.relations:
        rel = spec.relations[rname]
        kt = spec.tables[rel.table].columns[rel.key].type
        vt = spec.tables[rel.table].columns[rel.value].type
        f = rel_fun(rname, tag)
        pairs = [[_render(spec, kt, a, consts, now), _render(spec, vt, b, consts, now)]
                 for a in sorted(universe.get(kt, ())) for b in sorted(universe.get(vt, ()))
                 if model.app(f, a, b)]
        sc.relations[rname + (f"@{tag}" if tag else "")] = pairs
    return sc
