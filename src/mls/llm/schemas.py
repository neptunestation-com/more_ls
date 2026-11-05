"""JSON Schemas of the two pass outputs, derived from the pydantic spec model (D15), so the
answers can be decoded under the schema and validated locally with the same model."""
from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from mls.spec.model import Property, Rule, Spec

SKELETON_KEYS = ("$comment", "mls_version", "identity", "roles", "tables", "relations", "hierarchies", "opaques")


def _wrap(items_schema: dict, key: str, extra: dict[str, Any]) -> dict:
    defs = items_schema.pop("$defs", {})
    props = {key: items_schema, **extra}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False,
            "$defs": defs}


def properties_schema() -> dict:
    """{"properties": [Property, ...], "notes": str}."""
    return _wrap(TypeAdapter(list[Property]).json_schema(), "properties", {"notes": {"type": "string"}})


def rules_schema() -> dict:
    """{"rules": [Rule, ...], "notes": str}."""
    return _wrap(TypeAdapter(list[Rule]).json_schema(), "rules", {"notes": {"type": "string"}})


def skeleton_of(spec: Spec | dict) -> dict:
    """Everything of a spec except rules and properties: what both passes are given."""
    data = spec if isinstance(spec, dict) else spec.model_dump(by_alias=True, exclude_none=True)
    out = {k: data[k] for k in SKELETON_KEYS if k in data and data[k] not in ({}, None)}
    for t in out.get("tables", {}).values():  # the D45 fields at their defaults are noise in a prompt
        if t.get("mode") == "owned":
            t.pop("mode")
        for k in ("assume", "tags"):
            if t.get(k) == []:
                t.pop(k)
    return out
