# The LLM loop (Milestone 4, D16/D37–D39)

`mls generate` turns business requirements into a checked, proved spec; `mls eval --llm` measures
how well it does that on the corpus in `evals/cases`.

## What happens

1. **Pass 1 — properties.** The model gets the language reference, the fixed skeleton (identity,
   roles, tables, relations, hierarchies, opaques; the spec's `$comment` is an author's note and is
   not shown) and the JSON Schema of `{"properties": [...],
   "notes": ""}`, then the requirements.  It answers with properties and their plain-English
   descriptions: the approval artifact (D10).
2. **Pass 2 — rules.** Same material, the schema of `{"rules": [...], "notes": ""}`, the same
   requirements; it never sees pass 1.
3. **Check.** Skeleton + rules + properties are validated (pydantic), checked (`mls check`:
   types, fragment, relation shapes) and the compiler's soundness obligations are run.  Every error
   carries a JSON path and a fix; errors under `$.properties` go back to pass 1, the rest to pass 2.
4. **Prove.** `run_all`.  Each refuted property goes back to the rules pass as its English
   statement, the failing obligation and the counterexample scenario in English.
5. **Repair budget.** `--max-repairs N` bounds the total number of repair requests (structural,
   checker, proof).  When it is spent: `invalid` if the spec still does not check, `escalated` if
   properties are still refuted (each with its scenario and the rules pass's notes, for a human to
   decide whether the rules or the property is wrong), otherwise `proved`.

Prompts: `src/mls/llm/prompts.py`.  The stable part (role, language reference, skeleton, schema) is
the system prompt and is cached; requirements and feedback follow.

Decoding is not constrained by the schema: the API's structured outputs reject recursive schemas
(the predicate grammar is one) and a free-form envelope forces nested values to scalars (D37).  The
answer is parsed tolerantly (bare JSON, a ```json``` fence, or the first JSON object in prose) and
validated locally; an unusable answer keeps the previous one in the next repair request.  When the
rules pass returns its rules unchanged after a counterexample, the loop stops and escalates: the
model is standing by its rules, usually because it judges the property wrong, and more rounds
would only repeat the exchange.

## Running it

    export ANTHROPIC_API_KEY=...            # or `ant auth login`; never commit it
    uv run mls generate requirements.txt --skeleton skeleton.json --out spec.mls.json --transcript t.json
    uv run mls eval evals/cases --llm --transcripts out/   # metrics over the corpus
    uv run mls eval evals/cases --llm --repair             # start from each case's (mutated) rules
    uv run mls eval evals/cases --mutate evals/cases/synthetic   # regenerate solver-graded mutants

The default model is `claude-opus-5-5` with effort `high`; server-side refusal fallbacks are on
(`fallbacks: "default"`).  `MLS_LLM_SCRIPT=<json file>` replaces the model with a scripted client
(a list of answers) — this is how the tests and offline runs work, and no network call is made.

## Eval case format

```json
{"id": "hd_good", "origin": "seed",
 "requirements": "Customers open support tickets ...",
 "expected_properties": ["A ticket is visible only to its customer and to agents of its organization.", "..."],
 "reference_spec": "specs/hd_good.mls.json",
 "expected_verdicts": {"P1": "PROVED", "...": "..."},
 "expected_loop": {"status": "proved"}}
```

`skeleton` (optional) overrides the schema given to the passes (default: the reference spec
without rules and properties).  `expected_loop.status` defaults to `proved`: requirements are
self-consistent, and what the reference properties say about the generated rules — REFUTED
included — is `expected_verdicts`.  Synthetic cases carry `origin: "synthetic"`, `base_case`,
`mutation`, the base reference inline, `start_rules` (the mutant rules) and `mutant_verdicts`;
they are repair benchmarks (`--repair`) and are skipped otherwise.  Cases whose reference does not
check (`expected_check_errors`) are checker cases and are skipped by `--llm`.

## Metrics (`mls eval --llm`)

| Metric | Meaning |
|---|---|
| outcome_match_rate | status equals the expected status (`proved` unless the case says otherwise) |
| spec_correctness_rate | outcome match, the reference properties have their expected verdicts on the generated rules, and every role can do exactly what the reference lets it do (net-effect diff + grant diff, D38) |
| net_equivalence_rate | the net-effect part alone |
| equivalence_rate | layer-by-layer equivalence (access, integrity, grants): informative only |
| reference_properties_hold_rate | the reference properties, re-based on the generated rules, have their expected verdicts |
| mean_repair_rounds | repair requests per case |
| human_escalation_rate | cases ending with at least one escalation |

The corpus: 33 seed cases in 11 schema families (`evals/make_cases.py`; 32 are generation cases,
one is a checker case) and the solver-graded mutants of every seed case (`evals/cases/synthetic`,
213 cases, mutation score 75 %; a mutant counts as detected when a property the base proves no
longer proves — the lower-bound template `must_allow` (D40) is what makes a dropped grant count).

