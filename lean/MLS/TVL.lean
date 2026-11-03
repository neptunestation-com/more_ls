/-!
# Three-valued logic (SEMANTICS §1)

SQL truth values and the connectives on them: AND (FALSE dominates, then UNKNOWN), OR (TRUE
dominates, then UNKNOWN), NOT (swaps TRUE and FALSE, fixes UNKNOWN).  The admission rule: a
target admits a row iff the predicate is TRUE.

This file is the reference the Python encoding (`src/mls/logic/tvl.py`) is proved against in
`MLS.Encoding` and `MLS.Pred`.
-/
namespace MLS

/-- A SQL truth value. -/
inductive TV where
  | tt
  | ff
  | unk
  deriving DecidableEq, Repr

namespace TV

/-- AND: FALSE dominates, then UNKNOWN. -/
def and : TV → TV → TV
  | ff, _ => ff
  | _, ff => ff
  | tt, tt => tt
  | _, _ => unk

/-- OR: TRUE dominates, then UNKNOWN. -/
def or : TV → TV → TV
  | tt, _ => tt
  | _, tt => tt
  | ff, ff => ff
  | _, _ => unk

/-- NOT swaps TRUE and FALSE and fixes UNKNOWN. -/
def not : TV → TV
  | tt => ff
  | ff => tt
  | unk => unk

/-- The n-ary connectives the encoder uses (`and_(*ts)` in the Python). -/
def andList (vs : List TV) : TV := vs.foldr TV.and tt
def orList (vs : List TV) : TV := vs.foldr TV.or ff

/-- Admission (SEMANTICS §1): every target admits a row iff the predicate is TRUE. -/
def isTrue : TV → Bool
  | tt => true
  | _ => false

def isFalse : TV → Bool
  | ff => true
  | _ => false

/-! ## Truth tables -/

theorem and_comm (a b : TV) : TV.and a b = TV.and b a := by
  cases a <;> cases b <;> rfl

theorem and_assoc (a b c : TV) : TV.and (TV.and a b) c = TV.and a (TV.and b c) := by
  cases a <;> cases b <;> cases c <;> rfl

theorem or_comm (a b : TV) : TV.or a b = TV.or b a := by
  cases a <;> cases b <;> rfl

theorem or_assoc (a b c : TV) : TV.or (TV.or a b) c = TV.or a (TV.or b c) := by
  cases a <;> cases b <;> cases c <;> rfl

theorem not_not (a : TV) : TV.not (TV.not a) = a := by
  cases a <;> rfl

/-- De Morgan holds in three-valued logic. -/
theorem not_and (a b : TV) : TV.not (TV.and a b) = TV.or (TV.not a) (TV.not b) := by
  cases a <;> cases b <;> rfl

theorem not_or (a b : TV) : TV.not (TV.or a b) = TV.and (TV.not a) (TV.not b) := by
  cases a <;> cases b <;> rfl

/-- FALSE dominates AND ... -/
theorem and_ff (a : TV) : TV.and a ff = ff := by
  cases a <;> rfl

/-- ... then UNKNOWN. -/
theorem and_unk (a : TV) (h : a ≠ ff) : TV.and a unk = unk := by
  cases a <;> first | rfl | exact absurd rfl h

/-- TRUE dominates OR ... -/
theorem or_tt (a : TV) : TV.or a tt = tt := by
  cases a <;> rfl

/-- ... then UNKNOWN. -/
theorem or_unk (a : TV) (h : a ≠ tt) : TV.or a unk = unk := by
  cases a <;> first | rfl | exact absurd rfl h

/-! ## Admission

A row is admitted iff the predicate is TRUE.  Conjunction and disjunction commute with
admission, negation does not: that is why a two-valued shortcut is a bug (CLAUDE.md,
invariant 1). -/

theorem isTrue_and (a b : TV) : isTrue (TV.and a b) = (isTrue a && isTrue b) := by
  cases a <;> cases b <;> rfl

theorem isTrue_or (a b : TV) : isTrue (TV.or a b) = (isTrue a || isTrue b) := by
  cases a <;> cases b <;> rfl

/-- `NOT p` is admitted iff `p` is FALSE, not iff `p` is not admitted. -/
theorem isTrue_not (a : TV) : isTrue (TV.not a) = isFalse a := by
  cases a <;> rfl

/-- The two-valued shortcut `admitted (NOT p) = ¬ admitted p` is wrong on UNKNOWN. -/
theorem two_valued_shortcut_wrong : isTrue (TV.not unk) ≠ (!isTrue unk) := by
  decide

end TV

end MLS
