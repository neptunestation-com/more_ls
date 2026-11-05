"""Selectors (D48): `applies_to` is checker-time expansion, not a construct.  A rule that carries
a selector instead of `table` becomes one rule per matching governed table (`owned` or
`adopted`), with id `<id>.<table>` and `from` naming the selector rule; an `invariant_preserved`
reference to the selector rule becomes one reference per expansion.  Everything downstream of
the checker (solver, templates, diff, migrate, drift, English, terse, Lean) sees only concrete
rules.  A selector that matches no governed table is an error: a silent empty expansion is how
a renamed column disables a tenant policy.  `observed` tables are matched for the lint's
coverage finding (D49), never expanded into."""
from __future__ import annotations

from mls.errors import MlsError
from mls.spec.model import AppliesTo, InvariantPreserved, InvariantRef, Rule, Spec, Table


def matches(sel: AppliesTo, name: str, table: Table) -> bool:
    if sel.has_column is not None:
        col = table.columns.get(sel.has_column[0])
        return col is not None and (len(sel.has_column) == 1 or col.type == sel.has_column[1])
    if sel.named is not None:
        return name in sel.named
    if sel.tagged is not None:
        return sel.tagged in table.tags
    if sel.all:
        return True
    if sel.and_ is not None:
        return all(matches(x, name, table) for x in sel.and_)
    if sel.or_ is not None:
        return any(matches(x, name, table) for x in sel.or_)
    return False


def selected(sel: AppliesTo, spec: Spec, observed: bool = False) -> list[str]:
    """The tables a selector matches, in declaration order: the governed ones, or with
    `observed` only the observed ones (what the coverage lint names)."""
    return [n for n, t in spec.tables.items() if (t.mode == "observed") == observed and matches(sel, n, t)]


def phrase(sel: AppliesTo) -> str:
    """The selector in English, for `explain` and the errors."""
    if sel.has_column is not None:
        c = sel.has_column
        return f"has column {c[0]}" + (f" of type {c[1]}" if len(c) == 2 else "")
    if sel.named is not None:
        return "is " + " or ".join(sel.named) if len(sel.named) > 1 else f"is {sel.named[0]}"
    if sel.tagged is not None:
        return f"is tagged {sel.tagged}"
    if sel.all:
        return "every governed table"
    if sel.and_ is not None:
        return "(" + " and ".join(phrase(x) for x in sel.and_) + ")"
    if sel.or_ is not None:
        return "(" + " or ".join(phrase(x) for x in sel.or_) + ")"
    return "?"


def expand_selectors(spec: Spec) -> tuple[Spec, list[MlsError], list[str]]:
    """(the expanded spec, the errors, the original JSON path of every rule of the expanded
    list).  A rule that already carries `from` is an expansion and is kept as it is."""
    rules: list[Rule] = []
    paths: list[str] = []
    errors: list[MlsError] = []
    expanded: dict[str, list[str]] = {}  # selector rule id -> expansion ids
    for i, r in enumerate(spec.rules):
        path = f"$.rules[{i}]"
        if r.applies_to is None or r.from_ is not None:
            rules.append(r)
            paths.append(path)
            continue
        tables = selected(r.applies_to, spec)
        if not tables:
            would = selected(r.applies_to, spec, observed=True)
            hint = (f"; observed table{'s' if len(would) > 1 else ''} {', '.join(would)} would match: set its mode "
                    f"to \"adopted\" to govern it" if would else "")
            errors.append(MlsError(path + ".applies_to", f"rule '{r.id}' applies to no table ({phrase(r.applies_to)})"
                                                         + hint,
                                   "declare a table the selector matches, or name the table with \"table\"",
                                   "error"))
            continue
        expanded[r.id] = []
        for t in tables:
            new = r.model_copy(update={"table": t, "id": f"{r.id}.{t}", "from_": r.id})
            rules.append(new)
            paths.append(path)
            expanded[r.id].append(new.id)
    if not expanded and not errors:
        return spec, errors, paths
    props = []
    for p in spec.properties:
        if isinstance(p, InvariantPreserved):
            refs: list[InvariantRef] = []
            for inv in p.invariants:
                if inv.rule is not None and inv.rule in expanded:
                    refs += [InvariantRef(rule=x) for x in expanded[inv.rule]]
                else:
                    refs.append(inv)
            p = p.model_copy(update={"invariants": refs})
        props.append(p)
    return spec.model_copy(update={"rules": rules, "properties": props}), errors, paths
