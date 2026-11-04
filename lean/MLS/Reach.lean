import MLS.Hierarchy
import MLS.Pred

/-!
# The compiled hierarchy helper (D35; issue #4)

`MLS.Hierarchy` proved the encoder's unrolling equal to the reference walk.  The compiled side
is the helper `mls_reach_<h>(k)` of `compile/hierarchy.py:reach_fn_ddl`: a recursive CTE that
starts from the user's groups and descends `max_depth` levels, and the policy tests
`value = any(mls_reach_<h>(key))`.  The walk goes up from the row's group, the CTE comes down
from the user's groups; `Forest.reach_iff_walk` says they meet: `g` is among the ids the CTE
returns iff the walk from `g` over `max_depth + 1` nodes finds one of the user's groups, which
is `in_group` (`reach_iff_inGroup`).  `reach_shallow_misses` is the content of the
`HIERARCHY_SHALLOW` mutation: one level less misses a group at the bound.

The CTE's `stored` is every row of the hierarchy table because the helper runs as
`mls_internal` with a `USING (true)` policy on that table (D34); run as the caller it would see
the caller's rows only and the walk would stop early.  That privilege question is outside the
model: `Forest.stored` is the table.

The depth trigger (`depth_fn_ddl`: `0` for a root, the parent's depth `+ 1` otherwise, raising
when the parent is missing) and the CHECK on the depth keep `Forest.Depths` — the invariant the
exactness of the unrolling rests on — across an insert (`depths_insert`); without the CHECK a
node under a node at the bound exceeds it (`depths_insert_unchecked`, the
`HIERARCHY_NO_DEPTH_CHECK` mutation).
-/
namespace MLS

/-! ## The recursive CTE -/

/-- Level `i` of `with recursive r(id, lvl)`: level 0 is the stored groups of the user, level
`i + 1` the stored children of level `i`. -/
def Forest.level (f : Forest) (rel : Int → Int → Bool) (key : Int) : Nat → Int → Prop
  | 0, g => f.stored g = true ∧ rel key g = true
  | i + 1, c => f.stored c = true ∧ ∃ r, f.parent c = some r ∧ f.level rel key i r

/-- `mls_reach_<h>(k)`: the ids of the levels `0 .. depth` (`where r.lvl < depth`). -/
def Forest.reach (f : Forest) (rel : Int → Int → Bool) (key : Int) (depth : Nat) (g : Int) : Prop :=
  ∃ i, i ≤ depth ∧ f.level rel key i g

variable {f : Forest} {rel : Int → Int → Bool} {key : Int}

theorem Forest.reach_zero (g : Int) : f.reach rel key 0 g ↔ f.level rel key 0 g := by
  constructor
  · rintro ⟨i, hi, h⟩
    have : i = 0 := by omega
    subst this
    exact h
  · intro h
    exact ⟨0, Nat.le_refl 0, h⟩

theorem Forest.reach_succ (depth : Nat) (g : Int) :
    f.reach rel key (depth + 1) g
      ↔ f.level rel key 0 g ∨ (f.stored g = true ∧ ∃ r, f.parent g = some r ∧ f.reach rel key depth r) := by
  constructor
  · rintro ⟨i, hi, h⟩
    cases i with
    | zero => exact Or.inl h
    | succ j =>
      obtain ⟨hs, r, hp, hl⟩ := h
      exact Or.inr ⟨hs, r, hp, j, by omega, hl⟩
  · rintro (h | ⟨hs, r, hp, j, hj, hl⟩)
    · exact ⟨0, by omega, h⟩
    · exact ⟨j + 1, by omega, hs, r, hp, hl⟩

/-- Descending `depth` levels from the user's groups reaches `g` iff walking up from `g` over
`depth + 1` nodes meets one of them. -/
theorem Forest.reach_iff_walk : ∀ (depth : Nat) (g : Int),
    f.reach rel key depth g ↔ f.walk rel key g (depth + 1) = true
  | 0, g => by
    rw [Forest.reach_zero]
    show _ ↔ (f.stored g && (rel key g || (f.parent g).any fun p => f.walk rel key p 0)) = true
    cases f.parent g <;> simp [Forest.level, Forest.walk]
  | depth + 1, g => by
    rw [Forest.reach_succ]
    show _ ↔ (f.stored g && (rel key g || (f.parent g).any fun p => f.walk rel key p (depth + 1))) = true
    cases hp : f.parent g with
    | none => simp [Forest.level]
    | some r =>
      simp only [Forest.level, Option.any_some, Bool.and_eq_true, Bool.or_eq_true, Option.some.injEq,
        ← Forest.reach_iff_walk depth r]
      constructor
      · rintro (⟨hs, hr⟩ | ⟨hs, r', hr', hl⟩)
        · exact ⟨hs, Or.inl hr⟩
        · subst hr'
          exact ⟨hs, Or.inr hl⟩
      · rintro ⟨hs, hr | hl⟩
        · exact Or.inl ⟨hs, hr⟩
        · exact Or.inr ⟨hs, r, rfl, hl⟩

