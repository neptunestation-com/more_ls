# The LLM loop (`mls generate`, `mls eval --llm`)

Moved from the root CLAUDE.md: it loads when working under `src/mls/llm/`.

- M4: `uv run mls generate` / `mls eval --llm` (src/mls/llm; GUIDE §12 for users, docs/design/LLM.md for the internals and the evals; D37–D39). Tests never call the
  API: `MLS_LLM_SCRIPT=<json>` or `ScriptedClient` replays answers. `evals/make_cases.py` regenerates
  the seed corpus (the solver must confirm every intended verdict); `mls eval evals/cases --mutate
  evals/cases/synthetic` regenerates the mutants. Keep both committed and in sync (tests check).
  `must_allow` (D40) is the lower bound: without one per stated grant a reference's mutation score
  cannot see dropped grants, and `mls eval --mutate` is idempotent over its own output. Its written
  image is assumed valid (CHECKs and FKs are assumptions, not goals): a lower bound that had to
  restate the CHECKs made the model drop them.
  Grading is by net effect (`props/diff.py:behaviour_diff`), not layer by layer; when a live case
  fails, read its transcript first (`--transcripts DIR`): the model's notes usually name the real
  cause, and so far that was an ambiguous requirement sentence or a reference bug as often as the
  model. Live runs need `ANTHROPIC_API_KEY`; decoding is unconstrained (the API rejects recursive
  schemas), answers are validated locally. `--syntax terse` (D50, #16) swaps the JSON Schema
  for the grammar and the three proved examples of `llm/terse_prompt.py` (the fast suite keeps
  them parsing, canonical, proved and disjoint from the corpus's tables); the answer is a
  section read by `syntax.parse_section` (`loop._terse_answer` finds it in a fence or prose),
  repair quotes the model's own text with line:col feedback; the grader sees the JSON shape.
  Terse is the default (`loop.DEFAULT_SYNTAX`, D50 as amended after the 2026-11-05 corpus run
  in docs/design/LLM.md); tests that script JSON answers pass `syntax="json"` / `--syntax json`.
