"""Terse syntax: the whole spec rendered as text (D44, completed to the spec by D50).

One model, two concrete syntaxes: the JSON (canonical, D15) and this.  The renderer is a fixed
function of the pydantic model; `mls explain --terse` prints it, the `before`/`after` lines of
`mls diff` and the `spec_terse` of LLM transcripts use its predicate and rule forms, and the
parser (D50, `.mls` files, `mls fmt`) reads it back.  The English of `mls/english.py` stays the
approval artifact (D10, D33).  The grammar:

    spec       := 'mls' VERSION comment? identity roles table* relation* hierarchy* opaque*
                  'rules' '{' rule* '}' 'properties' '{' property* '}'
    comment    := 'comment' STRING                               ($comment, an author's note)
    identity   := 'identity' ADAPTER '{' [ctx (';' ctx)*] '}'    ctx := NAME ':' SOURCE TYPE
    roles      := 'roles' NAME (',' NAME)*
    table      := 'table' NAME MODE '{' [column (',' column)* [';']] [option (';' option)*] '}'
    column     := NAME TYPE ['not null']                          (a bare column is nullable)
    option     := 'primary_key' '(' names ')' | 'unique' '(' names ')' | 'tags' '(' names ')'
                | 'assume' '(' STRING (',' STRING)* ')'           ('owner r' | 'bypassrls r' | 'rls not_forced')
    relation   := 'relation' NAME '=' TABLE '(' KEY ',' VALUE ')'
    hierarchy  := 'hierarchy' NAME '=' TABLE 'id' COL 'parent' COL 'depth' COL 'max_depth' INT 'members' REL
    opaque     := 'opaque' NAME 'on' TABLE '(' [input (',' input)*] ')' 'relations' '(' [names] ')' 'sql' DOLLAR
    input      := COL | '@' CTX                                   DOLLAR := '$$' sql '$$' | '$tag$' sql '$tag$'
    rule       := ID KIND [NAME] ('on' TABLE | 'applies_to' selector) ['from' ID] ':' body ['requirement' STRING]
    selector   := 'has_column' '(' COL [TYPE] ')' | 'named' '(' names ')' | 'tagged' '(' NAME ')' | 'all'
                | 'and' '(' selector (',' selector)* ')' | 'or' '(' selector (',' selector)* ')'
    property   := ID TEMPLATE [table role command which]: ... ['description' STRING]
    term       := column | old.column | new.column | @context | 'text' | 123 | true | false
    atom       := term op term | term in (const, ...) | term is null | relation(key, value)
                | in_group(h, key, value) | now_between(from | -, until | -) | opaque(name) | true | false
    pred       := atom | not pred | pred and pred | pred or pred   (not > and > or; a nested same-
                                                                   connective group keeps its parens)
    least_privilege body := grant (';' grant)* | '[]'   grant := ROLE TABLE '[' [commands] ']' | ROLE '[]'

`# comments` to the end of a line.  Keywords (`RESERVED`) are reserved: a name spelled like one,
or not an identifier, is written `"quoted"`; `'strings'` double their quotes.  Every item is one
line, except a table, whose columns and options are one per line, so that a diff shows one
changed rule or column as one changed line.
"""
from __future__ import annotations

import re
from typing import Any

from mls.spec.model import (
    And, AppliesTo, CardinalityRule, CheckRule, ColRef, ColumnImmutable, ColumnRule, Cmp, Const, CtxRef, Custom,
    Hierarchy, Identity, In, InclusionRule, InGroup, InvariantPreserved, IsNull, LeastPrivilege, Lit, Member,
    MonotoneIn, MustAllow, NewRef, Not, NowBetween, OldRef, Opaque, OpaqueRef, Or, PolicyRule, Pred, Property,
    Relation, RoleSeparation, Rule, RuleBase, Spec, Table, TenantIsolation, TransitionRule, WriteSubsetRead,
    OwnerAssumed, BypassAssumed, RlsAssumed, ContextVar, Column,
)

OR, AND, NOT, ATOM = 1, 2, 3, 4

RESERVED = frozenset("""
mls comment identity roles table relation hierarchy opaque rules properties
on applies_to from requirement description using check for max on_delete restrict max_depth
and or not is null true false in in_group now_between old new
primary_key unique tags assume sql relations has_column named tagged all
owned adopted observed select insert update delete permissive restrictive read write
policy inclusion transition cardinality column_rule
tenant_isolation column_immutable monotone_in write_subset_read least_privilege invariant_preserved custom
must_allow role_separation
""".split())

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

