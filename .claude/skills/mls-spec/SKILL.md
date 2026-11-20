---
name: mls-spec
description: Turn business requirements into a proved MLS spec for a PostgreSQL database by running `mls generate`, then put the result in front of the user for approval. Use when the user wants row-level security, access rules or data-integrity rules written for their tables, wants to replace hand-written RLS policies on an existing database, or asks to write, generate or redo an MLS spec from requirements.
---

# Write an MLS spec from requirements

You are the front end of the MLS loop, not the author of the spec. `mls generate` writes the
spec: it asks a model for the properties (the promises) and, separately, for the rules (the
policies and constraints), never showing either pass the other's answer, and the solver proves
the rules against the properties. That independence is what makes a proof worth anything. Your
job is everything around it: the skeleton, the requirements, running the loop, and putting the
result in front of the user in words they can approve.

The user approves **requirements, relations and properties in plain English**. They never
approve MLS or SQL, and they should never have to read either.

## Ground rules

- **Never edit the rules, properties or relations the loop wrote**, and never write rules or
  properties yourself. If an approval sentence is wrong, a requirement is ambiguous or wrong: fix
  the sentence in the requirements file and run `mls generate` again. Editing the spec by hand
  throws away the independence of the passes, and editing a property until it proves turns the
  proof into a tautology. If the user explicitly asks to write the spec by hand, say once that the
  proof then checks your rules against your own properties, and follow GUIDE §4 instead of this
  skill.
- **Never change the database.** Import, check, prove, explain, diff and drift only read. The
  fixes the import suggests (closing a bypass path, forcing row-level security) and the deploy are
  the user's to run; show them the command and what it changes.
- **Never read, print or write the API key.** `mls generate` needs `ANTHROPIC_API_KEY` in the
  environment. If it is missing, say so and stop; the user sets it themselves.
- **Quote the loop's approval sentences verbatim.** They are rendered from what was proved. Do not
  summarize them into something stronger or weaker; add your own explanation after the quote.
- Run every command as `uv run mls ...` from the MLS checkout (the directory with
  `pyproject.toml` whose project is `mls`). Files may live anywhere; pass their paths.

## 1. The skeleton

The skeleton is the spec without its access rules: identity (how the database knows the current
user), roles, tables, relations, and whatever rules the database already enforces.

**An existing database** (the usual case; GUIDE §3 is the full walkthrough). Ask for a
connection string for the role that owns the tables, kept in an environment variable such as
`$DATABASE_URL`; never write it into a file. Then:

    uv run mls import --dsn "$DATABASE_URL" --out before.mls
    uv run mls import --dsn "$DATABASE_URL" --policies discard --out skeleton.mls

`before.mls` records what today's policies allow; `skeleton.mls` is the skeleton. Keep the two
apart from the generated spec and never overwrite them: every run of the loop starts from
`skeleton.mls`, and a skeleton that already holds a run's rules hands them to the next run as
rules it must keep, so a sharpened requirement could not take a wrong grant back. Read every note the
second import prints on stderr and report them to the user in plain words, grouped:

- **Discarded policies**: what the old rules said. They become input to the requirements.
- **Bypass paths** (views and functions that reach a table without its policies): each note ends
  with its fix. The new rules do not protect a table while a bypass path stays open. Ask the user
  whether to close it (they run the fix) or whether the path is intended (then the table assumes
  it, and every proof report says what it does not cover).
- **Assumptions** (row-level security not forced; the owner is trusted) and **foreign objects**
  (columns, keys and indexes MLS leaves alone): mention them; they need no decision now.
- **Tables without row-level security today**: the deploy turns it on, which changes who reads
  them. Make sure a requirement covers each one.

**A new schema.** There is nothing to import. Write the skeleton from the user's table
definitions: identity, roles, tables with their columns and keys, and the membership relations
(REFERENCE has the syntax; `examples/` has complete specs). It holds no access rules, so writing
it is not writing the spec. Run `uv run mls check skeleton.mls` until it passes.

## 2. The requirements

This is the part only the user can write, and where most failures start: in measured runs, a
failed case traced back to a sentence that admits two readings about as often as to the model. Interview the
user, then write `requirements.txt` and get it approved before running the loop.

- Start from what exists. On an imported database, `uv run mls explain before.mls` lists today's
  rules in English: go through them with the user, keep what is right, fix what is wrong, add
  what is missing.
- **One sentence per grant**, each saying **who** (a role, or a user in some relation to the
  row), **which rows**, and **what they may do** (read, create, edit, delete). "A member reads,
  creates and edits the tasks of their team's projects."
- Ask about every gap you can see, at once rather than one by one: every table that app roles
  can reach needs a sentence, or the user's confirmation that it stays closed; signed-out callers
  (on Supabase, `anon`) need an explicit answer; so do deletes, edits that move a row out of the
  user's reach, and rows whose owning column is NULL.
- Facts about the data (a status is one of three values, an admin is always a member, a closed
  ticket stays closed) are requirements too: write them as sentences.
- Keep the user's business words. Do not translate them into MLS terms.

## 3. Run the loop

    uv run mls generate requirements.txt --skeleton skeleton.mls --out spec.mls --transcript transcript.json

Every rerun uses the same command: `--skeleton` is always the import's skeleton, never the
`spec.mls` of an earlier run. It calls the model several times, so it is not instant. It exits 0 when the status is `proved` and 1 otherwise. The output ends
with the sections the user approves: `# Relations added to the schema (for approval)` (paths the
model added to the schema, often none) and `# Properties (for approval)`.

## 4. The outcome

**`proved`.** Present, verbatim, the added relations and the properties with their verdicts.
Then help the user read them against the requirements: is every grant there as a lower bound
(what a user must be able to do)? Does every table have an upper bound (what a user can never
see)? A relation is approved like a property: properties are stated in its terms, so a wrong
relation makes a right-sounding property say the wrong thing. Anything wrong or missing goes back
into `requirements.txt` (step 2), and the loop runs again.

**`escalated`.** A property is still refuted, and the rules pass stood by its rules. Show the user
the property, the scenario (a concrete case, in English) and the rules pass's notes, and ask the
one question the solver cannot answer: is the scenario acceptable to the business? GUIDE §8 has
the three answers:

- not acceptable: a requirement is missing or vague; sharpen it and rerun;
- acceptable: the requirement said more than the business means; reword it and rerun;
- the scenario is a state the data can never be in: a fact is missing; add it as a requirement
  and rerun.

Never resolve an escalation by editing the spec.

**`invalid`.** The spec still does not check after the repair budget. Read `transcript.json`
first: the model's notes usually name the cause, very often a requirement it could not express
or read two ways. Fix the requirement and rerun. If the notes point at the skeleton, fix the
skeleton (step 1). `--max-repairs N` (default 3) raises the budget; use it once, not as the fix.

**`failed`.** The API call itself failed. Report the error as printed.

## 5. After approval

Once the user approves every relation and property:

    uv run mls prove spec.mls
    uv run mls diff before.mls spec.mls

Show the proof report's **assumptions**: they are the conditions under which the proofs hold.
On an imported database, the diff lists every change in effect against today's policies, with an
example: go through each with the user; each should be one of their requirements or a mistake.
The diff exits 2 when anything changed, which is expected here.

Stop there. The deploy (`mls drift`, `mls preflight`, the generated DDL) is GUIDE §3 from step 9
on; offer to walk the user through it, and leave every command that changes the database to
them.
