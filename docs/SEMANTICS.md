# MLS semantics (v1)

This document defines what every construct means. The SMT encoding, the concrete evaluator used in
differential tests, and every compiler template must agree with it; `docs/REFERENCE.md` describes
how to write them, and `docs/design/DECISIONS.md` why each choice was made.

## 1. Values and logic

- Column values are integers (`int` or `bigint`: one sort, the type only bounds constants),
  text (compared for equality only in v1), enum labels (text from a fixed list, compared for
  equality only), booleans, timestamps (modeled as integers), or NULL if the column is nullable.
  Context values are never NULL.
- Predicates evaluate to TRUE, FALSE or UNKNOWN with SQL semantics:
  - comparison with a NULL operand → UNKNOWN; `IS NULL` is two-valued;
  - AND: FALSE dominates, then UNKNOWN; OR: TRUE dominates, then UNKNOWN; NOT swaps TRUE/FALSE.
- SMT encoding: each predicate becomes a pair (T, F) of booleans; UNKNOWN is ¬T ∧ ¬F.
- **Admission rule:** every target admits a row iff the predicate is TRUE (normalized; see §4).

## 2. Atoms (the verified fragment)

| Atom | Meaning | Index-driven when |
|---|---|---|
| `cmp(col, op, ctx|const)` | ordinary comparison | `col` indexed |
| `in(term, [c1, …, cn])` | sugar for `cmp(term, =, c1) ∨ … ∨ cmp(term, =, cn)`, expanded by the checker before encoding; compiled as `term in (c1, …, cn)` | `col` indexed (one `= any` condition) |
| `member(rel, key=ctx, val=col)` | `(ctx, col) ∈ rel` | compiles to `col = any(array(select ...))` |
| `member(R, key=ctx, val=col)`, `R` composed (§3.4) | `∃ t₁…tₙ: (ctx, t₁) ∈ base ∧ T₁(k₁ = t₁, v₁ = t₂) ∧ … ∧ Tₙ(kₙ = tₙ, vₙ = col)` | one nested `= any(array(select ...))` per level, each hop key indexed |
| `now_between(from_col, until_col)` | `from <= now < until`, NULL bound = open | indexed bounds or small row set |
| `in_group(hierarchy, key=ctx, value=col)` | `∃ g: (ctx, g) ∈ members ∧ g = col ∨ g = parent(col) ∨ … ∨ g = parentᵈ(col)` (the row's group is one of the user's groups or lies below one, within depth d) | array InitPlan from a recursive CTE descending from the user's groups |
| `is_null(col)` | two-valued | — |
| `opaque(name)` | uninterpreted predicate over declared inputs | never (excluded from perf guarantees) |

