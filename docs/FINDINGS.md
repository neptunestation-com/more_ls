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
- Removing the seller-immutability transition of the marketplace case does **not** refute
  `column_immutable(seller_id)`: the update policy pins `seller_id` to the user on both row images,
  so the solver proves immutability from the policies alone.  Only a hand-over rule breaks it.
- Mutation score of the property templates over the seed corpus: 75 % (213 of 283 mutants) once every
  reference carries `must_allow` properties (D40); 38 % with safety templates only (32 % before
  properties were re-based on predicates, which lost the FK and CHECK invariants of dropped rules;
  45 % while a mutant that merely *fixed* a planted leak — a refuted property turning proved — was
  miscounted as detected).  Almost
  every undetected mutant *removes* a grant: the templates are safety properties (upper bounds), so
  a spec that denies too much is not caught by them (only by write ⊆ read when a write survives).
- cvc5 `finite-model-find` answers `unknown` on the cardinality induction (∀ over Int with
  LIA + UF); plain `mbqi` agrees with Z3 (unsat) immediately.

## The LLM loop against a live model (Milestone 4, claude-opus-5-5)
- Structured outputs (`output_config.format`) reject the pass schemas: "Circular reference detected
  in schema definitions: And -> And".  The grammar of predicates is recursive and cannot be unrolled
  without a depth bound.  Constraining only the envelope (`{"rules": {"items": {}}}`) is worse:
  the decoder then forces every nested value to a scalar, and the model emitted placeholder strings
  and JSON-encoded strings for predicates.  Unconstrained, the model produced schema-valid answers
  at the first attempt (the only "error" was a ```json``` fence around them).
- Given a `least_privilege` property that forgot the implicit membership-lookup grant (D21), the
  rules pass refused to change its rules, explained (correctly) that the grant comes from how the
  toolchain compiles membership subqueries, and recommended the property be revised.  The loop
  now escalates as soon as the rules come back unchanged, and the language reference states the
  implicit privilege.
- Grading by solver equivalence found a 3VL reading in the author's reference, not in the model's
  rules: "Archived documents are hidden from everyone" over a nullable `archived int` was
  compiled as `not (archived = 1)` on every command, which hides rows with a NULL flag (UNKNOWN is
  denied); the model wrote `archived is null or archived = 0` on select only.  Both readings fit
  the sentence; the requirement text now says which one is meant.  The lesson generalizes:
  spec-correctness numbers measure the requirement text as much as the model.
- The model routinely enforces a requirement at a different layer than the reference: a transition
  rule for "once closed, never modified" where the reference has a restrictive policy; an
  inclusion rule left out because the insert policy already implies it; identity columns pinned by
  a transition rule next to a write column rule.  Layer by layer these are differences (and
  filtered-vs-error outcomes do differ); for what each role can do they are not, which is why
  grading moved to the net-effect diff (D38).
- The solver diff against the model's output found bugs in the author's references, not the
  model's: the help-desk reference's restrictive "closed tickets are frozen" policy had no WITH
  CHECK, so it applied to the new row too and nobody could close a ticket (a liveness defect the
  safety properties cannot see); tickets, notes and audit events could be moved to another
  organization or tenant although the requirement said otherwise.
- Given a property it could not satisfy because the *property* was NULL-unsafe (an optional owner
  stated as a bare membership), the rules pass twice "fixed" the rules by forbidding the NULL, and
  said so.  The properties pass now gets explicit guidance on guarding nullable columns.
