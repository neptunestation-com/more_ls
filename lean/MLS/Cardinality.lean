import MLS.Relational

/-!
# The cardinality template (D4, D36; issue #4)

`cardinality(table, group, parent, max)`: at most `N` stored rows per value of `group`.  The
compiled trigger (`compile/cardinality.py`) runs `BEFORE INSERT OR UPDATE OF group`: a NULL
group is not counted, an UPDATE that keeps its group is skipped, otherwise it locks the parent
row `FOR UPDATE`, counts the rows of the group and raises when `count + 1 > N`.

Rows are a bag here (a `List`), not the set of `MLS.Relational`: the count of a group is a
count.  Statements are a `cons` (INSERT), the replacement of one occurrence (UPDATE) and the
removal of one occurrence (DELETE).

* Sequential: the trigger's test keeps the bound across every statement (`insert_bound`,
  `update_bound`, `delete_bound`), the per-statement count axioms the solver is given
  (`props/obligations.py:cardinality_obligations`) follow from the bag (`count_insert_axioms`,
  `count_delete_axioms`), and the `CARDINALITY_OFF_BY_ONE` test lets one row too many in
  (`off_by_one_overshoots`).
* Concurrent: the model has no lock; what the lock buys is a *schedule*.  Under READ COMMITTED
  a writer that holds the parent row counts the committed rows and no other writer of the group
  commits before it does (D4), so every attempt counted the bag it extends: `Serial`.  A serial
  run keeps the bound (`serial_bound`).  Without the lock two writers can count the same bag
  with one place left and both commit (`unlocked_overshoot`): the `CARDINALITY_NO_LOCK` mutation
  that `tests/templates/test_cardinality.py` observes.  Whether `FOR UPDATE` under READ
  COMMITTED yields a serial schedule is the assumption the proof report states; it is not
  provable here (under REPEATABLE READ it does not: the count would use the snapshot).
-/
namespace MLS

/-- `select count(*) from T where group = v`. -/
def count (g : String) (v : Int) (l : List Row) : Nat :=
  (l.filter fun ρ => decide (ρ g = some v)).length

/-- At most `N` rows per group value. -/
def Bound (g : String) (N : Nat) (l : List Row) : Prop := ∀ v, count g v l ≤ N