A relation is the `(key, value)` projection of one table whose pair is unique there: the pair, or
one of its columns, is a primary key or unique constraint of the table (the owner of a parent row,
`projects(owner_id, id)`, is the second case).  Its facts are the pairs of the stored rows; a
relation with a condition (`where p`, over the table's own columns and constants) has the pairs
of the stored rows for which `p` is TRUE, so they are among the facts of a relation without a
condition over the same table, key and value, and the proofs assume so.

Restrictions checked by the fragment checker: right-hand sides of comparisons are context or
constants; membership keys are context or constants, not columns (no per-row correlated
subqueries in policies), and a key read through an enum column is one of its labels;
negated membership is rejected in policies (anti-join) unless inside an `opaque` block; membership
value columns are NOT NULL.  `in_group` follows the membership rules (key is context, value is a
column of the hierarchy's id type, no negation in policies) and is not allowed in checks and
transitions (it reads other tables).  NULL key or value makes it UNKNOWN; the walk-up stops at a
missing parent row (the FK makes that impossible for stored data) or at a root.

`now()` is the transaction start time and is modeled as a context value `ctx.now`.
Compiled as `(select now())`.

## 3. Rule constructs and compilation targets

| Construct | Logic | Target |
|---|---|---|
| `policy` (role, command, permissive/restrictive, using, check) | QF + UF | `CREATE POLICY` |
| `check` (table, predicate) | QF | `CHECK ((p) IS TRUE)` |
| `inclusion` (table, cols → rel) | EPR | composite FK, MATCH SIMPLE, `ON DELETE RESTRICT` |
| `transition` (table, predicate over old/new) | QF over two rows | one `BEFORE UPDATE` row trigger template |
| `column_rule` (role, table, columns, read/write) | propositional | column privileges (`GRANT SELECT (cols)`; no table-level grant to that role) |
| `cardinality` (table, group col, parent, max N) | LIA + UF | trigger template: lock parent row `FOR UPDATE`, count, raise |
| `hierarchy` (group table, parent col, max depth d) | unrolled to depth d | depth column + `CHECK (depth <= d)`, immutable parent (transition), recursive-CTE InitPlan in policies |
| `opaque` (name, inputs, sql) | uninterpreted predicate | raw SQL, wrapped and named |

Column rules are per Postgres role, not per row.  Row-dependent column visibility (a user sees
their own salary, only the titles of others) is modelled by normalization: the column is its
own table, one row per parent row, with its own row policies.  MLS has no masking construct.

### 3.1 Hierarchies

A hierarchy names a table, its single-column primary key `id`, a nullable `parent` column of the
same type, an int NOT NULL `depth` column, a bound `max_depth` (1–16) and a `members` relation
whose value has the id's type.  Meaning of the stored data: `parent` is NULL for roots and a stored
id otherwise (FK); `depth` is 0 for roots and the parent's depth + 1 otherwise (a BEFORE INSERT OR
UPDATE trigger sets it; the app never writes it); `0 ≤ depth ≤ max_depth` (CHECK); `parent` never
changes (implicit transition rule `mls_h_<name>_parent_immutable`, NULL-aware: a NULL parent stays
NULL).  Because the parent is immutable and every stored row's parent existed before it, the
stored rows form a forest of depth ≤ max_depth and a chain of parents from any row reaches a root in
≤ max_depth steps.  The members relation value column gets an FK to the id.  The proof side
unrolls `parent` exactly max_depth times (the `has_parent`/`parent` functions are uninterpreted;
the depth bound is what makes the unrolling exact).

### 3.2 Cardinality limits

`cardinality(table, group, parent, max)` means: for every stored value `g` of `group`, at most
`max` rows of `table` have `group = g`.  Rows with `group` NULL are not counted.  `group` gets an
FK to the parent table's single-column primary key.  Enforcement is a BEFORE INSERT OR UPDATE OF
`group` row trigger: a NULL new group returns; an UPDATE that keeps the group returns (no change
of the count); otherwise it locks the parent row `FOR UPDATE`, counts the rows of the new group
and raises `check_violation` if `count + 1 > max`.  Correct under READ COMMITTED (the count after
the lock sees committed rows of the writers that held the lock before); not under REPEATABLE READ,
where the count uses the transaction's snapshot (stated as an assumption in every report).  The
proof side uses integer functions `cnt@before`, `cnt@after` (SEMANTICS §7).

### 3.3 The internal role

Both templates need to read tables as a role the app policies do not filter: the reachability
function must see every group (a user's policy on `groups` would hide the ancestors they are not a
member of), the depth trigger must read the parent row, the counting trigger must see every row of
the group.  The compiler creates one NOLOGIN, NOBYPASSRLS, NOINHERIT role `mls_internal` that owns
the generated helpers (`SECURITY DEFINER`, `SET search_path FROM CURRENT`); for every table a helper
reads it gets a permissive `SELECT ... USING (true)` policy `mls_internal_<table>` (and `FOR ALL`
on cardinality parents, which it locks), plus the matching grants.  Nobody can log in as it, no app
role is a member of it (drift reports both), and the helpers are the only code it runs.

### 3.4 Composed relations

A composed relation is a base relation followed through up to three hops:
`relation in_task = member_of . projects(team_id, id) . tasks(project_id, id)` (JSON `{"of":
"member_of", "through": [{"table": "projects", "key": "team_id", "value": "id"}, ...]}`).  `(k,
v) ∈ in_task` iff some `t` has `(k, t) ∈ member_of`, a stored `projects` row has `team_id = t`
and `id = p`, and a stored `tasks` row has `project_id = p` and `id = v`.  Requirements, checked
with the JSON path of the offending hop: the base is a declared base relation (not composed);
each hop's `value` is its table's primary key or a NOT NULL unique column, its `key` and `value`
NOT NULL, and its `key`'s type that of the previous level's value; at most three hops.  Because
each hop's value is a key, the hop is a function from value to key and the ∃ disappears: the
encoding is `hop.T.v(x)` (a row with `v = x` exists) and `hop.T.v.k(x)` (its key), so
`(k, v) ∈ base . T(key, value)` is `hop.T.value(v) ∧ base(k, hop.T.value.key(v))`, unrolled
per hop, quantifier-free with uninterpreted functions (invariant 6).  An obligation that also
reads a stored row of a hop table (any image but `new`) adds `hop.T.v(row.v) ∧ hop.T.v.k(row.v)
= row.k`.  NULL behaves as for any membership: a NULL key or value is UNKNOWN; the hop columns
are NOT NULL.

A composed relation is a policy atom (and may appear in goals, `must_allow` and `tenant_isolation`).
It is refused, with the alternative, in an invariant (a write to a hop table changes it, which the
induction step does not model), as the relation of an inclusion rule (an FK names one table), as a
hierarchy's members relation, in an opaque block's relations and in `monotone_in` (each is about one
table's facts). Compilation nests one subquery per level: `task_id = any(array(select id from tasks
where project_id = any(array(select id from projects where team_id = any(array(select team_id from
team_members where user_id = (select auth.uid())))))))`. Every hop table is read as the app role, so
each hop gets the implicit relation policy: a permissive SELECT policy `mls_rel_<R>_h<i>_<role>_<n>`
whose `using` is the hop key's membership in the previous level (`<R>` the shortest composed
relation through that hop, so two relations sharing a prefix share the policy), a column grant on
`(key, value)`, and the relation-visibility obligation per level (a restrictive policy on a hop
table that could hide a reached row fails it). The hop key is indexed on an owned table and required
as an index condition by the EXPLAIN check.

## 4. NULL normalization (verified on PG 16)

- RLS: UNKNOWN denies. CHECK: UNKNOWN passes. Therefore every CHECK is emitted as
  `CHECK ((p) IS TRUE)` and every trigger test as `IF NOT ((p) IS TRUE)`.
- FK uses MATCH SIMPLE (any NULL in the referencing columns passes). The inclusion predicate is
  `is_null(c1) ∨ ... ∨ member(rel, c1, c2)` over the nullable columns.
- Other targets differ (e.g. Snowflake defaults FKs to MATCH FULL); out of scope for v1.

## 5. Policy combination (Postgres semantics)

For a table, role and command:
`effective = (OR of permissive) AND (AND of restrictive)`; no permissive policy → deny.
`WITH CHECK` defaults to `USING` for `ALL` and `UPDATE` policies.

**UPDATE** (verified by differential test on PG 16, 1,280 statements):
- a row is targeted iff UPDATE-USING(old) and, when the statement reads columns (any WHERE or
  RETURNING), SELECT-USING(old). Untargeted rows are silently skipped (0 rows, no error);
- the statement errors unless UPDATE-CHECK(new) and, when it reads columns, SELECT-USING(new),
  and all CHECKs, FKs and transitions hold. So an update that would hide the row from its
  author (e.g. raising `level` above clearance) is an error.
- The differential tests check this on every supported Postgres version.

**INSERT**: errors unless INSERT-CHECK(new) and constraints hold.
**DELETE**: rows targeted by DELETE-USING (and SELECT-USING when reading columns).

## 6. Identity adapters

An adapter maps context names to SQL expressions and declares its trust assumption.
- `supabase`: `uid → (select auth.uid())`, claims → `(select auth.jwt() ->> 'claim')`.
  Assumption: claims are set only by the API layer after JWT verification.
- `pg_roles`: `current_user → (select current_user::text)`; memberships are ordinary relations
  keyed by the role name. No external assumption: Postgres authenticated the session.
- `session_setting`: `name → (select nullif(current_setting('app.name', true), '')::type)`.
  A missing or empty setting is NULL, so every comparison with it is UNKNOWN and the row is
  denied. Assumption: clients cannot execute `SET` on `app.*`; a trusted path (pooler,
  `SET LOCAL` in a `SECURITY DEFINER` entry point, the application's connection layer) sets it.

## 7. Proof obligations

All are "assumptions ∧ ¬goal is UNSAT"; SAT yields a counterexample rendered as a scenario.

- **Template properties**, e.g. isolation: `effective_select(r) → member(memberships, uid, r.tenant)`.
  An update's assumptions include the column privileges: every column the role may not write
  keeps its value (Postgres refuses the statement otherwise), as in `must_allow` below.
- **must_allow (the lower bound):** `constraints(every image) ∧ when → admitted`, where
  `admitted` is the command's access layers — the effective policies and, for an update, the
  transition rules and the column privileges.  The written image is assumed *valid* (it satisfies
  the table's CHECKs and FKs), so `when` states what the role can do, not the integrity rules; the
  net effect the behaviour diff compares includes the constraints, the lower bound does not.  For
  update `when` ranges over `old`/`new`.  A second, *witness* obligation asks that
  `constraints ∧ when` be satisfiable; its model is reported as an example, and a condition no
  valid row meets refutes the property (it would require nothing) — an over-restrictive CHECK is
  caught here, not by the main obligation.
- **Invariant preservation (induction):** for every compiled transition τ (INSERT/UPDATE/DELETE on
  every table the invariant mentions, including referenced tables):
  `I(state) ∧ guards(τ) → I(τ(state))`. Referenced-table deletes are modeled with two copies of
  the relation (`small(x,y) ⇔ big(x,y) ∧ ¬(x=u ∧ y=t)`) plus the FK RESTRICT guard.
- **Access diff (v1 → v2):** for each table × command × role, check SAT of `v2 ∧ ¬v1` (new grants)
  and `v1 ∧ ¬v2` (new denials) over shared relation symbols; each model is a scenario.  Two base
  relations of the two versions over the same table, key and value are the same pairs when their
  conditions are equal, whatever their names, and one with a condition lies within one without.
- **Cardinality:** counts are integer functions `cnt(g)`; insert into g: `cnt'(g) = cnt(g)+1`, guarded
  by `cnt(g)+1 <= N`; prove `cnt(g) <= N` is preserved.
