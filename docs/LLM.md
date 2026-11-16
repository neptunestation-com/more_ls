# The LLM loop (Milestone 4, D16/D37–D39)

`mls generate` turns business requirements into a checked, proved spec; `mls eval --llm` measures
how well it does that on the corpus in `evals/cases`.

## What happens

1. **Pass 1 — properties.** The model gets the language reference, the fixed skeleton (identity,
   roles, tables, relations, hierarchies, opaques; the spec's `$comment` is an author's note and is
   not shown) and the output contract, then the requirements.  The contract is the grammar of a
   `properties { ... }` section and three examples (the default, terse syntax; below) or, with
   `--syntax json`, the JSON Schema of `{"properties": [...], "notes": ""}`.  It answers with properties and their plain-English
   descriptions: the approval artifact (D10).
2. **Pass 2 — rules.** Same material, the contract of the rules (a `rules { ... }` section, or the
   schema of `{"rules": [...], "notes": ""}`), the same requirements; it never sees pass 1.
3. **Check.** Skeleton + rules + properties are validated (pydantic), checked (`mls check`:
   types, fragment, relation shapes) and the compiler's soundness obligations are run.  Every error
   carries a JSON path and a fix; errors under `$.properties` go back to pass 1, the rest to pass 2.
4. **Prove.** `run_all`.  Each refuted property goes back to the rules pass as its English
   statement, the failing obligation and the counterexample scenario in English, followed by the
   lint findings of the rule set (D49: subsumed, redundant or contradictory policies, dead column
   rules) under a *Hints* heading, hygiene the model may act on, never a violation; a rule set
   that proves is never sent back for a finding.
5. **Repair budget.** `--max-repairs N` bounds the total number of repair requests (structural,
   checker, proof).  When it is spent: `invalid` if the spec still does not check, `escalated` if
   properties are still refuted (each with its scenario and the rules pass's notes, for a human to
   decide whether the rules or the property is wrong), otherwise `proved`.

Fixed rules (#26).  A skeleton may already have rules: `mls import --policies discard` writes the
CHECKs and FK-shaped inclusions it read.  Both passes are shown them after the schema, under
*Rules the spec already has (read from the database)*, as JSON or as a terse `rules { ... }`
section, with the instruction not to restate or reuse them and the first free id (`number your
rules from R3`).  Step 3 checks the model's rules followed by the fixed ones it did not restate
verbatim; a model rule with a fixed rule's id, or its name on the same table, is an error at the
model's own path, so the feedback and its line:col point into the answer.  `mls eval` passes no
fixed rules (a case's skeleton is its reference without the rules), so the corpus is unaffected.

Prompts: `src/mls/llm/prompts.py`.  The stable part (role, language reference, skeleton, schema) is
the system prompt and is cached; requirements and feedback follow.

## The syntax flag (D50)

`--syntax json|terse` on `mls generate` and `mls eval --llm` chooses what the passes answer in.
With `terse` the system prompt carries the language reference in terse notation, the grammar and
three complete examples (`src/mls/llm/terse_prompt.py`; they parse, check and prove, and share no
table with the corpus) instead of the JSON Schema; the skeleton is shown in terse; the answer is
one `rules { ... }` or `properties { ... }` section, read with the same tolerance as JSON (a
fenced block, else the first section at a line start with balanced braces, else the whole text;
the prose outside it is the notes).  Repair requests quote the section as the model wrote it
and every checker error names the line and column in it and the offending line (#15), so a
syntax error reads `at $.rules[0] (2:111: R1 policy ...): expected a term ..., found '='`.
Counterexample feedback is unchanged: English and scenarios (D10, D37).  The grader sees the
same JSON shape either way; transcripts keep `spec_terse` either way; `--out x.mls` writes the
terse spec.  The default is `terse` since the corpus run of 2026-11-05 (below; D50 as amended):
the same rates as JSON on the seed corpus, better on the repair benchmark, a third fewer output
tokens.

Decoding is not constrained by the schema: the API's structured outputs reject recursive schemas
(the predicate grammar is one) and a free-form envelope forces nested values to scalars (D37).  The
answer is parsed tolerantly (bare JSON, a ```json``` fence, or the first JSON object in prose) and
validated locally; an unusable answer keeps the previous one in the next repair request.  When the
rules pass returns its rules unchanged after a counterexample, the loop stops and escalates: the
model is standing by its rules, usually because it judges the property wrong, and more rounds
would only repeat the exchange.

## Running it

    export ANTHROPIC_API_KEY=...            # or `ant auth login`; never commit it
    uv run mls generate requirements.txt --skeleton skeleton.json --out spec.mls --transcript t.json

The default model is `claude-opus-5-5` with effort `high`; server-side refusal fallbacks are on
(`fallbacks: "default"`).  `MLS_LLM_SCRIPT=<json file>` replaces the model with a scripted client
(a list of answers) — this is how the tests and offline runs work, and no network call is made.

## How well it works

Measured with `claude-opus-5-5` on the eval corpus (2026-11-05, terse syntax): 32 of 35 seed cases
end proved and equivalent in effect to their reference, and 218 of 221 repair benchmarks (the loop
starting from deliberately broken rules) are repaired, after about one repair round.  The corpus's
requirement texts were written by the loop's author, so a corpus of real ones is the open question
(issue #1).  `docs/design/EVALS.md` has the corpus, the metrics and every measured run.
