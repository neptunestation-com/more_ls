# Evaluating the LLM loop

How `mls eval --llm` measures `mls generate` (the loop itself is `docs/LLM.md`): the corpus in
`evals/cases`, its format, the metrics, and every measured run.

## Running the evals

    export ANTHROPIC_API_KEY=...            # or `ant auth login`; never commit it
    uv run mls eval evals/cases --llm --transcripts out/   # metrics over the corpus
                                                           # (each transcript carries the generated spec as
                                                           # `spec_terse`, one line per rule and property, D44)
    uv run mls eval evals/cases --llm --repair             # start from each case's (mutated) rules
    uv run mls eval evals/cases --llm --syntax json        # the passes answer in JSON (the default is terse, D50)
    uv run mls eval evals/cases --mutate evals/cases/synthetic   # regenerate solver-graded mutants

The model and its settings are those of `mls generate` (`docs/LLM.md`, "Running it");
`MLS_LLM_SCRIPT` replays scripted answers here too.

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

The corpus: 36 seed cases in 15 schema families (`evals/make_cases.py` plus the hand-written
originals; 35 are generation cases, one is a checker case) and the solver-graded mutants of every
seed case (`evals/cases/synthetic`, 221 cases; mutation score 75 %, 221 of 295 mutants detected,
2026-11-05; a mutant counts as detected when a property the base proves no
longer proves — the lower-bound template `must_allow` (D40) is what makes a dropped grant count).
On 2026-11-13 (#24) `st_self_grant` and its three mutants were retired: its reference read the
membership relation in a policy on the membership table, which Postgres refuses at query time
("infinite recursion detected in policy", FINDINGS) and the checker now refuses, so the case had
no valid reference.  The corpus is now 35 seed cases and 218 mutants; the figures of the runs
below are those of the corpus at the time.

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

### The two syntaxes, 2026-11-05 (`claude-opus-5-5`, effort high, max 3 repairs)

The whole corpus run once with each syntax on the same day, the same model and settings, the
same requirement texts (the selector family `st_*` had been added since the runs above, so the
corpus is 35 seed cases and 221 mutants):

| Metric | Seed, JSON | Seed, terse | Repair benchmark, JSON | Repair benchmark, terse |
|---|---|---|---|---|
| cases | 35 | 35 | 221 (220 measured) | 221 |
| outcome_match_rate | 1.0 | 1.0 | 0.995 | 0.995 |
| spec_correctness_rate | 0.914 (32/35) | 0.914 (32/35) | 0.968 (213/220) | 0.986 (218/221) |
| net_equivalence_rate | 0.914 | 0.914 | 0.973 | 0.991 |
| reference_properties_hold_rate | 1.0 | 1.0 | 1.0 | 1.0 |
| mean_repair_rounds | 0.23 (8 cases one round) | 0.26 (9 cases one round) | 1.02 (217 one, 3 two, 1 three) | 1.05 (212 one, 8 two, 1 three) |
| human_escalation_rate | 0.0 | 0.0 | 0.005 (1 case) | 0.005 (1 case) |
| API calls | 78 | 79 | 446 | 452 |
| output tokens | 248k | 163k | 1.41M | 0.90M |
| input tokens (uncached + cache reads) | 34k + 467k | 23k + 235k | 457k + 4.07M | 279k + 1.97M |

The seed failures are the same three cases in both syntaxes, and none is about the syntax:
`st_good` and `st_files_forgotten` because "Memberships are read-only to users" was read as a
grant (the model wrote a select policy on memberships where the reference has only the implicit
membership lookup), and `st_self_grant`, a bad-reference case whose text asks for self-granted
memberships and whose leak the model declines to reproduce in both syntaxes (it denies the
insert and update instead), the same behaviour as the classroom leak of 2026-11-03.  The
sentence now says "Users never change memberships, and there is no membership listing: the
membership lookup is all they get"; the two reworded seed cases pass in both syntaxes with no
repair round.  The repair benchmark's extra JSON failures are three mutants of those two cases
(the old sentence), the three `st_self_grant` mutants in both syntaxes, one JSON-only judgement
call (`sd_good__drop_R2`: the model added an id-immutability transition "read into the
requirement", named as such in its notes; left alone), and one JSON case that did not run at all
(an API credit error; "220 measured").  Rerun with the reworded sentence, the five `st_good` /
`st_files_forgotten` mutants pass in both syntaxes, and the unrun JSON case passes, all in one
repair round (22 API calls); that brings the benchmark to 217/221 for JSON and 218/221 for
terse.  In terse the model wrote valid sections at the first attempt in every case; no repair
round was spent on syntax.

Decision (D50): the default syntax is `terse`.  The proved and net-equivalent rates are not
worse than JSON's on either corpus (equal on the seed cases, one case better on the benchmark
after the reruns, a judgement call), inside the run-to-run spread recorded above (two to four
cases between runs of the same corpus), and the token cost is a third lower.  The run cost
about 2.7M output tokens in all; the transcripts are not committed.

Seed run: 63 API calls, about 158k output tokens and 87k input tokens (most of them cache reads).
This is the third run: the first two found a schema-decoding problem (D37), ambiguous requirement
sentences and three reference bugs, which were fixed in the corpus, not by tuning to the model
(FINDINGS).  A run over the same cases is not a free re-measurement: the model's choices vary
between runs on sentences that admit two readings, so a failing case should be read, and its
text made precise, before the number is believed.
