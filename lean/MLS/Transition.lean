import MLS.TVL
import MLS.Pred
import MLS.Policy
import MLS.Normalize
import MLS.Relational

/-!
# The transition template against the relational model (D11, step 5; issue #4)

A transition rule is a predicate over the two images of an update (`transition_scope` in
`spec/check.py`: `old`/`new` references and constants, nothing else); the compiler emits one
`BEFORE UPDATE` row trigger, `IF NOT ((p) IS TRUE) THEN RAISE` (`compile/transition.py`).
`MLS.Normalize` proved that the trigger lets through exactly the admitted pairs and
`MLS.Policy` placed it in the statement outcomes; here it meets the relational model of
`MLS.Relational`:

* `eval_twoImage`: a transition predicate reads only the `old` and `new` images — not the
  state, the relations or the row name — so what the trigger decided on the two images is what
  holds of the replacement in any state (`transition_guard`).
* `invariant_update_step`, `invariant_insert_step`: the induction step the solver discharges for
  the written row (`props/obligations.py:_induction_step`, case 1: the invariant and the
  constraints on `old`, the transition rules on `(old, new)`, the constraints on `new` entail the
  invariant on `new`) is enough for the state: a stored-row invariant that holds before a
  guarded replacement holds after it.  This is the transition counterpart of `check_update`.
* `immutable_values`: the implicit rule that fixes a hierarchy's parent column
  (`props/effective.py:implicit_transitions`, transcribed as `immutablePred`) is admitted exactly
  when the column is unchanged (`immutablePred_admits`), and so a guarded replacement leaves the
  set of stored values of that column unchanged — the state-level reading of `column_immutable`.
-/
namespace MLS

/-! ## The transition fragment -/

def Term.isImage : Term → Bool
  | .old _ => true
  | .new _ => true
  | .const _ => true
  | _ => false

/-- `transition_scope`: `old`/`new` references and constants only. -/
def Pred.twoImage : Pred → Bool
  | .lit _ => true
  | .and p q => p.twoImage && q.twoImage
  | .or p q => p.twoImage && q.twoImage
  | .not p => p.twoImage
  | .isNull t => t.isImage
  | .cmp l _ r => l.isImage && r.isImage
  | _ => false

variable {s : Schema} {e e' : Env} {row row' : String}

