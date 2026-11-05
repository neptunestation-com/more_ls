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
Rejected: raw `current_setting('app.*')` as identity (any client can `SET` it). *Amended (D47):* a third
adapter, `session_setting`, with its trust assumption stated in every report; the rejection
stands for a direct connection.

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
policies. (Drift detection still reports foreign objects on MLS-owned tables.) *Amended (D45):* greenfield
is the `owned` mode; `adopted` and `observed` exist, and a policy MLS did not write is lifted
(D46) or an opaque block.

## Intent and trust

**D9. Properties: template library + custom.** Templates: tenant isolation, monotonicity in
membership, write ⊆ read, column immutability, least privilege (no role sees more than declared),
invariant preservation (inductive). Custom properties use the MLS predicate language.  (D40 adds
`must_allow`, the lower bound; everything else here is an upper bound.)

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
trusted roles (owner, superuser) explicitly. *Amended (D45):* on `adopted` tables hardening is
verified or assumed, never silently emitted.

**D14. Performance is checked three ways:** generated indexes for every membership/comparison atom;
EXPLAIN checks in CI asserting each policy atom appears as an index condition at realistic scale;
a leakproof lint for operators commonly used against protected tables.

**D15. IR: JSON + JSON Schema** (generated from the pydantic models), enabling constrained decoding.
A generated English rendering is the human view. *Amended (D50):* the JSON Schema
constrains the canonical serialization, not the only input; terse is a second concrete syntax
of the same model.

**D16. LLM loop (Milestone 4): separate passes + bounded repair.** Pass 1 requirements → properties.
Pass 2 requirements → rules, without seeing pass 1. The solver checks rules against properties;
counterexamples feed back for a bounded number of repair rounds; unresolved conflicts go to the
human as scenarios.

## Practical

**D17. Ownership: personal open-source project, Apache-2.0.** (David to confirm compatibility with
his employment agreement before publishing.)
**D18. Python.** **D19. Z3 primary behind a solver-agnostic layer, cvc5 cross-check in CI.**
**D20. Milestone 1 = core loop without the LLM.** See ROADMAP.

## Implementation defaults recorded during Milestone 1 (2026-11-02)

Each may be revised with a written rationale here.

**D21. Membership subqueries read the relation table through an implicit policy.** RLS is forced on
every MLS table, and a policy's `array(select value from rel where key = k)` runs as the app role.
So for every relation a role's policies use with key `k`, the compiler emits a permissive SELECT
policy on the relation's table for that role: `key = k` (named `mls_rel_<relation>_<role>_<n>`),
plus `GRANT SELECT (key, value)`. This is the least grant under which the atom means
"(k, col) ∈ relation". Restrictive policies the spec puts on the relation table still apply; the
compiler proves (relation-visibility obligation) that they keep every row with `key = k` visible,
and refuses otherwise with the JSON path of the policy and a fix.
Rejected: a SECURITY DEFINER helper returning the array (slower per FINDINGS, and another trusted
object); disabling RLS on relation tables (exposes every membership row).

**D22. Inclusion FKs are `ON UPDATE RESTRICT ON DELETE RESTRICT`.** Changing a referenced pair is
modeled as delete+insert, so the update case needs the same guard as the delete case.

**D23. Spec JSON shape (ROADMAP open detail).** Single-key objects for terms and predicates
(`{"col"}`, `{"ctx"}`, `{"const"}`, `{"old"}`, `{"new"}`; `{"cmp"}`, `{"member"}`, `{"now_between"}`,
`{"is_null"}`, `{"opaque"}`, `{"and"}`, `{"or"}`, `{"not"}`, `{"lit"}`), rules discriminated by
`kind`, properties by `template`. `mode` and `command` are mandatory on policies. The only default is
Postgres's own: an UPDATE/ALL policy without `check` uses `using` for WITH CHECK, and the compiler
always emits the WITH CHECK clause explicitly so the DDL is self-describing. Context values are
declared with a `source` (`auth.uid`, `jwt.claims.<name>`, `current_user`) and a `type`; `now` is
built in. Opaque SQL references context values as `{ctx:name}` and declares the `relations` it
reads (they become state arguments of the uninterpreted predicate, so a monotonicity proof cannot
be satisfied vacuously). A top-level `$comment` is allowed.

