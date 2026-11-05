# MLS user guide

This guide takes you from an empty directory to a deployed, proved, tested set of PostgreSQL
access-control and integrity rules, and then through changing them.  It assumes you know
PostgreSQL and have met row-level security (RLS) before.  `docs/REFERENCE.md` is the
construct-by-construct reference; this guide links to it rather than repeating it.

MLS is alpha software (version 0.1).  The spec language and the CLI may change before 1.0, but
the semantics in `docs/SEMANTICS.md` and the decisions in `docs/DECISIONS.md` are settled.

Contents

1. What MLS is and is not
2. Install
3. The shape of a spec
4. Walkthrough: tenant-scoped documents
5. Identity: who the current user is
6. Deploying the DDL
7. Changing a deployed spec: diff, migrate, preflight, drift
8. Writing specs with an LLM
9. Performance
10. Testing your own specs in CI
11. Limits and things MLS refuses to do
12. Where to read more

## 1. What MLS is and is not

An MLS spec is a JSON file that says, for a set of tables, who may see and change which rows and
which invariants the stored data must satisfy.  The `mls` tool does three things with it:

- **checks** it: JSON shape, column types, and that every predicate stays inside the fragment the
  prover and the compiler support;
- **proves** properties about it with an SMT solver (Z3, cross-checked with cvc5 in CI): tenant
  isolation, immutable columns, monotonicity in membership, write ⊆ read, least privilege,
  inductive invariants, custom upper bounds and `must_allow` lower bounds;
- **compiles** it to plain Postgres objects: RLS policies, `CHECK` constraints, composite foreign
  keys, trigger functions from fixed templates, column privileges, indexes, grants and hardening.

Every compiler template is differential-tested against Postgres 15, 16, 17 and 18, and every
template has a mutation test that shows the differential test would catch a wrong template.  The
proofs are about the model; the tests tie the model to Postgres.

What MLS is not:

- **Not a query language or an ORM.**  It never touches your application's SQL.  The compiled
  objects are ordinary Postgres objects that apply to every connection as the app role.
- **Not a wrapper around hand-written RLS.**  You never write a policy expression; you write
  predicates in a small language with SQL's three-valued meaning, and the compiler emits the one
  policy shape it can prove and index.  Raw SQL is allowed only in an explicit `opaque` block,
  and every proof that depends on one says so.
- **Not an importer.**  MLS owns the tables it creates (greenfield, D8).  You can deploy it next
  to other schemas, but it does not read existing policies.

The intended workflow (D10) is that an LLM writes the spec from business requirements and a
human approves the *properties* and the *scenarios* in plain English.  Nobody has to read MLS
JSON or DDL to approve a change, though both are there to be read.

## 2. Install

MLS is a Python 3.12+ package managed with `uv`.

    git clone <this repository>
    cd more_ls
    uv sync --all-extras        # z3, psycopg, pytest, hypothesis, testcontainers, cvc5, anthropic

Check that it works without Postgres:

    uv run mls check examples/tenant_docs.mls.json      # examples/tenant_docs.mls.json: OK
    uv run mls prove examples/tenant_docs.mls.json      # a Markdown proof report
    uv run pytest -m "not pg"                           # the fast suite, seconds

Anything that talks to Postgres (`mls test`, `mls preflight`, `mls drift`, `mls lint --dsn`,
`pytest -m pg`) needs either Docker, which is used to start `postgres:15` through `postgres:18`
containers, or a DSN of a server you provide.  The differential test needs a superuser on that
server because it creates roles and installs the compiled objects in a scratch database.

The LLM commands (`mls generate`, `mls eval --llm`) need `ANTHROPIC_API_KEY` in the environment.
Nothing else in the toolchain makes a network call.

## 3. The shape of a spec

A spec has a fixed skeleton and two lists:

    {
      "mls_version": "0.1",
      "identity":   { "adapter": "supabase", "context": { ... } },
      "roles":      ["authenticated"],
      "tables":     { ... },
      "relations":  { ... },       // membership relations: (key, value) projections of tables
      "hierarchies": { ... },      // optional: trees of groups
      "opaques":    { ... },       // optional: named raw-SQL predicates
      "rules":      [ ... ],       // what is enforced
      "properties": [ ... ]        // what is proved about the rules
    }

The skeleton (identity, roles, tables, relations, hierarchies, opaques) is the part a developer
writes, because it mirrors the schema.  The rules and properties are the part an LLM can write
from requirements (section 8).

**Rules** are of six kinds.  Each one compiles to exactly one Postgres mechanism:

