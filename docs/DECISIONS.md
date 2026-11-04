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
Security-invoker views for row-filtered column masking remain a v2 question.

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
and the Python are two artifacts with no mechanical link;
a change to the encoder is carried to the transcription by hand, or the gap is recorded in the
ROADMAP.  What the track cannot do: say anything about Postgres itself — the "small model of
Postgres" of the later steps is our own axiomatization, and the differential tests remain the only
link to the real thing (D11).  Out of scope on purpose: cardinality limits and hierarchies, whose
correctness rests on the concurrency test and the cvc5 cross-check.

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

**D33. English rendering is a fixed function of the spec (D10, D15).** `mls explain` and the diff
and proof reports use `mls/english.py`; the same construct always reads the same way, so a
reviewer can compare the rendering with the requirement text, and the Milestone 4 loop can check
an LLM's property statements against it.
