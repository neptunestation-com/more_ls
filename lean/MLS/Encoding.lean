import MLS.TVL

/-!
# The pair encoding (SEMANTICS §1, `src/mls/logic/tvl.py`)

Every predicate becomes a pair `(T, F)` of booleans: `T` = "is TRUE", `F` = "is FALSE";
UNKNOWN is `¬T ∧ ¬F`.  The connectives on pairs are exactly those `Encoder.tf` emits:

    And:  (⋀ Tᵢ, ⋁ Fᵢ)      Or:  (⋁ Tᵢ, ⋀ Fᵢ)      Not:  (F, T)

The theorems say the pair operations are the three-valued connectives in disguise.
-/
namespace MLS

/-- `T` = "is TRUE", `F` = "is FALSE". -/
structure Pair where
  T : Bool
  F : Bool
  deriving DecidableEq, Repr

namespace Pair

/-- A pair stands for a truth value only when it is not both TRUE and FALSE.  The encoder
asserts this as a side condition for every opaque block. -/
def consistent (p : Pair) : Bool := !(p.T && p.F)

def encode : TV → Pair
  | .tt => ⟨true, false⟩
  | .ff => ⟨false, true⟩
  | .unk => ⟨false, false⟩

def decode (p : Pair) : TV :=
  if p.T then .tt else if p.F then .ff else .unk

/-- `And`: all TRUE / some FALSE. -/
def and (a b : Pair) : Pair := ⟨a.T && b.T, a.F || b.F⟩

/-- `Or`: some TRUE / all FALSE. -/
def or (a b : Pair) : Pair := ⟨a.T || b.T, a.F && b.F⟩

/-- `Not`: swap. -/
def not (a : Pair) : Pair := ⟨a.F, a.T⟩

/-- The n-ary forms the encoder emits: `and_(*[t for t, _ in pairs]), or_(*[f for _, f in pairs])`. -/
def andList (ps : List Pair) : Pair := ⟨ps.all (·.T), ps.any (·.F)⟩
def orList (ps : List Pair) : Pair := ⟨ps.any (·.T), ps.all (·.F)⟩

/-! ## encode/decode -/

theorem consistent_encode (v : TV) : consistent (encode v) = true := by
  cases v <;> rfl

theorem decode_encode (v : TV) : decode (encode v) = v := by
  cases v <;> rfl

theorem encode_decode (p : Pair) (h : consistent p = true) : encode (decode p) = p := by
  revert h
  cases p with
  | mk T F => cases T <;> cases F <;> decide

/-- UNKNOWN is `¬T ∧ ¬F`. -/
theorem encode_unk_iff (v : TV) : v = .unk ↔ (encode v).T = false ∧ (encode v).F = false := by
  cases v <;> decide

/-- The admission rule reads the `T` component. -/
theorem isTrue_eq_T (v : TV) : TV.isTrue v = (encode v).T := by
  cases v <;> rfl

theorem isFalse_eq_F (v : TV) : TV.isFalse v = (encode v).F := by
  cases v <;> rfl

/-! ## Soundness of the connectives -/

theorem encode_and (a b : TV) : encode (TV.and a b) = Pair.and (encode a) (encode b) := by
  cases a <;> cases b <;> rfl

theorem encode_or (a b : TV) : encode (TV.or a b) = Pair.or (encode a) (encode b) := by
  cases a <;> cases b <;> rfl

theorem encode_not (a : TV) : encode (TV.not a) = Pair.not (encode a) := by
  cases a <;> rfl

theorem encode_andList : ∀ vs : List TV, encode (TV.andList vs) = andList (vs.map encode)
  | [] => rfl
  | v :: vs => by
    show encode (TV.and v (TV.andList vs)) = andList (encode v :: vs.map encode)
    rw [encode_and, encode_andList vs]
    rfl

theorem encode_orList : ∀ vs : List TV, encode (TV.orList vs) = orList (vs.map encode)
  | [] => rfl
  | v :: vs => by
    show encode (TV.or v (TV.orList vs)) = orList (encode v :: vs.map encode)
    rw [encode_or, encode_orList vs]
    rfl

/-! ## Consistency is preserved

An inconsistent pair can only come from an inconsistent input, so the opaque side condition
is all the encoder needs. -/

theorem consistent_and (a b : Pair) (ha : consistent a = true) (hb : consistent b = true) :
    consistent (Pair.and a b) = true := by
  revert ha hb
  cases a with
  | mk aT aF =>
    cases b with
    | mk bT bF => cases aT <;> cases aF <;> cases bT <;> cases bF <;> decide

theorem consistent_or (a b : Pair) (ha : consistent a = true) (hb : consistent b = true) :
    consistent (Pair.or a b) = true := by
  revert ha hb
  cases a with
  | mk aT aF =>
    cases b with
    | mk bT bF => cases aT <;> cases aF <;> cases bT <;> cases bF <;> decide

theorem consistent_not (a : Pair) (ha : consistent a = true) : consistent (Pair.not a) = true := by
  revert ha
  cases a with
  | mk aT aF => cases aT <;> cases aF <;> decide

end Pair

end MLS