| Kind | Says | Becomes |
|---|---|---|
| `policy` | which rows a role may select / insert / update / delete | `CREATE POLICY` |
| `check` | every stored row satisfies a predicate | `CHECK ((p) IS TRUE)` |
| `inclusion` | a pair of columns is a pair of a relation | composite foreign key, `ON DELETE RESTRICT` |
| `transition` | every update satisfies a predicate over old and new | `BEFORE UPDATE` trigger |
| `cardinality` | at most N rows per group value | trigger that locks the parent row and counts |
| `column_rule` | a role may read or write only these columns | column-level `GRANT` |

**Properties** are statements the solver proves about the rules.  They come in templates (tenant
isolation, column immutable, monotone in a relation, write ⊆ read, least privilege, invariant
preserved) plus two general ones: `custom` (an upper bound: every row a role can touch satisfies
a goal) and `must_allow` (a lower bound: every valid row meeting a condition is admitted).  A
good property set has both bounds: the upper bounds catch rules that grant too much, the
`must_allow`s catch rules that deny too much.

**Predicates** are small JSON trees over terms.  A term is a column (`{"col": "level"}`), a
context value of the current user (`{"ctx": "uid"}`), a constant (`{"const": 3}`) or, in
transitions and update conditions, an image column (`{"old": "tenant_id"}`, `{"new": "tenant_id"}`).
The predicate forms are `cmp`, `in`, `member`, `in_group`, `now_between`, `is_null`, `and`,
`or`, `not`, `lit` and `opaque`.  Their exact grammar and where each is allowed is in the
reference.

Two things about predicates matter from the start:

- **Three-valued logic.**  Every predicate means what the same expression means in SQL: a
  comparison with NULL is UNKNOWN, `and` and `or` propagate it the SQL way.  A policy *denies*
  on UNKNOWN; a check *passes* on UNKNOWN.  MLS does not paper over this; it normalizes the
  compiled objects so that every target admits a row exactly when the predicate is TRUE, and it
  makes you say what you mean about NULLs.  A nullable `owner_id` that must be a member is
  written `owner_id is null or member_of(owner_id, tenant_id)`, not the bare membership.
- **The fragment is deliberately small.**  Comparisons are `column op (context | constant)`,
  and `{"in": [column, [constants]]}` is shorthand for a disjunction of `=` (the checker expands
  it; the compiler emits one IN list).  Membership is `(context, column) ∈ relation`.  There are no joins, no subqueries, no
  arithmetic, no functions.  This is what makes every property decidable and every policy
  index-driven.  When you need more, you declare an `opaque` block and accept that proofs
  depending on it are conditional.

The JSON syntax is verbose by design (it is written by programs).  `mls explain --terse` prints
the same rules one per line in a readable notation, and the diff and proof reports use the same
notation; this guide uses it when quoting rules.

## 4. Walkthrough: tenant-scoped documents

`examples/tenant_docs.mls.json` is a small multi-tenant document store on Supabase.  Three tables:

- `memberships (user_id uuid, tenant_id int)`, primary key on both: who belongs to which tenant;
- `admins (user_id, tenant_id)`: who administers which tenant;
- `docs (id, tenant_id, owner_id uuid null, level int, archived int null)`.

Two relations name the membership tables as `(key, value)` pairs:

    "relations": {
      "member_of": {"table": "memberships", "key": "user_id", "value": "tenant_id"},
      "admin_of":  {"table": "admins",      "key": "user_id", "value": "tenant_id"}
    }

Identity is Supabase: the current user is `auth.uid()` and a `clearance` claim from the JWT.

    "identity": {
      "adapter": "supabase",
      "context": {
        "uid":       {"source": "auth.uid",             "type": "uuid"},
        "clearance": {"source": "jwt.claims.clearance", "type": "int"}
      }
    }

### 4.1 The rules

Here is the whole rule set as `mls explain --terse` prints it:

    R1 policy docs: authenticated select permissive using member_of(@uid, tenant_id) and level <= @clearance
    R2 policy docs: authenticated select permissive using admin_of(@uid, tenant_id)
    R3 policy docs: authenticated insert permissive check member_of(@uid, tenant_id) and level <= @clearance
    R4 policy docs: authenticated update permissive using member_of(@uid, tenant_id) check member_of(@uid, tenant_id) and level <= @clearance
    R5 policy docs: authenticated all restrictive using not archived = 1
    R6 check docs: level >= 0 and level <= 3
    R7 inclusion docs: (owner_id, tenant_id) in member_of on_delete restrict
    R8 inclusion admins: (user_id, tenant_id) in member_of on_delete restrict
    R9 transition docs: new.tenant_id = old.tenant_id

