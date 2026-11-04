# Roadmap

## Milestone 1 — core loop, no LLM

Pipeline: JSON spec → validate (schema) → typecheck → fragment check → prove → compile DDL →
differential test against PG 15–18.

Scope:
- Spec model (pydantic) and exported JSON Schema for: tables/columns, relations, identity adapter,
  `policy`, `check`, `inclusion`, `transition`, `column_rule`, `now_between` atom, `opaque` (parsed,
  modeled, compiled; full reporting in M3).
- Identity adapters `supabase` and `pg_roles`.
- Logic layer: term AST, 3VL (T, F) encoding, Z3 backend, SMT-LIB2 printer.
- Property templates from D9 + custom properties; proof reports (Markdown + JSON) listing
  assumptions; counterexamples as structured scenarios.
- Compiler templates for every M1 construct, plus generated indexes (D14) and hardening (D13).
- Test harness: Postgres 15–18 in Docker; per-template differential tests (SELECT, INSERT, UPDATE,
  DELETE outcomes: filtered / ok / error) with hypothesis-generated data; at least one mutation test
  per template; EXPLAIN (FORMAT JSON) checks asserting index conditions; cvc5 cross-check job.
- Leakproof lint.
- Eval case format (requirements text, expected properties, reference spec, expected verdicts),
  with the 3 corpora from `prototype/demo.py` converted as the first cases.
- CLI: `mls check | prove | compile | test`.

Acceptance:
- `examples/tenant_docs.mls.json` (finalized to the real schema) reproduces the prototype's results:
  isolation PROVED; UPDATE tenant move REFUTED without the transition and PROVED with it; naive
  CHECK NULL issue caught; membership-delete induction needs both FKs.
- 0 differential mismatches on all four PG versions; every mutation test fails as expected.
- Every generated policy passes its EXPLAIN check at ≥1M rows.
- A spec using a construct outside the fragment gets an error naming the JSON path and the fix.

## Milestone 2 — evolution

`mls diff | migrate | preflight | drift`.
- Access diff with scenarios (§7 of SEMANTICS).
- Migration generation: ordering, `NOT VALID` / `VALIDATE`, documented and tested lock levels per
  DDL statement, transactional policy swaps.
- Pre-flight: compiled `IS TRUE` predicates as violation queries, with counts and samples.
- Drift: compile the expected objects into a shadow schema on the same server and compare
  normalized catalog output (`pg_get_expr`, `pg_get_constraintdef`, `pg_get_triggerdef`, ACLs)
  rather than comparing strings we generated.
- English rendering of rules, properties and scenarios (the approval artifact, D10).

## Milestone 3 — harder constructs

- Hierarchies (D5) with depth bound, immutable parent, recursive-CTE InitPlan; EXPLAIN-checked.
- Cardinality (D4) with parent-row lock; concurrency test with parallel writers proving the bound
  holds under READ COMMITTED (and fails without the lock — mutation test).
- Opaque blocks: full reporting (conditional proofs, diff flags).

## Milestone 4 — LLM loop

- Evaluation set (D6): 30–50 seed cases from David plus synthetic mutations.
- Separate-pass generation (D16) with constrained decoding against the JSON Schema; bounded repair
  using counterexamples; LLM-oriented error messages; metrics (spec correctness, repair rounds,
  human-escalation rate).  (Constrained decoding turned out impossible with the API's structured
  outputs — recursive schema — so decoding is unconstrained and validated locally; D37.)

