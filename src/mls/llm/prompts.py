"""Prompts of the two passes and of the repair rounds.  Stable material (role, language
reference, the schema of the output) comes first so the prefix caches; the requirements and any
feedback come last."""
from __future__ import annotations

import json
import re

from mls.errors import MlsError
from mls.llm.terse_prompt import LANGUAGE_TERSE, terse_output_reference

LANGUAGE = """
# MLS in brief

An MLS spec states who may see and change which rows of which tables, and which invariants the
data must satisfy.  Everything is three-valued (SQL semantics): a predicate that is UNKNOWN (NULL)
denies in a policy and passes in a check.  The toolchain proves properties about the rules with
an SMT solver and compiles the rules to PostgreSQL (RLS policies, CHECK constraints, foreign keys,
triggers, grants).  You never write SQL.

## Terms
- {"col": "name"}            a column of the row (in transitions use {"old": "name"} / {"new": "name"})
- {"ctx": "name"}            a context value of the current user (declared in identity.context)
- {"const": 3 | "x" | true}  a constant (no timestamp constants; an enum column's constant is one of the
                             labels its type lists under "enums", compared by = <> or "in")

## Predicates
- {"cmp": [term, op, term]}  op in = <> < <= > >=; the right-hand side is a context value or a constant
- {"in": [term, [{"const": c1}, {"const": c2}, ...]]}  term equals one of the constants (sugar for an "or" of "=")
- {"member": {"relation": R, "key": {"ctx": c}, "value": {"col": v}}}   (ctx, col) is a pair of relation R
- {"in_group": {"hierarchy": H, "key": {"ctx": c}, "value": {"col": v}}}  col is one of the user's groups in
  hierarchy H or lies below one (policies only)
- {"now_between": {"from": term|null, "until": term|null}}   from <= now < until, NULL bound = open
- {"is_null": term}, {"and": [...]}, {"or": [...]}, {"not": p}, {"lit": true|false}, {"opaque": name}
- Negated member / in_group is not allowed in policies.  Context values are not allowed in checks.

## Relations (part of the schema: "relations": {name: relation})
- {"table": t, "key": k, "value": v}   the (k, v) pairs of table t (the user, the group they belong to);
  k and v are different NOT NULL columns, and the pair, or one of them, is the primary key or a unique
  constraint of t.  The owner of a row is one: {"table": "buildings", "key": "owner_id", "value": "id"}
- {"table": t, "key": k, "value": v, "where": p}   only the rows of t where p is TRUE; p reads t's own
  columns and constants only.  E.g. the managers: {"table": "building_staff", "key": "user_id",
  "value": "building_id", "where": {"cmp": [{"col": "role"}, "=", {"const": "manager"}]}}.  Not in
  inclusion rules, invariants or hierarchies.  A key column of an enum type takes a label as its key:
  {"member": {"relation": "floor_in_state", "key": {"const": "open"}, "value": {"col": "floor_id"}}}
- {"of": R, "through": [{"table": t1, "key": k1, "value": v1}, ...]}   composed: (u, x) when (u, y) is a
  pair of the base relation R, a t1 row has k1 = y and v1 = x (and so on through each hop); 1 to 3
  hops; each hop's key and value are different NOT NULL columns, the key has the type of the previous
  value, the value is the primary key or a unique column of the hop table (a lookup, not a join).
  E.g. the rooms of the floors of the buildings a user works in: {"of": "staff_of", "through":
  [{"table": "floors", "key": "building_id", "value": "id"}, {"table": "rooms", "key": "floor_id", "value": "id"}]}
- A composed relation is used in member (policies, goals, must_allow) and tenant_isolation only: not in
  inclusion rules, invariants, hierarchies or monotone_in.

## Rules (every rule has a unique "id", a "name" (identifier), a "table" and may cite a "requirement")
- policy: {"kind": "policy", "role", "command": select|insert|update|delete|all, "mode": permissive|restrictive,
  "using": p (rows the command may touch / read), "check": p (new rows it may write)}.
  Permissive policies of a command are OR-ed, restrictive ones AND-ed on top (PostgreSQL semantics).
  insert has only "check"; select and delete only "using"; update may have both.
- check: {"kind": "check", "predicate": p}  every stored row satisfies p (p may be UNKNOWN)
- inclusion: {"kind": "inclusion", "columns": [a, b], "relation": R, "on_delete": "restrict"}  (a, b) of every
  row is a pair of R (NULLs pass); the referenced pair cannot be deleted
- transition: {"kind": "transition", "predicate": p over {"old": c} and {"new": c}}  every update satisfies p
- cardinality: {"kind": "cardinality", "group": col, "parent": table, "max": N}  at most N rows per group value
- column_rule: {"kind": "column_rule", "role", "columns": [...], "access": read|write}  the role may read or
  write only these columns of the table (without a column rule the role's policies cover all columns)
  A column rule is per role, not per row: a column some users may see only on some rows (their own
  salary) is its own table, one row per parent row, with its own policies; there is no masking construct.
A role has a privilege on a table only through its policies; a command with no policy is denied.
One implicit privilege: a role whose policy uses a relation R (member / in_group) can thereby read
the key and value columns of R's table (the membership lookup runs as the user), and nothing else.

## Properties (each has a unique "id" and a plain-English "description")
- tenant_isolation: {"table", "column", "relation", "key": {"ctx": c}, "commands": [...]}  every row the
  role can touch has column in relation(key)
- column_immutable: {"table", "column"}  no admitted update changes the column
- monotone_in: {"relation"}  removing a pair from the relation never grants access
- write_subset_read: {"table"}  whatever a role can insert or update it can also read
- least_privilege: {"allowed": {role: {table: [commands]}}}  no role has more privileges than listed; at most one per spec, listing every role (a role it does not list is allowed nothing)
  (include "select" on the tables of the relations the role's policies will need; see the implicit
  privilege above)
- invariant_preserved: {"invariants": [{"rule": id} | {"table", "predicate"}]}  every admitted change
  keeps the invariants (inductive proof)
- custom: {"table", "role", "command", "which": old|new, "goal": p}  every row the role can <command>
  satisfies goal (an upper bound on access)
- role_separation: {"table", "roles": [a, b], "command"}  no row is admitted to <command> for both roles
  (the same user under either role)
- must_allow: {"table", "role", "command", "when": p}  every valid row where p holds is admitted by
  the role's <command> (a lower bound: catches policies, transitions and column rules that deny too
  much).  The rows are assumed to satisfy the table's checks and inclusions, so p states what the
  user can do, not the integrity rules.  For update p ranges over {"old": c} / {"new": c} (a bare
  {"col": c} is the new image); state the whole change, e.g. the status goes from open to closed
  and the other columns stay the same.  A p that no valid row can meet refutes the property.
""".strip()