Read them as Postgres does: the permissive policies of a command are OR-ed, and the restrictive
ones are AND-ed on top.  So a member reads a document if its tenant is one of theirs and its
level is within their clearance, *or* if they administer its tenant; and in either case only if it
is not archived.  Nobody can delete: there is no delete policy, and a command with no permissive
policy is denied.

The JSON of R1 is what the terse line abbreviates:

    {
      "id": "R1", "requirement": "REQ-1",
      "kind": "policy", "name": "member_read", "table": "docs",
      "role": "authenticated", "command": "select", "mode": "permissive",
      "using": {"and": [
        {"member": {"relation": "member_of", "key": {"ctx": "uid"}, "value": {"col": "tenant_id"}}},
        {"cmp": [{"col": "level"}, "<=", {"ctx": "clearance"}]}
      ]}
    }

The `requirement` field is free text: a pointer back to the business requirement the rule
implements.  The LLM loop fills it in; nothing else reads it.

Note R5.  `archived` is nullable.  `not archived = 1` is UNKNOWN when `archived` is NULL, and a
policy denies on UNKNOWN, so a document with `archived` NULL is invisible to everyone.  Whether
that is what you want is a business question; MLS makes it a visible one (the `must_allow`
properties below answer it).

### 4.2 The properties

    P1 tenant_isolation docs: tenant_id in member_of(@uid) for select, insert, update
    P2 column_immutable docs: tenant_id
    P3 monotone_in: member_of
    P4 invariant_preserved: R6; R7; R8
    P5 write_subset_read docs
    P6 least_privilege: authenticated docs[select, insert, update]; authenticated memberships[select]; authenticated admins[select]
    P7 must_allow docs authenticated select: member_of(@uid, tenant_id) and level <= @clearance and not archived is null and archived <> 1
    P8 must_allow docs authenticated select: admin_of(@uid, tenant_id) and not archived is null and archived <> 1
    P9 must_allow docs authenticated insert: member_of(@uid, tenant_id) and level >= 0 and level <= 3 and level <= @clearance and owner_id is null and archived = 0
    P10 must_allow docs authenticated update: member_of(@uid, old.tenant_id) and old.archived = 0 and new.archived = 0 and new.tenant_id = old.tenant_id and new.id = old.id and new.owner_id is null and new.level >= 0 and new.level <= 3 and new.level <= @clearance

Every property carries a `description` in plain English; that sentence is the approval artifact.
`mls explain` (without `--terse`) renders each rule and property as a sentence too:

    - **P1**: Any row of docs a user can read, insert, update has a tenant_id among the current user's member_of.
    - **P5**: Whatever a user can insert into or update in docs, that user can also read.
    - **P7**: authenticated can read every row of docs where the row's tenant_id is among the
      current user's member_of and the row's level is at most the user's clearance and (not the
      row's archived is empty and the row's archived is not 1).

P1 is interesting because it is *not* obviously true from R1 alone: R2 lets an admin read a
document of a tenant they administer.  P1 holds because R8 says every admin pair is a member
pair, and the solver uses that foreign key as an assumption (stored rows satisfy every CHECK and
FK).  Delete the inclusion rule R8 and P1 is refuted with a counterexample.

P6 lists `select` on `memberships` and `admins` even though no policy mentions those tables.  A
policy that uses a relation reads the relation's table as the app role, so the compiler emits an
implicit policy and column grant on it (D21), and least privilege must account for it.

P7 through P10 are the lower bounds.  P7 says: a member with enough clearance *can* read an
unarchived document of their tenant.  Note that it has to say `not archived is null`: with
`archived` NULL the row is invisible (R5 above), so a `must_allow` that did not exclude that case
would be refuted, and the report would show exactly the NULL row as the counterexample.  This is
how the NULL question in 4.1 gets answered and recorded.

### 4.3 Check

    $ uv run mls check examples/tenant_docs.mls.json
    examples/tenant_docs.mls.json: OK

`check` validates the JSON against the pydantic model, type-checks every predicate, and enforces
the fragment (which predicate forms are allowed where).  It also runs the compiler's soundness
obligations, so a spec that passes `check` compiles.  Errors name the JSON path and, where
there is one, the fix:

    $ uv run mls check broken.json
    error at $.rules[0].using.and[1].cmp[0]: unknown column 'nope' of table 'docs'
      fix: one of: id, tenant_id, owner_id, level, archived

`--json` gives the same as a list of `{code, path, message, fix}` objects.  The messages are
written so that a program (an LLM in the generation loop) can act on them.

