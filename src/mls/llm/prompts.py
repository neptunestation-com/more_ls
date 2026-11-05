"""Prompts of the two passes and of the repair rounds.  Stable material (role, language
reference, the schema of the output) comes first so the prefix caches; the requirements and any
feedback come last."""
from __future__ import annotations

import json

from mls.errors import MlsError

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
- {"const": 3 | "x" | true}  a constant (no timestamp constants)

## Predicates
- {"cmp": [term, op, term]}  op in = <> < <= > >=; the right-hand side is a context value or a constant
- {"in": [term, [{"const": c1}, {"const": c2}, ...]]}  term equals one of the constants (sugar for an "or" of "=")
- {"member": {"relation": R, "key": {"ctx": c}, "value": {"col": v}}}   (ctx, col) is a pair of relation R
- {"in_group": {"hierarchy": H, "key": {"ctx": c}, "value": {"col": v}}}  col is one of the user's groups in
  hierarchy H or lies below one (policies only)
- {"now_between": {"from": term|null, "until": term|null}}   from <= now < until, NULL bound = open
- {"is_null": term}, {"and": [...]}, {"or": [...]}, {"not": p}, {"lit": true|false}, {"opaque": name}
- Negated member / in_group is not allowed in policies.  Context values are not allowed in checks.

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
- least_privilege: {"allowed": {role: {table: [commands]}}}  no role has more privileges than listed
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

ROLE_RULES = """You write the RULES (policies, checks, inclusions, transitions, cardinality limits, column
rules) of a database access-control spec from business requirements, for the given schema.  Cover
every requirement exactly: nothing more permissive, nothing more restrictive.  Give each rule a
stable id (R1, R2, ...) and cite the requirement it implements.  Remember that NULL denies in
policies: make membership columns and comparisons NULL-safe where the requirement allows it.  Put any
doubt about the requirements in "notes".  Answer with JSON only, matching the output schema."""


def system_prompt(role: str, skeleton: dict, schema: dict) -> str:
    return "\n\n".join([
        role, LANGUAGE,
        "# The schema (tables, relations, hierarchies, identity, roles) is fixed:\n"
        # `$comment` is the author's note on the spec, not part of the schema: in the eval corpus it
        # names the case (and, for a bad reference, the planted defect), so the model never sees it.
        + json.dumps({k: v for k, v in skeleton.items() if k != "$comment"}, indent=1),
        "# Output JSON Schema:\n" + json.dumps(schema, separators=(",", ":")),
    ])


def user_requirements(requirements: str) -> str:
    return "# Requirements\n" + requirements.strip()


def errors_text(errors: list[MlsError]) -> str:
    return "\n".join(f"- at {e.path}: {e.message}" + (f" (fix: {e.fix})" if e.fix else "") for e in errors)


def user_repair(requirements: str, previous: dict | list, feedback: str, key: str) -> str:
    return "\n\n".join([
        user_requirements(requirements),
        f"# Your previous answer ({key})\n" + json.dumps(previous, indent=1),
        "# Problems found by the checker\n" + feedback,
        f"Return the complete corrected answer (all {key}, not a diff).  Keep ids stable where the item is unchanged.",
    ])