theorem Term.value_isImage (t : Term) (ht : t.isImage = true) (ho : e.col "old" = e'.col "old")
    (hn : e.col "new" = e'.col "new") : t.value e row = t.value e' row' := by
  cases t with
  | old n => show e.col "old" n = e'.col "old" n; rw [ho]
  | new n => show e.col "new" n = e'.col "new" n; rw [hn]
  | const v => rfl
  | _ => simp [Term.isImage] at ht

/-- A transition predicate depends on the two images only. -/
theorem eval_twoImage (ho : e.col "old" = e'.col "old") (hn : e.col "new" = e'.col "new") :
    ∀ p : Pred, p.twoImage = true → p.eval s e row = p.eval s e' row'
  | .lit _, _ => rfl
  | .and p q, hp => by
    simp only [Pred.twoImage, Bool.and_eq_true] at hp
    show TV.and (p.eval s e row) (q.eval s e row) = TV.and (p.eval s e' row') (q.eval s e' row')
    rw [eval_twoImage ho hn p hp.1, eval_twoImage ho hn q hp.2]
  | .or p q, hp => by
    simp only [Pred.twoImage, Bool.and_eq_true] at hp
    show TV.or (p.eval s e row) (q.eval s e row) = TV.or (p.eval s e' row') (q.eval s e' row')
    rw [eval_twoImage ho hn p hp.1, eval_twoImage ho hn q hp.2]
  | .not p, hp => by
    show TV.not (p.eval s e row) = TV.not (p.eval s e' row')
    rw [eval_twoImage ho hn p hp]
  | .isNull t, hp => by
    show ofBool (t.value e row).isNone = ofBool (t.value e' row').isNone
    rw [Term.value_isImage t hp ho hn]
  | .cmp l op r, hp => by
    simp only [Pred.twoImage, Bool.and_eq_true] at hp
    show liftCmp op.holds (l.value e row) (r.value e row) = liftCmp op.holds (l.value e' row') (r.value e' row')
    rw [Term.value_isImage l hp.1 ho hn, Term.value_isImage r hp.2 ho hn]
  | .member _ _ _, hp => by simp [Pred.twoImage] at hp
  | .inGroup _ _ _, hp => by simp [Pred.twoImage] at hp
  | .nowBetween _ _, hp => by simp [Pred.twoImage] at hp
  | .opaqueRef _, hp => by simp [Pred.twoImage] at hp

/-! ## The two images of a replacement -/

/-- The environment of a replacement: `old` is the stored row, every other image (`new`, `r`)
is the written one; relations are the state's. -/
def Env.ofImages (st : State) (x : Extra) (ρ ρ' : Row) : Env :=
  { col := fun img => if img = "old" then ρ else ρ', ctx := x.ctx, rel := st.rel,
    forest := x.forest, opaques := x.opaques }

variable {x : Extra} {st st' : State} {t : String} {ρ ρ' : Row}

theorem Env.ofImages_old : (Env.ofImages st x ρ ρ').col "old" = ρ := by
  simp [Env.ofImages]

theorem Env.ofImages_new : (Env.ofImages st x ρ ρ').col "new" = ρ' := by
  simp [Env.ofImages]

theorem Env.ofImages_r : (Env.ofImages st x ρ ρ').col "r" = ρ' := by
  simp [Env.ofImages]

/-- `IF NOT ((p) IS TRUE) THEN RAISE` on the two images. -/
def transitionPasses (s : Schema) (e : Env) (p : Pred) : Bool :=
  triggerPasses (TV.not (TV.isTrueOp (p.eval s e "new")))

theorem transitionPasses_admits (p : Pred) : transitionPasses s e p = admits s e "new" p :=
  trigger_normalized _

/-- What the trigger let through holds of the replacement in every state and under every row
name: the rule reads the two images only. -/
theorem transition_guard {p : Pred} (hp : p.twoImage = true)
    (hpass : transitionPasses s (Env.ofImages st x ρ ρ') p = true) (st'' : State) (row : String) :
    admits s (Env.ofImages st'' x ρ ρ') row p = true := by
  rw [transitionPasses_admits] at hpass
  unfold admits at hpass ⊢
  rw [eval_twoImage (e := Env.ofImages st'' x ρ ρ') (e' := Env.ofImages st x ρ ρ') (row' := "new")
    rfl rfl p hp]
  exact hpass

/-! ## The induction step of `invariant_preserved`, lifted to the state -/

variable {I c p : Pred}

/-- `_induction_step`, UPDATE, the written row: the solver's obligation for one `(old, new)` pair,
with the stored `old` row satisfying the invariant and the constraints, the transition rules
holding on the pair and the constraints on `new`, suffices for every guarded replacement to
keep the invariant on the whole table. -/
theorem invariant_update_step (hI : I.rowOnly = true) (hc : c.rowOnly = true)
    (step : ∀ ρ ρ', admits s (Env.ofImages st x ρ ρ') "old" I = true
      → admits s (Env.ofImages st x ρ ρ') "old" c = true
      → admits s (Env.ofImages st x ρ ρ') "new" p = true
      → admits s (Env.ofImages st x ρ ρ') "new" c = true
      → admits s (Env.ofImages st x ρ ρ') "new" I = true)
    (hu : Updated st st' t ρ ρ') (hρ : st.rows t ρ)
    (hinv : CheckHolds s x st t I) (hchk : CheckHolds s x st t c)
    (hpass : transitionPasses s (Env.ofImages st x ρ ρ') p = true)
    (hnew : checkPasses (TV.isTrueOp (c.eval s (Env.ofImages st x ρ ρ') "new")) = true) :
    CheckHolds s x st' t I := by
  intro ρ'' h''
  rw [admits_ofRow (st := st) hI]
  rw [hu] at h''
  rcases h'' with ⟨h'', _⟩ | ⟨_, heq⟩
  · exact hinv ρ'' h''
  · rw [heq]
    have hIold : admits s (Env.ofImages st x ρ ρ') "old" I = true := by
      have := hinv ρ hρ
      unfold admits at this ⊢
      rwa [eval_rowOnly (e := Env.ofImages st x ρ ρ') (e' := Env.ofRow st x ρ) (row' := "r") rfl I hI]
    have hcold : admits s (Env.ofImages st x ρ ρ') "old" c = true := by
      have := hchk ρ hρ
      unfold admits at this ⊢
      rwa [eval_rowOnly (e := Env.ofImages st x ρ ρ') (e' := Env.ofRow st x ρ) (row' := "r") rfl c hc]
    rw [transitionPasses_admits] at hpass
    rw [check_normalized] at hnew
    have := step ρ ρ' hIold hcold hpass hnew
    unfold admits at this ⊢
    rwa [eval_rowOnly (e := Env.ofRow st x ρ') (e' := Env.ofImages st x ρ ρ') (row := "r") (row' := "new")
      rfl I hI]

/-- `_induction_step`, INSERT, the written row: the constraints on `new` entail the invariant. -/
theorem invariant_insert_step (hI : I.rowOnly = true)
    (step : ∀ ρ', admits s (Env.ofRow st x ρ') "r" c = true → admits s (Env.ofRow st x ρ') "r" I = true)
    (hi : Inserted st st' t ρ') (hinv : CheckHolds s x st t I)
    (hnew : checkPasses (TV.isTrueOp (c.eval s (Env.ofRow st x ρ') "r")) = true) :
    CheckHolds s x st' t I := by
  intro ρ'' h''
  rw [admits_ofRow (st := st) hI]
  rw [hi] at h''
  rcases h'' with h'' | ⟨_, heq⟩
  · exact hinv ρ'' h''
  · rw [heq]
    rw [check_normalized] at hnew
    exact step ρ' hnew

/-! ## An immutable column -/

/-- `implicit_transitions`: `(is_null(old.c) ∧ is_null(new.c)) ∨ new.c = old.c` — the NULL-aware
"parent never changes" rule of every hierarchy (`mls_h_<h>_parent_immutable`, D35). -/
def immutablePred (c : String) : Pred :=
  .or (.and (.isNull (.old c)) (.isNull (.new c))) (.cmp (.new c) .eq (.old c))

theorem immutablePred_twoImage (c : String) : (immutablePred c).twoImage = true := rfl

/-- The rule is admitted exactly when the column is unchanged, NULL included. -/
theorem immutablePred_admits (c : String) :
    admits s e row (immutablePred c) = decide (e.col "new" c = e.col "old" c) := by
  unfold admits immutablePred
  simp only [Pred.eval, Term.value]
  cases e.col "old" c with
  | none => cases e.col "new" c <;> simp [liftCmp, ofBool, TV.or, TV.and, TV.isTrue]
  | some a =>
    cases e.col "new" c with
    | none => simp [liftCmp, ofBool, TV.or, TV.and, TV.isTrue]
    | some b => by_cases h : b = a <;> simp [liftCmp, ofBool, Op.holds, TV.or, TV.and, TV.isTrue, h]

/-- A guarded replacement leaves the set of stored values of an immutable column unchanged. -/
theorem immutable_values (c : String) (hu : Updated st st' t ρ ρ') (hρ : st.rows t ρ)
    (hpass : transitionPasses s (Env.ofImages st x ρ ρ') (immutablePred c) = true) (v : Option Int) :
    (∃ ρ'', st'.rows t ρ'' ∧ ρ'' c = v) ↔ (∃ ρ'', st.rows t ρ'' ∧ ρ'' c = v) := by
  rw [transitionPasses_admits, immutablePred_admits, Env.ofImages_new, Env.ofImages_old,
    decide_eq_true_iff] at hpass
  constructor
  · rintro ⟨ρ'', h'', hv⟩
    rcases (hu t ρ'').mp h'' with ⟨h'', _⟩ | ⟨_, rfl⟩
    · exact ⟨ρ'', h'', hv⟩
    · exact ⟨ρ, hρ, hpass ▸ hv⟩
  · rintro ⟨ρ'', h'', hv⟩
    by_cases heq : ρ'' = ρ
    · subst heq
      exact ⟨ρ', (hu t ρ').mpr (Or.inr ⟨rfl, rfl⟩), hpass.trans hv⟩
    · exact ⟨ρ'', (hu t ρ'').mpr (Or.inl ⟨h'', fun h => heq h.2⟩), hv⟩

end MLS