### 4.4 Prove

    $ uv run mls prove examples/tenant_docs.mls.json
    # MLS proof report

    **10 proved, 0 refuted, 0 unknown**

    ## Assumptions

    - identity adapter 'supabase': Clients reach the database only through Supabase's API layer, which verifies the JWT before setting request.jwt.claims; direct database connections by untrusted clients are out of scope.
    - trusted roles: the owner of the MLS schema and superusers bypass policies; the compiler forces RLS for owners, so only superusers (and roles granted BYPASSRLS outside MLS) remain trusted
    - isolation level: no assumption (every construct is enforced by the engine on single rows)
    - opaque blocks relied on: none
    - Postgres versions validated by the differential tests: PostgreSQL 15, 16, 17, 18
    - stored rows satisfy every CHECK and foreign key (constraints are created validated)
    - invariant preservation uses only CHECKs, FKs and triggers as guards (policies are not trusted: trusted roles bypass them)

    ## Properties

    ### P1 (tenant_isolation): PROVED — a user only touches docs of tenants they belong to

    - PROVED: select as authenticated: r.tenant_id ∈ member_of[key]
    - PROVED: insert as authenticated: new.tenant_id ∈ member_of[key]
    - PROVED: update as authenticated: new.tenant_id ∈ member_of[key]
    ...

Read the assumptions first; they are what the proofs are *about*.  Every report states the
identity adapter's trust assumption, which roles bypass the policies, the isolation level any
construct relies on, the opaque blocks relied on, and the Postgres versions the templates were
validated on.

Each property expands into obligations, one per command and role (and for invariant preservation
one per table and statement kind, including deletes on referenced tables).  A `must_allow` has two
obligations: a witness (`some row meets the condition`, with an example row so you can see the
condition is satisfiable) and the bound itself (`every row meeting the condition is admitted`).

A refuted obligation comes with a counterexample rendered as a scenario: the context values, the
row (or old and new rows), and the membership facts.  Scenarios are concrete and small, and are
meant to be read by the person who approves the properties.  `--format json` gives the same
report as data; `--timeout` bounds the solver per obligation (default 20 s), and an obligation
that times out is reported UNKNOWN, never PROVED.

### 4.5 Compile

    $ uv run mls compile examples/tenant_docs.mls.json

The output is one SQL script in fixed sections: preamble, roles, tables, checks, fks,
transitions, rls, policies, indexes, grants (hierarchies and cardinality limits add helpers and
their internal role).  Some excerpts, each with the rule it comes from in a trailing comment:

    -- ==== checks ====
    alter table "docs" add constraint "level_range" check ((("level" >= 0) and ("level" <= 3)) is true);  -- R6

    -- ==== transitions ====
    create function "mls_tr_docs_tenant_immutable"() returns trigger language plpgsql as $mls$
    begin
      if not ((new."tenant_id" = old."tenant_id") is true) then
        raise exception 'MLS transition tenant_immutable on docs violated' using errcode = 'check_violation';
      end if;
      return new;
    end $mls$;  -- R9

    -- ==== policies ====
    create policy "member_read" on "docs" as permissive for select to "authenticated"
      using ((("tenant_id" = any (array(select "tenant_id" from "memberships" where "user_id" = (select auth.uid())))) and ("level" <= (select (auth.jwt() ->> 'clearance')::integer))));  -- R1
    create policy "mls_rel_member_of_authenticated_1" on "memberships" as permissive for select to "authenticated"
      using (("user_id" = (select auth.uid())));  -- implicit:member_of:authenticated:1

    -- ==== indexes ====
    create index "mls_ix_docs_tenant_id_level" on "docs" ("tenant_id", "level");

    -- ==== grants ====
    grant select ("tenant_id", "user_id") on "memberships" to "authenticated";
    grant select on "docs" to "authenticated";

Things to notice:

- `CHECK ((p) IS TRUE)` and `IF NOT ((p) IS TRUE)`: the NULL normalization.  A CHECK that
  passed on UNKNOWN would let a row through that the policy would hide.
- Membership compiles to `col = any(array(select ...))` with the context value wrapped in
  `(select auth.uid())`.  That exact shape is the one Postgres turns into an InitPlan and an
  index condition; the alternatives are ten to a thousand times slower (`docs/FINDINGS.md`).
- The implicit policy `mls_rel_member_of_authenticated_1` on `memberships` is what lets the
  app role see its own membership rows, and only those, while RLS is forced on every table.
- The preamble refuses to run as an app role; the roles section strips `BYPASSRLS` and the
  ability to set `session_replication_role`; every table gets `FORCE ROW LEVEL SECURITY` so
  even the owner is subject to the policies.  `mls drift` verifies all of this on a live
  database.