# what the renderer covers; tests/test_terse.py compares these with the model so that a construct
# added without a terse form fails the fast suite (D50)
RENDERED_FIELDS: dict[type, set[str]] = {
    Spec: {"comment", "mls_version", "identity", "roles", "tables", "relations", "hierarchies", "opaques",
           "rules", "properties"},
    Identity: {"adapter", "context"}, ContextVar: {"source", "type"},
    Table: {"columns", "primary_key", "unique", "mode", "assume", "tags"}, Column: {"type", "nullable"},
    Relation: {"table", "key", "value"},
    Hierarchy: {"table", "id", "parent", "depth", "max_depth", "members"},
    Opaque: {"table", "columns", "context", "relations", "sql"},
    RuleBase: {"id", "name", "table", "applies_to", "from_", "requirement"},
    AppliesTo: {"has_column", "named", "tagged", "all", "and_", "or_"},
}
RENDERED_CLASSES: set[type] = {
    PolicyRule, CheckRule, InclusionRule, TransitionRule, CardinalityRule, ColumnRule,
    TenantIsolation, ColumnImmutable, MonotoneIn, WriteSubsetRead, LeastPrivilege, InvariantPreserved, Custom,
    MustAllow, RoleSeparation,
    Lit, And, Or, Not, IsNull, Cmp, In, Member, InGroup, NowBetween, OpaqueRef,
    ColRef, OldRef, NewRef, CtxRef, Const,
    OwnerAssumed, BypassAssumed, RlsAssumed,
}


# ----------------------------------------------------------------------------- lexical
def ident(name: str, dotted: bool = False) -> str:
    """A name as the grammar spells it: bare when it is an identifier and not a keyword."""
    ok = (_DOTTED if dotted else _IDENT).match(name) and name not in RESERVED
    return name if ok else '"' + name.replace('"', '""') + '"'


