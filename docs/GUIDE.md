# MLS user guide

Row-level security is one of PostgreSQL's best features, and one of the easiest to get
subtly wrong.  A policy that looks right can leak rows through a second policy nobody
re-read, hide rows because a column was NULL, or turn a sub-millisecond query into a scan of
every row.  MLS lets you state who may see and change which rows once, *proves* the
guarantees you care about before anything is deployed, and compiles the rules to ordinary
Postgres objects that have been tested against the real server.

The guide starts with a short tour (section 3) that builds a small spec one requirement at a
time and lets the prover catch two real mistakes along the way.  Then come a complete worked
example, recipes for common requirements, what to do when a proof fails, and the work that
comes after: deploying, adopting a schema you already have, changing a live spec, and having
an LLM write specs for you.

You should know PostgreSQL and have met RLS.  Everything else is introduced as it comes up.
`docs/REFERENCE.md` is the construct-by-construct reference; this guide links to it rather
than repeating it.

MLS is alpha software (version 0.1).  The spec language and the CLI may change before 1.0, but
the semantics in `docs/SEMANTICS.md` and the decisions in `docs/DECISIONS.md` are settled.

Contents

1. Why MLS
2. Install
3. A ten-minute tour
4. How a spec is put together
5. A complete example: tenant-scoped documents
6. Recipes
7. When a proof fails
8. Identity: who the current user is
9. Deploying the DDL
10. Changing a deployed spec: diff, migrate, preflight, drift
11. Writing specs with an LLM
12. Performance
13. Testing your own specs in CI
14. Limits and things MLS refuses to do
15. Where to read more

## 1. Why MLS

### Three ways hand-written RLS goes wrong

**A second policy opens a second door.**  Members read their team's notes.  Months later
someone adds "admins read their team's notes" as a second permissive policy.  Postgres ORs
permissive policies, so the old guarantee, *a user only sees the notes of their own teams*,
now depends on whether every admin is also a member: a fact stored in another table, enforced
by nothing, and written down nowhere.  Nobody re-reads the first policy when they add the
second.

**NULL is not false.**  `USING (NOT archived)` hides a row whose `archived` is NULL, because
`NOT NULL` is NULL and a policy admits a row only when its expression is TRUE.  A
`CHECK (NOT archived)` on the same column lets that row *in*, because a CHECK rejects a row
only when its expression is FALSE.  The same expression means two different things in two
places, and neither of them is what the person who wrote it was thinking about.

**The policy is right and slow.**  A membership check can be written several ways that all
return the same rows.  Depending on which one you pick, Postgres evaluates `auth.uid()` once
per statement or once per row, and uses the index on `tenant_id` or does not.  On a
million-row table the difference is three orders of magnitude (`docs/FINDINGS.md`), and
nothing in the policy text tells you which kind you wrote.

Each of these is easy to see once someone points at it, and hard to see in a review of forty
policies.  MLS takes them in turn: the prover finds the first, the three-valued semantics and
the compiler's normalization rule out the second, and the compiler emits only the policy shape
that stays on the index, which takes care of the third.

### What MLS does

An MLS spec is a file that says, for a set of tables, who may see and change which rows and
which invariants the stored data must satisfy.  It is written in a terse syntax (`.mls`, one
line per rule) or in the equivalent JSON (`.mls.json`).  The `mls` tool does three things with
it:

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

The pieces fit together like this:

        business requirements
                 │   written by you, or by an LLM with `mls generate`
                 ▼
               spec ─── mls check   is it well-formed, and inside what can be proved?
                 │  ─── mls prove   do the properties hold?  If not: a concrete counterexample
                 │  ─── mls lint    is a rule dead, redundant, or slow?
                 ▼
            mls compile ──► SQL script ──► Postgres ◄── mls drift   does it still match?

### What MLS is not

- **Not a query language or an ORM.**  It never touches your application's SQL.  The compiled
  objects are ordinary Postgres objects that apply to every connection as the app role.
- **Not a wrapper around hand-written RLS.**  You never write a policy expression; you write
  predicates in a small language with SQL's three-valued meaning, and the compiler emits the one
  policy shape it can prove and index.  Raw SQL is allowed only in an explicit `opaque` block,
  and every proof that depends on one says so.
- **Not only greenfield.**  MLS creates and owns the tables of a new schema, and it can also
  adopt an existing one: `mls import` reads the live tables and policies into a spec, lifting
  the policies it can read into the verified fragment and keeping the rest as opaque blocks
  (section 9.1).

The intended workflow is that an LLM writes the spec from business requirements and a
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

## 3. A ten-minute tour

This section builds a spec for a small team-notes app on Supabase, one requirement at a time.
You need no database: everything here runs on your machine in seconds.  Every output shown is
what `mls` actually prints, trimmed only where marked.  The finished spec is
`examples/team_notes.mls`, and the test suite replays this tour on it, so the outputs stay
current.

