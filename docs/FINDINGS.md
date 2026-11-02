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