/-- The helper computes `in_group`: with `depth = max_depth`, `g ∈ mls_reach_<h>(k)` is
`Env.inGroup`. -/
theorem reach_iff_inGroup (s : Schema) (e : Env) (hn : String) (k g : Int) :
    (e.forest hn).reach (e.rel (s.hier hn).members) k (s.hier hn).maxDepth g ↔ e.inGroup s hn k g = true :=
  Forest.reach_iff_walk _ _

/-! ## The `HIERARCHY_SHALLOW` mutation -/

/-- A root `0` with one child `1`. -/
def chain : Forest := ⟨fun x => decide (x = 0 ∨ x = 1), fun x => if x = 1 then some 0 else none⟩

/-- The user is in the root. -/
def rootMember : Int → Int → Bool := fun _ g => decide (g = 0)

/-- With `max_depth = 1` the child is in the user's groups (the walk finds the root), but the
helper stopped one level short (`depth = 0`) returns the root only. -/
theorem reach_shallow_misses :
    chain.walk rootMember 7 1 2 = true ∧ ¬ chain.reach rootMember 7 0 1 := by
  refine ⟨by decide, ?_⟩
  rw [Forest.reach_zero]
  simp [Forest.level, chain, rootMember]

/-! ## The depth trigger -/

/-- Insert node `x` with parent `p` (`x` not stored before). -/
def Forest.insertNode (f : Forest) (x : Int) (p : Option Int) : Forest :=
  ⟨fun y => decide (y = x) || f.stored y, fun y => if y = x then p else f.parent y⟩

/-- `depth_fn_ddl`: `0` for a root, the parent's depth `+ 1` otherwise. -/
def triggerDepth (depth : Int → Nat) : Option Int → Nat
  | none => 0
  | some q => depth q + 1

/-- The stored depth after the trigger set it on the new row. -/
def setDepth (depth : Int → Nat) (x : Int) (d : Nat) : Int → Nat :=
  fun y => if y = x then d else depth y

/-- An insert keeps `Forest.Depths`: the trigger computes the new depth from a stored parent
(the FK: `hparent`), the CHECK bounds it (`hcheck`), and the stored rows' parents are stored
(`hw`), so no stored row has the new node as parent. -/
theorem depths_insert {depth : Int → Nat} {maxDepth : Nat} (hd : f.Depths depth maxDepth)
    (hw : f.WellFormed rel) (x : Int) (p : Option Int) (hx : f.stored x = false)
    (hparent : ∀ q, p = some q → f.stored q = true)
    (hcheck : triggerDepth depth p ≤ maxDepth) :
    (f.insertNode x p).Depths (setDepth depth x (triggerDepth depth p)) maxDepth := by
  constructor
  · intro y q hs hp
    simp only [Forest.insertNode, Bool.or_eq_true, decide_eq_true_eq] at hs hp
    by_cases hy : y = x
    · subst hy
      simp only [↓reduceIte] at hp
      subst hp
      have hq : f.stored q = true := hparent q rfl
      have hqx : q ≠ y := fun h => by rw [h, hx] at hq; cases hq
      simp [setDepth, hqx, triggerDepth]
    · simp only [hy, ↓reduceIte] at hp
      have hs' : f.stored y = true := by
        rcases hs with h | h
        · exact absurd h hy
        · exact h
      have hq : f.stored q = true := hw.parent_stored y q hs' hp
      have hqx : q ≠ x := fun h => by rw [h, hx] at hq; cases hq
      simp [setDepth, hy, hqx, hd.child y q hs' hp]
  · intro y hs
    simp only [Forest.insertNode, Bool.or_eq_true, decide_eq_true_eq] at hs
    by_cases hy : y = x
    · subst hy
      simpa [setDepth] using hcheck
    · have hs' : f.stored y = true := by
        rcases hs with h | h
        · exact absurd h hy
        · exact h
      simpa [setDepth, hy] using hd.bound y hs'

/-- Without the CHECK: a node under a node at the bound gets depth `maxDepth + 1`. -/
theorem depths_insert_unchecked {depth : Int → Nat} {maxDepth : Nat} (q : Int)
    (hq : depth q = maxDepth) : maxDepth < triggerDepth depth (some q) := by
  simp [triggerDepth, hq]

end MLS
