# The LLM loop (Milestone 4, D16/D37–D39)

`mls generate` turns business requirements into a checked, proved spec; `mls eval --llm` measures
how well it does that on the corpus in `evals/cases`.

## What happens

1. **Pass 1 — properties.** The model gets the language reference, the fixed skeleton (identity,
   roles, tables, relations, hierarchies, opaques) and the JSON Schema of `{"properties": [...],
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

The corpus: 31 seed cases in 11 schema families (`evals/make_cases.py`; 30 are generation cases,
one is a checker case) and the solver-graded mutants of every seed case (`evals/cases/synthetic`,
194 cases, mutation score 75 %; a mutant counts as detected when a property the base proves no
longer proves — the lower-bound template `must_allow` (D40) is what makes a dropped grant count).

## Measured (2026-11-02, `claude-opus-5-5`, effort high, max 3 repairs)

| Metric | Seed corpus (30 cases) |
|---|---|
| outcome_match_rate | 1.0 |
| spec_correctness_rate | 1.0 |
| net_equivalence_rate | 1.0 |
| equivalence_rate (layer by layer) | 0.77 |
| reference_properties_hold_rate | 1.0 |
| mean_repair_rounds | 0.1 (three cases needed one round) |
| human_escalation_rate | 0.0 |

Repair benchmark (`--repair`, all 100 synthetic cases, same model and settings):

| Metric | Synthetic corpus (100 cases) |
|---|---|
| outcome_match_rate | 1.0 |
| spec_correctness_rate | 0.99 |
| net_equivalence_rate | 0.99 |
| reference_properties_hold_rate | 1.0 |
| mean_repair_rounds | 1.02 (98 cases one round, 2 cases two) |
| human_escalation_rate | 0.0 |

The mutant rules refute the model's properties and the counterexample in English is enough for
the repair.  The one failure is a judgement call the net diff makes visible: the model's property
set pinned the listing id and its repair enforced it, while the reference leaves the key writable
because the text says nothing about it (FINDINGS).  202 API calls, about 529k output tokens,
218k input tokens plus 1.2M cache reads.  The run also removed 16 synthetic cases whose mutation had
only dropped a planted leak (nothing to repair; see D39).

After these runs the properties prompt gained `must_allow` (D40), so the measured numbers are
for a prompt without it.  A sanity sample with the new prompt (2026-11-03: hd_good, sd_good,
cr_good, pj_good): 4/4 proved and net-equivalent, 3 to 5 `must_allow` properties per case, all
proved at the first attempt, each stating the whole change ("an agent can close an open ticket of
their organization, leaving everything else unchanged"); one case needed a repair round for its
own column-immutability properties, not for a lower bound.  The full corpus has not been
re-measured with it.

Seed run: 63 API calls, about 158k output tokens and 87k input tokens (most of them cache reads).
This is the third run: the first two found a schema-decoding problem (D37), ambiguous requirement
sentences and three reference bugs, which were fixed in the corpus, not by tuning to the model
(FINDINGS).  A run over the same cases is not a free re-measurement: the model's choices vary
between runs on sentences that admit two readings, so a failing case should be read, and its
text made precise, before the number is believed.