- `TRUNCATE` is never granted and `DELETE` is granted only to roles with a delete policy.

`--out FILE` writes the script instead of printing it.

### 4.6 Test

    $ uv run mls test examples/tenant_docs.mls.json --versions 16
    [pg16] differential: <n> statements, outcomes {...}, <n> rows visible, 0 mismatches

The differential test deploys the compiled script into a scratch database, generates random
rows, membership facts and contexts, runs random statements as the app role, and compares every
outcome and every visible row with a concrete evaluator of the spec's semantics written in
Python.  A mismatch means the compiler and the semantics disagree, and the first ten are printed
with the statement, the context and both outcomes.

Without `--dsn` it starts a Docker container per Postgres major version (all four by default;
`--versions 16,17` to limit).  `--seed`, `--rows`, `--contexts` and `--ops` size the run.  Add
`--explain` to also load the tables at `--scale` rows (default one million) and assert from
`EXPLAIN (FORMAT JSON)` that every membership and comparison atom of every select policy is an
index condition and the table is not sequentially scanned.

You do not need to run `mls test` on every spec you write: the templates are what is tested, and
the repository's own `pytest -m pg` runs them on every version.  Run it when you want the
evidence for a particular spec, or when you add an `opaque` block (whose SQL the test executes
but cannot otherwise check).

### 4.7 Lint

    $ uv run mls lint examples/tenant_docs.mls.json
    0 warning(s)

The lint warns, per column type, about operators that are not leakproof (`LIKE`, pattern
matching, most text operators): Postgres will not evaluate such a qual before the row policy, so
a query that filters on them cannot use its index and falls back to filtering every visible row.
Equality is leakproof.  With `--dsn` it reads the catalog of the real server rather than the
static list.

## 5. Identity: who the current user is

A policy's context values (`@uid`, `@clearance`) come from an identity adapter.  The adapter
decides how a name becomes SQL and what has to be true outside the database for the policy to
mean anything.  Three adapters ship:

**`supabase`.**  `auth.uid` is the user id (`(select auth.uid())`, type `uuid`) and
`jwt.claims.<name>` is a JWT claim (`(select (auth.jwt() ->> 'name')::type)`).  The trust
assumption, stated in every proof report, is that clients reach the database only through
Supabase's API layer, which verifies the JWT.  A direct connection as `authenticated` with a
forged claim is outside the model.

**`pg_roles`.**  `current_user` is the identity (type `text`).  No external assumption: Postgres
authenticated the session.  Use this when every user is a database role, or when a connection
pooler sets the role per request with `SET ROLE`.