def string(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def value(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return string(v)
    return str(v)


def dollar(sql: str) -> str:
    """Dollar-quoted, as Postgres does: `$$ ... $$`, or a tag the SQL does not contain."""
    tag = "$$"
    n = 0
    while tag in sql:
        n += 1
        tag = f"$mls{n if n > 1 else ''}$"
    return f"{tag} {sql} {tag}"


# ----------------------------------------------------------------------------- predicates
def term(t) -> str:
    if isinstance(t, ColRef):
        return ident(t.col)
    if isinstance(t, OldRef):
        return f"old.{ident(t.old)}"
    if isinstance(t, NewRef):
        return f"new.{ident(t.new)}"
    if isinstance(t, CtxRef):
        return f"@{ident(t.ctx)}"
    if isinstance(t, Const):
        return value(t.const)
    raise TypeError(t)


def _pred(p: Pred) -> tuple[str, int]:
    """The rendering and its precedence level."""
    if isinstance(p, Lit):
        return ("true" if p.lit else "false"), ATOM
    if isinstance(p, And):
        return " and ".join(_sub(a, AND) for a in p.and_), (AND if len(p.and_) > 1 else _pred(p.and_[0])[1])
    if isinstance(p, Or):
        return " or ".join(_sub(a, OR) for a in p.or_), (OR if len(p.or_) > 1 else _pred(p.or_[0])[1])
    if isinstance(p, Not):
        return f"not {_sub(p.not_, NOT)}", NOT
    if isinstance(p, IsNull):
        return f"{term(p.is_null)} is null", ATOM
    if isinstance(p, Cmp):
        l, op, r = p.cmp
        return f"{term(l)} {op} {term(r)}", ATOM
    if isinstance(p, In):
        t, consts = p.in_
        return f"{term(t)} in ({', '.join(term(c) for c in consts)})", ATOM
    if isinstance(p, Member):
        m = p.member
        return f"{ident(m.relation)}({term(m.key)}, {term(m.value)})", ATOM
    if isinstance(p, InGroup):
        g = p.in_group
        return f"in_group({ident(g.hierarchy)}, {term(g.key)}, {term(g.value)})", ATOM
    if isinstance(p, NowBetween):
        nb = p.now_between
        lo = "-" if nb.from_ is None else term(nb.from_)
        hi = "-" if nb.until is None else term(nb.until)
        return f"now_between({lo}, {hi})", ATOM
    if isinstance(p, OpaqueRef):
        return f"opaque({ident(p.opaque)})", ATOM
    raise TypeError(p)


def _sub(p: Pred, context: int) -> str:
    """A sub-predicate, parenthesized when its precedence is lower, and also when it is a
    connective group nested in a group of the same kind (`a and (b and c)`): the tree reads
    back as written, which the round trip needs (D50); `and`/`or` are associative, so the
    meaning is the same either way."""
    s, prec = _pred(p)
    same = prec == context and ((isinstance(p, And) and len(p.and_) > 1) or (isinstance(p, Or) and len(p.or_) > 1))
    return f"({s})" if prec < context or same else s


def pred(p: Pred) -> str:
    return _pred(p)[0]


# ----------------------------------------------------------------------------- rules
def selector(sel: AppliesTo) -> str:
    if sel.has_column is not None:
        return f"has_column({' '.join(ident(x) if i == 0 else x for i, x in enumerate(sel.has_column))})"
    if sel.named is not None:
        return f"named({', '.join(ident(t) for t in sel.named)})"
    if sel.tagged is not None:
        return f"tagged({ident(sel.tagged)})"
    if sel.all:
        return "all"
    if sel.and_ is not None:
        return f"and({', '.join(selector(s) for s in sel.and_)})"
    if sel.or_ is not None:
        return f"or({', '.join(selector(s) for s in sel.or_)})"
    raise TypeError(sel)


def _head(r: Rule) -> str:
    """`ID kind [name] on table | applies_to sel [from ID]`.  An expanded selector rule (D48) shows
    its table and `from`; the selector itself is shown on the rule that carries no `from`."""
    s = f"{ident(r.id, dotted=True)} {r.kind}"
    if r.name is not None:
        s += f" {ident(r.name)}"
    if r.from_ is not None or r.applies_to is None:
        s += f" on {ident(r.table)}"
    else:
        s += f" applies_to {selector(r.applies_to)}"
    if r.from_ is not None:
        s += f" from {ident(r.from_, dotted=True)}"
    return s + ":"


def _tail(r: Rule) -> str:
    return f" requirement {string(r.requirement)}" if r.requirement is not None else ""


def rule(r: Rule) -> str:
    head = _head(r)
    if isinstance(r, PolicyRule):
        s = f"{head} {ident(r.role)} {r.command} {r.mode}"
        if r.using is not None:
            s += f" using {pred(r.using)}"
        if r.check is not None:
            s += f" check {pred(r.check)}"
        return s + _tail(r)
    if isinstance(r, CheckRule):
        return f"{head} {pred(r.predicate)}" + _tail(r)
    if isinstance(r, InclusionRule):
        c1, c2 = r.columns
        return f"{head} ({ident(c1)}, {ident(c2)}) in {ident(r.relation)} on_delete {r.on_delete}" + _tail(r)
    if isinstance(r, TransitionRule):
        return f"{head} {pred(r.predicate)}" + _tail(r)
    if isinstance(r, CardinalityRule):
        return f"{head} {ident(r.group)} -> {ident(r.parent)} max {r.max}" + _tail(r)
    if isinstance(r, ColumnRule):
        return f"{head} {ident(r.role)} {r.access} {', '.join(ident(c) for c in r.columns)}" + _tail(r)
    raise TypeError(r)


# ----------------------------------------------------------------------------- properties
def prop(p: Property) -> str:
    pid = ident(p.id, dotted=True)
    if isinstance(p, TenantIsolation):
        s = (f"{pid} tenant_isolation {ident(p.table)}: {ident(p.column)} in {ident(p.relation)}({term(p.key)}) "
             f"for {', '.join(p.commands)}")
    elif isinstance(p, ColumnImmutable):
        s = f"{pid} column_immutable {ident(p.table)}: {ident(p.column)}"
    elif isinstance(p, MonotoneIn):
        s = f"{pid} monotone_in: {ident(p.relation)}"
    elif isinstance(p, WriteSubsetRead):
        s = f"{pid} write_subset_read {ident(p.table)}"
    elif isinstance(p, LeastPrivilege):
        parts = [f"{ident(role)} {ident(table)}[{', '.join(cmds)}]" if tables else f"{ident(role)} []"
                 for role, tables in p.allowed.items() for table, cmds in (tables.items() or [(None, None)])]
        s = f"{pid} least_privilege: " + ("; ".join(parts) or "[]")
    elif isinstance(p, InvariantPreserved):
        parts = [ident(inv.rule, dotted=True) if inv.rule is not None else f"{ident(inv.table)}: {pred(inv.predicate)}"
                 for inv in p.invariants]
        s = f"{pid} invariant_preserved: " + "; ".join(parts)
    elif isinstance(p, Custom):
        s = f"{pid} custom {ident(p.table)} {ident(p.role)} {p.command} {p.which}: {pred(p.goal)}"
    elif isinstance(p, MustAllow):
        s = f"{pid} must_allow {ident(p.table)} {ident(p.role)} {p.command}: {pred(p.when)}"
    elif isinstance(p, RoleSeparation):
        s = f"{pid} role_separation {ident(p.table)} {p.command}: {ident(p.roles[0])} | {ident(p.roles[1])}"
    else:
        raise TypeError(p)
    return s + (f" description {string(p.description)}" if p.description is not None else "")


# ----------------------------------------------------------------------------- scenarios
def scenario(sc) -> str:
    """`@uid = uuid#1; old: docs {tenant_id = 1}; member_of = {(uuid#1, 1)}`"""
    parts = [f"@{k} = {value(v)}" for k, v in sc.context.items()]
    for name, r in sc.rows.items():
        vals = ", ".join(f"{k} = {value(v)}" for k, v in r["values"].items())
        parts.append(f"{name}: {r['table']} {{{vals}}}")
    for name, pairs in sc.relations.items():
        shown = ", ".join(f"({value(a)}, {value(b)})" for a, b in pairs)
        parts.append(f"{name} = {{{shown}}}")
    return "; ".join(parts) if parts else "(no particular data)"


# ----------------------------------------------------------------------------- the spec
def identity(i: Identity) -> str:
    ctx = "; ".join(f"{ident(n)}: {ident(c.source, dotted=True)} {c.type}" for n, c in i.context.items())
    return f"identity {i.adapter} {{ {ctx} }}" if ctx else f"identity {i.adapter} {{ }}"


def assumption(a) -> str:
    if isinstance(a, OwnerAssumed):
        return string(f"owner {a.owner}")
    if isinstance(a, BypassAssumed):
        return string(f"bypassrls {a.bypassrls}")
    if isinstance(a, RlsAssumed):
        return string(f"rls {a.rls}")
    raise TypeError(a)


def table(name: str, t: Table) -> list[str]:
    cols = [f"{ident(c)} {col.type}" + ("" if col.nullable else " not null") for c, col in t.columns.items()]
    options = []
    if t.primary_key is not None:
        options.append(f"primary_key ({', '.join(ident(c) for c in t.primary_key)})")
    for u in t.unique:
        options.append(f"unique ({', '.join(ident(c) for c in u)})")
    if t.tags:
        options.append(f"tags ({', '.join(ident(x) for x in t.tags)})")
    if t.assume:
        options.append(f"assume ({', '.join(assumption(a) for a in t.assume)})")
    items = [c + "," for c in cols[:-1]] + ([cols[-1] + (";" if options else "")] if cols else [])
    items += [o + ";" for o in options[:-1]] + options[-1:]
    return [f"table {ident(name)} {t.mode} {{"] + [f"  {x}" for x in items] + ["}"]


def relation(name: str, r: Relation) -> str:
    return f"relation {ident(name)} = {ident(r.table)}({ident(r.key)}, {ident(r.value)})"


def hierarchy(name: str, h: Hierarchy) -> str:
    return (f"hierarchy {ident(name)} = {ident(h.table)} id {ident(h.id)} parent {ident(h.parent)} "
            f"depth {ident(h.depth)} max_depth {h.max_depth} members {ident(h.members)}")


def opaque(name: str, o: Opaque) -> str:
    inputs = [ident(c) for c in o.columns] + [f"@{ident(c)}" for c in o.context]
    return (f"opaque {ident(name)} on {ident(o.table)} ({', '.join(inputs)}) "
            f"relations ({', '.join(ident(r) for r in o.relations)}) sql {dollar(o.sql)}")


def render(spec: Spec) -> str:
    """The whole spec in the terse syntax (D50)."""
    out: list[str] = [f"mls {spec.mls_version}"]
    if spec.comment is not None:
        out.append(f"comment {string(spec.comment)}")
    out += ["", identity(spec.identity), f"roles {', '.join(ident(r) for r in spec.roles)}"]
    for name, t in spec.tables.items():
        out += [""] + table(name, t)
    for section, fn in ((spec.relations, relation), (spec.hierarchies, hierarchy), (spec.opaques, opaque)):
        if section:
            out += [""] + [fn(n, x) for n, x in section.items()]
    out += ["", "rules {"] + [f"  {rule(r)}" for r in spec.rules] + ["}"]
    out += ["", "properties {"] + [f"  {prop(p)}" for p in spec.properties] + ["}"]
    return "\n".join(out) + "\n"


def section(key: str, items) -> str:
    """One `rules { ... }` or `properties { ... }` section from model objects (what the LLM loop
    quotes back as the previous answer when it has no text of the model's own)."""
    fn = rule if key == "rules" else prop
    return "\n".join([f"{key} {{"] + [f"  {fn(x)}" for x in items] + ["}"])


spec_listing = render  # the D44 name
