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
| `in_group(col, hierarchy)` | `col` ∈ groups reachable from the user within depth d | array InitPlan from recursive CTE |
| `is_null(col)` | two-valued | — |
| `opaque(name)` | uninterpreted predicate over declared inputs | never (excluded from perf guarantees) |

Restrictions checked by the fragment checker: right-hand sides of comparisons are context or
constants; membership keys are context, not columns (no per-row correlated subqueries in policies);
negated membership is rejected in policies (anti-join) unless inside an `opaque` block; membership
value columns are NOT NULL.

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

Column rules in v1 are per Postgres role, not per row. Row-dependent column masking is v2.

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
- **Invariant preservation (induction):** for every compiled transition τ (INSERT/UPDATE/DELETE on
  every table the invariant mentions, including referenced tables):
  `I(state) ∧ guards(τ) → I(τ(state))`. Referenced-table deletes are modeled with two copies of
  the relation (`small(x,y) ⇔ big(x,y) ∧ ¬(x=u ∧ y=t)`) plus the FK RESTRICT guard.
- **Access diff (v1 → v2):** for each table × command × role, check SAT of `v2 ∧ ¬v1` (new grants)
  and `v1 ∧ ¬v2` (new denials) over shared relation symbols; each model is a scenario.
- **Cardinality:** counts are integer functions `cnt(g)`; insert into g: `cnt'(g) = cnt(g)+1`, guarded
  by `cnt(g)+1 <= N`; prove `cnt(g) <= N` is preserved.
- **Hierarchy:** reachability unrolled to depth d with the depth CHECK as an axiom.
- **Opaque blocks:** uninterpreted predicates; any proof whose UNSAT core includes one is reported as
  conditional on it. A property that holds only by constraining an opaque block is REFUTED.

## 8. Explicit assumptions in every proof report

Identity adapter assumption; trusted roles (owner, superuser) after hardening; isolation level for
non-engine-enforced constructs (cardinality relies on the parent-row lock); opaque blocks used;
Postgres version range validated by the differential tests.