Implemented (D37–D39, `docs/LLM.md`) and measured live: 30/30 seed generation cases correct
after the first run's findings were fixed in the corpus (`docs/LLM.md`, FINDINGS).  Open: replace
the author's seed cases with David's anonymized corpus (the measured number is bounded by the
quality of the author's requirement texts).  Done since the first measurement: `must_allow`,
the lower-bound template (D40, amended after its first live run), in every reference — mutation
score 38 % → 75 %, 194 synthetic repair benchmarks, measured with the `must_allow` prompt at
187/194 (the 100 that existed before scored 99/100; both in `docs/LLM.md`), and the seed corpus
at 28/30 (FINDINGS).  The terse rendering of the JSON syntax for reading transcripts and diffs
(`mls explain --terse`, the `before`/`after` lines of `mls diff`, `spec_terse` in every
transcript) is done: D44, `mls/terse.py`; not a new input format, D15 stands.  The last open detail of the
first roadmap — security-invoker views for row-filtered column masking (D24) — is settled by D42:
no construct; the column is its own table with its own policies (`cr_own_salary` in the corpus).

## Parallel track — Lean 4

Formalize: 3VL, predicate semantics, policy combination, NULL normalization lemma, and correctness
of the CHECK and FK templates against a small model of Postgres. No milestone depends on it.

Step 1 done (`lean/`, D41): the three-valued connectives and their truth tables, the (T, F) pair
encoding with `encode`/`decode`, soundness of the pair connectives and of the n-ary folds, and
`Pred.tf_sound`: the encoder of `logic/tvl.py`, transcribed clause for clause, emits the encoding
of the SQL truth value of every predicate in the verified fragment except `in_group`, under the two
assumptions the toolchain makes (non-nullable columns hold no NULL; opaque pairs are consistent).
Also `two_valued_shortcut_wrong`: `NOT` does not commute with admission.

Step 2 done: (a) `in_group` joins the encoder theorem (`lean/MLS/Hierarchy.lean`): the encoder's
unrolling over the uninterpreted `has_parent`/`parent` equals the evaluator's walk when the
functions describe the forest and the hierarchy FKs hold, and with the depth column bounded by
`max_depth` a longer walk finds nothing more, so the unrolling is exact (SEMANTICS §3.1); the
encoder theorem now covers every construct of the verified fragment, over named row images
(`r`/`old`/`new`).  (b) Policy combination (`lean/MLS/Policy.lean`, transcribed from
`props/effective.py`): `effective_admits` — a row is admitted iff some permissive policy is TRUE
on it and every restrictive policy is TRUE on it — with its corollaries: no permissive policy
denies, a restrictive policy that is FALSE or UNKNOWN denies, a permissive policy that is UNKNOWN
does not grant, adding a permissive policy only grants and adding a restrictive one only denies.
(c) The statement outcomes of SEMANTICS §5 (filtered / ok / error, transcribed from
`pg/model.py`) in the logic layer, and their link to the proofs: every executed statement
satisfies the obligation's assumptions (`update_ok_assumptions` etc., so a property proved under
them holds of every executed statement), an update that hides the row from its author is never
ok, and for a statement that reads no columns "executed" is exactly "admitted by the net-effect
predicate" of D38/D40 (`update_ok_iff_net` etc.).  Not modelled: column privileges (the
`unwritable_unchanged` conjunct of `net_terms`), grants, uniqueness, FK existence, cardinality.
Step 3 done (`lean/MLS/Normalize.lean`): how each target decides on a three-valued result (RLS
passes TRUE; CHECK is violated only by FALSE; a PL/pgSQL `IF` takes its branch only on TRUE; FK
MATCH SIMPLE is satisfied by any NULL), and the normalization lemma: `CHECK ((p) IS TRUE)` and
`IF NOT ((p) IS TRUE)` admit exactly the rows the admission rule admits (`one_meaning`,
invariant 2), what a stored row is assumed to satisfy in a proof is what the normalized CHECK
and trigger enforce (`check_matches_proof`, `trigger_matches_proof`, through the encoder
theorem), and without the `IS TRUE` the targets disagree exactly on UNKNOWN
(`check_naive_disagrees`, `trigger_naive_disagrees`: the semantic content of the `CHECK_NAIVE`
and `TRANSITION_NAIVE` mutations).  For FKs, `inclusion_pred` is admitted exactly when MATCH
SIMPLE is satisfied (`inclusion_matches_fk`).
Step 4 done (`lean/MLS/Relational.lean`): a relational model — a state is the set of stored
rows per table plus the facts of each relation, a relation is backed by its table, statements
are inserts, deletes and replacements of one row — and the two templates against it.  CHECK:
`CHECK ((p) IS TRUE)` keeps "every stored row is admitted by `p`" across every statement,
because a CHECK predicate reads only its row (`eval_rowOnly`); without the `IS TRUE` a row with
a NULL passes while the admission rule rejects it.  FK: `MATCH SIMPLE ON UPDATE RESTRICT ON
DELETE RESTRICT` keeps "every stored row of the referencing table satisfies MATCH SIMPLE" across
every statement on either table, with the RESTRICT guard being exactly the one
`props/obligations.py:_restrict_guard` gives the solver, and the relation-change axioms of
`_relation_change` following from the model.  Self-referencing FKs are outside the theorems;
rows are sets, not bags.  With this the track listed in D11 is complete: 3VL, predicate
semantics, policy combination, NULL normalization, CHECK and FK templates.
Step 5 done (issue #4): the four gaps left open after step 4.  (a) The transition trigger
relationally (`lean/MLS/Transition.lean`): a transition predicate reads only its two images
(`eval_twoImage`), so what the trigger decided holds of the replacement in every state
(`transition_guard`); the induction step the solver discharges for the written row
(`_induction_step`, case 1) is enough for the state (`invariant_update_step`,
`invariant_insert_step`); the implicit parent-immutable rule is admitted exactly when the column
is unchanged and keeps the set of stored values of the column (`immutable_values`).  With it the
delete and update relation-change axioms of `_relation_change` are derived too, under the
uniqueness the spec requires of a relation's columns (`State.PairUnique`,
`rel_after_delete_iff`, `rel_after_update` in `Relational.lean`; the insert axiom needed no
such hypothesis).  (b) The compiled hierarchy helper (`lean/MLS/Reach.lean`): the recursive
CTE of `mls_reach_<h>` descending `max_depth` levels from the user's groups returns `g` iff the
walk up from `g` meets one of them (`Forest.reach_iff_walk`), which is `in_group`
(`reach_iff_inGroup`); one level less misses a group at the bound (`HIERARCHY_SHALLOW`); the
depth trigger with the CHECK keeps `Forest.Depths` across an insert (`depths_insert`), without
the CHECK a node under one at the bound exceeds it.  The `SECURITY DEFINER` side (the helper
sees every row, D34) is the model's `stored`, stated, not proved.  (c) Cardinality
(`lean/MLS/Cardinality.lean`) over a bag of rows: the trigger's test keeps the bound across
insert, update (the skip when the group is unchanged included) and delete, the solver's count
axioms follow from the bag, `CARDINALITY_OFF_BY_ONE` lets one row too many in, and the lock is
modelled as what it buys — a serial schedule, in which every attempt counted the bag it extends,
keeps the bound (`serial_bound`), while two attempts that counted the same bag overshoot
(`unlocked_overshoot`, the `CARDINALITY_NO_LOCK` mutation).  That `FOR UPDATE` under READ
COMMITTED yields a serial schedule remains the stated assumption of the proof report.  (d) The
transcription is checked mechanically by shared test vectors (D43): `tests/lean/vectors.py`
generates `lean/MLS/Vectors.lean`, 340 `#guard`s comparing `Pred.eval`, `Pred.tf` (the
hierarchy walk and unrolling included) and the statement outcomes with what the Python
evaluator, encoder and oracle computed on the same data; the fast suite checks the file is
current, the Lean build checks the guards.  Still by hand: a change to a Python original must be
carried to `lean/MLS` (CLAUDE.md) — the vectors catch a divergence in meaning on the sampled
data, not a missing theorem.
