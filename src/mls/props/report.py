"""Proof reports (Markdown + JSON).  Every report states its assumptions (SEMANTICS §8)."""
from __future__ import annotations

import json
from dataclasses import dataclass

from mls.identity import get_adapter
from mls.props.runner import PropertyResult
from mls.spec.model import Spec

PG_VERSIONS = "PostgreSQL 15, 16, 17, 18"


def assumptions(spec: Spec, results: list[PropertyResult]) -> list[str]:
    adapter = get_adapter(spec)
    opaque = sorted({d for r in results for d in r.conditional_on})
    return [
        f"identity adapter '{adapter.name}': {adapter.assumption}",
        "trusted roles: the owner of the MLS schema and superusers bypass policies; the compiler "
        "forces RLS for owners, so only superusers (and roles granted BYPASSRLS outside MLS) "
        "remain trusted",
        "isolation level: no assumption (every Milestone 1 construct is enforced by the engine "
        "on single rows)",
        "opaque blocks relied on: " + (", ".join(opaque) if opaque else "none"),
        f"Postgres versions validated by the differential tests: {PG_VERSIONS}",
        "stored rows satisfy every CHECK and foreign key (constraints are created validated)",
        "invariant preservation uses only CHECKs, FKs and triggers as guards (policies are not "
        "trusted: trusted roles bypass them)",
    ]


def to_json(spec: Spec, results: list[PropertyResult]) -> dict:
    return {
        "assumptions": assumptions(spec, results),
        "summary": {v: sum(1 for r in results if r.verdict == v) for v in ("PROVED", "REFUTED", "UNKNOWN")},
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
    s = {v: sum(1 for r in results if r.verdict == v) for v in ("PROVED", "REFUTED", "UNKNOWN")}
    lines.append(f"**{s['PROVED']} proved, {s['REFUTED']} refuted, {s['UNKNOWN']} unknown**")
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
        lines.append("")
    return "\n".join(lines)


def dumps_json(spec: Spec, results: list[PropertyResult]) -> str:
    return json.dumps(to_json(spec, results), indent=2, default=str)
