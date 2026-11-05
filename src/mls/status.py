"""`mls status` (Milestone 5): the governance status of every table, as data.

Per table: `ungoverned` (no row-level security: an `observed` table, a table the spec does not
mention, or with `--dsn` a governed table that is missing or has RLS disabled), `refuted` (a
property about the table does not hold), `opaque` (governed, every proof about it holds, but
some proof is conditional on an opaque block or a rule on it reads one), `unproved` (governed,
no property mentions it) or `proved`.  This is the red / yellow / green of the pg_more_ls
schema overlay as data; the proofs come from `props.runner.run_all`, the live facts from
`pg.catalog.snapshot`."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mls.props.obligations import obligations_for
from mls.props.runner import PropertyResult
from mls.spec.model import (InvariantPreserved, LeastPrivilege, MonotoneIn, Property, Spec)

STATUSES = ("ungoverned", "refuted", "opaque", "unproved", "proved")


@dataclass
class TableStatus:
    table: str
    status: str
    detail: str
    opaques: list[str] = field(default_factory=list)      # blocks the proofs about the table depend on
    properties: list[str] = field(default_factory=list)   # property ids that mention the table

    def __str__(self) -> str:
        return f"{self.table}: {self.status} ({self.detail})"

    def to_dict(self) -> dict[str, Any]:
        return {"table": self.table, "status": self.status, "detail": self.detail,
                "opaques": self.opaques, "properties": self.properties}


def _opaques_in(node: Any) -> set[str]:
    """The opaque blocks a (dumped) predicate references."""
    if isinstance(node, dict):
        out = {node["opaque"]} if isinstance(node.get("opaque"), str) else set()
        for v in node.values():
            out |= _opaques_in(v)
        return out
    if isinstance(node, list):
        return set().union(*(_opaques_in(v) for v in node)) if node else set()
    return set()


def rule_opaques(spec: Spec, table: str) -> set[str]:
    """The opaque blocks the rules on `table` read."""
    out: set[str] = set()
    for r in spec.rules:
        if r.table == table:
            out |= _opaques_in(r.model_dump(by_alias=True, exclude_none=True))
    return out


def property_tables(spec: Spec, p: Property) -> set[str]:
    """The governed tables a property is about.  `least_privilege` and `monotone_in` range over
    every table; an invariant names the tables of its rules."""
    if isinstance(p, (LeastPrivilege, MonotoneIn)):
        return set(spec.governed())
    if isinstance(p, InvariantPreserved):
        return {spec.rule(i.rule).table if i.rule is not None else i.table for i in p.invariants}
    return {p.table}


def table_statuses(spec: Spec, results: list[PropertyResult], snapshot: dict | None = None) -> list[TableStatus]:
    """One status per table of the spec (declaration order), then with a `snapshot` (the
    catalog, `pg.catalog.snapshot`) the tables of the schema the spec does not mention."""
    by_id = {r.id: r for r in results}
    props = {p.id: p for p in spec.properties}
    out: list[TableStatus] = []
    for name, t in spec.tables.items():
        if t.mode == "observed":
            out.append(TableStatus(name, "ungoverned", "observed: the spec does not govern it"))
            continue
        if snapshot is not None:
            live = snapshot["tables"].get(name)
            if live is None:
                out.append(TableStatus(name, "ungoverned", "missing from the database"))
                continue
            if not live["rls"]:
                out.append(TableStatus(name, "ungoverned", "row level security is not enabled; deploy the spec"))
                continue
        mentions: list[str] = []
        failing: list[str] = []
        deps: set[str] = rule_opaques(spec, name)
        for pid, p in props.items():
            if name not in property_tables(spec, p):
                continue
            r = by_id.get(pid)
            if r is None:
                continue
            if isinstance(p, (LeastPrivilege, MonotoneIn)):
                # a global property: only the obligations whose rows are of this table count
                obs = obligations_for(spec, p)
                mine = [(ob, res) for ob, res in zip(obs, r.obligations)
                        if name in ob.rows.values() or not ob.rows]
                if not mine:
                    continue
                mentions.append(pid)
                if any(res.verdict != "PROVED" for _, res in mine):
                    failing.append(pid)
                deps |= {d for _, res in mine for d in res.opaque_deps}
            else:
                mentions.append(pid)
                if r.verdict != "PROVED":
                    failing.append(pid)
                deps |= set(r.conditional_on)
        opaques = sorted(deps)
        if failing:
            out.append(TableStatus(name, "refuted", "not proved: " + ", ".join(failing), opaques, mentions))
        elif opaques:
            out.append(TableStatus(name, "opaque", "conditional on " + ", ".join(opaques), opaques, mentions))
        elif not mentions:
            out.append(TableStatus(name, "unproved", "no property mentions it"))
        else:
            out.append(TableStatus(name, "proved", ", ".join(mentions), [], mentions))
    if snapshot is not None:
        for name in snapshot["other_tables"]:
            out.append(TableStatus(name, "ungoverned", "not in the spec"))
    return out


def failing(statuses: list[TableStatus]) -> list[TableStatus]:
    """The statuses that fail the command: a refuted table, or (with --dsn) a governed table
    that is missing or has RLS disabled.  Observed tables, tables outside the spec, opaque and
    unproved tables are information, not failures."""
    return [s for s in statuses if s.status == "refuted"
            or (s.status == "ungoverned" and s.detail.split(";")[0] in
                ("missing from the database", "row level security is not enabled"))]


def to_json(statuses: list[TableStatus]) -> dict[str, Any]:
    return {"ok": not failing(statuses), "tables": [s.to_dict() for s in statuses]}


def to_text(statuses: list[TableStatus]) -> str:
    counts = {k: sum(1 for s in statuses if s.status == k) for k in STATUSES}
    summary = ", ".join(f"{n} {k}" for k, n in counts.items() if n)
    return "\n".join([str(s) for s in statuses] + [f"status: {summary}"])
