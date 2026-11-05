"""Proof reports (Markdown + JSON).  Every report states its assumptions (SEMANTICS §8)."""
from __future__ import annotations

import json
from dataclasses import dataclass

from mls import english
from mls.identity import get_adapter
from mls.props.runner import PropertyResult
from mls.spec.model import Spec

PG_VERSIONS = "PostgreSQL 15, 16, 17, 18"


def opaque_lines(spec: Spec, names: list[str]) -> list[str]:
    """One line per opaque block a report depends on: its signature and its SQL, so a reviewer sees
    exactly which unverified SQL the verdicts rest on (D3)."""
    out = []
    for name in names:
        o = spec.opaques[name]
        sig = [f"columns {', '.join(o.columns)}" if o.columns else "no columns",
               f"context {', '.join(o.context)}" if o.context else "no context",
               f"relations {', '.join(o.relations)}" if o.relations else "no relations"]
        out.append(f"opaque block '{name}' on {o.table} ({'; '.join(sig)}) is unverified SQL: "
                   f"`{' '.join(o.sql.split())}`")
    return out


def summary(results: list[PropertyResult]) -> dict[str, int]:
    s = {v: sum(1 for r in results if r.verdict == v) for v in ("PROVED", "REFUTED", "UNKNOWN")}
    s["CONDITIONAL"] = sum(1 for r in results if r.verdict == "PROVED" and r.conditional_on)
    return s


def assumptions(spec: Spec, results: list[PropertyResult]) -> list[str]:
    adapter = get_adapter(spec)
    opaque = sorted({d for r in results for d in r.conditional_on})
    internal = bool(spec.hierarchies or spec.cardinalities())
    trusted = ("trusted roles: the owner of the MLS schema and superusers bypass policies; the compiler "
               "forces RLS for owners, so only superusers (and roles granted BYPASSRLS outside MLS) "
               "remain trusted")
    if internal:
        trusted += ("; the internal role mls_internal owns the generated SECURITY DEFINER helpers "
                    "(group reachability, depth and counting triggers) and may read the tables they "
                    "need through its own policies; nobody can log in as it")
    isolation = ("isolation level: READ COMMITTED or stronger with the parent-row lock the cardinality "
                 "trigger takes; under REPEATABLE READ the count after the lock would use a stale "
                 "snapshot and the bound could be exceeded"
                 if spec.cardinalities() else
                 "isolation level: no assumption (every construct is enforced by the engine on single rows)")
    adopted = [t for t, tb in spec.tables.items() if tb.mode == "adopted"]
    observed = [t for t, tb in spec.tables.items() if tb.mode == "observed"]
    brownfield = []
    if adopted:
        brownfield.append(f"adopted tables (not created by MLS; their columns, keys and foreign objects are "
                          f"what the catalog says, and drift compares them): {', '.join(adopted)}")
    if observed:
        brownfield.append(f"observed tables (not governed; no verdict says anything about them): "
                          f"{', '.join(observed)}")
    return [
        f"identity adapter '{adapter.name}': {adapter.assumption}",
        trusted,
        *english.assumption_lines(spec),
        *brownfield,
        isolation,
        "opaque blocks relied on: " + (", ".join(opaque) if opaque else "none"),
        *opaque_lines(spec, opaque),
        f"Postgres versions validated by the differential tests: {PG_VERSIONS}",
        "stored rows satisfy every CHECK and foreign key (constraints are created validated)",
        "invariant preservation uses only CHECKs, FKs and triggers as guards (policies are not "
        "trusted: trusted roles bypass them)",
    ]


def to_json(spec: Spec, results: list[PropertyResult]) -> dict:
    return {
        "assumptions": assumptions(spec, results),
        "summary": summary(results),
        "properties": [
            {
                "id": r.id, "template": r.template, "description": r.description,
                "verdict": r.verdict, "conditional_on": r.conditional_on,
                "obligations": [
                    {"name": o.name, "verdict": o.verdict, "message": o.message,
                     "scenario": o.scenario.to_dict() if o.scenario else None,
                     "opaque_deps": o.opaque_deps}
                    for o in r.obligations
                ],
            }
            for r in results
        ],
    }


def to_markdown(spec: Spec, results: list[PropertyResult]) -> str:
    lines = ["# MLS proof report", ""]
    s = summary(results)
    cond = f" ({s['CONDITIONAL']} conditional on opaque blocks)" if s["CONDITIONAL"] else ""
    lines.append(f"**{s['PROVED']} proved{cond}, {s['REFUTED']} refuted, {s['UNKNOWN']} unknown**")
    lines += ["", "## Assumptions", ""]
    lines += [f"- {a}" for a in assumptions(spec, results)]
    lines += ["", "## Properties", ""]
    for r in results:
        desc = f" — {r.description}" if r.description else ""
        lines.append(f"### {r.id} ({r.template}): {r.verdict}{desc}")
        if r.conditional_on:
            lines.append(f"conditional on opaque blocks: {', '.join(r.conditional_on)}")
        lines.append("")
        for o in r.obligations:
            lines.append(f"- {o.verdict}: {o.name}" + (f" — {o.message}" if o.message else ""))
            if o.scenario:
                lines.append("  counterexample:")
                lines += ["  " + ln for ln in o.scenario.to_markdown().splitlines()]
            if o.example:
                lines.append("  example:")
                lines += ["  " + ln for ln in o.example.to_markdown().splitlines()]
        lines.append("")
    return "\n".join(lines)


def dumps_json(spec: Spec, results: list[PropertyResult]) -> str:
    return json.dumps(to_json(spec, results), indent=2, default=str)