ROLE_PROPERTIES = """You write the PROPERTIES a database access-control rule set must satisfy, from business
requirements.  Properties are what a human reviewer approves; they must restate the requirements as
checkable statements (who may never see what, what never changes, what the data must always
satisfy, and what must be possible), be independent of how the rules will be written, and cover
every requirement in both directions: an upper bound for every restriction (tenant_isolation,
custom, ...) and a must_allow for every grant the requirements state, so that rules that deny too
much are caught too.  Prefer template properties; use custom for anything else.  Do not write rules.  A property's predicate
must be TRUE to hold, so guard nullable columns: an optional owner that must be a member is
{"or": [{"is_null": {"col": "owner_id"}}, {"member": ...}]}, not the bare membership.  Write the plain-English
"description" of each property so a product owner can verify it against the requirements.  Put any
doubt about the requirements in "notes".  Answer with JSON only, matching the output schema."""

ROLE_RELATIONS = """You complete the SCHEMA of a database access-control spec, before anyone writes its rules
or properties: you write the RELATIONS the business requirements need that the schema does not
already have.  A relation names who belongs to what: a base relation is the (key, value) projection
of one table (the user column, the group column), possibly only of its rows that meet a condition
on that table's own columns (the managers among the staff); a composed relation follows a base relation
through up to three tables, each by a key, to the rows a requirement reaches only indirectly (the
rooms of the floors of the buildings a user works in).  Write a relation only where a requirement needs one and
the schema has none that says the same; never restate, rename or change the schema's relations.
Name each by what it means (staff_of, in_room).  Many schemas need none: then answer with no
relations.  Do not write rules or properties.  Put any
doubt about the requirements in "notes".  Answer with JSON only, matching the output schema."""

ANSWER_TERSE = {
    "properties": "Answer with one `properties { ... }` section in the MLS terse syntax below (in a ```mls fence, "
                  "one property per line); any doubt about the requirements goes in prose outside the fence.  "
                  "Do not write rules.",
    "rules": "Answer with one `rules { ... }` section in the MLS terse syntax below (in a ```mls fence, one rule "
             "per line); any doubt about the requirements goes in prose outside the fence.",
    "relations": "Answer with one `relations { ... }` section (in a ```mls fence, one `relation` line per "
                 "relation, as in the schema; `relations { }` when none is needed); any doubt about the "
                 "requirements goes in prose outside the fence.",
}