- A "bad reference" case (a deliberate leak the reviewer's property must catch) only works when the
  requirement text describes the leaky behaviour: given "exactly the groups they belong to" the
  model wrote the direct-membership rule and *fixed* the planted subtree leak; given an outsider
  assignee, it pinned the task's project because its own tenant-isolation property was refuted.
  Four such cases were rewritten so that the text says what the reference does ("may move it to
  any project"); the reviewer's English property keeps saying what it should do.
- After those corrections all 30 generation cases pass; the four reruns passed at the first
  attempt.  The remaining variance is in the model's reading of sentences that admit two
  interpretations, and it shows up as a net-effect difference, never as a false proof.
- The full repair benchmark (every synthetic case, `--repair`) first exposed a counting bug in the
  corpus generator: 16 "detected" mutants of bad-reference cases had merely dropped the planted
  leak (a refuted property turned proved), so the loop had nothing to repair, ended proved in 0
  rounds, and was graded not net-equivalent to the leaky reference.  Detection now means a proved
  property stops proving.  Over the 100 remaining cases: 99 ended proved and net-equivalent to the
  base reference (98 after one repair round, 2 after two), none escalated.  The one failure is a
  judgement call, not a wrong proof: the model's own property set said "a listing's id never
  changes" and its repair added that transition rule; the marketplace reference lets a seller
  rewrite the primary key because the requirement text says nothing about it.  The net diff reports
  exactly that (updates the reference allows, the repaired rules deny).  Left as is at first:
  making every reference pin its keys would have been tuning the corpus to one run's choice.
  When the same choice recurred in three runs (two seed runs, the 194-case benchmark) it was no
  longer one run's: the text now says a listing keeps its id, the references pin it, and the
  family passes 25/26 when rerun — the remaining failure is a different invention of the model's
  (a hand-over may change nothing but the seller), flagged as optional in its own notes.
- `must_allow` (D40) sees what the safety templates could not: on the help-desk reference with the
  old bug (restrictive policy without WITH CHECK) "an agent of the ticket's organization closes an
  open ticket" is refuted with the exact scenario, and dropping any grant of a reference is refuted
  by the property that states it.  With one `must_allow` per stated grant in every reference the
  mutation score went from 38 % to 75 %.  Measured live, the first semantics was wrong: with the
  written image's CHECKs in the goal, the model's natural lower bound ("a member can insert a
  document in their tenant within their clearance") was refuted by the level-range CHECK, and the
  rules pass either escalated or *dropped the CHECK* to satisfy it (after which the invariant was
  refuted): 4 of 30 seed cases, 24/30 correct, escalation rate 0.13.  The amended D40 assumes the
  written row valid and the corpus's score is unchanged (75 %); re-measured, 28/30, no
  escalation, 0.13 repair rounds — the two left are the listing-id judgement call and a sentence
  ("may move it to any project") that did not say the task keeps its assignee, which it now does.  The remaining undetected mutants are, almost entirely,
  mutations without a behaviour change: the per-user membership-read policies of the salaries case
  are redundant with the implicit relation grant (D21); dropping the student-name check of a
  submission insert changes nothing because the FK still forces enrolment; dropping the seller
  immutability transition changes nothing because the update policy pins the seller on both images.
  The rest are cardinality rule references (not re-based) and grants the author's property sets
  do not state (own-group reads in the org tree, the admin read of the pg_roles example).
- The full repair benchmark with the `must_allow` prompt (194 mutants, 2026-11-03): 187 ended
  proved and net-equivalent to the base reference, none escalated, 1.07 repair rounds on average.
  The seven failures (none a wrong proof): the listing-id judgement call twice; one corpus leak —
  the skeleton the model is given carried the spec's `$comment`, which for every bad-reference
  case names the planted defect ("classroom where students see unpublished assignments"), and the
  model's notes cite the "schema comment" as its reason for restricting students to published
  assignments, i.e. for fixing the leak the case exists to keep.  The comment is no longer part of
  the prompt.  The other four were sentences the model's notes flagged as read one way where the
  reference meant the other: "may carry an expiry time" (may an invite be created already
  expired? the model's write ⊆ read property said no, the reference allowed it — the text now says
  the expiry must still lie ahead at creation and the reference requires it), "only the assignee
  may update a task" (may they hand it over or unassign it? the reference says yes, the text now
  says so) and "users may see which groups they belong to" (the groups table, as the reference
  reads it, or the membership rows, as the model read it in this run; the text now names the
  groups).  Rerun on the corrected corpus, the five mutants and the six seed cases of those
  families all pass (one repair round for the mutants, none for the seeds).  Same lesson as the
  seed corpus: a run's failures are read before they are counted, and the fix goes into the text,
  not the model.
- The corpus with both syntaxes (2026-11-05, D50; `docs/LLM.md`, "The two syntaxes"): JSON and
  terse score the same on the 35 seed cases (32 each, the same three failures) and terse scores
  better on the 221 mutants (218 against 213), with a third fewer output tokens and no repair
  round spent on syntax: the model wrote parseable sections at the first attempt in every case,
  so the grammar plus three examples is enough of an output contract.  The failures were the
  texts' again: "Memberships are read-only to users" read as a grant of a membership listing
  (now "no membership listing: the membership lookup is all they get"; both seed cases pass
  reworded), and the self-grant leak of `st_self_grant`, which the model declines to reproduce
  in either syntax (it denies the insert and update that the text asks for), like the classroom
  leak above: a bad-reference case whose text states the defect as a requirement measures the
  model's willingness to write a leak, not the loop.  One JSON-only failure was a one-off
  invention the model named in its notes (an id-immutability transition); left alone.
