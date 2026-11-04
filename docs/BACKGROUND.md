# Background: what `pg_more_ls` established

MLS grew out of [`pg_more_ls`](https://github.com/neptunestation-com/pg_more_ls) (February to
May 2026, Kotlin, archived in November 2026), a governance tool for PostgreSQL row-level
security: a human-authored DSL, nine proofs, introspection of live policies, drift against them,
and late-binding selectors. `docs/ROADMAP.md` Milestone 5 says what MLS re-expresses from it and
D45 to D49 in `docs/DECISIONS.md` say how. This document carries over the two pieces of that
project that are argument rather than code: the undecidability theorem that motivates a
restricted language, and the survey of existing authorization languages that justified writing
one. Both are cited, not rewritten. The paper is
[`paper/policy-algebra.pdf`](https://github.com/neptunestation-com/pg_more_ls/blob/main/paper/policy-algebra.pdf)
in that repository (source: `paper/policy-algebra.tex`; the same text as
[`spec/policy-algebra.md`](https://github.com/neptunestation-com/pg_more_ls/blob/main/spec/policy-algebra.md),
version 1.0, 2026-02-21, with the same section numbers). Section references below are to it.

## 1. Arbitrary RLS is undecidable (paper §1.1)

> **Theorem 1.1** (Undecidability of arbitrary RLS). *Given an arbitrary set of RLS policies
> expressed as SQL predicates, determining whether a given row is accessible to a given user is
> undecidable in general.*
>
> *Sketch.* Reduce from the halting problem. Encode a Turing machine's transition function as a
> PL/pgSQL function invoked within an RLS `USING` clause. The predicate returns `true` if and only
> if the machine halts. Determining row accessibility therefore requires solving the halting
> problem. ∎
>
> — paper §1.1, Theorem 1.1

The paper draws the consequence (§1.1): no tool can, in general, prove that tenant isolation
holds across a set of policies, detect a contradictory set that blocks all access, identify a
redundant policy, or verify that a change preserves the intended access semantics. Its answer
(§1.2, "The Compiler Insight") is to stop analysing SQL and instead generate SQL from a language
in which those questions are decidable by construction: RLS becomes a compilation target, not an
authoring surface. The decidability property in §2.5 states the fragment: atoms are column
comparisons against session variables and literals, which translate into quantifier-free linear
integer arithmetic with equality and uninterpreted functions (QF-LIA ∪ QF-EUF), decidable by the
Nelson–Oppen combination that Z3 and cvc5 implement. §1.3 names the formalisms the algebra draws
on: Bonatti, De Capitani di Vimercati and Samarati's access-control algebra for the
permissive/restrictive composition, lattice theory for subsumption, SMT for satisfiability,
Formal Concept Analysis for selectors, and Galois connections for compiler correctness.

MLS takes the theorem and the thesis unchanged. They are the reason for the sixth invariant in
`CLAUDE.md` (decidability is a property of the spec language: no construct whose obligations
leave quantifier-free / EPR + linear integer arithmetic + uninterpreted functions without a
declared bound) and for D3 (whatever lies outside the fragment is an explicit `opaque` block,
listed in every proof report that depends on it). Three things changed in the crossing:

- **The logic is three-valued.** The algebra's atoms are two-valued with a per-column
  nullability flag; the paper's decidability argument does not cover `UNKNOWN`. MLS encodes every
  predicate as an (is-true, is-false) pair (`docs/SEMANTICS.md`; `logic/tvl.py`, proved sound
  against the SQL meaning in `lean/MLS/Pred.lean`), so the fragment stays in the same theories
  and the proofs talk about the same outcomes Postgres produces (`docs/FINDINGS.md`, "NULL
  semantics").
- **The bounds are declared per construct, not globally.** The paper's traversals (§7) are
  existential subqueries over declared relationships, cut off at a global maximum depth
  (Definition 7.4, default 2) and never recursive (Property 7.2: org trees need a closure
  table). MLS keeps the decidability argument but states the bound where it is needed:
  hierarchies and cardinality limits are in the language (Milestone 3) because each one
  declares the depth its obligations are quantified over.
- **The target is not only RLS.** The paper's compilation (§10) emits `CREATE POLICY`. MLS
  compiles the same spec to CHECK constraints, foreign keys, transition triggers, column
  privileges and grants as well, because some properties (a tenant id that never changes, an
  owner who is always a member) are not expressible as policies at all (`docs/FINDINGS.md`,
  "Integrity proofs"); RLS is one compilation target, not the foundation.

## 2. The survey of authorization languages (`spec/dsl-survey.md`)

[`spec/dsl-survey.md`](https://github.com/neptunestation-com/pg_more_ls/blob/main/spec/dsl-survey.md)
(an architecture decision record, version 1.0, 2026-05-24, "Accepted") asked whether an existing
authorization language could replace the custom DSL as the source language. It graded five
candidates on eight dimensions traced to the algebra: Cedar (AWS), OPA/Rego (CNCF), the SpiceDB
schema language (Authzed), Oso Polar, and the custom DSL, with Zanzibar as a model, Casbin and
XACML 4.0 as honorable mentions (§2, §2.5). A scan for database-specific candidates (Postgres,
Supabase, CockroachDB, SQL Server, Kusto) found none with a non-SQL source language: every RLS
engine authors in SQL, which is the rejected starting point (§2).

The matrix (§2.1; A = excellent native fit down to F = not feasible without fundamental
compromise):

| Dimension | Cedar | OPA/Rego | SpiceDB | Oso Polar | Custom DSL |
|---|:-:|:-:|:-:|:-:|:-:|
| Primitive expressiveness | C | B- | D | C+ | A+ |
| Formal semantics | A+ | D | D | C | A |
| Decidability of fragment | A | C | B | C | A |
| Compilability to PG RLS | C | D | D | D | A |
| Schema integration | D | C | F | C | A |
| Verification tooling | A | C | D | D | A- |
| Ecosystem maturity | B | A | B | F | C |
| Onramp and migration | B | C+ | D | C | A- |

The verdict (§4): keep the custom language. Oso Polar is disqualified on sustainability (the
open-source library is deprecated), SpiceDB on category (a graph of relations over named
resources, not predicates over rows), XACML and Casbin on the absence of formal semantics and
compile-time analysis, and Rego is downgraded for having no denotational semantics to encode and
no upstream SQL emitter, only partial evaluation that external tooling may specialize (§2.3, §6.3). Cedar is the closest competitor and fails on three
requirements the custom language meets by construction (§4.1): its decisions are per
(principal, action, resource) rather than over row sets, its schema is a fixed declaration
rather than metadata the tool consumes, and it has no analogue of a selector that covers every
table with a given column. The survey's Cedar analysis (§6.1) records that Cedar Analysis names
four of the algebra's five checks (satisfiability, subsumption, redundancy, contradiction) and
can express the fifth, tenant isolation, as the same satisfiability question.

What the survey recommended borrowing from Cedar (§4.4), and where it went:

- **Mechanize the semantics in a proof assistant.** That is the Lean track (D11, D41): the
  three-valued evaluator, the effective-policy composition, the statement outcomes, the
  normalization, the templates against a relational model, and generated `#guard` vectors
  tying the Lean definitions to the Python (D43).
- **Differential testing against a reference.** In MLS the reference is Postgres itself: every
  compiler template has a differential test on PG 15 to 18 and a mutation that proves the test
  can fail (`CLAUDE.md`, invariant 3), rather than a second implementation as the oracle.
- **Align user-facing names with Cedar's analyses** ("shadowed permits", "impossible
  conditions"). Not adopted. The survey's hygiene checks became `mls lint` findings, not proof
  verdicts (D49), and the user-facing artifact is the English rendering of the spec (D33), which
  names the business rule rather than the analysis.

Two of the survey's premises do not hold for MLS, which is why its answer was retained but its
object was not. The persona was a security engineer authoring policies by hand; in MLS the spec
is written by an LLM from requirements and humans approve properties and scenarios, so the
canonical form is JSON with a schema (D15) and a surface syntax is a rendering of it, not the
language (D50, which also reverses the grammar-first ordering `pg_more_ls` followed: the
survey's §7.4 lists the ANTLR grammar as the algebra's BNF transcribed, and the AST after it). And
the ecosystem cost the survey accepted (the custom DSL's C on maturity) was the cost of a
language nobody else would read; a spec that an LLM writes and a reader checks in English does
not pay it in the same way.

## 3. The May 2026 performance notes (`spec/rls-exists/REPORT.md`)

[`spec/rls-exists/REPORT.md`](https://github.com/neptunestation-com/pg_more_ls/blob/main/spec/rls-exists/REPORT.md)
(PostgreSQL 17.7, 500k line items) asked whether an `EXISTS` chain inside a policy is planned
like the same chain written in the query. It is not: the policy is wrapped in a security-barrier
subquery that the pull-up pass refuses to flatten (`is_simple_subquery()` in
`prepjointree.c`), so the `EXISTS` becomes a hashed SubPlan filter on the scan, 10 to 20 times
slower than the hand-written join, while a flat column predicate under the same policy was the
fastest plan measured. `docs/FINDINGS.md` supersedes those notes: it measures the same effect at
1M rows on PG 16, adds the shapes the notes did not try, and finds the one that keeps the index
condition (`col = any(array(select ...))`), which is what the compiler emits (D14) and what the
EXPLAIN checks in the test suite hold it to.