def role_text(role: str, syntax: str) -> str:
    """The role paragraph for the syntax: JSON asks for JSON under the schema, terse for a section."""
    if syntax == "json":
        return role
    key = {ROLE_PROPERTIES: "properties", ROLE_RELATIONS: "relations"}.get(role, "rules")
    return (role.replace('{"or": [{"is_null": {"col": "owner_id"}}, {"member": ...}]}', "`owner_id is null or R(@uid, ...)`")
                .replace('Put any\ndoubt about the requirements in "notes".  Answer with JSON only, matching the output schema.',
                         ANSWER_TERSE[key])
                .replace('"description"', "description").replace('"id"', "id"))

ROLE_RULES = """You write the RULES (policies, checks, inclusions, transitions, cardinality limits, column
rules) of a database access-control spec from business requirements, for the given schema.  Cover
every requirement exactly: nothing more permissive, nothing more restrictive.  Give each rule a
stable id (R1, R2, ...) and cite the requirement it implements.  Remember that NULL denies in
policies: make membership columns and comparisons NULL-safe where the requirement allows it.  Put any
doubt about the requirements in "notes".  Answer with JSON only, matching the output schema."""


def next_rule_id(fixed_rules: list[dict]) -> str:
    """The first `R<n>` after the fixed rules' ids (`R1` when there are none)."""
    ns = [int(m.group(1)) for r in fixed_rules if (m := re.fullmatch(r"R(\d+)", str(r.get("id", ""))))]
    return f"R{max(ns, default=0) + 1}"


def fixed_rules_text(fixed_rules: list[dict], syntax: str) -> str:
    """The rules the skeleton already has (#26: an import's CHECKs, FK-shaped inclusions): kept as
    they are and added to the answer, so the model must neither restate nor reuse them."""
    head = (f"# Rules the spec already has (read from the database): they are kept as they are and "
            f"added to your rules.  Do not restate or change them, and do not reuse their ids or names; "
            f"number your rules from {next_rule_id(fixed_rules)}.\n")
    if syntax == "json":
        return head + json.dumps(fixed_rules, indent=1)
    from pydantic import TypeAdapter

    from mls.spec.model import Rule
    from mls.terse import section

    return head + section("rules", TypeAdapter(list[Rule]).validate_python(fixed_rules))


def system_prompt(role: str, skeleton: dict, schema: dict, syntax: str = "json",
                  fixed_rules: list[dict] = ()) -> str:
    """Role, language reference, the fixed skeleton (and the rules it already has), and the output
    contract: the JSON Schema of the pass, or (terse, D50) the grammar and three examples."""
    # `$comment` is the author's note on the spec, not part of the schema: in the eval corpus it
    # names the case (and, for a bad reference, the planted defect), so the model never sees it.
    skel = {k: v for k, v in skeleton.items() if k != "$comment"}
    fixed = [fixed_rules_text(list(fixed_rules), syntax)] if fixed_rules else []
    if syntax == "json":
        return "\n\n".join([
            role, LANGUAGE,
            "# The schema (tables, relations, hierarchies, identity, roles) is fixed:\n" + json.dumps(skel, indent=1),
            *fixed,
            "# Output JSON Schema:\n" + json.dumps(schema, separators=(",", ":")),
        ])
    from mls.spec.model import Spec
    from mls.terse import render

    skel_text = render(Spec.model_validate({**skel, "rules": [], "properties": []}))
    skel_text = skel_text[:skel_text.index("\nrules {")].rstrip()
    return "\n\n".join([
        role_text(role, syntax), LANGUAGE_TERSE,
        "# The schema (identity, roles, tables, relations, hierarchies) is fixed; your section completes it:\n"
        + skel_text,
        *fixed,
        terse_output_reference(),
    ])


def user_requirements(requirements: str) -> str:
    return "# Requirements\n" + requirements.strip()


def errors_text(errors: list[MlsError]) -> str:
    """One line per error: the path (and the source position when the answer was terse), the
    message, the fix."""
    def where(e: MlsError) -> str:
        return f"{e.path} ({e.span.line}:{e.span.col}: {e.span.text.strip()})" if e.span else e.path

    return "\n".join(f"- at {where(e)}: {e.message}" + (f" (fix: {e.fix})" if e.fix else "") for e in errors)


def user_repair(requirements: str, previous: dict | list | str, feedback: str, key: str) -> str:
    """`previous` is the JSON of the last answer, or (terse) its section text as the model wrote
    it, so that the line:col of the feedback point into what is quoted."""
    quoted = previous if isinstance(previous, str) else json.dumps(previous, indent=1)
    ask = (f"Return the complete corrected `{key} {{ ... }}` section (all {key}, not a diff)." if isinstance(previous, str)
           else f"Return the complete corrected answer (all {key}, not a diff).")
    return "\n\n".join([
        user_requirements(requirements),
        f"# Your previous answer ({key})\n" + quoted,
        "# Problems found by the checker\n" + feedback,
        ask + "  Keep ids stable where the item is unchanged.",
    ])
