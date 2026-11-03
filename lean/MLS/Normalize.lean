import MLS.TVL
import MLS.Encoding
import MLS.Pred
import MLS.Policy

/-!
# NULL normalization (SEMANTICS §4; CLAUDE.md invariant 2)

How each Postgres target decides on a three-valued result:

* RLS: a row passes a policy expression iff it is TRUE.
* CHECK: a row violates the constraint iff the expression is FALSE; UNKNOWN passes.
* A trigger `IF cond THEN RAISE`: PL/pgSQL takes the branch iff `cond` is TRUE, so `IF NOT (p)`
  raises iff `p` is FALSE and lets UNKNOWN through.
* FK with MATCH SIMPLE: any NULL among the referencing columns satisfies the constraint.

The compiler therefore emits `CHECK ((p) IS TRUE)` (`compile/check.py`) and
`IF NOT ((p) IS TRUE) THEN RAISE` (`compile/transition.py`); the two `*_NAIVE` mutations of
`compile/mutations.py` drop the `IS TRUE`.  The theorems: with the normalization every target
admits exactly the rows the admission rule admits (`one_meaning`), and without it the targets
disagree exactly on UNKNOWN (`check_naive_disagrees`, `trigger_naive_disagrees`), which is why the
differential tests can catch the mutations.  For FKs, the inclusion predicate the proofs use —
`is_null(c1) ∨ is_null(c2) ∨ member(rel, c1, c2)` over the nullable columns
(`props/effective.py:inclusion_pred`) — is admitted exactly when MATCH SIMPLE is satisfied
(`inclusion_matches_fk`).
-/
namespace MLS

/-- `v IS TRUE`: two-valued. -/
def TV.isTrueOp (v : TV) : TV := if TV.isTrue v then .tt else .ff

/-! ## The targets -/

/-- RLS: rows for which the expression returns TRUE. -/
def rlsPasses (v : TV) : Bool := TV.isTrue v

/-- CHECK: violated only when FALSE. -/
def checkPasses (v : TV) : Bool := !TV.isFalse v

/-- `IF cond THEN RAISE`: the branch is taken iff `cond` is TRUE. -/
def triggerPasses (cond : TV) : Bool := !TV.isTrue cond

/-- FK MATCH SIMPLE on two referencing columns. -/
def fkMatchSimple (rel : Int → Int → Bool) : Option Int → Option Int → Bool
  | some a, some b => rel a b
  | some _, none => true
  | none, _ => true

/-! ## Without normalization -/

/-- `CHECK (p)` accepts a row on which `p` is UNKNOWN (FINDINGS: a naive CHECK accepted a NULL). -/
theorem check_passes_unknown : checkPasses .unk = true := rfl

/-- `IF NOT (p)` does not raise when `p` is UNKNOWN. -/
theorem trigger_naive_passes_unknown : triggerPasses (TV.not .unk) = true := rfl

/-- RLS and a naive CHECK disagree exactly on UNKNOWN. -/
theorem check_naive_disagrees (v : TV) : (rlsPasses v ≠ checkPasses v) ↔ v = .unk := by
  cases v <;> decide

/-- RLS and a naive trigger disagree exactly on UNKNOWN. -/
theorem trigger_naive_disagrees (v : TV) : (rlsPasses v ≠ triggerPasses (TV.not v)) ↔ v = .unk := by
  cases v <;> decide

/-! ## With normalization -/

theorem rls_is_admission (v : TV) : rlsPasses v = TV.isTrue v := rfl

/-- `CHECK ((p) IS TRUE)` admits exactly the rows the admission rule admits. -/
theorem check_normalized (v : TV) : checkPasses (TV.isTrueOp v) = TV.isTrue v := by
  cases v <;> rfl

/-- `IF NOT ((p) IS TRUE) THEN RAISE` lets through exactly the rows the admission rule admits. -/
theorem trigger_normalized (v : TV) : triggerPasses (TV.not (TV.isTrueOp v)) = TV.isTrue v := by
  cases v <;> rfl

/-- One meaning per predicate across targets. -/
theorem one_meaning (v : TV) :
    rlsPasses v = TV.isTrue v
      ∧ checkPasses (TV.isTrueOp v) = TV.isTrue v
      ∧ triggerPasses (TV.not (TV.isTrueOp v)) = TV.isTrue v :=
  ⟨rfl, check_normalized v, trigger_normalized v⟩

/-- What the proofs assume of a stored row (`T` of the encoded constraint, `MLS.Pred.admitted`)
is what the normalized CHECK enforces. -/
theorem check_matches_proof {s : Schema} {σ : Valuation} {e : Env} (h : Agrees s σ e)
    (hop : ∀ n img, (e.opaques n img).consistent = true) (hw : e.hierarchiesWellFormed s)
    (row : String) (p : Pred) :
    checkPasses (TV.isTrueOp (p.eval s e row)) = (p.tf s σ row).T := by
  rw [check_normalized, Pred.admitted h hop hw row p]

/-- Likewise for a transition rule and its trigger. -/
theorem trigger_matches_proof {s : Schema} {σ : Valuation} {e : Env} (h : Agrees s σ e)
    (hop : ∀ n img, (e.opaques n img).consistent = true) (hw : e.hierarchiesWellFormed s)
    (row : String) (p : Pred) :
    triggerPasses (TV.not (TV.isTrueOp (p.eval s e row))) = (p.tf s σ row).T := by
  rw [trigger_normalized, Pred.admitted h hop hw row p]

/-! ## FK MATCH SIMPLE -/

/-- `inclusion_pred`: `is_null(c)` for each nullable column, then the membership atom, `_or`-ed. -/
def inclusionPred (nullable1 nullable2 : Bool) (rel c1 c2 : String) : Pred :=
  disj ((if nullable1 then [Pred.isNull (.col c1)] else [])
        ++ (if nullable2 then [Pred.isNull (.col c2)] else [])
        ++ [Pred.member rel (.col c1) (.col c2)])

variable {s : Schema} {e : Env} {row : String}

/-- Under the schema (a non-nullable column holds no NULL), the inclusion predicate is admitted
exactly when the FK with MATCH SIMPLE is satisfied. -/
theorem inclusion_matches_fk (hs : ∀ c, s.nullable c = false → (e.col row c).isSome = true)
    (rel c1 c2 : String) :
    admits s e row (inclusionPred (s.nullable c1) (s.nullable c2) rel c1 c2)
      = fkMatchSimple (e.rel rel) (e.col row c1) (e.col row c2) := by
  unfold inclusionPred admits
  rw [disj_eval, TV.isTrue_orList]
  cases h1 : s.nullable c1 <;> cases h2 : s.nullable c2 <;>
    cases hc1 : e.col row c1 <;> cases hc2 : e.col row c2 <;>
    (try have := hs c1 h1) <;> (try have := hs c2 h2) <;>
    simp_all [Pred.eval, Term.value, liftCmp, TV.isTrue_ofBool, fkMatchSimple]

end MLS