**`session_setting`.**  `app.<name>` is a session setting read with
`current_setting('app.<name>', true)` and cast to the declared type (D47).  A setting that is
absent or empty is NULL, so every comparison with it is UNKNOWN and the row is denied: no
context set, denied.  MLS rejected this as an identity source at first (D1), because any client
with a direct connection can `SET` it; that is still true, so the adapter states its trust
assumption in every proof report, `explain` and diff: clients cannot execute `SET` on `app.*`;
the setting is established by a trusted path (a pooler, a `SET LOCAL` inside a `SECURITY
DEFINER` entry point, or the application's connection layer) that no untrusted client reaches.
`mls drift` checks the two ways that path leaks into the database: an app role that may execute
a function whose body sets `app.*`, and an `ALTER ROLE ... SET app.*` default on an app role.
`examples/session_docs.mls.json` is the document store on this adapter.

`now` is a built-in context value in every adapter (the transaction start time), used by
`now_between`.

Choose the adapter once.  `mls migrate` refuses to change it, because every policy would change
meaning at the same time.

## 6. Deploying the DDL

The compiled script is idempotent for roles (`create role` if missing) but not for tables and
policies: it is the *initial* deployment.  Run it as the role that will own the objects, in a
transaction:

    uv run mls compile spec.mls.json --out deploy.sql
    psql "$DSN" --single-transaction -v ON_ERROR_STOP=1 -f deploy.sql

The owner must not be an app role (the preamble checks).  On Supabase run it as `postgres`
through the SQL editor or a migration; the roles `authenticated` and `anon` already exist and
the script only hardens them.

After deploying, confirm the live database matches the spec:

    uv run mls drift spec.mls.json --dsn "$DSN"
    drift: none, the live schema matches the spec

`drift` deploys the spec into a shadow schema in a transaction it rolls back, snapshots both
catalogs and compares them.  It reports missing objects, unexpected objects on MLS tables (a
hand-added policy, a trigger), definitions that differ (an edited expression, a constraint left
NOT VALID, a changed grant) and hardening violations (an app role with `BYPASSRLS`, RLS not
forced, a login-capable `mls_internal`).  On an adopted table (section 6.1) it also lists, without
failing, the objects MLS did not create and the hardening the spec assumes.  Run it from CI
against production; it needs only read access to the catalog plus the ability to create and roll
back the shadow schema.

### 6.1 Adopting an existing schema

MLS does not have to create the tables it governs.  A table declares its `mode`:

- `owned` (the default): MLS creates it, hardens it, and everything on it that the spec did not
  produce is drift.
- `adopted`: the table exists.  Its columns and keys are described in the spec as they are in
  the catalog, and MLS adds rules to it: policies, constraints, triggers, indexes, grants, and
  `ENABLE ROW LEVEL SECURITY`.  What MLS would do to an owned table but cannot do to one it did
  not create (force row-level security, change the owner) is either already in place, and drift
  verifies it, or stated as an assumption the proofs repeat.
- `observed`: in the schema, mentioned, not governed.  No rule may name it.

To adopt a live schema, let MLS write the first version of the spec:

    uv run mls import --dsn "$DSN" --out spec.mls.json

Every table with row-level security becomes `adopted` (a table without it is `observed`: a
policy on it would mean nothing), the identity adapter is guessed from what the policies read,
relations are proposed from the foreign keys and the keys the policies look up, and every
existing policy becomes a rule whose predicate is an `opaque` block carrying the columns,
context values and relations the SQL reads.  The notes printed on stderr list what was left
out: a column whose type MLS does not model, a CHECK or trigger (listed, not imported), a grant
with no policy behind it, a policy whose name is not an identifier.  The hardening state is
recorded as assumptions:

    "tasks": {
      "columns": { ... },
      "mode": "adopted",
      "assume": [{"rls": "not_forced"}]
    }

which the proof report states as *assumed: row level security is not forced on adopted table
`tasks`; its owner bypasses every policy and is trusted*, and which `mls drift` reports as
`assumed` rather than as drift.  Force row-level security on the table, remove the assumption,
and drift is clean; leave the assumption in and drift tells you it is no longer needed.  An
`adopted` table owned by an app role works the same way with `{"owner": "<role>"}`; the same
table declared `owned` is refused by `mls check --dsn`, because the compiler never emits a
hardening it then has to assume.

The imported spec is a starting point.  It checks, compiles and proves as it is, but every
proof over an imported policy is conditional on its opaque block, which says little a reviewer
can act on.  The lifter (`docs/ROADMAP.md`, Milestone 5) turns the policies it can read into the
fragment; until then, rewrite the ones that matter by hand in the fragment and let `mls diff`
confirm the rewrite changes nothing.  From the import on, the spec is the source of truth:
`mls check --dsn` verifies that the adopted tables still have the columns the spec declares,
and `mls drift` says whether the database and the spec still agree.

Keep the spec in version control next to the application.  The spec, not the SQL, is the source
of truth; the SQL is a build artifact.

## 7. Changing a deployed spec: diff, migrate, preflight, drift

Specs change.  `examples/tenant_docs_v2.mls.json` is the next version of the walkthrough spec:
owners may delete their documents, documents get a `title` that must not be blank, the level
range widens to 4, a `tenant_settings` table appears, and an owner can no longer be changed once
set.  The release sequence is:

### 7.1 See what changed in effect

    $ uv run mls diff examples/tenant_docs.mls.json examples/tenant_docs_v2.mls.json
    # MLS access diff

    **2 access change(s), 3 integrity change(s), 2 privilege change(s), 0 undecided**

    ## Access changes

    - authenticated can now delete rows of docs that were not allowed before
      - before: `false`
      - after: `owner_id = @uid and not archived = 1`
      - for example: the current user is uuid#2; the existing row of docs with tenant_id = 3, owner_id = uuid#2, level = 0, archived = 2, title = empty; member_of has (uuid#2, 0), (uuid#2, 2), (uuid#2, 3).

    ## Integrity changes

    - some rows of docs that were valid before are now rejected
      - before: `level >= 0 and level <= 3 and (owner_id is null or member_of(owner_id, tenant_id))`
      - after: `level >= 0 and level <= 4 and (title is null or title <> '') and (owner_id is null or member_of(owner_id, tenant_id))`
      - for example: a row of docs with tenant_id = 0, owner_id = empty, level = 0, title = ''; member_of has no entries.
    ...

The diff is semantic, not textual.  For every table, role and command it asks the solver whether
some row is admitted by the new spec and not the old (a new grant) or by the old and not the
new (a new denial), and shows a concrete scenario for each.  Integrity changes are the same
question about the stored-row constraints and the transitions.  A rename of a rule that keeps
its meaning produces nothing.  The `before` and `after` lines are the effective predicates in
the terse notation, so a reviewer sees what the net effect was and now is rather than which
rule changed.  Exit code 2 means something changed, which makes the diff a usable CI gate:
require a human sign-off on the scenarios when it is non-zero.

Properties also travel: `mls prove` on the new spec tells you which of the old guarantees still
hold.  The usual practice is to keep every property and add new ones; a property you have to
delete is a decision to record.

### 7.2 Check the existing data

    uv run mls preflight new.mls.json --old old.mls.json --dsn "$DSN"

For every new or tightened CHECK, foreign key, NOT NULL, UNIQUE, cardinality limit and hierarchy
bound, pre-flight counts the existing rows that would violate it, with samples, in a read-only
transaction.  With `--old` only the changed constraints are checked.  Fix or migrate the data
before the schema; the migration script aborts on the first violating row.

### 7.3 Generate the migration

    uv run mls migrate old.mls.json new.mls.json --out migrate.sql

The migration is phased (D28): additive schema changes; new constraints as `NOT VALID` and new
triggers; `CREATE INDEX CONCURRENTLY`; `VALIDATE CONSTRAINT`; then the swap of policies, grants
and constraint names in one transaction; then `DROP INDEX CONCURRENTLY`.  Every statement is
annotated with the lock it takes, from a table that the test suite observes on every Postgres
version:

    alter table "docs" add column "title" text;  -- lock: AccessExclusiveLock on docs
    create index concurrently "mls_ix_docs_owner_id" on "docs" ("owner_id");  -- lock: ShareUpdateExclusiveLock on docs
    alter table "docs" validate constraint "title_not_blank";  -- lock: ShareUpdateExclusiveLock on docs  -- R11

Policies go live only after validation, because the proofs assume stored rows satisfy the
constraints.  The phases marked "autocommit" must run outside a transaction (`psql -f` does
this; a migration tool that wraps the file in one transaction will fail on `CONCURRENTLY`).
`DROP COLUMN` and `DROP TABLE` are emitted only with `--allow-destructive`.  Column type
changes, primary key changes and identity adapter changes are refused with the JSON path and
the manual procedure.  `docs/MIGRATION.md` has the full phase and lock tables.

### 7.4 Confirm

    uv run mls drift new.mls.json --dsn "$DSN"

must report nothing.  This whole sequence (deploy v1 with data, migrate, drift clean,
differential test of v2 on the migrated database) is the repository's own acceptance test for
migrations, on every Postgres version.

## 8. Writing specs with an LLM

`mls generate` turns business requirements and a skeleton into a checked and proved spec:

    export ANTHROPIC_API_KEY=...
    uv run mls generate requirements.txt --skeleton skeleton.json --out spec.mls.json --transcript t.json

The skeleton is a spec without rules and properties (identity, roles, tables, relations,
hierarchies, opaques).  The requirements are plain prose.  The loop:

1. asks the model for the *properties* and their plain-English descriptions (the approval
   artifact), from the requirements;
2. separately asks it for the *rules*, from the same requirements, without showing it the
   properties;
3. checks the spec; every error goes back to the pass that owns that JSON path;
4. proves the properties; every refuted one goes back to the rules pass as its English
   statement, the failing obligation and the counterexample scenario in English;
5. stops after `--max-repairs` rounds (default 3) with status `proved`, `invalid` (still does not
   check) or `escalated` (a property is still refuted and the model stands by its rules: a human
   decides whether the rule or the property is wrong).

The output ends with the properties for approval, each with its verdict:

    status: proved after 0 repair round(s)

    # Properties (for approval)
    - P1 (PROVED): <the property's description, as the model wrote it>
    - P2 (PROVED): ...

Approve the sentences.  If one is wrong, the requirements were ambiguous or wrong; fix the text
and rerun rather than editing the JSON.  The transcript (`--transcript`) records every prompt
and answer, the generated spec in terse form, and the model's notes, which usually name the
ambiguity.

The eval corpus (`evals/cases`) and `mls eval` measure the loop; `docs/LLM.md` explains the
case format, the metrics and the measured numbers.  Tests never call the API: setting
`MLS_LLM_SCRIPT` to a JSON file of answers replays them.

## 9. Performance

The compiler emits the one policy shape that stays index-driven at scale (`col = any(array(select
...))` with context in an InitPlan) and a composite index for every conjunctive group of a
permissive policy, membership columns first.  Measured on a one-million-row table with a
thousand tenants, a count as the app role takes a fraction of a millisecond with this shape and
tens to thousands of milliseconds with the shapes hand-written policies usually use
(`docs/FINDINGS.md`).  `mls test --explain` checks the plan on your spec; `mls lint` warns
about non-leakproof operators that defeat the index.

Hierarchies (`in_group`) compile to a `SECURITY DEFINER` function that returns the array of
groups reachable from the user's groups, called once per statement as an InitPlan, so the policy
atom is again `col = any(...)`.  The function and the cardinality trigger are owned by a
dedicated role `mls_internal` that cannot log in and has no other privileges; drift checks that.

Opaque blocks are excluded from every performance guarantee.

## 10. Testing your own specs in CI

A minimal pipeline for a repository that holds `spec.mls.json`:

    uv run mls check spec.mls.json                    # exit 1 on any error
    uv run mls prove spec.mls.json --format json --out proof.json   # exit 2 if a property is refuted
    git show main:spec.mls.json > /tmp/spec.main.json
    uv run mls diff /tmp/spec.main.json spec.mls.json             # exit 2 if the effect changed
    uv run mls drift spec.mls.json --dsn "$PROD_RO_DSN"             # exit 1 on drift (scheduled)

Keep the proof report as a build artifact: it is the record of what was guaranteed, under what
assumptions, at each release.  Gate merges on a clean `check`, a `prove` with no refuted
property and, when `diff` exits 2, an explicit approval of the scenarios it printed.

For the toolchain itself, `uv run pytest -m "not pg"` is the fast suite, `-m pg` runs the
differential, mutation, migration, lock, preflight and drift tests on Postgres 15–18 in Docker
(`MLS_PG_VERSIONS=16` for one, `MLS_PG_DSN=...` for an existing superuser server), `-m slow`
runs the EXPLAIN checks at a million rows, `-m cvc5` cross-checks the proofs with a second
solver, and `-m lean` builds the Lean formalization when `lake` is on the path.

## 11. Limits and things MLS refuses to do

These are decisions, not gaps; `docs/DECISIONS.md` has the reasoning.

- **Ownership is per table.**  MLS creates the tables it owns and adopts the ones it did not
  create (section 6.1), but it never silently applies to an adopted table a hardening it would
  have to assume: an app-role owner or unforced row-level security is stated in the spec and
  repeated in every report, or it is drift.  A hand-written policy is adopted as an opaque block
  (D45); turning it into the verified fragment is the lifter's job (Milestone 5).
- **The fragment.**  No joins, subqueries, arithmetic or functions in predicates.  Comparison
  right-hand sides are context or constants; membership keys are context, not columns; negated
  membership is rejected in policies (it is an anti-join the planner cannot drive from an
  index); `in_group` is not allowed in checks and transitions (it reads other tables).  Anything
  else is an `opaque` block, and its proofs are conditional.
- **Timestamp constants** are not supported in v1; compare timestamp columns with `now_between`
  or with a context value.
- **Column rules are per role, not per row.**  A column some users may see only on some rows
  (your own salary, others' titles) is its own table with its own row policies (D42).  There is
  no masking construct.
- **Hierarchies** have a declared maximum depth (1–16) and an immutable parent: a group cannot
  be reparented in v1.  Both are what make the proofs exact.
- **Cardinality limits** rely on a parent-row lock and are correct under READ COMMITTED.  The
  proof report says so whenever a spec has one.
- **Trusted roles.**  Superusers and roles granted `BYPASSRLS` outside MLS bypass everything.
  The owner does not (RLS is forced), but it can of course alter the objects; drift will tell
  you.
- **Postgres only**, versions 15 through 18.  The templates are validated on those four;
  another database would need its own normalization (Snowflake, for one, defaults foreign keys
  to MATCH FULL).
- **Migrations** refuse column type changes, primary key changes and identity adapter changes.

## 12. Where to read more

- `docs/REFERENCE.md`: every construct, field, predicate form, property template, CLI flag,
  exit code and error format.
- `docs/SEMANTICS.md`: the meaning of every construct, the source of truth for the prover, the
  evaluator and the compiler.
- `docs/DECISIONS.md`: the settled design decisions, numbered, with what was rejected and why.
- `docs/MIGRATION.md`: migration phases, lock modes, what pre-flight and drift check.
- `docs/LLM.md`: the generation loop, the eval corpus and the measured results.
- `docs/FINDINGS.md`: what the experiments on real Postgres established (performance tiers,
  leakproofness, NULL and UPDATE semantics, what the live LLM runs found).
- `lean/`: the Lean 4 formalization of the logic, the policy combination, the normalization and
  the templates, checked against the Python by generated test vectors.