variable {g : String} {N : Nat} {v : Int} {l l1 l2 : List Row} {ρ ρ' : Row}

theorem count_nil : count g v [] = 0 := rfl

theorem count_cons (ρ : Row) (l : List Row) :
    count g v (ρ :: l) = (if ρ g = some v then 1 else 0) + count g v l := by
  by_cases h : ρ g = some v <;> simp [count, h, Nat.add_comm]

theorem count_append (l1 l2 : List Row) : count g v (l1 ++ l2) = count g v l1 + count g v l2 := by
  simp [count, List.filter_append]

/-! ## The trigger -/

/-- The trigger's decision against the committed bag `l`: NULL is not counted, an UPDATE that
keeps its group is skipped (`is not distinct from`), otherwise `count + 1 > N` raises. -/
def cardPasses (g : String) (N : Nat) (l : List Row) (old : Option Row) (new : Row) : Bool :=
  match new g with
  | none => true
  | some v => if old.any (fun ρ => decide (ρ g = some v)) then true else decide (count g v l + 1 ≤ N)

/-- `CARDINALITY_OFF_BY_ONE`: `n > N` instead of `n + 1 > N`. -/
def cardPassesNaive (g : String) (N : Nat) (l : List Row) (old : Option Row) (new : Row) : Bool :=
  match new g with
  | none => true
  | some v => if old.any (fun ρ => decide (ρ g = some v)) then true else decide (count g v l ≤ N)

/-- INSERT. -/
theorem insert_bound (hB : Bound g N l) (hpass : cardPasses g N l none ρ' = true) :
    Bound g N (ρ' :: l) := by
  intro v
  rw [count_cons]
  cases hn : ρ' g with
  | none => simpa using hB v
  | some w =>
    by_cases hw : w = v
    · subst hw
      simp only [cardPasses, hn, Option.any_none, Bool.false_eq_true, ↓reduceIte,
        decide_eq_true_eq] at hpass
      simp
      omega
    · simpa [hw] using hB v

/-- UPDATE: `ρ` is replaced by `ρ'`; the trigger counted the bag with `ρ` still in it. -/
theorem update_bound (hB : Bound g N (l1 ++ ρ :: l2))
    (hpass : cardPasses g N (l1 ++ ρ :: l2) (some ρ) ρ' = true) : Bound g N (l1 ++ ρ' :: l2) := by
  intro v
  have hb := hB v
  rw [count_append, count_cons] at hb ⊢
  cases hn : ρ' g with
  | none => simp; omega
  | some w =>
    by_cases hw : w = v
    · subst hw
      simp only [cardPasses, hn, Option.any_some] at hpass
      by_cases hold : ρ g = some w
      · simp only [hold, ↓reduceIte] at hb
        simpa using hb
      · simp only [hold, decide_false, Bool.false_eq_true, ↓reduceIte, decide_eq_true_eq,
          count_append, count_cons] at hpass hb
        simp
        omega
    · simp [hw]
      omega

/-- DELETE: nothing to check. -/
theorem delete_bound (hB : Bound g N (l1 ++ ρ :: l2)) : Bound g N (l1 ++ l2) := by
  intro v
  have hb := hB v
  rw [count_append, count_cons] at hb
  rw [count_append]
  omega

/-! ## The solver's count axioms (`cardinality_obligations`) -/

/-- INSERT: `cnt'(x) = cnt(x) + 1 ⇔ ¬new.null ∧ x = new.group`, else `cnt'(x) = cnt(x)`. -/
theorem count_insert_axioms (ρ' : Row) (l : List Row) (v : Int) :
    (ρ' g = some v → count g v (ρ' :: l) = count g v l + 1)
      ∧ (ρ' g ≠ some v → count g v (ρ' :: l) = count g v l) := by
  constructor <;> intro h <;> simp [count_cons, h, Nat.add_comm]

/-- DELETE: `cnt'(x) = cnt(x) - 1 ⇔ ¬old.null ∧ x = old.group`, else `cnt'(x) = cnt(x)`. -/
theorem count_delete_axioms (l1 l2 : List Row) (ρ : Row) (v : Int) :
    (ρ g = some v → count g v (l1 ++ l2) + 1 = count g v (l1 ++ ρ :: l2))
      ∧ (ρ g ≠ some v → count g v (l1 ++ l2) = count g v (l1 ++ ρ :: l2)) := by
  constructor <;> intro h <;> simp [count_append, count_cons, h] <;> omega

/-! ## `CARDINALITY_OFF_BY_ONE` -/

/-- With the group full, the naive test passes and the bound is exceeded. -/
theorem off_by_one_overshoots (hc : count g v l = N) (h : ρ' g = some v) :
    cardPassesNaive g N l none ρ' = true ∧ count g v (ρ' :: l) = N + 1 := by
  constructor
  · simp [cardPassesNaive, h, hc]
  · simp [count_cons, h, hc, Nat.add_comm]

/-! ## Schedules -/

/-- One writer's attempt: the committed bag its trigger counted and the row it inserts. -/
structure Attempt where
  seen : List Row
  row : Row

/-- A run of successful inserts in which every attempt counted the bag it extends: what the
parent-row lock provides under READ COMMITTED (D4). -/
def Serial (g : String) (N : Nat) : List Row → List Attempt → List Row → Prop
  | l, [], l' => l' = l
  | l, a :: as, l' => a.seen = l ∧ cardPasses g N a.seen none a.row = true ∧ Serial g N (a.row :: l) as l'

theorem serial_bound (as : List Attempt) : ∀ (l l' : List Row), Bound g N l →
    Serial g N l as l' → Bound g N l' := by
  induction as with
  | nil =>
    intro l l' hB h
    simp only [Serial] at h
    subst h
    exact hB
  | cons a as ih =>
    intro l l' hB h
    obtain ⟨hseen, hpass, hrest⟩ := h
    subst hseen
    exact ih _ l' (insert_bound hB hpass) hrest

/-- `CARDINALITY_NO_LOCK`: two writers count the same bag with one place left; both pass, and
together they overshoot. -/
theorem unlocked_overshoot (ρ1 ρ2 : Row) (hc : count g v l + 1 = N) (h1 : ρ1 g = some v)
    (h2 : ρ2 g = some v) :
    cardPasses g N l none ρ1 = true ∧ cardPasses g N l none ρ2 = true
      ∧ count g v (ρ2 :: ρ1 :: l) = N + 1 := by
  refine ⟨?_, ?_, ?_⟩
  · simp [cardPasses, h1, hc]
  · simp [cardPasses, h2, hc]
  · simp [count_cons, h1, h2]
    omega

end MLS
