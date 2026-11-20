# MLS reference

The spec language, construct by construct, then the command-line interface.  `docs/GUIDE.md` is
the narrative introduction; `docs/SEMANTICS.md` is the formal meaning the prover, the evaluator
and the compiler agree on.  Where this reference and SEMANTICS differ, SEMANTICS wins.

Contents

1. [The spec file](#1-the-spec-file)
2. [Identifiers, types and values](#2-identifiers-types-and-values)
3. [`identity`](#3-identity)
4. [`roles`](#4-roles)
5. [`tables`](#5-tables)
6. [`relations`](#6-relations)
7. [`hierarchies`](#7-hierarchies)
8. [`opaques`](#8-opaques)
9. [Terms](#9-terms)
10. [Predicates](#10-predicates)
11. [Rules](#11-rules)
12. [Properties](#12-properties)
13. [Renderings: English and terse](#13-renderings-english-and-terse)
14. [Generated objects](#14-generated-objects)
15. [Command-line interface](#15-command-line-interface)
16. [Environment variables](#16-environment-variables)
17. [Errors](#17-errors)

## 1. The spec file

A spec is one JSON object (a `.mls.json` or `.json` file) or the same spec in the terse syntax
(a `.mls` file, §13); every command reads either, by extension, and `mls fmt` converts between
them.  The JSON is the canonical serialization: its keys, in order of appearance, are below,
and the JSON Schema is `mls schema`.

| Key | Required | Value |
|---|---|---|
| `$comment` | no | free text; an author's note, ignored by every command and never shown to the LLM |
| `mls_version` | yes | the string `"0.1"` |
| `identity` | yes | §3 |
| `roles` | yes | non-empty list of role names, §4 |
| `enums` | no | map of Postgres enum type name to its labels, in order (below) |
| `tables` | yes | map of table name to table, §5 |
| `relations` | no | map of relation name to relation, §6 |
| `hierarchies` | no | map of hierarchy name to hierarchy, §7 |
| `opaques` | no | map of opaque name to opaque block, §8 |
| `rules` | no | list of rules, §11 |
| `properties` | no | list of properties, §12 |

Unknown keys are errors everywhere (the model is strict).  There are no silent defaults in the
schema part: every column states its type and nullability, every policy its mode.  The only
defaults are `custom.which` (`"new"`), the `check` of an `update` or `all` policy (defaults to
its `using`, as in Postgres) and the optional lists.

## 2. Identifiers, types and values

**Identifiers.**  Table, column and role names are the catalog's names, exactly: they match
`[A-Za-z_][A-Za-z0-9_]*`, at most 63 characters, and are case-sensitive, so `userId` and
`userid` are two columns.  MLS emits every one double-quoted, so a mixed-case name such as
Prisma's `"userId"` or `"TeamMember"`, or a Postgres keyword, means exactly that name.  A name
outside the rule (a `$`, a space, a dot, a non-ASCII letter) is refused; rename it, or leave the
table out of the spec.  MLS's own names (relation, hierarchy, opaque and context names, rule
`name`) match `[a-z_][a-z0-9_]*`: they become parts of the names of generated objects.

**Column types**

| MLS | Postgres | Comparisons | Notes |
|---|---|---|---|
| `int` | `integer` | `= <> < <= > >=` | constants −2³¹ … 2³¹−1 |
| `bigint` | `bigint` | `= <> < <= > >=` | constants −2⁶³ … 2⁶³−1; Supabase's identity key type |
| `text` | `text` | `= <>` | ordering is not in the fragment; an adopted `varchar(n)` column is `text` |
| `bool` | `boolean` | `= <>` | |
| `timestamp` | `timestamptz` | via `now_between` only | no timestamp constants in v1 |
| `uuid` | `uuid` | `= <>` | constants are canonical UUID strings |
| `enum` | the declared enum type | `= <>`, `in` | adopted tables only; constants are the type's labels |

`int` and `bigint` compare with each other, in a comparison, a membership, an inclusion and a
cardinality group: the fragment has no arithmetic, so they differ only in range.  A
context value may be declared `bigint` too (`org_id: jwt.claims.org_id bigint`).  On an
adopted table a `character varying(n)` column is declared `text`: Postgres compares it as text,
`mls import` reads it so, `check --dsn` and `drift` accept it; MLS creates owned tables `text`.

**Enum columns.**  A Postgres enum type the database already has is declared under `enums`,
with its labels in the type's order, and a column of an adopted table names it:

    "enums": {"MemberRole": ["OWNER", "ADMIN", "MEMBER", "VIEWER"]},
    "columns": {"role": {"type": "enum", "enum": "MemberRole", "nullable": false}}

In the terse syntax, `enum MemberRole ('OWNER', 'ADMIN', 'MEMBER', 'VIEWER')` and
`role enum MemberRole not null`.  The type name is a catalog name (case-sensitive).  An enum
column compares with constants that are labels of its type, by `=`, `<>` and `in`, and is
tested with `is_null`; a string that is not a label is an error listing them, and so is a
comparison with another column, a context value or an ordering operator.  The proofs know that
the column holds nothing but its labels (or NULL), as Postgres guarantees: `role <> 'VIEWER'`
and `role in ('OWNER', 'ADMIN', 'MEMBER')` are the same predicate.  An enum column cannot be a
membership, inclusion, cardinality or hierarchy column, and a context value cannot be an enum.
MLS creates no types, so an owned table has no enum column: declare it `text` with a check rule
`col in ('A', 'B', ...)`.  `mls import` reads the schema's enum types and their columns;
`check --dsn` and `drift` compare the labels with the catalog's.

**Constants** (`{"const": v}`): a JSON integer, string or boolean.  An integer constant must be
within the range of the type it is compared with (`constant 2147483648 is out of range for int`,
fix: declare the column bigint).  A string compared with a
`uuid` column must be a UUID literal.  There are no NULL constants; use `is_null`.  There are no
timestamp constants.

**Context values** are never NULL (SEMANTICS §1).  Column values may be NULL when the column is
nullable.

## 3. `identity`

    "identity": {
      "adapter": "supabase" | "pg_roles" | "session_setting",
      "context": { "<name>": {"source": "<source>", "type": "<column type>"}, ... }
    }

A context value is a per-session value the policies may read as `{"ctx": "<name>"}`.  The
adapter maps each source to SQL and declares the trust assumption the proof report states.

| Adapter | Source | SQL | Type | Assumption |
|---|---|---|---|---|
| `supabase` | `auth.uid` | `(select auth.uid())` | must be `uuid` | clients reach the database only through Supabase's API layer, which verifies the JWT before setting `request.jwt.claims` |
| `supabase` | `jwt.claims.<claim>` | `(select (auth.jwt() ->> '<claim>')::<type>)` | any | same |
| `pg_roles` | `current_user` | `(select current_user::text)` | must be `text` | none |
| `session_setting` | `app.<name>` | `(select nullif(current_setting('app.<name>', true), '')::<type>)` | any | clients cannot execute `SET` on `app.*`; the setting is established by a trusted path (a pooler, a `SET LOCAL` inside a `SECURITY DEFINER` entry point, or the application's connection layer) that no untrusted client reaches |

`now` is a built-in context value in every adapter (the transaction start time, compiled as
`(select now())`) and may not be declared.  The adapter cannot be changed by `mls migrate`.
With `session_setting` a missing or empty setting is NULL: every comparison with it is UNKNOWN
and the row is denied (the English says "no context set: denied").  The assumption of the
adapter in use is stated by `mls prove`, `mls explain` and `mls diff`.

An adapter may declare its platform's roles.  `supabase` declares `anon`, `authenticated`
and `service_role`, which Supabase's default privileges grant every new table and function in
`public`.  The deploy revokes every privilege on governed tables and MLS functions from those
the spec does not use; `service_role` is the platform's trusted role (BYPASSRLS by design: the
backend key) and is kept, named among the trusted roles of every proof report, and not compared
by `mls drift`.

## 4. `roles`

    "roles": ["authenticated", "service"]

The Postgres roles the policies are written for.  The compiler creates a role if it does not
exist (`NOLOGIN`), and in every case removes `BYPASSRLS`, `SUPERUSER`, `CREATEDB` and
`CREATEROLE` from it and revokes `SET` on `session_replication_role`.  A role has a privilege
on a table only through its policies (§11.1) and column rules (§11.6).

The compiler also creates `mls_internal` when a hierarchy or a cardinality limit needs it (§14).
It is not a role you declare or grant to.

## 5. `tables`

    "tables": {
      "docs": {
        "columns": {
          "id":        {"type": "int",  "nullable": false},
          "owner_id":  {"type": "uuid", "nullable": true}
        },
        "primary_key": ["id"],
        "unique": [["owner_id", "slug"]],
        "mode": "owned",
        "assume": [],
        "tags": []
      }
    }

| Field | Required | Meaning |
|---|---|---|
| `columns` | yes, non-empty | name to `{type, nullable}` |
| `primary_key` | no | list of NOT NULL columns; compiled as the primary key constraint `<table>_pkey` |
| `unique` | no | list of column lists; each compiled as a UNIQUE constraint `<table>_<cols>_key` |
| `mode` | no, default `owned` | `owned`, `adopted` or `observed` (below) |
| `assume` | no | hardening the compiler cannot establish on an `adopted` table, stated as assumptions (below) |
| `tags` | no | free strings with no meaning of their own; `{"tagged": "x"}` selectors (§11) match them |

There is no way to reference a table the spec does not declare.  What the compiler does with a
declared table depends on its mode:

- **`owned`** (the default): MLS creates the table.
  The compiler emits `CREATE TABLE` with exactly these columns and constraints, revokes all
  privileges from `public` and from every declared role, then enables and forces row-level
  security.  Every object on it that the spec does not produce is drift.
- **`adopted`**: the table exists already.  `columns`, `primary_key` and `unique` describe it
  (`mls import` reads them from the catalog; `mls check --dsn` verifies them; `drift` compares
  them).  The compiler creates nothing and emits what the rules need: policies, constraints,
  triggers, indexes, privileges, and `ENABLE ROW LEVEL SECURITY`.  It does not `FORCE` row-level
  security and does not touch the owner: that hardening is either in place (drift verifies it)
  or stated under `assume`.  A constraint, trigger, index or column on the table that the spec
  does not declare is `foreign` (listed by drift, not drift); a policy or privilege the spec does
  not declare is still drift.  The spec owns the table's whole policy set: the deploy drops
  every policy already on it before creating the spec's (§14), so a hand-written permissive
  policy cannot keep granting what the proofs deny.
- **`observed`**: the table is in the schema and is mentioned so that the spec can speak about
  it; nothing governs it.  No rule, relation, hierarchy, opaque block or property may name it
  (the checker refuses with the path and the fix: set the mode to `adopted`).  Drift only checks
  that it exists.  `mls import` marks a table without row-level security as observed: a policy on
  such a table means nothing, so nothing can be assumed about it.

`assume` is a list of single-key objects, allowed only on an `adopted` table:

| Assumption | Meaning | What drift reports |
|---|---|---|
| `{"owner": "<app role>"}` | the table is owned by that app role, which could alter its policies, and the role is trusted | `assumed` instead of `hardening: owned by app role`; the owner's privileges are not compared (an owner holds them all) |
| `{"rls": "not_forced"}` | row-level security is not forced; the owner bypasses the policies and is trusted | `assumed` instead of `hardening: row level security is not forced` |
| `{"bypassrls": "<app role>"}` | the role has `BYPASSRLS`, reads and writes the table unfiltered, and is trusted | `assumed` instead of `hardening: role has BYPASSRLS` (only when every governed table assumes it) |
| `{"bypass": "view <v>"}` / `{"bypass": "function <f>(<types>)"}` | a bypass path (below) reaches the table without its policies and is trusted; what an app role reaches through it is not covered by the proofs | `assumed` instead of `hardening: <path> reaches table <t> without its policies` |

Every assumption is repeated in the proof report, `mls explain`, `mls diff` and the compiled
script's header (the list of trusted roles grows by it).  When the catalog no longer needs an
assumption (the owner changed, RLS was forced), drift says so in a note: the fix shows up as the
assumption going away.  Refused at `check`: `assume` on an `owned` table (its hardening is
emitted and verified, so an app-role owner is an error there, not an assumption; `mls check
--dsn` reports it as such), on an `observed` table, naming a role that is not an app role of the
spec, or an `rls` value other than `not_forced` (there is no assumption under which a policy on a
table without row-level security enabled means anything), or a `bypass` value that is not
`view <name>` or `function <name>(<argument types>)`.

**Bypass paths**.  On an existing database the holes in row-level security are rarely
`BYPASSRLS`; they are views and functions.  `mls drift`, `mls status --dsn` and `mls import`
report, for every governed table:

- a view over the table (directly or through other views) that is not `security_invoker`, on
  which an app role holds any privilege: it runs as its owner, whom the policies of an adopted
  table usually do not bind;
- a materialized view over the table on which an app role holds a privilege (its rows were read
  by its owner);
- a `SECURITY DEFINER` function an app role may execute, whose owner bypasses the table's
  policies (a superuser, a `BYPASSRLS` role, or the table's owner while row-level security is
  not forced) and whose body names the table (a text match, so a function that only mentions
  the name is reported too).

The path is spelled `view <name>` or `function <name>(<argument types>)` (the types as
Postgres prints them, `function is_team_member(bigint)`), schema-qualified outside `--schema`.
Each is `hardening` with the fix (`alter view <v> set (security_invoker = true)` or revoking
the app roles' privileges; for a function, `revoke execute on function <f> from public, <roles>`);
on an adopted table `{"bypass": "<path>"}` assumes it instead.  An assumed path the catalog no
longer shows is a note.  The trusted platform roles (`service_role` on Supabase) are not app
roles, so their privileges on a view or function are not a bypass path.

## 6. `relations`

    "relations": {
      "member_of": {"table": "memberships", "key": "user_id", "value": "tenant_id"}
    }

A relation names the `(key, value)` projection of a table, for use in `member`, `inclusion`,
`tenant_isolation` and `monotone_in`.  `key` and `value` must be different NOT NULL columns of
the table, and together must be the primary key or a declared unique constraint (so a pair is
stored at most once and a composite foreign key can target it).

Reading a relation in a policy has an implicit cost: because RLS is forced on the
relation's table too, the compiler emits a permissive SELECT policy `key = <context>` on it for
every role whose policies use the relation with that key, plus `GRANT SELECT (key, value)`.  A
restrictive policy the spec puts on the relation table must keep those rows visible; the
compiler proves this (the relation-visibility obligation) and refuses otherwise.

A policy on a relation's own table cannot read the relation (nor a composed relation with a hop
through the table): its subquery would apply the table's policies again, and Postgres refuses
every query on the table with "infinite recursion detected in policy" (FINDINGS).  The checker
refuses such a rule at its `member.relation`; a user's own rows of a membership table are
`user_id = @uid`, a comparison, not a membership.

**Composed relations** (SEMANTICS §3.4) follow a base relation through up to three tables:

    "relations": {
      "member_of": {"table": "team_members", "key": "user_id", "value": "team_id"},
      "in_project": {"of": "member_of", "through": [{"table": "projects", "key": "team_id", "value": "id"}]},
      "in_task": {"of": "member_of", "through": [{"table": "projects", "key": "team_id", "value": "id"},
                                                 {"table": "tasks", "key": "project_id", "value": "id"}]}
    }

`(k, v) ∈ in_task` when `(k, t) ∈ member_of`, a `projects` row has `team_id = t` and `id = p`, and a
`tasks` row has `project_id = p` and `id = v`. `of` is a base relation (not composed); at most three
hops; each hop's `key` and `value` are different NOT NULL columns, the `key` has the type of the
previous level's value, and the `value` is the table's primary key or a unique column (so each hop
is a lookup, not a join). A hop through a column that is not a key is refused at
`$.relations.R.through[i].value`; the ways out are a membership table (a base relation) or the
column copied onto the referencing table with an inclusion rule. A composed relation is used with
`member` like any relation, in policies, goals, `must_allow` and `tenant_isolation`; it is refused
in an invariant, as an inclusion rule's relation, as a hierarchy's `members`, in an opaque block's
`relations` and in `monotone_in`. The implicit relation policy above applies to every hop table: a
permissive SELECT policy `mls_rel_<R>_h<i>_<role>_<n>` (the hop key in the previous level's values;
`<R>` is the shortest composed relation through the hop, so relations sharing a prefix share it),
`GRANT SELECT (key, value)`, and the relation-visibility obligation.

## 7. `hierarchies`

    "hierarchies": {
      "org": {"table": "groups", "id": "id", "parent": "parent_id", "depth": "depth",
              "max_depth": 3, "members": "group_member"}
    }

A hierarchy is a forest over `table`, for use in `in_group`.

| Field | Constraint |
|---|---|
| `table` | a declared table |
| `id` | its single-column primary key |
| `parent` | a nullable column of the same type as `id` (NULL for roots) |
| `depth` | an `int` NOT NULL column that the compiler maintains; the app never writes it |
| `max_depth` | integer 1–16: the bound the proofs unroll to |
| `members` | a relation whose `value` has the type of `id`: who is in which group |

`id`, `parent` and `depth` must be three different columns.  Compiled objects (SEMANTICS §3.1):
a foreign key from `parent` to `id`; `CHECK (0 <= depth <= max_depth)`; a `BEFORE INSERT OR
UPDATE` trigger `mls_h_<name>_depth` that sets `depth` from the parent; an implicit transition
rule `mls_h_<name>_parent_immutable` (a group is never reparented); a foreign key from the
members relation's `value` to `id`; and a `SECURITY DEFINER` function `mls_reach_<name>(key)`
returning the ids reachable downward from the key's groups, which `in_group` policies call once
per statement.  The hierarchy's table and the members table get an `mls_internal_<table>`
policy for the helper's role.

## 8. `opaques`

    "opaques": {
      "low_or_cleared": {"table": "docs", "columns": ["level"], "context": ["clearance"],
                         "relations": [], "sql": "level < 1 or {ctx:clearance} >= 3"}
    }

An opaque block is raw SQL with a declared signature, used as the predicate `{"opaque": name}`.

| Field | Meaning |
|---|---|
| `table` | the table whose rows the SQL is evaluated against; the block may be used only in predicates of that table |
| `columns` | the columns it reads, by bare name |
| `context` | the context values it reads, written `{ctx:<name>}` in the SQL |
| `relations` | relations it reads (state-dependent); affects which changes the diff treats as relevant |
| `sql` | a boolean SQL expression; non-empty |

The prover models an opaque block as an uninterpreted predicate over its declared inputs.  A
proof that needs the block is reported as `conditional on opaque blocks: <names>` and the
report's assumptions list the block with its SQL.  "Needs" is checked: the proved
obligation is solved again with every occurrence of the block an independent unknown; if it
still holds, it holds whatever the block's SQL does (even if the SQL reads more than its
signature), and the block is not listed.  So a property proved by a restrictive policy, or by
the other permissive policies, is not conditional on a permissive block next to them; a property
whose goal reads the same block a policy reads is.  `mls lint` findings and `mls status` follow
the same rule (the policy-set findings).  A property that could hold only by
constraining the block is REFUTED.  Opaque blocks are excluded from the performance guarantees
and from the lint; the differential test executes their SQL.

## 9. Terms

| Term | Meaning | Allowed in |
|---|---|---|
| `{"col": "c"}` | column `c` of the row | every predicate kind except transitions |
| `{"ctx": "n"}` | context value `n` | policies, goals, `must_allow` conditions |
| `{"const": v}` | a constant (§2) | everywhere |
| `{"old": "c"}` | column `c` of the row before an update | transitions, `must_allow` conditions for update |
| `{"new": "c"}` | column `c` of the row after an update | transitions, `must_allow` conditions for update |

In a `must_allow` condition for `update`, a bare `{"col": "c"}` means `{"new": "c"}`.

## 10. Predicates

A predicate is a single-key JSON object.  Every predicate has a value in {TRUE, FALSE, UNKNOWN}
with SQL semantics; the SMT encoding is a pair of booleans and the compiled targets are
normalized so that a row is admitted exactly when the predicate is TRUE (SEMANTICS §1, §4).

### 10.1 Forms

**`{"cmp": [term, op, term]}`**, `op` in `= <> < <= > >=`.  UNKNOWN if either operand is NULL.
The left operand is a column (or an old/new image column); the right is a context value or a
constant, or in transitions and update conditions another image column.  The operand types
must agree and the operator must be allowed on the type (§2).  Two constants are rejected.

**`{"in": [term, [const, ...]]}`**: `term` equals one of the constants.  Sugar: the checker
expands it to `{"or": [{"cmp": [term, "=", c1]}, ...]}` (a single constant becomes the bare
comparison) before anything else reads the spec, so it is allowed exactly where a `=`
comparison of `term` with each constant is, has that disjunction's meaning (UNKNOWN if `term`
is NULL) and renders as it in `explain` and `--terse`.  The list must be non-empty.  Compiled as
`term in (c1, c2, ...)`, one index condition; a hand-written `or` of the same shape
compiles the same way.

**`{"member": {"relation": R, "key": k, "value": v}}`**: `(k, v)` is a stored pair of relation
`R`.  `k` is a context value (or, in invariants and goals, a column); `v` is a column of the
relation's value type and must be NOT NULL when the predicate is a policy atom.  UNKNOWN if `v`
is NULL.  Compiled as `v = any(array(select value from <table> where key = <k>))`; over a
composed relation, one nested `= any(array(select ...))` per hop, innermost the base relation.

**`{"in_group": {"hierarchy": H, "key": k, "value": v}}`**: the group `v` is one of the key's
groups in `H` or lies below one, within `max_depth`.  `k` is a context value, `v` a column of
the hierarchy's id type.  UNKNOWN if `v` is NULL.  Compiled as
`v = any((select mls_reach_H(<k>))::<type>[])`.

**`{"now_between": {"from": t | null, "until": t | null}}`**: `from <= now < until`.  Both
bounds are timestamp columns; a missing bound is open; a NULL bound is open.  Two-valued.  At
least one bound must be given.

**`{"is_null": term}`**: two-valued; applies to a column.

**`{"and": [p, ...]}`, `{"or": [p, ...]}`, `{"not": p}`**: SQL three-valued connectives.  Lists
are non-empty.

**`{"lit": true | false}`**: a constant predicate.

**`{"opaque": name}`**: the named opaque block (§8); only in predicates of the block's table.

### 10.2 Where each form is allowed

The checker enforces a scope per predicate kind.  "ctx" is a context term anywhere in the
predicate; "col key" means the membership key may be a column instead of a context value.

| Predicate kind | `col` | `ctx` | `old`/`new` | `member` | col key | `in_group` | `now_between` | `opaque` | opaque with context |
|---|---|---|---|---|---|---|---|---|---|
| policy `using` / `check` | yes | yes | no | yes¹ | no | yes¹ | yes | yes | yes |
| `check` rule | yes | no | no | no | – | no | no | yes | no |
| `transition` rule | no | no | yes | no | – | no | no | no | no |
| invariant (`invariant_preserved` inline) | yes | no | no | yes | yes | no | no | no | no |
| `custom.goal` | yes | yes | no | yes | yes | yes | yes | yes | yes |
| `must_allow.when` | yes | yes | update only | yes | yes | yes | yes | yes | yes |

¹ Not under a `not` (a negated membership is an anti-join the planner cannot drive from an
index).  Use an opaque block if you need one.

A `check` rule cannot mention membership; that is what `inclusion` is for (and it compiles to
a foreign key, which a CHECK cannot express soundly).

## 11. Rules

Every rule has `id` (unique across rules), `kind`, `name` (an identifier, unique per table; it
names the compiled object), `table` or `applies_to` (below), and optionally `requirement`
(free text).

**Selectors**.  `"applies_to"` in place of `"table"` makes one rule apply to every
governed table a selector matches:

    {"id": "R1", "kind": "policy", "name": "member_read",
     "applies_to": {"has_column": ["tenant_id", "int"]},
     "role": "authenticated", "command": "select", "mode": "permissive", "using": ...}

| Selector | Matches a table that |
|---|---|
| `{"has_column": ["c"]}` / `["c", "type"]` | has column `c` (of that type) |
| `{"named": ["t", ...]}` | is one of the names |
| `{"tagged": "tag"}` | lists the tag under `tags` (§5) |
| `{"all": true}` | is any governed table |
| `{"and": [...]}`, `{"or": [...]}` | matches all / any of the selectors |

`mls check` expands the rule, before anything else, into one rule per matching `owned` or
`adopted` table in declaration order, with id `<id>.<table>` (`R1.docs`, `R1.files`), the same
`name`, `"table"` set and `"from": "R1"`; an `invariant_preserved` reference to `R1` becomes
one reference per expansion.  Everything downstream, the solver, the compiler, `diff`,
`migrate`, `drift`, the English and terse renderings, sees only the concrete rules, so a
selector rule and its hand-written expansion (the same ids, with `from`) are the same spec:
`mls explain --terse` prints them byte-identically and `mls diff` reports changes per concrete
rule.  A selector that matches no governed table is an error at `check`, `rule 'R1' applies to
no table (has column tenant_id)`, at `$.rules[i].applies_to` (a silent empty expansion is how
a renamed column disables a tenant policy); the message names an `observed` table that would
have matched.  Late binding: a table added to the spec (by hand or by re-running `mls import`)
with the selected column is covered the next time `check` runs.  Errors inside an expanded rule
report under the selector rule's path.

### 11.1 `policy`

    {"id": "R1", "kind": "policy", "name": "member_read", "table": "docs",
     "role": "authenticated", "command": "select", "mode": "permissive",
     "using": p, "check": p}

| Field | Values |
|---|---|
| `role` | a declared role |
| `command` | `select`, `insert`, `update`, `delete`, `all` |
| `mode` | `permissive` or `restrictive` |
| `using` | rows the command may touch (existing rows); required for select, update, delete, all; not allowed for insert |
| `check` | rows the command may write (new rows); required for insert; not allowed for select and delete; for update and all defaults to `using` |

Meaning (SEMANTICS §5): for a table, role and command, a row is admitted iff some permissive
policy is TRUE on it and every restrictive policy is TRUE on it.  No permissive policy for a
command means the command is denied (and not granted).  An update targets rows where its
`using` is TRUE and, when the statement reads columns, the select `using` too; it errors unless
its `check` is TRUE on the new row (and the select `using`, when the statement reads columns).

Compiles to `CREATE POLICY <name> ON <table> AS <mode> FOR <command> TO <role> USING (...)
[WITH CHECK (...)]`, a `GRANT <command>` on the table for each permissive policy (`all` grants
all four), the implicit relation policies and grants of §6, and the indexes of §14.

### 11.2 `check`

    {"id": "R6", "kind": "check", "name": "level_range", "table": "docs", "predicate": p}

Every stored row satisfies `p`, where UNKNOWN does *not* satisfy: compiled as
`ALTER TABLE ... ADD CONSTRAINT <name> CHECK ((p) IS TRUE)`.  The constraint is created
validated; the proofs assume every stored row satisfies every check.

### 11.3 `inclusion`

    {"id": "R7", "kind": "inclusion", "name": "owner_in_tenant", "table": "docs",
     "columns": ["owner_id", "tenant_id"], "relation": "member_of", "on_delete": "restrict"}

Every row whose two columns are both non-NULL has `(columns[0], columns[1])` as a stored pair
of the relation (predicate `is_null(c1) ∨ is_null(c2) ∨ member(R, c1, c2)`), and such a pair
cannot be deleted or changed while a row refers to it.  The two columns must differ and match
the relation's key and value types.  `on_delete` is `restrict` only.  Compiles to a composite
foreign key `MATCH SIMPLE ON UPDATE RESTRICT ON DELETE RESTRICT` and an index on the two
columns.

### 11.4 `transition`

    {"id": "R9", "kind": "transition", "name": "tenant_immutable", "table": "docs",
     "predicate": {"cmp": [{"new": "tenant_id"}, "=", {"old": "tenant_id"}]}}

Every update of a row satisfies `p` over the old and new images (TRUE required).  Compiles to
a `BEFORE UPDATE ... FOR EACH ROW` trigger `<name>` calling `mls_tr_<table>_<name>()`, which
raises `check_violation` with the message `MLS transition <name> on <table> violated` unless
`(p) IS TRUE`.  A transition guards every update, by every role, including the owner's.

### 11.5 `cardinality`

    {"id": "R5", "kind": "cardinality", "name": "files_per_tenant", "table": "files",
     "group": "tenant_id", "parent": "tenants", "max": 5}

At most `max` rows of `table` share each non-NULL value of `group`.  `parent` is a different
table with a single-column primary key of `group`'s type; `group` gets a foreign key to it.
Compiles to a `BEFORE INSERT OR UPDATE OF <group>` trigger `<name>` calling the `SECURITY
DEFINER` function `mls_card_<table>_<name>()` owned by `mls_internal`: it returns at once for a
NULL group or an update that keeps the group, otherwise locks the parent row `FOR UPDATE`,
counts, and raises `check_violation` if the count would exceed `max`.  Correct under READ
COMMITTED (stated in the proof report); the parent table gets an `mls_internal_<parent>` policy
`FOR ALL`.

### 11.6 `column_rule`

    {"id": "C1", "kind": "column_rule", "table": "docs", "role": "authenticated",
     "columns": ["id", "tenant_id", "level"], "access": "read"}

The role may `read` (SELECT) or `write` (INSERT and UPDATE) only the listed columns of the
table.  At most one rule per role, table and access.  Without a read rule the role's select
policies cover every column; without a write rule, its insert and update policies do.
Compiles to column-list grants (`GRANT SELECT (cols)`, `GRANT INSERT (cols)`, `GRANT UPDATE
(cols)`) in place of the table-level grant.  `name` is optional for this kind.

Column rules are per role, not per row.  A column whose visibility depends on the row is
modelled as its own table with its own policies.

## 12. Properties

Every property has `id` (unique across properties) and an optional `description`, the
plain-English sentence a reviewer approves.  `mls prove` expands each property into named
obligations and reports each as PROVED, REFUTED (with a counterexample scenario) or UNKNOWN
(solver timeout).  A property's verdict is REFUTED if any obligation is, else UNKNOWN if any
is, else PROVED; a PROVED property whose proof depends on an opaque block is marked
`conditional on`.

Assumptions shared by every obligation: stored rows satisfy every check, inclusion and
hierarchy invariant; context values are not NULL; the identity adapter's assumption.

### 12.1 `tenant_isolation`

    {"id": "P1", "template": "tenant_isolation", "table": "docs", "column": "tenant_id",
     "relation": "member_of", "key": {"ctx": "uid"}, "commands": ["select", "insert", "update"]}

For each listed command: every row the role can `<command>` (the new image for insert and
update) has `(key, column)` in the relation.  One obligation per command and role.

### 12.2 `column_immutable`

    {"id": "P2", "template": "column_immutable", "table": "docs", "column": "tenant_id"}

No admitted update changes the column: for every role with an update policy,
`new.column = old.column` (NULL-aware: NULL stays NULL).

### 12.3 `monotone_in`

    {"id": "P3", "template": "monotone_in", "relation": "member_of"}

Removing a pair from the relation never admits a row that was not admitted: for every policy
predicate that reads the relation, admitted with fewer facts implies admitted with more.  One
obligation per policy predicate (using and check separately).

### 12.4 `write_subset_read`

    {"id": "P5", "template": "write_subset_read", "table": "docs"}

For every role: every row it can insert, and the new image of every row it can update, is a
row it can select.

### 12.5 `least_privilege`

    {"id": "P6", "template": "least_privilege",
     "allowed": {"authenticated": {"docs": ["select", "insert", "update"], "memberships": ["select"]}}}

The privileges the compiler derives (from policies, column rules and the implicit relation
grants of §6) are within the declared allowance.  Not a solver obligation: a comparison of
grant sets.  A role or table absent from `allowed` is allowed nothing.

### 12.6 `invariant_preserved`

    {"id": "P4", "template": "invariant_preserved",
     "invariants": [{"rule": "R6"}, {"rule": "R7"}, {"table": "salaries", "predicate": p}]}

Inductive: for every table the invariants mention (including the tables of referenced
relations) and every statement kind on it, if the invariants hold before the statement and the
statement is admitted by the *integrity* guards (checks, foreign keys, transitions; policies
are not trusted because trusted roles bypass them), the invariants hold after.  An invariant is
a `check` or `inclusion` rule by id, or an inline `{table, predicate}` in invariant scope
(§10.2).  Deletes and updates on a relation's table are modelled with the RESTRICT guard of the
foreign key.

### 12.7 `custom`

    {"id": "P1", "template": "custom", "table": "files", "role": "authenticated",
     "command": "select", "which": "new", "goal": p}

An upper bound: every row the role can `<command>` satisfies `goal` (TRUE).  `which` selects the
image for update: `old` (the rows the update may target, its `using`) or `new` (the rows it may
write, its `check`); the default is `new`.  Goal scope is §10.2.

### 12.8 `must_allow`

    {"id": "P7", "template": "must_allow", "table": "docs", "role": "authenticated",
     "command": "select", "when": p}

A lower bound: every row that satisfies `when` and is valid (satisfies the table's checks
and inclusions) is admitted by the role's `<command>`, where "admitted" means the effective
policies and, for update, the transition rules and the column privileges too.  Two obligations:
a witness (`some row meets the condition`, reported with an example; a condition no valid row
meets refutes the property) and the bound (`every row meeting the condition is admitted`).  For
update, `when` ranges over `old` and `new` and should state the whole change.  The written row
is assumed valid, so `when` states what the user can do, not the integrity rules.

### 12.9 `role_separation`

    {"id": "S1", "template": "role_separation", "table": "approvals",
     "roles": ["requester", "approver"], "command": "update"}

No row is admitted to `<command>` for both roles: the English form of the `custom`
property it already was.  One obligation, `<command>: no row is admitted to both a and b`:
the stored-row constraints and both roles' effective policies (for update, the targeted and the
written row, with the transition rules) are unsatisfiable together.  Both roles are evaluated
with the same context values, so it states that no user acting under either role reaches the
same row.  A counterexample is a row (and user) both roles may touch.  Two different declared
roles are required.

## 13. Renderings: English and terse

`mls explain` prints every rule, property and (in reports) scenario as an English sentence
(`src/mls/english.py`).  This is the approval artifact: it is what a product owner reads.
A selector rule (§11) is printed once, *applies to every governed table that has column
tenant_id: docs, files*, with its expansion as an indented list of concrete rules.  A composed
relation (§6) gets a sentence of its own under *Relations read through other tables*: *a user's
in_task are the id of every tasks row whose project_id is the id of a projects row whose team_id
is among that user's member_of*.  A context value reads as *the current user* when the adapter
names it as the user (`auth.uid`, `current_user`, or on `session_setting` the one context value
the policies use as a membership key), and as *the user's <name>* otherwise.

The terse syntax is the second concrete syntax of
the same model: `mls explain --terse` prints the whole spec in it (`src/mls/terse.py:render`),
the `before`/`after` lines of `mls diff` and the `spec_terse` field of LLM transcripts use its
predicate and rule forms, and the parser (`src/mls/spec/syntax.py`) reads `.mls` files back,
with a source span per node (the JSON stays the canonical serialization).  The round trip
`parse(render(spec)) == spec` holds for every example and every eval case and is tested; a
one-element `and`/`or` is the one shape it cannot keep (it reads back as its element).  The
grammar:

    spec       := 'mls' VERSION [comment] identity roles enum* table* relation* hierarchy* opaque*
                  'rules' '{' rule* '}' 'properties' '{' property* '}'
    comment    := 'comment' STRING                               (the spec's $comment)
    identity   := 'identity' ADAPTER '{' [ctx (';' ctx)*] '}'    ctx := NAME ':' SOURCE TYPE
    roles      := 'roles' NAME (',' NAME)*
    table      := 'table' NAME MODE '{' [column (',' column)* [';']] [option (';' option)*] '}'
    enum       := 'enum' NAME '(' STRING (',' STRING)* ')'        (a Postgres enum type and its labels, in order)
    column     := NAME TYPE ['not null']                          (a bare column is nullable; TYPE 'enum' NAME for an enum)
    option     := 'primary_key' '(' names ')' | 'unique' '(' names ')' | 'tags' '(' names ')'
                | 'assume' '(' STRING (',' STRING)* ')'           ('owner r' | 'bypassrls r' | 'rls not_forced' | 'bypass view v' | 'bypass function f(t)')
    relation   := 'relation' NAME '=' TABLE '(' KEY ',' VALUE ')'
                | 'relation' NAME '=' BASE ('.' TABLE '(' KEY ',' VALUE ')')+   (composed)
    hierarchy  := 'hierarchy' NAME '=' TABLE 'id' COL 'parent' COL 'depth' COL 'max_depth' INT 'members' REL
    opaque     := 'opaque' NAME 'on' TABLE '(' [input (',' input)*] ')' 'relations' '(' [names] ')' 'sql' DOLLAR
    input      := COL | '@' CTX                                   DOLLAR := $$ sql $$ | $tag$ sql $tag$
    rule       := ID KIND [NAME] ('on' TABLE | 'applies_to' selector) ['from' ID] ':' body ['requirement' STRING]
    selector   := has_column(COL [TYPE]) | named(T, ...) | tagged(TAG) | all | and(sel, ...) | or(sel, ...)
    property   := ID TEMPLATE [table role command which]: ... ['description' STRING]
    term       := column | old.column | new.column | @context | 'text' | 123 | true | false
    atom       := term op term | term in (const, ...) | term is null | relation(key, value)
                | in_group(h, key, value) | now_between(from | -, until | -) | opaque(name) | true | false
    pred       := atom | not pred | pred and pred | pred or pred   (not > and > or; a nested same-
                                                                   connective group keeps its parens)
    least_privilege body := grant (';' grant)* | '[]'   grant := ROLE TABLE '[' [commands] ']' | ROLE '[]'

`# comments` run to the end of the line.  Keywords are reserved (the section, clause, kind,
template, command, mode and predicate words; `terse.RESERVED`): a name spelled like one, or
that is not an identifier, is written `"quoted"`; `'strings'` double their quotes; the SQL of
an opaque block is dollar-quoted as in Postgres, with a tag (`$mls$`) when it contains `$$`.
Every item is one line, except a table, whose columns and options are one per line, so a diff
shows one changed rule or column as one changed line.  A nested group of the same connective
keeps its parentheses (`a and (b and c)`), so the text reads back as the tree it was.  The
parser accepts the sections in any order (`identity`, `roles`, `rules` and `properties` at
most once each) and any whitespace; the formatter writes them in the order above.  A rule's
`applies_to` is shown on the selector rule; after `mls check` expands it, each expansion shows its table and `from`.  An
`in` list is shown as written before `check` and as its expansion (`level = 2 or level = 3`)
after.

    mls 0.1
    comment 'Tenant-scoped documents'

    identity supabase { uid: auth.uid uuid; clearance: jwt.claims.clearance int }
    roles authenticated

    table docs adopted {
      id int not null,
      tenant_id int not null,
      owner_id uuid;
      primary_key (id);
      unique (tenant_id, owner_id);
      tags (tenant);
      assume ('rls not_forced')
    }

    relation member_of = memberships(user_id, tenant_id)
    hierarchy org = groups id id parent parent_id depth depth max_depth 3 members group_member
    opaque gate on docs (level, @clearance) relations () sql $$ level < 1 or {ctx:clearance} >= 3 $$

    rules {
      R1 policy member_read on docs: authenticated select permissive using member_of(@uid, tenant_id) and level <= @clearance requirement 'REQ-1'
      R2 policy tenant_read applies_to has_column(tenant_id int): authenticated select permissive using member_of(@uid, tenant_id)
      R7 inclusion owner_in_tenant on docs: (owner_id, tenant_id) in member_of on_delete restrict
      R9 transition tenant_immutable on docs: new.tenant_id = old.tenant_id
      R5 cardinality files_per_tenant on files: tenant_id -> tenants max 5
      C1 column_rule on employees: hr write title, grade
    }

    properties {
      P1 tenant_isolation docs: tenant_id in member_of(@uid) for select, insert, update description 'members only'
      P6 least_privilege: authenticated docs[select, insert, update]; authenticated memberships[select]
      P7 must_allow docs authenticated select: member_of(@uid, tenant_id) and not archived is null
      S1 role_separation approvals update: requester | approver
    }

A scenario (a counterexample or an example) lists the context, the row or rows, and the
relation facts, in the proof report as

    - context: uid = uuid#3, clearance = 0
    - r (docs): tenant_id = 2, owner_id = None, level = 0, archived = 2
    - member_of = {(uuid#3, 0), (uuid#3, 2)}

plus, when a composed relation is read, the hop rows the scenario depends on (`- projects(team_id,
id) = {(0, 4)}`).

and in English in the diff (`the current user is uuid#2; the existing row of docs with ...`).
`uuid#n` are distinct symbolic UUIDs.

## 14. Generated objects

All names are deterministic functions of the spec, so `mls drift` can match them.

| Object | Name |
|---|---|
| primary key | `<table>_pkey` |
| unique constraint | `<table>_<col>[_<col>...]_key` |
| check constraint, foreign key, policy, trigger | the rule's `name` |
| transition trigger function | `mls_tr_<table>_<name>()` |
| implicit relation policy | `mls_rel_<relation>_<role>_<n>` |
| hop visibility policy of a composed relation | `mls_rel_<relation>_h<i>_<role>_<n>` on the i-th hop table |
| internal-role policy | `mls_internal_<table>` |
| hierarchy depth check and parent foreign key | `mls_h_<hierarchy>_depth_max`, `mls_h_<hierarchy>_parent` |
| hierarchy depth trigger and function | `mls_h_<hierarchy>_depth` |
| hierarchy parent-immutable trigger | `mls_h_<hierarchy>_parent_immutable` (function `mls_tr_<table>_mls_h_<hierarchy>_parent_immutable`) |
| hierarchy reachability function | `mls_reach_<hierarchy>(key)` |
| cardinality trigger function | `mls_card_<table>_<name>()` |
| index | `mls_ix_<table>_<col>[_<col>...]` |
| migration: replacement constraint before the swap | `<name>_mls_new` |
| adopted-table constraint comparison (dropped in the same block) | `mls_probe` |
| internal role | `mls_internal` (NOLOGIN, NOBYPASSRLS, NOINHERIT; owns the helpers) |

Policy-set replacement, on every `adopted` table, first in the policies section: a `do`
block that drops every policy on the table, by name read from `pg_policy` when the script runs,
and raises the notice `MLS drops policy <name> on <table>: the spec does not declare it` for
each one the spec does not declare; the spec's policies are created after it.  The policies the
notices name are exactly the `unexpected` policies `mls drift` reports before the deploy.
Dropping the spec's own names too is what lets the script run again on a deployed database.
Owned tables need no replacement (MLS created them), and `mls migrate` emits none (it starts from
a deployment, which has no foreign policy).

Constraints on an adopted table: the table may already have a constraint under the name
of a check or inclusion rule (`mls import` names its rules after the constraints it read), so
each is a `do` block instead of a bare `add constraint`.  When no constraint has the name, it
is added.  When one has, the spec's definition is added `NOT VALID` as `mls_probe` (no scan),
its `pg_get_constraintdef` is compared with the existing one's, and the probe is dropped; equal
definitions keep the existing constraint as it is, any difference (a `NOT VALID` one included)
drops it and adds the spec's, which validates the rows, with the notice `MLS replaces
constraint <name> on <table>: <existing> (the spec: <definition>)`.  Rows that violate the
spec's constraint fail the deploy.  Before the deploy, `mls drift` reports each such
constraint as `differs`.

On an adopted table the indexes are `create index if not exists`, so the script runs again
after a rollback that left them in place (a policy-set rollback by deploying the previous spec,
GUIDE §3); drift reports a same-named index whose definition differs.

Indexes: one composite index per conjunctive group of a permissive policy (membership
columns first, then comparison columns), one on each foreign key's referencing columns, and a
key-leading one on each relation table (its primary key or unique constraint usually supplies
it), and a `(key, value)` one on each hop table of a composed relation.  A key that is a prefix of another wanted key is covered by it, whatever order the rules
name them in, so the index set does not depend on rule order.

Hardening, on every deployment: the preamble refuses to run as an app role; app roles
lose `BYPASSRLS`, `SUPERUSER`, `CREATEDB`, `CREATEROLE` and `SET` on `session_replication_role`
(one `ALTER ROLE` per attribute the role has, so a role that has none is not altered: Supabase
refuses any `ALTER ROLE` on its API roles); the helpers' ownership moves to `mls_internal`
with `CREATE` on the schema granted for the transfer only (a non-superuser deployer needs it on
PG 15+); the platform's untrusted roles lose their default privileges (§3);
every owned table gets `ENABLE` and `FORCE ROW LEVEL SECURITY` (an adopted table only `ENABLE`,
§5); all privileges are revoked from `public`; `TRUNCATE` is never granted.  With the `session_setting` adapter, `mls drift` also
reports (kind `hardening`) an app role that may execute a function whose body sets `app.*`
(`set_config('app....` or `SET app....`, in any schema) and an app role with an
`ALTER ROLE ... SET app.*` default; nothing is emitted for these, since the fix is to
revoke or reset something MLS did not create.

## 15. Command-line interface

    uv run mls <command> [options]

Every command reads a spec in either syntax, by extension (`.mls` is terse, anything else is
JSON; §1, §13).  Commands that load a spec exit 1 with the checker's errors if it does not
check (a syntax error is `syntax at <path>: <file>:<line>:<col>: expected ..., found ...`
followed by the offending line).  Commands that need a database take `--dsn`; `mls test` can
start Docker containers instead.

### `mls check SPEC [--dsn DSN [--schema NAME]] [--json]`

Schema, types, fragment and the compiler's soundness obligations.  Prints `SPEC: OK` or the
errors (§17).  `--json`: `{"ok": true}` or `{"ok": false, "errors": [...]}`.  Exit 0 or 1.
With `--dsn`, the spec's tables are also verified against the catalog: an `adopted` or
`observed` table must exist; an `adopted` table must have every column the spec declares, with
the declared type (a missing column is an error here and drift otherwise); an `owned` table
that exists must not be owned by an app role (the fix names the `adopted` mode and the
assumption to add).

### `mls prove SPEC [--format md|json] [--out FILE] [--timeout SECONDS]`

The proof report (§12).  `--timeout` is per obligation, default 20; a timed-out obligation is
UNKNOWN.  JSON shape: `{"assumptions": [str], "summary": {"PROVED", "REFUTED", "UNKNOWN",
"CONDITIONAL"}, "properties": [{"id", "template", "description", "verdict", "conditional_on",
"obligations": [{"name", "verdict", "message", "scenario", "opaque_deps"}]}]}`.  Exit 2 if
any property is REFUTED or UNKNOWN, else 0.

### `mls compile SPEC [--out FILE]`

The deployment script (§14), with the assumptions as a header comment and the originating rule
as a trailing comment on every statement.  Exit 1 on a check or compile error.

### `mls test SPEC [--dsn DSN | --versions V,V] [--dbname NAME] [--seed N] [--rows N] [--contexts N] [--ops N] [--explain] [--scale N]`

Differential test of the compiled script against the semantics.  Without `--dsn`, one Docker
container per version (default `15,16,17,18`).  With `--dsn`, a superuser connection; the
scratch database `--dbname` (default `mls_difftest`) is dropped and recreated.  `--rows`
(default 25) rows per table, `--contexts` (default 12) random users, `--ops` (default 20)
statements per user, `--seed` (default 0).  Prints `[label] differential: <n> statements,
outcomes <counts>, <n> rows visible, <n> mismatches` and the first ten mismatches.  `--explain`
additionally loads `--scale` rows (default 1,000,000) and checks, from `EXPLAIN (FORMAT
JSON)`, that every membership and comparison atom of every select policy is an index condition
and the table is not sequentially scanned: `[label] explain <table> as <role>: ok | FAIL ...`.
Exit 1 on any mismatch or failed plan check.

### `mls explain SPEC [--terse]`

Rules and properties in English, or one terse line each (§13).

### `mls lint SPEC [--dsn DSN] [--timeout SECONDS] [--json]`

Hygiene, never safety: nothing here enters a proof verdict, the eval grading or the exit code.
Two parts.  The leakproof lint: for each column of a protected table, the operators of
its type that are not leakproof and therefore cannot use an index before the row policy; with
`--dsn` the list comes from the server's catalog.  Printed as `warning: ...`.  The policy-set
lint (`src/mls/props/lint.py`), printed as `<kind>: ...`:

| Kind | Finding |
|---|---|
| `coverage` | a column rule whose role has no permissive policy for the access it limits (the grant is dead); a governed table on which no role has a permissive policy (nobody can do anything); an `observed` table a selector (§11) would match, or that has the column a `tenant_isolation` property names |
| `contradiction` | a policy whose `using` (or `check`) cannot be TRUE on any row that satisfies the table's constraints: it never admits a row |
| `subsumption` | two permissive policies on the same table and role, the commands of one within the other's, where every row the first admits the second admits too (`T(p2) → T(p1)` valid under the constraints, on the three-valued encoding); two policies that admit the same rows are reported once as a pair |
| `redundancy` | a policy whose removal leaves the effective predicate the same for every command and side it takes part in, on its table and on the tables of the relations it reads; a policy already reported as contradictory, subsumed or equivalent is not reported again |

A finding whose question relied on an opaque block ends with `(conditional on opaque block x)`.
The last line is `N warning(s), M finding(s)`.  `--json` prints `{"warnings": [{"table",
"column", "type", "operators", "message"}], "findings": [{"kind", "table", "message", "rules",
"conditional_on"}]}`.  Exit 0 whatever is found; 1 only if the spec does not check.  The LLM
loop quotes the findings as hints in its repair prompt (GUIDE §12).

### `mls diff OLD NEW [--format md|json] [--out FILE] [--timeout SECONDS]`

Semantic diff (SEMANTICS §7): per table, role and command, new grants and new denials with a
scenario each; per table, rows and changes newly rejected or newly accepted; privilege changes;
notes (new and removed tables and roles; a table observed in either version, which is not
compared: what roles can do on it is not modelled); undecided questions (solver timeout).  `before` and
`after` are the effective predicates in terse notation.  JSON keys: `notes`, `summary` (with
the count of undecided questions), `access`, `integrity`, `grants`.  Exit 2 if anything
changed, else 0.

### `mls migrate OLD NEW [--format sql|json] [--out FILE] [--allow-destructive]`

Phased migration script: 1 schema, 2 constraints as NOT VALID and
triggers, 3 indexes concurrently, 4 validate, 5 swap, 6 cleanup, 7 destructive (only with
`--allow-destructive`).  Every statement is annotated with its lock mode (`LOCKS` in `compile/migrate.py`, observed in
`pg_locks` on Postgres 15–18 by `tests/templates/test_locks.py`).  Refuses column type
changes, primary key changes and identity adapter changes, with the JSON path.  `--format
json` gives the phases as data.  Exit 1 if refused.

### `mls preflight NEW --dsn DSN [--old OLD] [--schema NAME] [--samples N] [--json]`

Counts existing rows that would violate the new (or, with `--old`, the new and changed)
constraints, with up to `--samples` (default 5) sample rows each, in a READ ONLY transaction on
`--schema` (default `public`).  Prints each violation as `<rule> on <table>: <n> row(s) <what>`
with samples, then `pre-flight: OK ...` or the count of violation groups.  JSON: `{"ok",
"notes", "violations": [{"rule", "table", "what", "count", "samples", "sql"}]}`.  Exit 1 if any.

### `mls drift SPEC --dsn DSN [--schema NAME] [--json]`

Shadow-deploys the spec in a transaction it rolls back, then compares catalogs.  Items are
`<kind>: <object> (expected ..., found ...)` with kinds `missing`, `unexpected`, `differs`,
`hardening` (drift; the bypass paths of §5 among them), `assumed` (hardening the spec's `assume` accounts for, §5) and `foreign`
(an object on an adopted table that the spec does not declare and that cannot grant access: a
constraint, trigger, index or column).  An imported CHECK written without `IS TRUE` is
`differs` until a deploy rewrites it to the check template's form (the notes of `mls import`
say so).  Prints `drift: none, the live schema matches the spec`
or the count of drift items, followed by the counts of assumed and foreign items in parentheses.
Exit 1 if any drift item; `assumed` and `foreign` items are the expected state of an adopted
table and do not fail.

### `mls status SPEC [--dsn DSN [--schema NAME]] [--timeout SECONDS] [--json]`

The governance status of every table, as data (`src/mls/status.py`): the red, yellow and
green of a schema overlay.  Runs the proofs (`mls prove`) and prints one line per table of the
spec, in declaration order, `<table>: <status> (<detail>)`:

| Status | Meaning | Detail |
|---|---|---|
| `ungoverned` | no row-level security from the spec: an `observed` table; with `--dsn` also a governed table that is missing or has RLS disabled, and a table of the schema the spec does not mention | the reason |
| `refuted` | a property that mentions the table is not proved | the property ids |
| `opaque` | every proof about the table holds, but one is conditional on an opaque block, or a rule on the table reads one | the block names |
| `unproved` | governed, and no property mentions it | |
| `proved` | every property that mentions the table is proved unconditionally | the property ids |

A property *mentions* the tables it names (`table`, an invariant's rules); `least_privilege`
mentions every governed table and `monotone_in` every table an obligation of it ranges over.
With `--dsn` a governed table's bypass paths (§5) follow its line: `; bypassed by <path>, ...`
for the ones the table does not assume, `; trusted path <path>, ...` for the assumed ones.
The last line is a count per status, then `; <n> bypassed (see mls drift)` when a table
has an unassumed path.  `--json` prints `{"ok", "tables": [{"table", "status", "detail",
"opaques", "properties", "bypass", "trusted"}]}`.  Exit 2 when a table is `refuted` or, with
`--dsn`, a governed table is missing, has RLS disabled or has an unassumed bypass path;
`opaque`, `unproved` and observed or unmentioned tables are information, exit 0.

### `mls import --dsn DSN [--schema NAME] [--role R]... [--policies keep|discard] [--observe T1,T2]... [--out FILE] [--json]`

Writes a spec from a live schema: a starting point the human or the loop edits; from then
on the spec is the source of truth and `mls drift` says whether the database still agrees.
Every table with row-level security enabled is `adopted` (columns of MLS types, columns of the
schema's enum types with the types' labels, primary key and unique constraints read from the
catalog; other columns are left out and noted), every other
table `observed`.  The hardening state is recorded as assumptions (an app-role owner, RLS not
forced, an app role with `BYPASSRLS`).  The app roles are the roles the policies and grants
name, minus owners, superusers and `mls_internal` (`--role` overrides).  The identity adapter
is guessed from the policy expressions (`auth.uid()`/`auth.jwt()` → `supabase`,
`current_setting('app.*')` → `session_setting`, `current_user` → `pg_roles`) with the context
values they read, and reported when ambiguous.  A relation is proposed for every `(key, value)`
pair an inclusion-shaped foreign key targets and for every two-column key a policy's SQL
reads.  Every policy becomes a rule, one per role it applies to, whose `using` and `check` the
lifter reads (below); what it cannot read is an opaque block with the policy's catalog
signature: the columns it depends on or names, the context values whose source it reads, the
relations on the tables it names.  A foreign key of the inclusion shape (two columns onto a key
pair, `MATCH SIMPLE`, `ON UPDATE/DELETE RESTRICT`) becomes an inclusion rule; column-level
grants become column rules; a CHECK the lifter reads becomes a check rule; a trigger of the
transition template MLS itself emits becomes a transition rule.  The compiler's own relation
policies (`mls_rel_<relation>_<role>_<n>`, which give the relation its name back) and relation
grants are recognized and not imported as rules.  Listed in the notes and not imported: other
triggers, other foreign keys, a CHECK the lifter cannot read or that is `NOT VALID`, a CHECK
over nullable columns written without `IS TRUE` (it passes on NULL where a check rule rejects),
grants with no permissive policy, policies for no app role, policies whose names are not
identifiers (the rule gets the identifier; a deploy renames the policy).  Notes prefixed
`lint:` name hand-written shapes a deploy improves on (a bare `auth.uid()` evaluated per row,
a correlated `EXISTS`).  The spec goes to stdout or `--out` (terse when the file name ends in
`.mls`, else JSON), the notes to stderr; `--json`
prints `{"ok", "spec", "notes"}`.  Exit 1 with the errors when no app role is found, the
schema has no tables, `--observe` names a table the schema does not have, or `--observe` is
given without `--policies discard`.

`--policies discard` writes a skeleton for a takeover (keep the tables, replace the
policies) instead: identity, roles, tables, relations, inclusion rules and readable CHECKs, and
no policy, column rule or opaque block.  Every table is `adopted`, with row-level security
enabled today or not (the deploy enables it; the notes name the tables where it is off today);
`--observe` leaves tables out.  Each discarded policy is a note with its text (`policy <name>
on <table> discarded (--policies discard; the deploy drops it): permissive for select to
authenticated using (...)`), and the grants are not imported (the deploy grants what the rules
need).  With no policy to read memberships from, relations are proposed from the schema: a
table whose two-column primary or unique key has a single-column foreign key on each column is
a membership table, keyed by the column that refers to the user table (`auth.users`, a table
whose primary key refers to `auth.users` such as `profiles`, or failing those the column of the
identity's `uid` type); when neither or both columns qualify, a note says so and no relation is
proposed.  The identity is still read from the discarded policies' text; without any, a
foreign key to `auth.users` means `supabase` with `uid` from `auth.uid`.  The platform's
trusted roles (`service_role`) are not taken as app roles.  `mls generate --skeleton`
takes the result as it is.

In either mode the bypass paths of the adopted tables (§5) are notes, `bypass path: <path>
reaches table <t> without its policies: ... (fix: ...)`.  The import never assumes one: drift
reports each as `hardening` until it is fixed or the spec assumes it.

**The lifter** (`pg/lift.py`) parses the deparsed text of an expression and reads:

| Shape in the catalog | Lifted to |
|---|---|
| `col op (select <ctx>)`, `col op const`, `(select <ctx>) op const` | `cmp` (the column on the left; `<` and `>` flip) |
| `col = any (array[c1, ...])`, `col in (c1, ...)`, `col <> all (array[...])` | `in` (negated: `not` of `in`) |
| `col = any (array(select value from rel where key = <ctx>))` | `member` |
| `col in (select value from rel where key = <ctx>)` | `member` (noted as lifted; the compiler writes the array shape) |
| `exists (select 1 from rel where rel.key = <ctx> and rel.value = t.col)` | `member` (noted as the slow per-row shape of FINDINGS) |
| `col = any ((select mls_reach_<h>(<ctx>))::t[])` | `in_group`, when the spec declares hierarchy `h` |
| `col = any (array(select v from T where k = any (array(select ... from rel where key = <ctx>))))`, or the same nesting of `in (select ...)`, up to three levels | `member` over a composed relation: an identical chain in the spec is reused, else one is proposed (named `<base>_<last table>`) with a note; when a hop's value is not a key or a hop column is nullable the policy stays opaque |
| `col is [not] null`, `and`, `or`, `not`, `true`, `false` | the same |
| `(f is null or f <= (select now())) and (u is null or (select now()) < u)` | `now_between` |
| a bare boolean column `b` | `b = true` |
| `(p) is true` at the top of a CHECK | `p` |
| the adapter's identity expression, with or without the `(select ...)` wrapper | `ctx` (without the wrapper a `lint:` note names the index-friendly form) |

A `(key, value)` membership proposes the relation when the pair is a primary key or unique
constraint of a governed table, else the lookup stays opaque with the reason.  The lifter
works at the boolean connectives: a conjunct, disjunct or negand it cannot read becomes an
opaque block of its own text (an expression it cannot parse at all is one block), so a lifted
spec always checks.  Never read: a call to any function that is not an identity source or a
compiler helper (a one-line SQL wrapper included: user functions are never inlined),
`like` (an opaque block over the column, which the leakproof lint reports), a negated
membership (an anti-join), a membership subquery that joins tables (nested subqueries are
composed relations), `IS TRUE` inside an expression (the fragment is three-valued), `CASE`,
arithmetic.  The notes say which part of which policy stayed opaque and why.

### `mls fmt SPEC [--to json|terse] [--out FILE]`

Converts a spec between the two syntaxes: to the other one by default, so `mls fmt
spec.mls.json` prints the terse form and `mls fmt spec.mls` the JSON; `--to terse` on a `.mls`
file is the formatter (the renderer: its output is idempotent).  The JSON written is the
model's canonical serialization (aliases, no nulls, the defaults spelled out).  Exit 1 with
the errors when the input does not parse or validate; the fragment is not checked (`mls
check` is).

### `mls schema`

The JSON Schema of the spec language (draft 2020-12, from the pydantic model).

### `mls generate REQUIREMENTS --skeleton SKELETON [--out FILE] [--syntax json|terse] [--model M] [--effort E] [--max-repairs N] [--transcript FILE] [--json]`

The LLM loop (GUIDE §12).  `REQUIREMENTS` is a text file or `-` for stdin; `SKELETON` is a
spec or a spec without rules and properties (either syntax, by extension: the `.mls` that
`mls import --policies discard --out skeleton.mls` writes is taken as it is).  Three passes: the
*relations* pass adds the relations the requirements need that the skeleton lacks (a
`relations { relation ... }` section of the spec's own `relation` lines, `relations { }` for none;
JSON `{"relations": {name: relation}}`); a relation that takes a skeleton relation's name with
another definition is an error at `$.relations.<name>` (a verbatim restatement is dropped), and the
skeleton with the new relations must check, its errors going back to the relations pass within the
repair budget (status `invalid` when they remain).  The *properties* and *rules* passes are then
shown the skeleton with those relations, and neither sees the other's answer.  The skeleton's own
rules (an import's check and inclusion rules) are *fixed*: the properties and rules passes are shown them,
with the first free id `R<n>`; the result is the model's rules followed by the fixed ones (a
verbatim restatement is not duplicated); a model rule that takes a fixed rule's id, or its name
on the same table, is an error at the answer's path (`rule id 'R1' is taken by a rule the spec
already has`, fix: number from `R<n>`), repaired like any other.  `--out` writes the spec as
written (an `in` list stays one), not as the checker expands it.  Defaults: model
`claude-opus-5-5`, effort `high`, 3 repairs, syntax `terse`.  With `terse`
the passes answer with `relations { ... }` / `properties { ... }` / `rules { ... }` sections: the prompt carries the
grammar and three examples instead of the JSON Schema, the skeleton is shown in terse, and
the checker's feedback names the line and column in the model's own text; `--syntax json`
gives the passes the JSON Schema and has them answer in JSON.  Prints the status,
the stages, the relations the model added (`# Relations added to the schema (for approval)`, one
sentence each), the properties with their verdicts for approval, any errors, any escalations, and
the model's notes.  `--out` writes the spec (terse when the file name ends in `.mls`, else
JSON), the added relations after the skeleton's.  Exit 0 when the status is `proved`, 1 otherwise.  Needs `ANTHROPIC_API_KEY` unless
`MLS_LLM_SCRIPT` is set.

### `mls eval CASES... [--llm [--repair] [--syntax json|terse]] [--mutate DIR] [--model M] [--effort E] [--max-repairs N] [--transcripts DIR] [--json]`

Over the eval corpus (`evals/cases`, case files or directories).  Without flags: proves every
case's reference spec and checks the expected verdicts (`ok: <id> — <n> properties as
expected`).  `--llm`: runs the loop on every generation case and grades the result against the
reference (metrics in `docs/design/LLM.md`; the summary also says the syntax and totals the API
calls and tokens); `--repair` starts from each synthetic case's mutated rules; `--syntax` as
for `generate`.  `--mutate DIR`: writes solver-graded mutants of the cases to `DIR` and prints
the mutation score.  Exit 1 if any case fails.

## 16. Environment variables

| Variable | Read by | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | `mls generate`, `mls eval --llm` | the API key; never commit it |
| `MLS_LLM_SCRIPT` | the same | path to a JSON list of answers that replaces the model (tests and offline runs; no network call) |
| `MLS_PG_VERSIONS` | `pytest -m pg` | comma-separated Postgres majors to test (default all four) |
| `MLS_PG_DSN` | `pytest -m pg` | an existing superuser server instead of Docker |
| `MLS_SKIP_PG` | `pytest` | skip every test that needs Postgres |

## 17. Errors

Checker and compiler errors are written for a program to act on (an LLM in the generation
loop): what is wrong, where, and the allowed alternative.

    error at $.rules[0].using.and[1].cmp[0]: unknown column 'nope' of table 'docs'
      fix: one of: id, tenant_id, owner_id, level, archived

The path is a JSONPath into the spec file.  When the input was terse (a `.mls` file, §13) the
same error also says where in the text, `file:line:col` at the node the path names (the atom
of a predicate, the rule, the column, ...) and the offending line; the path and the message
are the ones the JSON form of the spec gives:

    error at $.rules[0].using.and[1].cmp[0] (docs.mls:32:103): unknown column 'nope' of table 'docs'
        R1 policy member_read on docs: authenticated select permissive using member_of(@uid, tenant_id) and nope <= @clearance
      fix: one of: id, tenant_id, owner_id, level, archived

A syntax error (the text does not parse) names the position, what was expected and what was
found, with the path of the item being read; exit code 1, as for a shape error on JSON:

    syntax at $.rules[0] (docs.mls:4:52): expected a term (column, old.column, new.column, @context, 'text', a number, true, false), found '='
        R1 policy p on t: a select permissive using id = = 1
      fix: see the grammar (REFERENCE §13)

With `--json` every error carries `span` (`null` for JSON input):

    {"ok": false, "errors": [{"code": "error", "path": "$.rules[0].using.and[1].cmp[0]",
                              "message": "unknown column 'nope' of table 'docs'",
                              "fix": "one of: id, tenant_id, owner_id, level, archived",
                              "span": {"file": "docs.mls", "line": 32, "col": 103,
                                       "text": "  R1 policy member_read on docs: ..."}}]}

Classes of error, with the typical message:

- **shape**: unknown key, missing key, wrong JSON type (from the pydantic model, with the path);
- **names**: unknown table, column, role, relation, hierarchy, opaque block, rule or context
  value; invalid identifier; duplicate rule id, rule name per table or property id;
- **selectors**: `rule 'R1' applies to no table (has column tenant_id)`; `a rule needs
  "table" (one table) or "applies_to" (a selector)`; `"table" and "applies_to" are exclusive`;
  `a selector is exactly one of {has_column}, {named}, {tagged}, {all}, {and}, {or}`;
- **modes**: `table 't' is observed; a rule cannot name it`; `table 't' is owned: ...
  nothing is assumed`; `'r' is not an app role of this spec`; `'disabled' is not an assumption
  the proofs can state`; with `--dsn`: `adopted table 't' has no column 'c'`; `column 't.c' is
  bigint in the catalog, declared int`; `owned table 't' is owned by app role 'r'`;
- **types**: `type mismatch: int = text`; `operator < is not allowed on text values`;
  `constant 2147483648 is out of range for int (...)`; a uuid
  constant that is not a UUID; `timestamp constants are not supported in v1`; `'in' needs at
  least one constant`;
- **fragment**: `context values are not allowed in a check predicate`; `membership is not
  allowed in a check predicate`; `negated membership in a policy is an anti-join and cannot be
  ...`; `in_group is not allowed in a transition predicate`; `comparisons in policies must be
  column op (context | constant)`; `old/new references are only allowed in transition
  predicates and in the ...`;
- **structure**: `a select policy needs 'using'`; `an insert policy has no 'using' (no existing
  row)`; `relation column 'c' must be NOT NULL`; `(key, value) must be a primary key or unique
  ...`; `'parent' must be nullable (roots have no parent)`; `'parent' needs a single-column
  primary key that 'group' ...`; `a second read column rule for role 'r' on 't'`;
- **compiler** (soundness obligations): a restrictive policy on a relation table that hides
  rows a membership atom needs, with the policy's path and a fix; for a composed
  relation, the same on each hop table (`... can hide rows whose <key> the previous level
  reaches ...`).
- **recursion**: `a policy on 'T' cannot read 'R': relation 'R' is backed by table 'T', so its
  subquery would apply this table's policies again and Postgres refuses every query on the
  table ('infinite recursion detected in policy')` (or `reads table 'T' in one of its hops`;
  fix: the user's own rows, `key` compared with the context value, or a relation backed by
  another table);
- **composed relations**: `'R' is itself composed; compose from a base relation`; `N hops;
  a composed relation has at most 3`; `hop column 'T.c' must be NOT NULL`; `'T.v' is not the
  primary key or a unique column of 'T'` (fix: a membership table, or the denormalized column
  with an inclusion rule); `type of T.k (...) does not match the previous level's value`;
  `'R' is a composed relation; an invariant cannot read one ...` (likewise for an inclusion
  rule, a hierarchy's members, an opaque block's relations and `monotone_in`).

`mls migrate` refusals, `mls preflight` notes and `mls import` errors use the same path convention.