- **Hierarchy:** `in_group(h, k, v)` is encoded as `∨_{i=0..d} chain_i(v) ∧ member(members, k, parentⁱ(v))`
  with `chain_i(v)` = the first i ancestors exist (`has_parent`), over uninterpreted `parent` /
  `has_parent` functions; the depth bound d is the declared `max_depth`.  Two versions of a spec
  share these functions when the tree (table, columns, members relation) is the same, so a
  changed `max_depth` diffs exactly; a changed tree is a different function (tagged `@new`).
- **Composed relations:** the hop functions `hop.T.v`, `hop.T.v.k` are named by the hop, so
  two versions of a spec (and two relations sharing a hop) share them; the stored-row axioms of
  §3.4 tie them to every stored row an obligation reads.
- **Relation row axioms:** a stored row of a base relation's table is in the relation.  For
  every row an obligation reads of the table of a relation it uses (any image but `new`, which is
  not stored yet when the policies read the table), the encoding asserts `rel(row.key,
  row.value)`; for a relation with a condition `p`, `rel(row.key, row.value) ⇔ T(p(row))`, exact
  because the table holds each pair at most once.  Without it a property over the relation table itself (`team_id in
  team_members(@uid)` on `team_members`) is refuted by a row the relation does not contain, a
  state no database can be in.
