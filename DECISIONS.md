# Decisions (settled 2026-11-01)

Each decision is final for v1 unless marked *implementation default*, which Claude Code may revise
with a written rationale in this file.

## Foundations

**D1. Identity: pluggable adapters, Supabase first.**
The spec declares an identity provider. v1 ships two adapters:
- `supabase`: context comes from `auth.uid()` and `auth.jwt()` claims (always wrapped as
  `(select auth.uid())` for InitPlan evaluation). Trust assumption, stated in every proof report:
  clients reach the database only through Supabase's API layer, which verifies the JWT before
  setting the request claims; direct database connections by untrusted clients are out of scope.
- `pg_roles`: identity is `current_user` / role membership. No external trust assumption.
Rejected: raw `current_setting('app.*')` as identity (any client can `SET` it).

**D2. v1 scope adds four constructs** to the existing fragment (comparisons, membership,
and/or/not, CHECK, inclusion/FK, row transitions):
- group hierarchies (D5), cardinality limits (D4), time-bounded access, column-level rules.

**D3. Escape hatch: explicit `opaque` blocks.** A named block of raw SQL with a declared signature
(columns and context it reads). Modeled as an uninterpreted predicate. Every proof that depends on
one reports "conditional on: <names>". Opaque blocks are excluded from performance guarantees and
flagged in access diffs.

**D4. Cardinality limits lock a parent row.** The generated trigger runs
`SELECT ... FOR UPDATE` on the parent (group/tenant) row before counting. Serializes writers per
group only; correct under READ COMMITTED. A cardinality rule requires a declared parent table.
Proof side: counts are integer-valued uninterpreted functions with per-transition axioms (LIA + UF).

**D5. Hierarchies use a declared maximum depth.** The solver unrolls to that depth. The bound is
also enforced in the data (stored `depth` column = parent depth + 1, `CHECK (depth <= d)`).
Policies compile to an uncorrelated array InitPlan built by a recursive CTE, so they stay index-driven.
*Implementation default:* `parent_id` is immutable in v1 (a Transition rule), which rules out cycles
and subtree depth recomputation. Reparenting is v2.

**D6. Evaluation set: real patterns + synthetic.** Seed 30–50 cases from anonymized real-world RLS
patterns David has seen in the field; expand with solver-graded synthetic mutations.
Built in Milestone 4, but the case format is defined in Milestone 1 (see ROADMAP).

## Change over time

**D7. All four evolution features are v1:** access diff between versions, migration generation
(online-safe ordering, `NOT VALID` then `VALIDATE`), pre-flight data check for new invariants,
drift detection against the live catalog.

**D8. Greenfield only in v1.** MLS owns the schemas it creates. No import of existing hand-written
policies. (Drift detection still reports foreign objects on MLS-owned tables.)

## Intent and trust

**D9. Properties: template library + custom.** Templates: tenant isolation, monotonicity in
membership, write ⊆ read, column immutability, least privilege (no role sees more than declared),
invariant preservation (inductive). Custom properties use the MLS predicate language.

**D10. Humans approve properties + scenarios.** The approval artifact is the plain-English property
list plus generated scenarios: allowed and denied examples, every counterexample, every access-diff
change. Humans never need to read MLS or DDL.

**D11. Compiler correctness: two parallel tracks.**
- Ship on translation validation + per-template differential tests + mutation checks in CI.
- Separately, a Lean 4 formalization of the core semantics (3VL, policy combination, templates).
  Not on the critical path of any milestone.

**D12. Postgres 15, 16, 17, 18** in the semantic model and the CI matrix.

## Enforcement and performance

**D13. Bypass paths: emit hardening and verify it.** The compiler emits: app roles are not table
owners, no `BYPASSRLS`, `ALTER TABLE ... FORCE ROW LEVEL SECURITY`, `REVOKE TRUNCATE`, no grant of
`SET` on `session_replication_role`. Drift detection verifies these. Proof reports list the remaining
trusted roles (owner, superuser) explicitly.

**D14. Performance is checked three ways:** generated indexes for every membership/comparison atom;
EXPLAIN checks in CI asserting each policy atom appears as an index condition at realistic scale;
a leakproof lint for operators commonly used against protected tables.

**D15. IR: JSON + JSON Schema** (generated from the pydantic models), enabling constrained decoding.
A generated English rendering is the human view.

**D16. LLM loop (Milestone 4): separate passes + bounded repair.** Pass 1 requirements → properties.
Pass 2 requirements → rules, without seeing pass 1. The solver checks rules against properties;
counterexamples feed back for a bounded number of repair rounds; unresolved conflicts go to the
human as scenarios.

## Practical

**D17. Ownership: personal open-source project, Apache-2.0.** (David to confirm compatibility with
his employment agreement before publishing.)
**D18. Python.** **D19. Z3 primary behind a solver-agnostic layer, cvc5 cross-check in CI.**
**D20. Milestone 1 = core loop without the LLM.** See ROADMAP.