## Measured (`claude-opus-5-5`, effort high, max 3 repairs)

| Metric | Seed corpus, 2026-11-02, before `must_allow` | Seed corpus, 2026-11-03, with `must_allow` (D40 as amended) |
|---|---|---|
| outcome_match_rate | 1.0 | 1.0 |
| spec_correctness_rate | 1.0 | 0.93 |
| net_equivalence_rate | 1.0 | 0.93 |
| equivalence_rate (layer by layer) | 0.77 | — |
| reference_properties_hold_rate | 1.0 | 1.0 |
| mean_repair_rounds | 0.1 (three cases needed one round) | 0.13 (four cases needed one round) |
| human_escalation_rate | 0.0 | 0.0 |

The 2026-11-03 run: 64 API calls, about 200k output tokens.  The two failures are not proof
failures: the marketplace hand-over case, where the model pins the listing id by its own property
(the judgement call of the repair benchmark below), and the project case, where the text said
"may move it to any project" and the reference also kept the assignee fixed; the text now says
so, and the case passes when rerun on its own (0 repair rounds).  Both were visible only as net-effect differences, with the reference properties
holding.

Repair benchmark (`--repair`, every synthetic case, same model and settings):

| Metric | 2026-11-02, 100 cases, before `must_allow` | 2026-11-03, 194 cases, with `must_allow` (D40 as amended) |
|---|---|---|
| outcome_match_rate | 1.0 | 1.0 |
| spec_correctness_rate | 0.99 | 0.96 (187/194) |
| net_equivalence_rate | 0.99 | 0.96 |
| reference_properties_hold_rate | 1.0 | 0.98 |
| mean_repair_rounds | 1.02 (98 cases one round, 2 cases two) | 1.07 (181 one round, 12 two, 1 three) |
| human_escalation_rate | 0.0 | 0.0 |

The mutant rules refute the model's properties and the counterexample in English is enough for
the repair.  The 2026-11-02 run (202 API calls, about 529k output tokens, 218k input tokens plus
1.2M cache reads) had one failure, a judgement call the net diff makes visible: the model's
property set pinned the listing id and its repair enforced it, while the reference leaves the key
writable because the text says nothing about it (FINDINGS).  The run also removed 16 synthetic
cases whose mutation had only dropped a planted leak (nothing to repair; see D39).

The 2026-11-03 run (402 API calls, about 1.24M output tokens) had seven failures, none a wrong
proof or an escalation: two are the listing-id judgement call again (both marketplace hand-over
mutants); one was a corpus leak — the skeleton carried the spec's `$comment`, which for a
bad-reference case names the planted defect ("classroom where students see unpublished
assignments"), and the model duly fixed the leak instead of reproducing it (the comment is no
longer shown); four were sentences that admit two readings, which the model's notes named
exactly: whether an invite may be created already expired (twice: write ⊆ read says no, the
reference said nothing), whether an assignee may hand a task over, and whether "see which groups
they belong to" is about the groups table or the membership rows.  The texts now say (FINDINGS),
and the five mutants plus the six seed cases of those families were rerun on their own: all
eleven pass (the mutants in one round, the seeds in none; 22 API calls).  The listing-id
judgement call was then settled the same way, after it had recurred in three runs: the text says
a listing keeps its id and the marketplace references pin it (a transition rule and a
`column_immutable` property; three new mutants, 197 in all).  The whole family rerun — 3 seeds,
23 mutants, 55 API calls — scores 25/26: the one failure is a new choice of the model's, named
in its own notes as such (a transition rule that lets a hand-over change nothing but the seller,
"drop R11 if combined edit-and-handover in one update must be allowed"); the text does not
suggest it and was left alone.

After these runs the properties prompt gained `must_allow` (D40).  The first re-measurement
(2026-11-03, same model and settings) scored 24/30: outcome match 0.87, spec correctness 0.80,
net equivalence 0.87, mean repair rounds 0.57, escalation rate 0.13.  Four failures were one
defect in D40's first semantics — the written image's CHECKs were part of the goal, so a lower
bound that did not restate them was refuted by a CHECK and the rules pass escalated or dropped
the CHECK (FINDINGS) — one was the primary-key judgement call of the repair benchmark, and one a
reading of "may move it to any project".  D40 was amended (the written row is assumed valid) and
the corpus re-measured: the table above; then the repair benchmark, also above.

Seed run: 63 API calls, about 158k output tokens and 87k input tokens (most of them cache reads).
This is the third run: the first two found a schema-decoding problem (D37), ambiguous requirement
sentences and three reference bugs, which were fixed in the corpus, not by tuning to the model
(FINDINGS).  A run over the same cases is not a free re-measurement: the model's choices vary
between runs on sentences that admit two readings, so a failing case should be read, and its
text made precise, before the number is believed.