- **Enum domain axioms:** an enum column holds one of its type's labels (or NULL when it is
  nullable): Postgres refuses any other value.  For every row an obligation reads, every enum
  column of its table is asserted equal to one of the labels, interned as distinct constants like
  any text constant.  When two versions of a spec are compared, the domain is the union of their
  labels, and a column that one version does not declare enum is not constrained.
- **role_separation:** `constraints ∧ effective_a ∧ effective_b` is UNSAT over the command's
  images (for update: `using` on `old`, `check` on `new`, plus the transition constraints), with one
  set of context values for both roles; a model is a row both roles may touch.
- **Opaque blocks:** uninterpreted predicates (a block is a function of its declared inputs). A
  PROVED obligation is conditional on a block when its proof needs that: re-encoded with every
  occurrence of the block an independent, unconstrained (T, F) pair, it is no longer UNSAT.
  Freeing over-approximates whatever the block's SQL does, so a proof that survives holds for
  every block and is not conditional on it; only the blocks occurring in the obligation's own
  terms are tried (the block's `¬(T ∧ F)` axiom is not a use). A property that holds only by
  constraining an opaque block is REFUTED.

Policy-set hygiene asks the solver the same kind of question but is never a verdict: a
contradiction is `constraints ∧ T(p)` UNSAT, a subsumption `constraints ∧ T(p2) → T(p1)` valid,
a redundancy `constraints → (T(eff) ↔ T(eff without p))` valid, all on the (T, F) encoding; the
two-valued `p2 ∧ ¬p1` UNSAT is wrong where `p1` is UNKNOWN on a row `p2` admits.

## 8. Explicit assumptions in every proof report

Identity adapter assumption; trusted roles (owner, superuser, and the internal role `mls_internal`
that owns the SECURITY DEFINER helpers) after hardening; the `assume` entries of adopted tables
(an app-role owner, unforced row-level security, an app role with BYPASSRLS, each trusted),
and the lists of adopted and observed tables; isolation level for non-engine-enforced
constructs (cardinality relies on the parent-row lock under READ COMMITTED); opaque blocks used,
each with its declared signature and SQL, and the number of proofs conditional on them; Postgres
version range validated by the differential tests.