**D24. Column rules are column privileges, derived together with grants.** Per (role, table), each
permissive policy grants its command; a `read` column rule restricts SELECT to a column list, a
`write` rule restricts INSERT and UPDATE; DELETE is table-level; TRUNCATE is never granted. The
`least_privilege` property compares the derived grants (including D21's) with a declared allowance.
Security-invoker views for row-filtered column masking were the v2 question of this decision; D42
settled it: no masking construct, the column is its own table with its own policies.

**D25. Proof assumptions.** Stored rows satisfy every CHECK and FK (constraints are created
validated). Enforced inclusions between relation tables become EPR axioms (`admin_of ⊆ member_of`).
Invariant preservation assumes no policy (trusted roles bypass them) and uses only CHECKs, FKs
(including RESTRICT guards) and transition triggers as guards. `write_subset_read` is the strong
form: the policies alone guarantee that an insertable or updatable new image is visible, without
relying on the statement reading columns.

**D26. The differential oracle models** policy combination (§5 incl. SELECT policies on UPDATE/DELETE
when the WHERE reads columns), CHECK normalization, MATCH SIMPLE FKs with RESTRICT on both delete and
update, transition triggers, PK/UNIQUE, NOT NULL, and table/column privileges. Expected error
classes are SQLSTATE 23xxx, 22xxx and 42501; anything else is a harness bug.

**D27. Supabase on plain Postgres.** The test harness installs Supabase's own `auth.uid()` and
`auth.jwt()` definitions (reading `request.jwt.claims`) so the compiled DDL is tested unchanged.

## Implementation defaults recorded during Milestone 2 (2026-11-02)

**D28. Migrations are seven ordered phases; enforcement is never weaker than v1 nor stricter than
v2 in between.** (1) additive schema in one transaction; (2) new and changed CHECKs/FKs as
`NOT VALID` plus new triggers, one transaction; (3) `CREATE INDEX CONCURRENTLY`, autocommit;
(4) `VALIDATE CONSTRAINT`, one statement per transaction; (5) the swap, one transaction: policies
dropped and created, grants and revokes, changed constraints renamed into place, removed
constraints and triggers dropped; (6) `DROP INDEX CONCURRENTLY`; (7) destructive drops, only
with `--allow-destructive`.  Validation precedes the policy swap because the proofs assume stored
rows satisfy the constraints.  A changed CHECK/FK keeps its name through `<name>_mls_new` →
validate → drop old → rename.  Column type changes, primary-key changes and identity-adapter
changes are refused with a JSON path and a fix (the data migration is not MLS's to generate).
New NOT NULL columns get no default; pre-flight reports the row count that would make
`ADD COLUMN` fail.  Rejected: a single-transaction migration (holds ACCESS EXCLUSIVE across the
validation scans); `CREATE INDEX` without CONCURRENTLY (SHARE lock blocks writes for the build).

**D29. Lock levels are observed, not assumed.** `compile/migrate.py` documents the lock mode each
statement kind holds on its table until the end of the transaction, and
`tests/templates/test_locks.py` reads them from `pg_locks` on Postgres 15–18 for every kind
(CONCURRENTLY statements are observed as waiting requests behind a SHARE lock held by another
session).  Finding: GRANT/REVOKE hold no relation lock past the statement; CREATE/DROP POLICY,
ADD CONSTRAINT (CHECK), DROP/RENAME CONSTRAINT, ADD/DROP COLUMN, SET/DROP NOT NULL, ADD UNIQUE,
DROP TRIGGER and RLS changes hold ACCESS EXCLUSIVE; ADD FOREIGN KEY NOT VALID and CREATE TRIGGER
hold SHARE ROW EXCLUSIVE; VALIDATE CONSTRAINT and the CONCURRENTLY index statements hold
SHARE UPDATE EXCLUSIVE.  See `docs/MIGRATION.md`.

**D30. Drift compares catalog output of a shadow deploy on the same server.** `mls drift` creates
a throwaway schema inside one transaction, runs the compiled script into it (roles excepted:
they are global and their attributes are compared directly), snapshots both schemas with
`pg_get_expr`, `pg_get_constraintdef`, `pg_get_triggerdef`, `pg_get_indexdef`, `prosrc` and
`aclexplode`, then rolls back; the server is left untouched.  Reported: missing, differing and
foreign objects on MLS tables (D8), un-validated constraints, privileges, and the D13 hardening
(RLS enabled and forced, owner not an app role, no BYPASSRLS/superuser, no SET on
session_replication_role).  Tables the spec does not mention are notes, not drift.  Rejected:
comparing generated strings with the catalog (deparsing differs across versions and settings).

**D31. Pre-flight evaluates the compiled `IS TRUE` predicates, negated, in a READ ONLY
transaction.** Columns the new spec adds are substituted by NULL (what every row holds after
`ADD COLUMN`), relation tables that do not exist yet count as empty, and NOT NULL / UNIQUE
additions are checked too.  Only constraints that are new or changed relative to the deployed
spec (`--old`) are evaluated.  Samples are capped (`--samples`).

**D32. Access diff semantics.** Symbols shared between versions: columns by name, context values
by name, relations with an identical definition, opaque blocks with identical SQL; a relation or
opaque block whose definition changed is a different symbol in v2.  Rows are assumed to satisfy
the stored-row constraints of *both* versions, so a constraint change is reported once, by the
integrity diff, and not again as an access change.  Each (table, role, command, side) yields two
obligations (granted: `v2 ∧ ¬v1`; denied: `v1 ∧ ¬v2`); a model is rendered as a scenario in
English.  Every obligation that depends on an opaque block is flagged (D3).  A table absent from
one version has no access in that version.

**D34. Helpers run as an internal role, not as the owner (M3).** Hierarchy reachability, the
depth trigger and the counting trigger must read rows the calling role's policies would hide.
Rather than `SECURITY DEFINER` as the schema owner (D21 forces RLS on the owner, and the owner
may be a superuser in development), the compiler creates `mls_internal` (NOLOGIN, NOBYPASSRLS,
NOINHERIT, no app role a member), transfers ownership of every generated helper to it, and gives it
explicit `USING (true)` policies on exactly the tables the helpers read (`FOR ALL` on cardinality
parents it locks).  Its privileges are visible in the catalog and drift checks them (login,
membership, helper bodies, owners and EXECUTE grants).  `grant mls_internal to current_user` is
emitted so the deploying role may transfer ownership; it is harmless for the owner (NOINHERIT).

**D35. Hierarchy template (M3, refines D5).** The spec declares the depth column and the bound;
the compiler maintains the depth (BEFORE INSERT OR UPDATE trigger, SECURITY DEFINER), bounds it
(`CHECK ((0 <= depth and depth <= d) IS TRUE)`), fixes the parent (implicit NULL-aware transition
rule `mls_h_<h>_parent_immutable`) and adds the two FKs (parent → id, members value → id).
`in_group` compiles to `col = any ((select mls_reach_<h>(key))::type[])` — the cast is required,
`any ((select f()))` parses as a subquery-ANY — where `mls_reach_<h>` is a STABLE SQL function
with a recursive CTE descending from the user's groups, bounded by the level counter (mutation
HIERARCHY_SHALLOW stops one level short; HIERARCHY_NO_DEPTH_CHECK drops the CHECK).  The proof
unrolls exactly `max_depth` parents.  Reparenting stays v2.  The EXPLAIN loader builds the tree
level by level (node i has parent i / 10), because BEFORE triggers cannot see rows of the same
statement.

**D36. Cardinality template (M3, refines D4).** NULL groups are not counted; an UPDATE that keeps
the group skips the check; the trigger locks the parent row `FOR UPDATE`, then counts; proofs and
reports assume READ COMMITTED (under REPEATABLE READ the count after the lock reads a stale
snapshot).  Mutations: CARDINALITY_OFF_BY_ONE (`count > max`) is caught by the differential test;
CARDINALITY_NO_LOCK is caught only by the parallel-writer test (`tests/templates/test_cardinality.py`:
8 writers × 6 attempts against max 5 end with exactly 5 rows with the lock and more without it).
Pre-flight counts the rows of over-full groups; the differential oracle (`pg/model.py`) implements
the same rules.

**D37. The LLM loop is two independent passes judged by the solver (M4, refines D16).** Pass 1
(requirements → properties) and pass 2 (requirements → rules) share the system prompt material
(language reference, the fixed skeleton: identity, roles, tables, relations, hierarchies, opaques,
and the JSON Schema of the answer) but neither sees the other's answer.  Decoding is *not*
constrained (a deviation from D15's intent, forced by the API): structured outputs reject recursive
schemas and the predicate grammar is recursive; constraining only a free-form envelope is worse,
because nested values are then forced to scalars.  So the schema is in the prompt, the answer is
parsed tolerantly (bare JSON, a ```json``` block, or the first JSON object in prose) and validated
locally with the pydantic models; `AnthropicClient(use_schema=True)` sends the schema anyway for an
API that accepts it.
Repair is bounded (`--max-repairs`, default 3, counted over every kind of repair): structural
errors and checker errors (JSON path + fix, invariant 7) go back to the pass that produced them;
compile-time soundness errors and refuted properties go to the rules pass, as the property in
English plus the counterexample scenario in English (D10) — never as MLS or SMT.  Properties are
never repaired after a refutation: they are the approved artifact.  Whatever is still refuted when
the budget is spent — or when the rules pass returns its rules unchanged, i.e. stands by them
against the property (in the first live run it correctly argued the property was wrong) — is
escalated to a human as (property, verdict, scenario, the rules pass's notes).  Status is
`proved | escalated | invalid | failed`.

**D38. Evaluation grades the generated rules by net-effect equivalence (M4, refines D6).** A case
is requirements + expected properties (English) + reference spec + expected verdicts.  Every
case's requirements are self-consistent, so the loop is expected to prove its own properties; a
"bad" seed case (e.g. a requirement that lets closed tickets be reopened) is one whose *reference*
properties are REFUTED by design, and the generated rules must reproduce exactly those verdicts.
A generated spec is correct when (1) the loop ends `proved`, (2) the reference properties (rule
references replaced by their predicates, FKs included) have their expected verdicts on the
generated rules, and (3) every role can do exactly what the reference lets it do: the net-effect
diff (`props/diff.py:behaviour_diff`: policies, stored-row constraints, transition rules and
column privileges taken together, per table × role × command, both directions) plus the grant
diff finds nothing.  The layer-by-layer access/integrity diff of Milestone 2 is reported but does
not decide: enforcing "closed tickets are never modified" as a transition rule instead of a
restrictive policy is not a mistake (the first live run showed the model doing this routinely),
although filtered-vs-error outcomes differ.  Other metrics: repair rounds, human escalation rate.
The 33 seed cases (`evals/make_cases.py`, 11 schema families) are the author's rendering of common
field patterns, not David's anonymized corpus yet; every intended verdict is confirmed by the
solver when the generator runs, and a test reproduces the committed files.  The first live run
showed that this grading measures the requirement text as much as the model: every sentence that
admits two readings (does "update within clearance" bound the old row or the new one?) must be
made precise before a mismatch counts against the model.

**D39. Synthetic mutations are solver-graded and double as a coverage score.** Each rule of a
reference spec is dropped, loosened (one conjunct removed), made permissive or given a higher
limit; a mutant the checker rejects is discarded; a mutant on which a property the base proves no
longer proves is a new case (a weakening the property set detects); the rest — including a
mutant that only makes a refuted property prove, i.e. drops a planted leak, which leaves nothing
to repair — are reported as undetected.  The share detected is a mutation score of the property set.
Properties are re-based on predicates before mutating (as in D38), so dropping a CHECK or FK whose
invariant a property names is detected rather than silently removed from the property.  A synthetic
case is a repair benchmark: it keeps its base case's requirements, reference and verdicts, carries
the mutant rules as `start_rules`, and `mls eval --llm --repair` starts the loop from them; the
loop must end `proved` and net-equivalent to the base reference.  Without `--repair` synthetic
cases are skipped (they would repeat their base case).  Current corpus: 75 % (213/283) with
`must_allow` properties in every reference (D40); 38 % before them, when the templates were all
safety properties (upper bounds on access) and removing a grant was rarely caught.  Measured live
over the 100 cases that existed before D40: 99 proved and net-equivalent after 1.02 repair rounds
on average, none escalated (LLM.md).

**D40. `must_allow`: the lower bound on access (liveness).** Every other template is a safety
property, an upper bound on what a role can do, so a rule set that denies too much proves
everything: a dropped grant, or the help-desk reference whose restrictive "closed tickets are
frozen" policy lacked WITH CHECK and so made closing a ticket impossible (found only by diffing
against the model's output).  `must_allow {table, role, command, when}` states that every row —
for update, every pair of images — that satisfies `when` and the stored-row constraints is admitted
by the command's access layers: the effective policies, the transition rules and the column
privileges, so a policy, a transition rule and a column rule that forbid the same change refute it
the same way.  *Amended the same day after the first live run:* the written image is assumed valid
(it satisfies the table's CHECKs and FKs) rather than proved valid.  With the constraints in the
goal, a lower bound had to restate every CHECK of the table in its condition; the model never did,
its natural "a member can insert a document in their tenant" was refuted by the level-range CHECK,
and the rules pass either stood by its rules and escalated or dropped the CHECK to satisfy the
bound — four of thirty seed cases.  Now the condition states what the role can do; an
over-restrictive CHECK surfaces as vacuity in the witness obligation, with its message.  The net
effect the behaviour diff compares (D38) keeps the constraints; only the lower bound drops them.
It is universal, hence decidable in the same fragment as `custom` (same encoding, implication
reversed).  The existential part — that `when` is satisfiable — is a witness obligation whose model
is reported as an example (the "allowed" scenarios D10 asks for come from here); a condition no row
can meet refutes the property, because a liveness property that requires nothing is a mistake
(usually a NULL-unsafe condition).  `when` on update ranges over `old`/`new`, a bare column is the
new image, and the author states the whole change (what changes, what stays).  The properties
prompt asks for one `must_allow` per grant the requirements state.  Mutation score of the seed
corpus: 38 % → 75 % (213/283).  What stays undetected is now mostly a mutation that does not change
behaviour (a rule redundant with another policy, with the implicit relation grant of D21 or with an
FK), a cardinality rule reference (not re-based by `portable`), or a grant the author's property
set does not state.

**D41. The Lean track lives in `lean/` and mirrors the Python by hand.** A Lake project in this
repository (core Lean 4 pinned in `lean/lean-toolchain`, no Mathlib: the objects are finite truth
tables, booleans, integers and an inductive syntax, and the proofs are case analysis), built by
its own CI job.  Keeping it next to the code is what makes the structural match enforceable:
`lean/MLS/Pred.lean` defines the encoder clause for clause after `logic/tvl.py` (the `FALSE` null
flag of a non-nullable column, `(ok ∧ core, ok ∧ ¬core)` for comparisons, the two-valued
`now_between`, the `(T, F)` pair of an opaque block) and proves it sound against the reference
meaning of SEMANTICS §1–2 (`Pred.eval`) under exactly the assumptions the toolchain makes: the
schema (a non-nullable column holds no NULL) and the opaque side condition (`¬(T ∧ F)`).  That
theorem is the universal form of `tests/logic/test_tvl.py`, which samples the same statement
against the concrete evaluator.  `lean/MLS/Policy.lean` does the same for `props/effective.py`
(policy combination), for the statement outcomes of `pg/model.py` and for the assumption
builders of `props/obligations.py`: the outcome model is transcribed, and the theorems say that
every executed statement satisfies the proof assumptions and that, without column reads,
"executed" is "admitted by the net-effect predicate" (D38).  `lean/MLS/Normalize.lean` states how
each target decides on a three-valued result and proves the normalization of SEMANTICS §4: with
`IS TRUE` every target admits what the admission rule admits, without it they disagree exactly
on UNKNOWN (the `*_NAIVE` mutations).  `lean/MLS/Relational.lean` is the "small model of
Postgres" D11 asked for: states, backed relations, one-row statements, and the CHECK and FK
templates proved to keep their invariants under every statement, with the FK RESTRICT guard and
the relation-change axioms of the invariant-preservation obligations derived from it.  The Lean
and the Python are two artifacts whose only mechanical link is the shared test vectors of D43;
a change to the encoder is carried to the transcription by hand, or the gap is recorded in the
ROADMAP.  What the track cannot do: say anything about Postgres itself — the "small model of
Postgres" of the later steps is our own axiomatization, and the differential tests remain the only
link to the real thing (D11).  *Amended (issue #4):* the transition trigger
(`lean/MLS/Transition.lean`), the compiled hierarchy helper and depth trigger
(`lean/MLS/Reach.lean`) and the cardinality trigger over a bag of rows
(`lean/MLS/Cardinality.lean`) are in the track since step 5 (ROADMAP).  What stays outside it,
by nature: that `FOR UPDATE` under READ COMMITTED serializes the writers of a group (the model
takes the serial schedule as the assumption the proof report states) and that the
`SECURITY DEFINER` helper sees every row (D34) — the concurrency test and the Postgres
differential tests remain their evidence.

**D42. Row-dependent column visibility is modelled by normalization; there is no masking
construct (settles the v2 question of D24, GitHub issue #3).** A column rule is per (role, table);
"staff see their own salary but only the titles of their colleagues" is per row.  MLS adds no
construct for it: a column whose visibility depends on the row is its own table — one row per
parent row, the parent's key as primary key, the columns the row predicate needs (`user_id`,
`tenant_id`) and, when a property needs the link, an inclusion rule onto the parent's relation —
with its own row policies; the parent table keeps the columns everyone may see.  Every existing
layer then covers it with its one meaning: the row predicate is a policy `using`, a column rule on
the child says who may write it, `custom` and `tenant_isolation` are the upper bound ("staff see no
salary but their own"), `must_allow` the lower bound, and the net-effect diff, drift and migration
need nothing new.  Evidence: `cr_own_salary` and `cr_salary_leak` in the eval corpus (every verdict
confirmed by the solver, the leak refuted with a counterexample, the compiled spec at 0 mismatches
in the differential test on PG 16); the rules prompt states the modelling rule.
Rejected: a `security_invoker` view per (role, table) with `CASE WHEN (p) IS TRUE THEN col END`,
and its filtered variant (`WHERE (p) IS TRUE ... WITH CHECK OPTION`).  Each reason is a conflict
with a settled decision, not a cost: (1) a masked value is a NULL in a NOT NULL column, and "a
non-nullable column holds no NULL" is an assumption of the encoder (D41: the FALSE null flag),
of the Lean soundness theorem, of the differential oracle's value domain and of every proof;
reads through the view would need a second value domain ("masked") in every predicate a client
evaluates, and `write_subset_read` / `must_allow` a second meaning of "can read" (invariants 1
and 2 demand one).  (2) Two relations per table for one role: the role must lose SELECT on the
base column (or the view is bypassed), so reads go to the view and writes to the table (a CASE
column is not updatable; an INSERT or UPDATE through the view that assigns it errors), and the
spec's `tables` would no longer be what the application addresses — grants, drift, migration lock
modes, `explain` and the harness would all acquire "the relation a role reads", a change to the
meaning of a table (D15), not a template.  (3) Decidability is not the obstacle (the row
predicate is a policy predicate); soundness of the filter reading is: Postgres cannot deny a
column per row, so "no SELECT on the column unless p" has no target but two relations, which is
the normalization again.  Cost of the decision: the schema is designed for it (D8: MLS owns the
schema; the language reference tells the model) and a client reads the salary with a join.  If a
later version needs masking for a schema MLS does not own, it is a new kind of relation ("the view
a role reads"), to be decided against D15 and D26, not an option on a column rule.

**D43. Shared test vectors are the mechanical link between the Lean transcription and the
Python (amends D41).** `tests/lean/vectors.py` generates `lean/MLS/Vectors.lean` from a fixed
seed: random predicates over the harness spec of `tests/logic/test_tvl.py`, rows, contexts and
relation facts; random forests for `in_group`; random policy sets with a check and a transition
rule for the statement outcomes.  For each vector the generator records what the Python computed
— the evaluator's truth value (`pg/model.py:eval_pred`), the encoder's pair under the harness
valuation (`logic/tvl.py:Encoder.tf`, evaluated as the sampling test does), the oracle's outcome
(`pg/model.py:Oracle`) — and emits a `#guard` on the Lean transcription (`Pred.eval`, `Pred.tf`,
`Rules.visible/insert/update/delete`).  The generator asserts, while generating, that evaluator
and encoder agree (the universal form of that agreement is `Pred.tf_sound`); the fast suite
asserts the committed file is what the generator produces; the Lean build asserts the guards.
Why vectors and not a generated Lean term per spec: the theorems of the track are about the
*definitions* (`Pred.tf` transcribes `Encoder.tf`), so what needs checking is that the two
definitions compute the same function, and sampled agreement on the same inputs is the test
that a definition-by-definition transcription can be given without a Lean toolchain on the
Python side or a Python interpreter on the Lean side.  Limits: sampled, not universal; the
outcome vectors keep to the layers the Lean models (every policy set has a permissive policy
for the command and for select, so the oracle's grants are table level; no FKs, uniqueness or
cardinality on the harness table); constants are integers (text and uuid values are interned
to integers as the encoder does).  A new construct in `Pred` or a new outcome rule gets its
vectors in the same generator.

**D44. A terse rendering of the JSON syntax, for reading only (issue #2; D10, D15, D33
stand).** `mls/terse.py` renders predicates, rules, properties and scenarios one line per item:
`member_of(@uid, tenant_id) and now_between(-, expires_at)`, `R9 transition docs: new.tenant_id =
old.tenant_id`, `P1 tenant_isolation docs: tenant_id in member_of(@uid) for select, insert,
update`.  Grammar: a term is `column`, `old.column`, `new.column`, `@context`, a quoted text
constant or a number; atoms are `term op term` (the JSON operators), `term is null`,
`relation(key, value)`, `in_group(hierarchy, key, value)`, `now_between(from, until)` with `-`
for an open bound, `opaque(name)`, `true`/`false`; `not` binds tighter than `and`, `and` than
`or`, and parentheses appear only where needed.  Rules read `ID kind table: ...` (a policy adds
role, command, mode and the `using`/`check` clauses it has), properties `ID template [table role
command which]: ...`, a scenario `@uid = uuid#1; old: docs {tenant_id = 1}; member_of = {(uuid#1,
1)}`.  Where it is used: `mls explain --terse`; the `before`/`after` lines of every access and
integrity change in `mls diff` (the effective predicate, the stored-row constraints or the
transition rules of each version — the English says *that* access changed, the terse lines say
*what* changed); and `spec_terse` in the transcripts of `mls generate` and `mls eval --llm`, so a
transcript can be read without expanding the JSON.  It is rendering only: nothing parses it, the
JSON stays the only input (D15) and the English stays the approval artifact (D10).  Why one
more rendering: the English is written for a product owner and deliberately verbose; a reviewer
reading a diff or a transcript wants the predicate at a glance, and the JSON is three lines per
atom.  The golden test (`tests/test_terse.py`) pins every construct of the verified fragment. *Amended
(D50):* terse is a concrete syntax as well as a rendering; the JSON stays canonical.

**D33. English rendering is a fixed function of the spec (D10, D15).** `mls explain` and the diff
and proof reports use `mls/english.py`; the same construct always reads the same way, so a
reviewer can compare the rendering with the requirement text, and the Milestone 4 loop can check
an LLM's property statements against it.

## Milestone 5 — brownfield (2026-11-04)

**D45. Ownership is per table; D8 is split (amends D8).** D8 bundled two claims: MLS owns the
schema it creates, and MLS imports no hand-written policies. The first is what makes the
compiler sound (constraints created validated, FKs RESTRICT, owner not an app role,
`mls_internal` present) and stays, scoped to the tables it is true of. The second is dropped
through D3: a policy MLS did not write is an opaque block, which the proofs were built to
tolerate. Each table declares `"mode": "owned" | "adopted" | "observed"` (default `owned`, so
every existing spec is unchanged):
- `owned` — as today. Foreign objects are drift; hardening is emitted and verified.
- `adopted` — the table exists. `columns`, `primary_key`, `unique` and inclusion FKs are read
  from the catalog by `mls import` and checked against the catalog by `drift` (a column the
  spec declares that the table lacks is an error at `check --dsn` and drift otherwise). MLS adds
  rules to it; every policy, trigger, CHECK and FK already on it is either lifted (D46), declared
  as an opaque block with its catalog signature, or listed as *foreign and unmodelled* in the
  proof report's assumptions. Hardening the compiler cannot establish without changing something
  it does not own — the owner is an app role, a role has `BYPASSRLS`, RLS is not forced — is not
  emitted; it is recorded in the spec as `"assume": [...]` on the table, repeated in every proof
  report as a trusted-role assumption (D13's list grows by it), and reported by `drift` under a
  new kind `assumed`, so that a hardening fix later shows up as the assumption going away.
  Refused: a table-level `assume` that the proofs cannot state (there is no assumption under
  which a policy on a table without RLS enabled means anything; `adopted` without RLS enabled
  is `observed`).
- `observed` — in the schema, mentioned so that coverage (D49) and `status` can speak about it.
  No rules may name it.
`mls import --dsn DSN [--schema public]` writes a spec in which every table is `adopted`, the
identity adapter is guessed from the policies (`auth.uid()` → `supabase`, `current_user` →
`pg_roles`, `current_setting('app.*')` → `session_setting`, D47) and reported when ambiguous,
relations are proposed for every table whose policies read it as `(key, value)`, and every policy
is first lifted (D46) and otherwise an opaque block. The import is a starting point the human
or the loop edits; the spec, not the catalog, remains the source of truth from then on (D15),
and `drift` is what says whether they still agree. Rejected: a third mode "managed by MLS but
not owned" (every example collapsed to `adopted` plus an assumption); importing into the
compiler's object model instead of the spec (the LLM loop and the English need the spec).

**D46. The lifter: catalog expressions → the verified fragment, else opaque.** `pg/lift.py`
parses the `pg_get_expr` text of a policy's `using`/`with check` (and of a CHECK's
`pg_get_constraintdef`) into a predicate when the expression is built from the shapes the
compiler emits — `col op (select <ctx>)`, `col = any ((select …)::t[])`, `col = any (array
(select value from rel where key = (select <ctx>)))`, `col is null`, `(p) is true`, `and`/`or`/
`not` of these — and from a fixed list of hand-written idioms: a bare `auth.uid()` or
`current_setting('…')` without the InitPlan wrapper (lifted, and a lint says the index-friendly
form is `(select …)`), `col in (c1, c2, …)`, `col = any (array[…])`, `exists (select 1 from rel
where rel.key = <ctx> and rel.value = t.col)` (the correlated form, lifted to `member` and
flagged by the EXPLAIN lint as the 332 ms shape), `col like 'const%'`. The lifter is total: an
expression it cannot read becomes an opaque block whose signature is the set of columns and
context sources the expression references (read from the catalog's dependency information and a
conservative scan of the text), so a lifted spec always `check`s. Evidence it must carry: a
round-trip test per template (`tests/templates/test_lift.py`): compile a fixture, deploy,
import, lift; the lifted rules and the source rules must be net-equivalent per
`props/diff.py:behaviour_diff`, and `mls drift` of the lifted spec against the deployment must
be clean, on PG 15–18; plus one fixture per hand-written idiom with the expected lifted
predicate pinned in a golden test. Deparsing differs across versions (D30), so the parser works
on the deparsed text of each version in the matrix and the golden files are per-shape, not
per-string. Why lift at all, given D3: a proof conditional on an opaque block says nothing a
reviewer can act on; a lifted policy is proved, diffed and explained like one MLS wrote, which is
the whole value of adopting. Limits: a policy that calls a user function stays opaque even when
the function is a one-line SQL wrapper — the lifter reads `pg_proc` only to build the signature,
never to inline (a SECURITY DEFINER body is exactly the object D34 says must be visible, not
trusted). Rejected: lifting through `pg_node_tree` (`polqual`) instead of the deparsed text —
version-specific and undocumented; a `--lift-functions` option — reopens D3.
*Implementation defaults (2026-11-05):* the lifter works at the boolean connectives — a
conjunct, disjunct or negand it cannot read becomes an opaque block of its own text, so a
single user function does not hide the rest of the policy; every lifted atom is validated with
the spec checker before it is kept. The compiler's own transition trigger (`mls_tr_<table>_
<name>`, the fixed body of `compile/transition.py`) is read back as a transition rule, because
the acceptance criterion (an empty `behaviour_diff` against the source) needs the transition
layer; any other trigger is listed. A CHECK is a check rule when it is the template's `(p) IS
TRUE` or when it is bare over NOT NULL columns only (then the two forms mean the same; drift
reports `differs` until a deploy rewrites it); a bare CHECK over a nullable column is listed,
since it passes on NULL where a check rule rejects. The compiler's relation-visibility
policies give an imported relation its name back, and those policies and the relation grants
are not imported as rules (the compiler regenerates them). A membership proposes its relation
only when the pair is a key of a governed table. `NOT IN (c1, ...)` deparses as `<> ALL`, read
as the negated `in`. The generated index set is made order-independent (a key that is a prefix
of another wanted key is dropped) so that an imported spec, whose rules are in catalog order,
compiles to the indexes of the source.

**D47. `session_setting` identity adapter (amends D1).** `current_setting('app.<name>', true)`
as a context source, typed by the spec, wrapped `(select … ::t)` for InitPlan evaluation, NULL
or missing setting → every comparison UNKNOWN → denied (3VL does the right thing without a
special case; the English says "no context set: denied"). D1 rejected this as identity because
any client can `SET` it; the rejection stands for a direct connection and the adapter states the
trust assumption D1 already accepts for Supabase, in its own words: *clients cannot execute `SET`
on `app.*`; the setting is established by a trusted path (a pooler, a `SET LOCAL` inside a
SECURITY DEFINER entry point, or the application's connection layer) that no untrusted client
reaches.* Hardening (D13) adds, for this adapter only, a drift check that no app role has
`EXECUTE` on a function that sets the namespace and that `app.*` has no `ALTER ROLE … SET`
default for an app role. The harness sets the value with `set local` inside the transaction, as
the `pg_roles` harness sets the role. Why now: every pre-Supabase schema `mls import` meets uses
it, and without the adapter every such policy is opaque and brownfield is a listing exercise.
Rejected: treating `current_setting` as an opaque context value with no adapter — the proofs
need to know it is constant within a statement and typed.

**D48. Selectors are checker-time expansion (`applies_to`), not a construct (D15 stands).**
A rule may carry `"applies_to": {"has_column": [name] | [name, type], "in_schema": s,
"named": [t, …], "tagged": tag, "all": true, "and": […], "or": […]}` in place of `"table"`.
`spec/check.py` expands it, before typechecking, into one rule per matching table (`owned` or
`adopted`; `observed` tables are matched for coverage, D49, but no rule is generated), with ids
`R1.docs`, `R1.files`, and a `"from": "R1"` back-reference; everything downstream — the solver,
every template, `diff`, `migrate`, `drift`, `english.py`, `terse.py`, `lean/` — sees only
concrete rules, so nothing in them changes and the Lean track is untouched by construction.
Tables gain `"tags": [...]` (strings, no semantics) for `tagged`. A selector that matches no
table is an error at `check` ("R1 applies to no table"), because a silent empty expansion is
how a renamed column disables a tenant policy. Late binding, the property `pg_more_ls` wanted
from selectors (a new table with `tenant_id` is covered without a new policy), becomes: re-run
`mls import` (or `check --dsn`, which refreshes `adopted` tables), the expansion picks the table
up, and `mls lint` names any `observed` table the selector would have matched. `explain` renders
the selector once and the expansion as a list; `diff` reports changes per concrete rule. The
LLM loop may emit selectors (the schema allows it); the eval corpus gains cases where the
reference uses one and the generated rules may use either form, since grading is by net effect
(D38). Rejected: selectors as a solver-visible quantifier over tables (the fragment would need a
sort of tables; nothing is gained, since the set of tables is finite and declared); selectors
resolved against the catalog at compile time rather than against the spec (the compiled DDL
would depend on something the spec does not say; D15).
*Implementation defaults (2026-11-05):* an expanded rule keeps `applies_to` next to `table` and
`from`, so that `explain` can print the selector once without a side table; nothing else reads
it. Errors inside an expanded rule report under the selector rule's JSON path. `in_schema` is
not implemented: a table of the spec carries no schema (the schema is a deployment argument,
`--schema`), so the selector would have nothing to match against; it returns when tables do.
An `invariant_preserved` reference to a selector rule becomes one reference per expansion; the
eval mutants operate on the unexpanded reference, so a mutation of a selector rule is one
mutant for all its tables.

**D49. Policy-set hygiene is lint, not proof.** `pg_more_ls` had nine proofs; four already exist
here as templates or as `diff` (tenant isolation, write ⊆ read, policy equivalence, role
separation as `custom`), and the remaining four are facts about the shape of a policy set that
do not change what any role can do. They are `mls lint` findings, each with a positive and a
negative fixture:
- *coverage* — an `owned`/`adopted` table and command with no permissive policy for a role that
  has a grant, and an `observed` table a selector would match (D48), or that has a column
  another table's `tenant_isolation` property names;
- *contradiction* — a policy whose `using` (or `check`) is unsatisfiable under the stored-row
  constraints, i.e. the witness obligation of D40 applied to every policy; the English says
  "this policy never admits a row";
- *subsumption* — permissive `p1`, `p2` on the same (table, role, command) with
  `T(p2) → T(p1)` valid over the (T, F) encoding; the two-valued check `pg_more_ls` ran
  (`p2 ∧ ¬p1` UNSAT) is wrong where `p1` is UNKNOWN on a row `p2` admits — `tests/logic` keeps
  such a case;
- *redundancy* — a policy whose removal leaves the effective predicate (`props/effective.py`)
  equivalent for every (table, role, command, side); an opaque block in either policy makes the
  finding conditional, as proofs are.
`role_separation {table, roles: [a, b], command}` joins the templates as the English form of
the `custom` property it already was (no row admitted for both). None of these enters the
proof report's verdicts or the eval grading (D38 grades net effect, which they do not change);
the LLM loop sees them as hints in the repair prompt, never as refutations. Why lint: a
redundant policy is not a defect of the spec's meaning, and `pg_more_ls` conflating hygiene with
safety made its exit code mean two things.
*Implementation defaults (2026-11-05):* `props/lint.py`; `mls lint` prints the findings after
the leakproof warnings and still exits 0. Coverage's "grant without a policy" is, in MLS terms,
a column rule whose role has no permissive policy for the access it limits (every other grant
is derived from a permissive policy); a governed table with no permissive policy at all is
reported too. Subsumption compares policies whose commands lie within the other's, on every
side the commands evaluate; two policies that admit the same rows are one finding, as a pair.
A contradictory policy is within anything and trivially redundant, a subsumed one is redundant:
each policy is reported once, under the most specific kind. `role_separation` evaluates both
roles with the same context values (one user, either role). Hints reach the LLM loop only in a
proof-driven repair request.

## Syntax (2026-11-04)

**D50. The terse rendering becomes a concrete syntax; JSON stays canonical (amends D44, D15
stands).** D44 made `mls/terse.py` a rendering only, because D15 wanted one input and JSON Schema
was to constrain the LLM's decoding. D37 then recorded that constrained decoding is impossible
(recursive schema), so today the schema is pasted into the prompt, the answer is free text,
parsed tolerantly and validated locally — the pipeline a text syntax would use, with a longer
grammar in the prompt. Meanwhile D44 exists because the JSON was unreadable in diffs and
transcripts, and every construct of the fragment already has a terse form pinned by a golden
test. The remaining distance is a parser. So:

- **Two concrete syntaxes, one model.** The pydantic model (`spec/model.py`) is the language;
  `SEMANTICS.md` gives it meaning; JSON and terse are both views of it. `mls` reads either by
  extension (`.mls.json`, `.mls`) and `mls fmt` converts between them. JSON remains the
  canonical serialization: `mls schema`, the eval cases, the synthetic mutator, the Lean vectors
  generator and every program that manipulates a spec keep working on the model or its JSON and
  need no change. The terse grammar is not the definition of the language; a construct exists
  when the model has it, and the same change adds it to both syntaxes (CLAUDE.md's rule for
  GUIDE and REFERENCE applies).
- **The grammar** is D44's, completed to the whole spec. A file is a sequence of sections:
  `identity supabase { uid: auth.uid uuid; org: jwt.claims.org_id int }`, `roles authenticated,
  staff`, `table docs owned { id uuid not null, tenant_id int not null, …; primary_key (id);
  unique (tenant_id, slug); tags (pii) }` (D45's mode and tags, `assume` as a list of quoted
  strings), `relation member_of = memberships(user_id, tenant_id)`, `hierarchy org_tree = orgs
  id id parent parent_id depth depth max_depth 6 members member_of`, `opaque legacy_flag on docs
  (archived, @uid) relations () sql $$ archived = 0 $$` (dollar-quoted raw SQL, as Postgres
  does), then `rules` and `properties` blocks with D44's one-line forms, `applies_to
  has_column(tenant_id uuid)` in place of a table name for D48, and `# comments`. Terms,
  atoms, precedence and parenthesization are D44's; `in (c1, c2)` is the Milestone 5 sugar.
  Keywords are reserved; a column or table named like one is written `"quoted"`. Every item is
  one line unless it holds a parenthesized list, so `git diff` shows one changed rule as one
  changed line.
- **Round trip is the correctness evidence.** `parse(render(spec)) == spec` for every example,
  every eval case (seed and synthetic) and hypothesis-generated specs (`tests/test_terse.py`
  grows the parse direction; `tests/spec/test_syntax.py` the generator); `render(parse(text))`
  is idempotent on the committed `.mls` examples (the formatter is the renderer). A construct
  with no terse form fails the fast suite, as a construct with no golden line does today.
- **Errors keep invariant 7.** The parser records a source span per node; the checker's errors
  carry the JSON path as today *and*, when the input was terse, `file:line:col` and the
  offending line, so the LLM and the human see where in the text they wrote. Internally the
  JSON path is the key; the span is looked up from it.
- **The LLM loop gets a syntax flag, and the corpus decides the default.** `mls generate
  --syntax json|terse` and `mls eval --llm --syntax …` choose what the rules pass and the
  properties pass emit (the prompt carries the grammar and three examples instead of the JSON
  Schema; the skeleton is rendered in the same syntax; repair feedback is unchanged — English
  and scenarios, D10/D37). The answer is parsed with the same tolerance (a fenced block, or the
  first section keyword in prose). Acceptance for changing the default: the full corpus (30 seed
  cases, 194 repair benchmarks) run with both syntaxes on the same model, reporting proved rate,
  net-equivalence, repair rounds, escalations and prompt/completion tokens, written into
  `docs/LLM.md`. The default becomes terse only if proved and net-equivalent rates are not worse
  by more than the run-to-run spread already recorded there; token reduction alone does not
  decide. Transcripts keep `spec_terse` either way.
- **What does not change.** D10: the English is the approval artifact; nobody is asked to read
  terse to approve anything. D15: the JSON Schema is still exported and still what tools
  constrain against. D33/D44: the renderers are fixed functions of the model. The Lean track
  sees neither syntax.

*Implementation defaults (2026-11-05, the renderer):* `terse.render` is the whole-spec form
and `spec_listing` its D44 name. A rule line carries everything the model holds, so that the
round trip can hold: `ID kind name on table: body requirement '...'`, the name between the kind
and `on` (`R1 policy member_read on docs:`; a column rule may have none), `applies_to sel` in
place of `on table` on a selector rule, `on table from ID` on its expansions (the selector is
not repeated: a checked spec is not an input), `requirement` and a property's `description`
as trailing quoted clauses. The file starts `mls 0.1`; `$comment` is `comment '...'` (a
`# comment` is discarded by the parser, the author's note is data). Tables are the one
multi-line item (one column or option per line, `,` between columns, `;` before each option).
`id`, `parent`, `depth`, `members` are positional in a hierarchy and not reserved, so the
common column names stay bare; the reserved words are the section, clause, kind, template,
command, mode and predicate words (`terse.RESERVED`). `in` renders as written on an unchecked
spec and as its expansion after `check`. `tests/test_terse.py` compares the model's fields and
union members with what the renderer declares it covers (`RENDERED_FIELDS`, `RENDERED_CLASSES`),
which is how a construct without a terse form fails the fast suite.
*Implementation defaults (2026-11-05, the parser):* `spec/syntax.py` builds the JSON shape and
validates it with the model, so a schema error from a `.mls` input has the same JSON path as
from JSON; a syntax error is `syntax at <partial path>: file:line:col: expected ..., found
...` with the offending line. Spans are recorded per JSON path (`parse_data`) for the
source-span errors. Sections may come in any order; the formatter (`mls fmt --to terse`, the
renderer) writes the fixed order. The round trip is exact except for a one-element `and`/`or`
(it reads back as its element; nothing in the corpus has one), and a nested group of the same
connective keeps its parentheses so that it does. `mls fmt` to JSON writes the model's dump
(aliases, no nulls, defaults spelled out). Every example has a committed `.mls` twin, checked
against the renderer by the fast suite.
*Implementation defaults (2026-11-05, source spans):* `MlsError.span` (`errors.Span`: file,
line, col, the source line) is optional and never part of the key; `__str__` prints it after
the path and quotes the line, `--json` has `span` (`null` for JSON input). The parser records
positions per node (predicate and selector nodes by identity, assigned a path once the tree
is built) and `syntax.Source.locate` attaches the span of the longest recorded path an error
lies under; `spec.load_checked` is `load_spec` + `check_spec` with that, `spec.locate(path,
errors)` re-parses a terse file for errors raised later (the compiler's soundness
obligations). The checker never sees the text: `tests/spec/test_check.py` runs every error
fixture through both syntaxes and requires the same errors, each located.

Why not JSON alone: the schema in the prompt is several times the grammar; a spec is three
lines per atom, resent on every repair round; structural slips in nested single-key objects
are repair rounds spent on brackets; and `git diff` of a JSON spec does not show a reviewer
which rule changed. Why not terse alone: the eval cases, the mutator, the vectors and the
schema export are programs over the model, and a serialization with a schema is what programs
want; a parser is a second thing to keep sound, and the round trip is what keeps it so. Why not
`pg_more_ls`'s ordering (grammar first, AST after): the grammar would become the language, the
model its shadow, and every proof in `lean/` and every template test would be about the shadow.
Rejected: YAML (indentation-significant, every quoting rule a surprise, no gain over terse);
an s-expression form (one syntax for machines and humans, but no better in a diff than terse
and worse to read); making terse the only input and generating JSON from it (reopens D15 for
the tools for no reader's benefit). Cost: a recursive-descent parser over about twenty
productions with spans, a formatter that is the renderer, and the two-syntax discipline on
every construct — paid once, checked by the round trip forever.