### Step 1: one rule, one guarantee

The requirement: *members of a team can read the team's notes.*  Save this as `notes.mls`:

    mls 0.1

    identity supabase { uid: auth.uid uuid }
    roles authenticated

    table members owned {
      user_id uuid not null,
      team_id int not null;
      primary_key (user_id, team_id)
    }

    table notes owned {
      id int not null,
      team_id int not null,
      author_id uuid not null,
      body text not null;
      primary_key (id)
    }

    relation member_of = members(user_id, team_id)

    rules {
      R1 policy member_read on notes: authenticated select permissive using member_of(@uid, team_id)
    }

    properties {
      P1 tenant_isolation notes: team_id in member_of(@uid) for select description 'a user only reads the notes of their own teams'
    }

Reading it from the top: the current user's id comes from Supabase's `auth.uid()` and is
called `@uid`.  There are two tables.  `relation member_of` says that the `members` table, read
as (user, team) pairs, is a membership relation, so `member_of(@uid, team_id)` means "the
current user is a member of this row's team".  `R1` is a rule, the thing that gets enforced.
`P1` is a property, the thing that gets *proved*: whatever the rules end up being, a user only
ever reads notes of teams they belong to.

    $ uv run mls check notes.mls
    notes.mls: OK

    $ uv run mls prove notes.mls
    # MLS proof report

    **1 proved, 0 refuted, 0 unknown**

    ## Assumptions
    ...
    ### P1 (tenant_isolation): PROVED — a user only reads the notes of their own teams

    - PROVED: select as authenticated: r.team_id ∈ member_of[key]

With one rule that is unsurprising.  The value comes when the rules grow.

### Step 2: a second policy, and the first mistake

The next requirement: *team admins can read every note of their team.*  Add an `admins` table
and a relation for it next to the others:

    table admins owned {
      user_id uuid not null,
      team_id int not null;
      primary_key (user_id, team_id)
    }

    relation admin_of = admins(user_id, team_id)

and a second policy under `R1`:

      R2 policy admin_read on notes: authenticated select permissive using admin_of(@uid, team_id)

