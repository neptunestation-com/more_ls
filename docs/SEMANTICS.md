# MLS semantics (v1)

This is the reference meaning of every construct. The SMT encoding, the concrete evaluator used in
differential tests, and every compiler template must agree with it.

## 1. Values and logic

- Column values are integers, text (compared for equality only in v1), booleans, timestamps
  (modeled as integers), or NULL if the column is nullable. Context values are never NULL.
- Predicates evaluate to TRUE, FALSE or UNKNOWN with SQL semantics:
  - comparison with a NULL operand → UNKNOWN; `IS NULL` is two-valued;
  - AND: FALSE dominates, then UNKNOWN; OR: TRUE dominates, then UNKNOWN; NOT swaps TRUE/FALSE.
- SMT encoding: each predicate becomes a pair (T, F) of booleans; UNKNOWN is ¬T ∧ ¬F.
- **Admission rule:** every target admits a row iff the predicate is TRUE (normalized; see §4).

## 2. Atoms (the verified fragment)

| Atom | Meaning | Index-driven when |
|---|---|---|
| `cmp(col, op, ctx|const)` | ordinary comparison | `col` indexed |
| `member(rel, key=ctx, val=col)` | `(ctx, col) ∈ rel` | compiles to `col = any(array(select ...))` |
| `now_between(from_col, until_col)` | `from <= now < until`, NULL bound = open | indexed bounds or small row set |
| `in_group(hierarchy, key=ctx, value=col)` | `∃ g: (ctx, g) ∈ members ∧ g = col ∨ g = parent(col) ∨ … ∨ g = parentᵈ(col)` (the row's group is one of the user's groups or lies below one, within depth d) | array InitPlan from a recursive CTE descending from the user's groups |
| `is_null(col)` | two-valued | — |
| `opaque(name)` | uninterpreted predicate over declared inputs | never (excluded from perf guarantees) |

Restrictions checked by the fragment checker: right-hand sides of comparisons are context or
constants; membership keys are context, not columns (no per-row correlated subqueries in policies);
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
their own salary, only the titles of others) is modelled by normalization (D42): the column is its
own table, one row per parent row, with its own row policies.  MLS has no masking construct.

### 3.1 Hierarchies (D5, D35)

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

### 3.2 Cardinality limits (D4, D36)

`cardinality(table, group, parent, max)` means: for every stored value `g` of `group`, at most
`max` rows of `table` have `group = g`.  Rows with `group` NULL are not counted.  `group` gets an
FK to the parent table's single-column primary key.  Enforcement is a BEFORE INSERT OR UPDATE OF
`group` row trigger: a NULL new group returns; an UPDATE that keeps the group returns (no change
of the count); otherwise it locks the parent row `FOR UPDATE`, counts the rows of the new group
and raises `check_violation` if `count + 1 > max`.  Correct under READ COMMITTED (the count after
the lock sees committed rows of the writers that held the lock before); not under REPEATABLE READ,
where the count uses the transaction's snapshot (stated as an assumption in every report).  The
proof side uses integer functions `cnt@before`, `cnt@after` (SEMANTICS §7).

### 3.3 The internal role (D34)

Both templates need to read tables as a role the app policies do not filter: the reachability
function must see every group (a user's policy on `groups` would hide the ancestors they are not a
member of), the depth trigger must read the parent row, the counting trigger must see every row of
the group.  The compiler creates one NOLOGIN, NOBYPASSRLS, NOINHERIT role `mls_internal` that owns
the generated helpers (`SECURITY DEFINER`, `SET search_path FROM CURRENT`); for every table a helper
reads it gets a permissive `SELECT ... USING (true)` policy `mls_internal_<table>` (and `FOR ALL`
on cardinality parents, which it locks), plus the matching grants.  Nobody can log in as it, no app
role is a member of it (drift reports both), and the helpers are the only code it runs.

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
- Re-verify this per PG version in the M1 differential tests.

**INSERT**: errors unless INSERT-CHECK(new) and constraints hold.
**DELETE**: rows targeted by DELETE-USING (and SELECT-USING when reading columns).

## 6. Identity adapters

An adapter maps context names to SQL expressions and declares its trust assumption.
- `supabase`: `uid → (select auth.uid())`, claims → `(select auth.jwt() ->> 'claim')`.
  Assumption: claims are set only by the API layer after JWT verification.
- `pg_roles`: `role → current_user`, membership via `pg_has_role`. No external assumption.

## 7. Proof obligations

All are "assumptions ∧ ¬goal is UNSAT"; SAT yields a counterexample rendered as a scenario.

- **Template properties** (D9), e.g. isolation: `effective_select(r) → member(memberships, uid, r.tenant)`.
- **must_allow (the lower bound, D40):** `constraints(every image) ∧ when → admitted`, where
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
  and `v1 ∧ ¬v2` (new denials) over shared relation symbols; each model is a scenario.
- **Cardinality:** counts are integer functions `cnt(g)`; insert into g: `cnt'(g) = cnt(g)+1`, guarded
  by `cnt(g)+1 <= N`; prove `cnt(g) <= N` is preserved.
- **Hierarchy:** `in_group(h, k, v)` is encoded as `∨_{i=0..d} chain_i(v) ∧ member(members, k, parentⁱ(v))`
  with `chain_i(v)` = the first i ancestors exist (`has_parent`), over uninterpreted `parent` /
  `has_parent` functions; the depth bound d is the declared `max_depth`.  Two versions of a spec
  share these functions when the tree (table, columns, members relation) is the same, so a
  changed `max_depth` diffs exactly; a changed tree is a different function (tagged `@new`).
- **Opaque blocks:** uninterpreted predicates; any proof whose UNSAT core includes one is reported as
  conditional on it. A property that holds only by constraining an opaque block is REFUTED.

## 8. Explicit assumptions in every proof report

Identity adapter assumption; trusted roles (owner, superuser, and the internal role `mls_internal`
that owns the SECURITY DEFINER helpers) after hardening; isolation level for non-engine-enforced
constructs (cardinality relies on the parent-row lock under READ COMMITTED); opaque blocks used,
each with its declared signature and SQL, and the number of proofs conditional on them; Postgres
version range validated by the differential tests.
