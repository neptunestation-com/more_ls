# MLS — "More LS": RLS more-or-less (prototype)

Files
- `mls.py` — the DSL (AST), fragment checker, SQL compiler, SQL three-valued evaluator, Z3 encoder.
- `demo.py`   — a policy corpus, theorems, counterexamples, and a differential test against real Postgres.

Run
    pip install z3-solver psycopg2-binary
    python3 demo.py                              # proofs only
    python3 demo.py --pg <socket-dir> <port>     # + differential test (needs a superuser connection)

## The fragment
- Comparisons: `column op (session var | constant)`.
- Membership: `column IN (select val from rel where key = session var)`, with `column` NOT NULL.
  Compiles to `col = any(array(select ...))`, which Postgres plans as an index condition.
- `and` / `or` / `not`. Negated membership is flagged as an anti-join.
- No per-row correlated subqueries, no user functions, no recursion. This is what keeps the logic
  decidable: quantifier-free formulas over uninterpreted membership predicates (EPR-style).

## What gets proved (all by one pattern: `assumptions ∧ ¬goal` is UNSAT)
1. Fragment membership (syntactic).
2. Tenant isolation for SELECT / INSERT / UPDATE (new row).
3. UPDATE tenant-preservation (refuted for the example corpus: a user in two tenants can move a row).
4. NULL liveness: a restrictive `NOT (archived = 1)` hides rows where `archived IS NULL`.
5. Redundancy: which policies can be dropped without changing the effective predicate.
6. Monotonicity: shrinking `memberships` never grants access.

## Model vs reality
Proofs are about the model. The differential test (random data, every user x clearance, SELECT and INSERT
decisions compared against Postgres 16) is what ties the model to the engine. Not modeled: UPDATE's
interaction with SELECT policies, foreign keys (bypass RLS), SECURITY DEFINER, BYPASSRLS/owners, triggers,
isolation-level effects, the planner. "Index-driven" is a syntactic assumption about the planner, checked
separately with EXPLAIN.

## Part 2: RLS as one target among several (`integrity.py`)

| DSL construct | Logic | Compiled to |
|---|---|---|
| `Policy` (ctx, row) | quantifier-free + uninterpreted relations | `CREATE POLICY` |
| `Check` (row) | quantifier-free | `CHECK ((p) IS TRUE)` |
| `Inclusion` (row ⊆ rel) | universal (EPR) | `FOREIGN KEY ... ON DELETE RESTRICT` (MATCH SIMPLE) |
| `Transition` (old, new) | quantifier-free over two rows | one fixed `BEFORE UPDATE` trigger template |

Run
    python3 integrity.py                          # proofs
    python3 integrity.py --pg <socket-dir> <port> # + differential test + mutation checks

Proved / refuted
1. A naive `CHECK (p)` admits NULL rows (refuted); `CHECK ((p) IS TRUE)` matches RLS's deny-on-unknown (proved).
2. UPDATE preserves `tenant_id`: refuted with RLS alone, proved with the transition trigger.
3. Inductive invariant `level_range ∧ owner_in_tenant` plus `admins ⊆ memberships`:
   INSERT and UPDATE on docs proved; DELETE from memberships refuted unguarded, refuted with only
   the docs FK (admins still dangle), proved with both FKs.

Differential test (Postgres 16): 1,280 real UPDATEs (filtered / ok / error outcomes) across 32 contexts,
every membership DELETE, the unguarded-delete counterexample realized (rows violating I), and both CHECK
compilations. 0 mismatches; two mutated models (no trigger; no SELECT check on the new row) each fail.

Assumed, not modeled: triggers disabled via `session_replication_role`, `TRUNCATE`, owners/`BYPASSRLS`,
`DEFERRABLE` constraints, concurrency (all constructs here are engine-enforced or single-row, so none race).