This is exactly what you would write by hand, and it looks harmless.  Prove again:

    $ uv run mls prove notes.mls
    **0 proved, 1 refuted, 0 unknown**
    ...
    ### P1 (tenant_isolation): REFUTED — a user only reads the notes of their own teams

    - REFUTED: select as authenticated: r.team_id ∈ member_of[key]
      counterexample:
      - context: uid = uuid#0
      - r (notes): team_id = 1
      - admin_of = {(uuid#0, 1)}
      - member_of = {∅}

The counterexample is a complete, concrete situation: a user `uuid#0` (a stand-in for some
UUID) is an admin of team 1 but a member of no team (`{∅}` is an empty relation), and can read
a note of team 1.  Nothing in the spec says admins are members, so R2 lets an outsider in.
This is the first story of section 1, caught before it shipped.  `mls prove` exits with code 2
whenever something is refuted, so in CI this fails the build.

### Step 3: fix it, and let the lint have a say

There are two honest fixes, and choosing between them is a business decision, not a technical
one.  Either admins must be members, or P1 was the wrong promise.  Here, admins are members,
so say so with an *inclusion* rule, which compiles to a foreign key from `admins` to
`members`:

      R3 inclusion admin_is_member on admins: (user_id, team_id) in member_of on_delete restrict

    $ uv run mls prove notes.mls
    **1 proved, 0 refuted, 0 unknown**
    ...
    ### P1 (tenant_isolation): PROVED — a user only reads the notes of their own teams

The prover now uses the foreign key as a fact about every stored row, and P1 holds.  Now ask
the linter:

    $ uv run mls lint notes.mls
    warning: notes.body (text): queries filtering on ~~ (LIKE), ~~* (ILIKE), ~ (regex), ~* (regex, case-insensitive), <, <=, >, >= cannot use an index before the row policy (operator is not leakproof); equality is leakproof
    subsumption: policy R2 (admin_read) is subsumed by policy R1 (member_read) on notes: every row it admits to authenticated, the other admits too
    1 warning(s), 1 finding(s)

The finding is worth a second look.  Once every admin is a member, `admin_read` admits nothing
that `member_read` does not already admit: the requirement "admins read their team's notes"
was met before you wrote R2.  You can delete R2, or keep it as documentation; either way you
now *know*, rather than suspect.  (The warning is about performance, not safety: a
`LIKE` search on `body` cannot run ahead of the row policy.  Section 5.7 explains.)

### Step 4: a NULL, and the second mistake

The next requirement: *archived notes are hidden from everyone.*  Add a nullable column to
`notes`:

      body text not null,
      archived int;

and a *restrictive* policy, which Postgres ANDs on top of the permissive ones:

      R4 policy hide_archived on notes: authenticated all restrictive using not archived = 1

So far the properties only say what users must *not* see.  A spec that denies everything
satisfies all of those, so add the other direction too, a lower bound: *a member reads every
note of their team that is not archived*.  Notes that were never archived have `archived`
NULL or 0:

      P2 must_allow notes authenticated select: member_of(@uid, team_id) and (archived is null or archived = 0) description 'a member reads every note of their team that is not archived'

    $ uv run mls prove notes.mls
    **1 proved, 1 refuted, 0 unknown**
    ...
    ### P2 (must_allow): REFUTED — a member reads every note of their team that is not archived

    - PROVED: select as authenticated: some row meets the condition
      ...
    - REFUTED: select as authenticated: every row meeting the condition is admitted
      counterexample:
      - context: uid = uuid#2
      - r (notes): team_id = 3, archived = None
      - admin_of = {∅}
      - member_of = {(uuid#2, 3)}

A member of team 3 cannot read a note of team 3 whose `archived` is NULL (`None` in the
report).  `not archived = 1` is `not NULL`, which is NULL, and a policy denies on anything
but TRUE.  This is the second story of section 1.  Every note created without an explicit
`archived` value would have silently disappeared.

The fix is to say what NULL means:

      R4 policy hide_archived on notes: authenticated all restrictive using archived is null or archived <> 1

    $ uv run mls prove notes.mls
    **2 proved, 0 refuted, 0 unknown**

(Declaring the column `not null` would have been the other fix.)

### Step 5: compile

    $ uv run mls compile notes.mls

prints one SQL script.  The policies on `notes`:

    create policy "member_read" on "notes" as permissive for select to "authenticated"
      using (("team_id" = any (array(select "team_id" from "members" where "user_id" = (select auth.uid())))));  -- R1
    create policy "admin_read" on "notes" as permissive for select to "authenticated"
      using (("team_id" = any (array(select "team_id" from "admins" where "user_id" = (select auth.uid())))));  -- R2
    create policy "hide_archived" on "notes" as restrictive for all to "authenticated"
      using ((("archived" is null) or ("archived" <> 1)))
      with check ((("archived" is null) or ("archived" <> 1)));  -- R4

Each statement ends with the rule it came from.  The membership test has the one shape that
Postgres runs once per statement and drives from an index (the third story of section 1), and
the script adds that index.  It also adds the foreign key from R3, a policy that lets each
user see their own `members` and `admins` rows (the membership lookups need to read them), the
grants, and the hardening: row-level security forced on every table, so the owner obeys the
policies too.

### Step 6: read it back in English

The person who approves a change should not have to read either of the files above:

    $ uv run mls explain notes.mls
    # What the rules say

    - **R1**: authenticated may read rows of notes where the row's team_id is among the current user's member_of.
    - **R2**: authenticated may read rows of notes where the row's team_id is among the current user's admin_of.
    - **R3**: Every row of admins with both user_id and team_id set has (user_id, team_id) listed in member_of (table members); such a member_of entry cannot be removed or changed while a row of admins refers to it.
    - **R4**: authenticated may read and change rows of notes only where the row's archived is empty or the row's archived is not 1.

    # What is guaranteed (properties)

    - **P1**: Any row of notes a user can read has a team_id among the current user's member_of.
    - **P2**: authenticated can read every row of notes where the row's team_id is among the current user's member_of and (the row's archived is empty or the row's archived is 0).

The second list is what gets approved.  Each sentence is a promise the solver has checked
against every possible database, not just the rows you happened to test with.

### What you just did

In six steps you wrote four rules and two properties, and the toolchain caught a privilege
leak and a NULL that hid rows, each with a concrete example small enough to read aloud in a
meeting.  You did not write SQL, and the SQL you got is the shape that stays fast.  The rest of
this guide is the same loop at a larger scale: section 4 names the parts you just used,
section 5 walks through a complete spec, section 6 has a recipe for most common requirements,
and section 7 is what to do the next time a proof comes back REFUTED.

## 4. How a spec is put together

The tour used most of the parts of a spec without naming them.  Here they are by name.  A
spec has a fixed skeleton and two lists:

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
from requirements (section 11).

**Rules** are of six kinds.  Each one compiles to exactly one Postgres mechanism:

| Kind | Says | Becomes |
|---|---|---|
| `policy` | which rows a role may select / insert / update / delete | `CREATE POLICY` |
| `check` | every stored row satisfies a predicate | `CHECK ((p) IS TRUE)` |
| `inclusion` | a pair of columns is a pair of a relation | composite foreign key, `ON DELETE RESTRICT` |
| `transition` | every update satisfies a predicate over old and new | `BEFORE UPDATE` trigger |
| `cardinality` | at most N rows per group value | trigger that locks the parent row and counts |
| `column_rule` | a role may read or write only these columns | column-level `GRANT` |

A rule that should hold on several tables carries `"applies_to"` instead of `"table"`: a
selector such as `{"has_column": ["tenant_id", "int"]}`, `{"tagged": "tenant"}` or `{"named":
["docs", "files"]}`.  `mls check` expands it into one concrete rule per matching table
(`R1.docs`, `R1.files`) before anything else looks at the spec, so proofs, DDL, diffs and
reports are always about concrete rules; `mls explain` prints the selector once with the tables
it matched.  A selector that matches nothing is an error, not an empty rule: that is how a
renamed column would otherwise switch a tenant policy off.  Add a table with the column later
and the rule covers it the next time `check` runs.

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

The JSON syntax is verbose by design (it is written by programs).  The same spec has a second,
terse syntax: one line per item, which `mls explain --terse` prints and this guide uses
when quoting rules.  The example above, rendered:

    mls 0.1
    comment 'Tenant-scoped documents with membership-based access, clearance levels, ...'

    identity supabase { uid: auth.uid uuid; clearance: jwt.claims.clearance int }
    roles authenticated

    table docs owned {
      id int not null,
      tenant_id int not null,
      owner_id uuid,
      level int not null,
      archived int;
      primary_key (id)
    }

    relation member_of = memberships(user_id, tenant_id)

    rules {
      R1 policy member_read on docs: authenticated select permissive using member_of(@uid, tenant_id) and level <= @clearance requirement 'REQ-1'
      R6 check level_range on docs: level >= 0 and level <= 3 requirement 'REQ-5'
    }

    properties {
      P1 tenant_isolation docs: tenant_id in member_of(@uid) for select, insert, update description '...'
    }

The two are views of one model: a construct exists when the model has it, and both syntaxes
get it in the same change.  The JSON stays the canonical serialization (what `mls schema`
describes, what the eval cases and every program hold); the terse form is what a person reads
in a diff or a transcript, and what `mls` reads from a `.mls` file (`mls fmt` converts either
way; every example has its `.mls` twin).  The grammar is in the reference, §13.

## 5. A complete example: tenant-scoped documents

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

### 5.1 The rules

Here is the whole rule set as `mls explain --terse` prints it (the `rules` block of the terse
syntax, §4):

    R1 policy member_read on docs: authenticated select permissive using member_of(@uid, tenant_id) and level <= @clearance requirement 'REQ-1'
    R2 policy admin_read on docs: authenticated select permissive using admin_of(@uid, tenant_id) requirement 'REQ-2'
    R3 policy member_insert on docs: authenticated insert permissive check member_of(@uid, tenant_id) and level <= @clearance requirement 'REQ-3'
    R4 policy member_update on docs: authenticated update permissive using member_of(@uid, tenant_id) check member_of(@uid, tenant_id) and level <= @clearance requirement 'REQ-3'
    R5 policy not_archived on docs: authenticated all restrictive using not archived = 1 requirement 'REQ-4'
    R6 check level_range on docs: level >= 0 and level <= 3 requirement 'REQ-5'
    R7 inclusion owner_in_tenant on docs: (owner_id, tenant_id) in member_of on_delete restrict requirement 'REQ-6'
    R8 inclusion admin_is_member on admins: (user_id, tenant_id) in member_of on_delete restrict requirement 'REQ-6'
    R9 transition tenant_immutable on docs: new.tenant_id = old.tenant_id requirement 'REQ-7'

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

### 5.2 The properties

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
implicit policy and column grant on it, and least privilege must account for it.

P7 through P10 are the lower bounds.  P7 says: a member with enough clearance *can* read an
unarchived document of their tenant.  Note that it has to say `not archived is null`: with
`archived` NULL the row is invisible (R5 above), so a `must_allow` that did not exclude that case
would be refuted, and the report would show exactly the NULL row as the counterexample.  This is
how the NULL question in 5.1 gets answered and recorded.

### 5.3 Check

    $ uv run mls check examples/tenant_docs.mls.json
    examples/tenant_docs.mls.json: OK

The same spec in the terse syntax is `examples/tenant_docs.mls`; every command reads it the same
way (`uv run mls check examples/tenant_docs.mls`), and `uv run mls fmt` converts either way.
`check` validates the JSON against the pydantic model, type-checks every predicate, and enforces
the fragment (which predicate forms are allowed where).  It also runs the compiler's soundness
obligations, so a spec that passes `check` compiles.  Errors name the JSON path and, where
there is one, the fix:

    $ uv run mls check broken.json
    error at $.rules[0].using.and[1].cmp[0]: unknown column 'nope' of table 'docs'
      fix: one of: id, tenant_id, owner_id, level, archived

The same mistake in a terse file also says where in the text it is, at the atom, and quotes
the line; the path and the message are the ones the JSON gives:

    $ uv run mls check broken.mls
    error at $.rules[0].using.and[1].cmp[0] (broken.mls:32:103): unknown column 'nope' of table 'docs'
        R1 policy member_read on docs: authenticated select permissive using member_of(@uid, tenant_id) and nope <= @clearance
      fix: one of: id, tenant_id, owner_id, level, archived

`--json` gives the same as a list of `{code, path, message, fix, span}` objects (`span` is null
for JSON input).  The messages are written so that a program (an LLM in the generation loop)
can act on them.

### 5.4 Prove

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

### 5.5 Compile

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

### 5.6 Test

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

### 5.7 Lint

    $ uv run mls lint examples/tenant_docs.mls.json
    0 warning(s), 0 finding(s)

The lint is hygiene, not safety: it never changes a verdict and always exits 0.  The warnings
are about performance: per column type, the operators that are not leakproof (`LIKE`, pattern
matching, most text operators), which Postgres will not evaluate before the row policy, so a
query filtering on them cannot use its index and falls back to filtering every visible row.
Equality is leakproof.  With `--dsn` it reads the catalog of the real server rather than the
static list.

The findings are about the shape of the policy set, the things a reviewer would otherwise spot
by reading the rules twice: a policy that never admits a row (`contradiction`), one that admits
only rows another policy already admits (`subsumption`), one whose removal changes nothing
(`redundancy`), and a column rule or a governed table that no permissive policy backs, or an
`observed` table that a selector or a tenant column suggests should be governed (`coverage`):

    subsumption: policy R11 (legacy_member_read) and policy R1 (member_read) on docs admit the same rows for authenticated: one of them is enough
    0 warning(s), 1 finding(s)

The questions are put to the solver on the same three-valued encoding as the proofs, which
matters: `level < 1 or level >= 1` is a tautology in two-valued logic and UNKNOWN on a NULL
level, so it subsumes an owner policy only when the column is NOT NULL.  When the LLM loop
(§11) repairs a rule set, the findings go into the repair prompt as hints.

To say that two roles never reach the same row, use the `role_separation` property, which is
proved like any other: *No row of approvals can be updated by both requester and approver.*

## 6. Recipes

Most access requirements are one of a dozen shapes.  Each recipe below gives the requirement
in English, the terse lines that implement it and, where there is one, a property that pins
it down.  They come from small specs in the style of section 3 (a `posts` table with a
`team_id`, the `member_of` relation, a `teams` table); every one of them checks and proves as
shown.

**Members see their team's rows, on every table that has a team.**  A *selector* writes the
rule once and applies it to every table with the column:

    R1 policy team_read applies_to has_column(team_id int): authenticated select permissive using member_of(@uid, team_id)

`mls check` expands it into `R1.posts`, `R1.comments`, and so on.  Look at the list `mls
explain` prints: here it also matched `members`, which means members can see who else is on
their teams.  That may be what you want; if not, use `tagged(tenant)` with `tags (tenant)` on
the tables you mean, or `named(posts, comments)`.  A selector that matches no table is an
error, so renaming a column cannot quietly switch a policy off.

**Only the author edits, and cannot give the row away.**

    R2 policy author_update on posts: authenticated update permissive using author_id = @uid and member_of(@uid, team_id)
    P3 write_subset_read posts description 'a user can read back every post they edit'

An update policy without `check` uses its `using` for the new row as well, so the edited post
must still belong to the editor and to one of their teams.  `write_subset_read` catches the
classic mistake of an edit policy broader than the read policy.

**A column never changes once set.**

    R3 transition author_fixed on posts: new.author_id = old.author_id
    P2 column_immutable posts: author_id description 'a post never changes author'

A transition is a trigger, so it binds every role, the table owner included, which a policy
does not.

**A state that only moves forward.**

    R4 transition final_stays_final on posts: old.status <> 'final' or new.status = 'final'

**A closed set of values.**

    R5 check known_status on posts: status in ('draft', 'review', 'final')

Compiles to `CHECK (... IS TRUE)`, so a NULL `status` would be rejected, not waved through.
(Here the column is `not null` anyway.)

**At most N rows per parent.**

    R6 cardinality posts_per_team on posts: team_id -> teams max 100

`teams` must have a single-column primary key of `team_id`'s type.  The compiled trigger locks
the parent row before counting, which is what makes the limit hold with concurrent inserts.
That argument holds under READ COMMITTED, and the proof report says so: under REPEATABLE READ
the count would read a stale snapshot.

**Anonymous visitors see published rows inside a time window, and only some columns.**

    roles authenticated, anon
    ...
    R7 policy public_read on posts: anon select permissive using published = true and now_between(visible_from, visible_until)
    R8 column_rule on posts: anon read id, status
    P4 custom posts anon select new: published = true description 'an anonymous visitor only reads published posts'

`now_between` treats a missing or NULL bound as open.  A column rule is per role, not per row:
if a column should be visible on some rows and not others, it belongs in its own table with
its own policies (section 14).

**Archived rows are hidden from everyone.**  See step 4 of the tour, and say what NULL means.

**Two roles never act on the same row.**  A request is edited by its requester while it is a
draft, and decided by an approver once submitted.  (This spec uses the `pg_roles` adapter,
where `@user` is `current_user`; section 8.)

    R1 policy request_edit on approvals: requester update permissive using requested_by = @user and state = 'draft'
    R2 policy approve on approvals: approver update permissive using state = 'submitted' check state = 'approved' or state = 'rejected'
    S1 role_separation approvals update: requester | approver description 'no request can be edited both by a requester and by an approver'

**Access flows down a tree of groups.**  A user sees the files of their groups and of every
group below them:

    hierarchy org = groups id id parent parent_id depth depth max_depth 3 members group_member
    R1 policy group_read on files: authenticated select permissive using in_group(org, @uid, group_id)

The tree has a declared maximum depth, which is what keeps the proofs exact.
`examples/org_tree.mls` is the complete spec.

**Something the language cannot say.**  Declare an `opaque` block with the raw SQL and the
columns and context values it reads (reference §8).  It is enforced like any other policy
expression, but the prover treats it as an unknown, so every proof that depends on it says
so.  Keep opaque blocks few and small.

**Every grant deserves a lower bound.**  Upper-bound properties (`tenant_isolation`, `custom`,
`least_privilege`) say what must *not* happen, and a spec that denies everything satisfies
all of them.  For each thing a requirement says a user *can* do, add a `must_allow`, as in
step 4 of the tour.  That is how you find out that a rule is narrower than you meant.

## 7. When a proof fails

A REFUTED property comes with a counterexample: one small, concrete database state in which
the property is false.  Read it as a story.

    - context: uid = uuid#2                      who is asking (every context value)
    - r (notes): team_id = 3, archived = None    the row in question (old and new for an update)
    - admin_of = {∅}                             the facts in each relation; {∅} is empty
    - member_of = {(uuid#2, 3)}

`uuid#0`, `uuid#1` and so on are distinct, otherwise arbitrary UUIDs.  `None` is NULL.  Columns
the solver did not need are usually left out.  `mls diff` and the LLM loop print the same
scenario as an English sentence.

Then decide which of three things is wrong.  The solver cannot decide this for you, and it is
the most useful question the toolchain asks:

1. **The rule is wrong.**  The counterexample shows a user doing something the business would
   not allow.  Fix the rule (tour, step 4).
2. **The property is wrong.**  The counterexample is fine by the business: the promise was
   stated too broadly.  Fix the property, and record why; a property you weaken is a decision
   someone should sign off on.
3. **A fact is missing.**  The counterexample is a database state that should never exist,
   like an admin who is not a member (tour, step 3).  State the fact as a rule (an
   `inclusion`, a `check`, a `transition`) so that Postgres enforces it and the prover may
   rely on it.  Only state facts that are true of your data: `mls preflight` counts the
   existing rows that violate a new constraint before you deploy it (section 10.2).

Some other outcomes:

- **A `must_allow` whose first obligation (`some row meets the condition`) is refuted.**  The
  condition can never be true, usually because it contradicts a `check` rule.  The property
  is vacuous; fix the condition.
- **UNKNOWN.**  The solver ran out of time (`--timeout`, 20 seconds per obligation by default).
  UNKNOWN is never counted as PROVED.  Raise the timeout.
- **Rows vanish after deploying, and every property is proved.**  Then nothing says those rows
  should be visible.  Write the `must_allow` that does; its counterexample will show you the
  row, very often one with a NULL in it.

`mls check` errors come before any of this.  Each one gives the JSON path, the place in the
`.mls` file, the line, and the allowed alternative.  Two of them:

    $ uv run mls check posts.mls
    error at $.rules[1].using.and[1].not (posts.mls:39:100): negated membership in a policy is an anti-join and cannot be index-driven
        R2 policy author_update on posts: authenticated update permissive using author_id = @uid and not member_of(@uid, team_id)
      fix: restate positively, or wrap it in an opaque block

    $ uv run mls check posts.mls
    error at $.rules[0].applies_to (posts.mls:38:34): rule 'R1' applies to no table (has column tenant_id of type int)
        R1 policy team_read applies_to has_column(tenant_id int): authenticated select permissive using member_of(@uid, team_id)
      fix: declare a table the selector matches, or name the table with "table"

They are written to be acted on by a program as much as by a person: the LLM loop (section
11) feeds them back to the model.

## 8. Identity: who the current user is

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
`current_setting('app.<name>', true)` and cast to the declared type.  A setting that is
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

## 9. Deploying the DDL

The compiled script is idempotent for roles (`create role` if missing) but not for owned tables
and their policies: it is the *initial* deployment.  Run it as the role that will own the objects, in a
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
forced, a login-capable `mls_internal`).  On an adopted table (section 9.1) it also lists, without
failing, the objects MLS did not create and the hardening the spec assumes.  Run it from CI
against production; it needs only read access to the catalog plus the ability to create and roll
back the shadow schema.

### 9.1 Adopting an existing schema

MLS does not have to create the tables it governs.  A table declares its `mode`:

- `owned` (the default): MLS creates it, hardens it, and everything on it that the spec did not
  produce is drift.
- `adopted`: the table exists.  Its columns and keys are described in the spec as they are in
  the catalog, and MLS adds rules to it: policies, constraints, triggers, indexes, grants, and
  `ENABLE ROW LEVEL SECURITY`.  What MLS would do to an owned table but cannot do to one it did
  not create (force row-level security, change the owner) is either already in place, and drift
  verifies it, or stated as an assumption the proofs repeat.
- `observed`: in the schema, mentioned, not governed.  No rule may name it.

MLS takes over the whole policy set of an adopted table.  Whatever policies are on it when the
script runs, hand-written or left over from an earlier attempt, are dropped before the spec's
are created, in the same transaction, so a permissive `using (true)` someone wrote last year
cannot quietly keep granting what the proofs say is denied.  The script names each one it drops
that the spec does not declare:

    NOTICE:  MLS drops policy Enable read access for all users on profiles: the spec does not declare it

To see the list before deploying, run `mls drift`: its `unexpected` policies are exactly the
ones the deploy will drop.

To adopt a live schema, let MLS write the first version of the spec:

    uv run mls import --dsn "$DSN" --out spec.mls.json

Every table with row-level security becomes `adopted` (a table without it is `observed`: a
policy on it would mean nothing), the identity adapter is guessed from what the policies read,
relations are proposed from the foreign keys and the memberships the policies look up, and
every existing policy becomes a rule.  The lifter reads the policy's SQL into the fragment when
it is built from the shapes MLS itself emits or from the usual hand-written idioms (a bare
`auth.uid()`, `col in (...)`, `col in (select ...)`, a correlated `exists`, a boolean column);
what it cannot read, a call to a function of yours for instance, becomes an `opaque` block
carrying the columns, context values and relations the SQL reads, and the notes say which part
of which policy and why.  A CHECK it reads becomes a check rule.  The notes printed on stderr
list what was left out: a column whose type MLS does not model, a trigger (listed, not
imported), a grant with no policy behind it, a policy whose name is not an identifier; the ones
prefixed `lint:` name shapes a deploy improves on.  The hardening state is recorded as
assumptions:

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

The imported spec is a starting point.  It checks, compiles and proves as it is.  A lifted
policy is proved, diffed and explained like one MLS wrote; a proof over a policy that stayed
opaque is conditional on its block, which says little a reviewer can act on, so rewrite the
ones that matter by hand in the fragment and let `mls diff` confirm the rewrite changes
nothing.  Importing the database that `mls compile` of a spec deployed gives that spec back:
the same rules, no opaque block, an empty `mls diff`.  From the import on, the spec is the
source of truth: `mls check --dsn` verifies that the adopted tables still have the columns the
spec declares, and `mls drift` says whether the database and the spec still agree (a
hand-written CHECK it imported shows as `differs` until a deploy rewrites it to the template's
`IS TRUE` form).

`mls status` is the overview while the rewrite is under way: one line per table, `proved` when
every property about it holds outright, `opaque` with the blocks its proofs still depend on,
`unproved` when no property mentions it yet, `refuted`, and `ungoverned` for an observed
table.  With `--dsn` it also reads the live catalog, so a table that lost row-level security or
a table nobody has added to the spec shows up as `ungoverned`:

    $ uv run mls status spec.mls.json --dsn "$DSN"
    projects: proved (P1, P3)
    tasks: opaque (conditional on insert_into_own_projects_check)
    project_members: unproved (no property mentions it)
    audit_log: ungoverned (not in the spec)
    status: 1 ungoverned, 1 opaque, 1 unproved, 1 proved

Keep the spec in version control next to the application.  The spec, not the SQL, is the source
of truth; the SQL is a build artifact.

## 10. Changing a deployed spec: diff, migrate, preflight, drift

Specs change.  `examples/tenant_docs_v2.mls.json` is the next version of the walkthrough spec:
owners may delete their documents, documents get a `title` that must not be blank, the level
range widens to 4, a `tenant_settings` table appears, and an owner can no longer be changed once
set.  The release sequence is:

### 10.1 See what changed in effect

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

### 10.2 Check the existing data

    uv run mls preflight new.mls.json --old old.mls.json --dsn "$DSN"

For every new or tightened CHECK, foreign key, NOT NULL, UNIQUE, cardinality limit and hierarchy
bound, pre-flight counts the existing rows that would violate it, with samples, in a read-only
transaction.  With `--old` only the changed constraints are checked.  Fix or migrate the data
before the schema; the migration script aborts on the first violating row.

### 10.3 Generate the migration

    uv run mls migrate old.mls.json new.mls.json --out migrate.sql

The migration is phased: additive schema changes; new constraints as `NOT VALID` and new
triggers; `CREATE INDEX CONCURRENTLY`; `VALIDATE CONSTRAINT`; then the swap of policies, grants
and constraint names in one transaction; then `DROP INDEX CONCURRENTLY`.  Every statement is
annotated with the lock it takes, from a table that the test suite observes on every Postgres
version:

    alter table "docs" add column "title" text;  -- lock: AccessExclusiveLock on docs
    create index concurrently "mls_ix_docs_tenant_id_level" on "docs" ("tenant_id", "level");  -- lock: ShareUpdateExclusiveLock on docs
    alter table "docs" validate constraint "title_not_blank";  -- lock: ShareUpdateExclusiveLock on docs  -- R11

Policies go live only after validation, because the proofs assume stored rows satisfy the
constraints.  The phases marked "autocommit" must run outside a transaction (`psql -f` does
this; a migration tool that wraps the file in one transaction will fail on `CONCURRENTLY`).
`DROP COLUMN` and `DROP TABLE` are emitted only with `--allow-destructive`.  Column type
changes, primary key changes and identity adapter changes are refused with the JSON path and
the manual procedure.  `docs/MIGRATION.md` has the full phase and lock tables.

### 10.4 Confirm

    uv run mls drift new.mls.json --dsn "$DSN"

must report nothing.  This whole sequence (deploy v1 with data, migrate, drift clean,
differential test of v2 on the migrated database) is the repository's own acceptance test for
migrations, on every Postgres version.

## 11. Writing specs with an LLM

`mls generate` turns business requirements and a skeleton into a checked and proved spec:

    export ANTHROPIC_API_KEY=...
    uv run mls generate requirements.txt --skeleton skeleton.json --out spec.mls.json --transcript t.json

The skeleton is a spec without rules and properties (identity, roles, tables, relations,
hierarchies, opaques), in either syntax.  The requirements are plain prose.  `--out` writes the
spec in the syntax its extension names (`spec.mls` for terse).  The loop:

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
and rerun rather than editing the spec.  The transcript (`--transcript`) records every prompt
and answer, the generated spec in terse form, and the model's notes, which usually name the
ambiguity.

The model writes its answers in the terse syntax by default: the prompt carries the grammar and
three worked examples, and a checker error points at the line and column of the model's own
text.  `--syntax json` gives it the JSON Schema instead.  Both were measured on the whole corpus
and scored the same or better in terse, at a third fewer tokens, which is why terse is the
default.

The eval corpus (`evals/cases`) and `mls eval` measure the loop; `docs/LLM.md` explains the
case format, the metrics and the measured numbers.  Tests never call the API: setting
`MLS_LLM_SCRIPT` to a JSON file of answers replays them.

## 12. Performance

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

## 13. Testing your own specs in CI

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

## 14. Limits and things MLS refuses to do

These are decisions, not gaps; `docs/DECISIONS.md` has the reasoning.

- **Ownership is per table.**  MLS creates the tables it owns and adopts the ones it did not
  create (section 9.1), but it never silently applies to an adopted table a hardening it would
  have to assume: an app-role owner or unforced row-level security is stated in the spec and
  repeated in every report, or it is drift.  A hand-written policy is lifted into the fragment
  when the lifter reads it (D46) and adopted as an opaque block otherwise; a function of yours
  in a policy is never inlined, however short its body (D34).
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

## 15. Where to read more

- `docs/REFERENCE.md`: every construct, field, predicate form, property template, CLI flag,
  exit code and error format.
- `docs/SEMANTICS.md`: the meaning of every construct, the source of truth for the prover, the
  evaluator and the compiler.
- `docs/DECISIONS.md`: the settled design decisions, numbered, with what was rejected and why.
- `docs/MIGRATION.md`: migration phases, lock modes, what pre-flight and drift check.
- `docs/LLM.md`: the generation loop, the eval corpus and the measured results.
- `docs/FINDINGS.md`: what the experiments on real Postgres established (performance tiers,
  leakproofness, NULL and UPDATE semantics, what the live LLM runs found).
- `docs/BACKGROUND.md`: what `pg_more_ls`, the project MLS grew out of, established: the
  undecidability result that motivates the fragment and the survey of authorization languages.
- `lean/`: the Lean 4 formalization of the logic, the policy combination, the normalization and
  the templates, checked against the Python by generated test vectors.
