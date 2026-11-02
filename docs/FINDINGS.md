# Findings from experiments (PostgreSQL 16.15, 2026-10-30)

## Policy performance tiers
1M-row table, 1,000 tenants, 3,000 rows visible; `select count(*)` as a non-owner role.

| Policy shape | Time | Plan |
|---|---|---|
| `tenant_id = (select current_setting(..)::int)` | 0.17 ms | index cond |
| `tenant_id = any(array(select tenant_id from memberships where user_id = (select ..)))` | 0.23 ms | index cond |
| `tenant_id = any(security_definer_fn_returning_int_array())` | 0.46 ms | index cond |
| `tenant_id in (select ...)` | 71 ms | hashed SubPlan filter, scans whole index |
| inline correlated `exists(...)` | 332 ms | seq scan + hashed SubPlan |
| per-row PL/pgSQL function | 5,700 ms | 1M calls |
| per-row SQL function containing EXISTS | 7,700 ms | not inlined, 1M calls |

Conclusion: membership must compile to `col = any(array(select ...))`. `IN (subquery)` in a
policy is not pulled up into a semi-join or index condition.

## Leakproofness
With the best policy in place, `slug LIKE 'doc-99999%'` went from 0.09 ms (no RLS) to 4.2 ms: the
planner won't use a non-leakproof qual (`textlike`) for the index scan before the policy, so it
fetches all of the user's rows and filters. `=` (leakproof) still uses its index. Basis for D14's lint.

## NULL semantics
- A restrictive `NOT (archived = 1)` hides every row with `archived IS NULL`.
- Naive `CHECK (archived = 0 or archived = 1)` accepted a NULL; `CHECK ((...) IS TRUE)` rejected it.

## UPDATE semantics
Differential test, 1,280 UPDATEs: SELECT policies apply to the new row when the statement reads
columns; an update that hides the row from its author errors. A model without this check mismatched.

## Integrity proofs
- RLS alone cannot keep `tenant_id` fixed (counterexample: user in tenants 3 and 4 moves a row).
  A transition trigger fixes it.
- Invariant "owner is a member of the row's tenant" + "admins ⊆ memberships": deleting a membership
  breaks it unless **both** FKs exist (RESTRICT). Without the FK, a real delete left 9 violating rows.

## Test methodology that worked
Random data, every (user × context) combination, three-way outcomes (filtered / ok / error), and a
deliberately wrong model as a mutation check (a 2VL model produced 157 mismatches).

## Hierarchies and cardinality (Milestone 3, PG 15–18)
- `col = any ((select f(k)))` is a subquery-ANY ("operator does not exist: integer = integer[]");
  the array InitPlan form needs `any ((select f(k))::int[])`. With it, `in_group` policies on
  1M rows are index-driven (Index Cond on the group column) on all four versions.
- A BEFORE row trigger does not see rows inserted earlier by the same statement: a hierarchy
  table must be loaded level by level (parents in an earlier statement).
- The parent-row `FOR UPDATE` lock is what makes the count correct: 8 parallel writers × 6
  attempts against a limit of 5 end with exactly 5 rows with the lock and more than 5 without
  it (CARDINALITY_NO_LOCK), under READ COMMITTED; the single-session differential test cannot
  tell the two apart, so the concurrency test is the mutation test for the lock.
- `ALTER FUNCTION ... OWNER TO` / `GRANT EXECUTE` / `DROP FUNCTION` hold no lock on tables; a
  function referenced by a policy cannot be dropped before the policy (dependency error).
- `pg_proc` snapshots (body, language, prosecdef, owner, EXECUTE acl) make tampering with a
  SECURITY DEFINER helper visible to drift; trigger definitions alone do not.
- cvc5 `finite-model-find` answers `unknown` on the cardinality induction (∀ over Int with
  LIA + UF); plain `mbqi` agrees with Z3 (unsat) immediately.
