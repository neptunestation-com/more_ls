import MLS.TVL
import MLS.Encoding

/-!
# Predicates: SQL meaning and the encoder (SEMANTICS §1–2, `src/mls/logic/tvl.py`)

Two readings of one predicate:

* `Pred.eval`: the reference meaning over a row (`Env`): comparison with a NULL operand is
  UNKNOWN, `IS NULL` is two-valued, `now_between` treats a NULL bound as open, an opaque block
  is whatever consistent pair its definition gives.
* `Pred.tf`: the pair the Python encoder emits, over a valuation of the SMT variables it
  introduces (`row.col`, `row.col.null`, `ctx.name`, `rel.name`, `opaque.name.T/F`).  Written
  clause for clause after `Encoder.tf`: a non-nullable column's null flag is the constant
  `FALSE`, a comparison is `(ok ∧ core, ok ∧ ¬core)` with `ok = ¬(lnull ∨ rnull)`, membership
  likewise over the uninterpreted relation, `now_between` is two-valued.

`Pred.tf_sound`: whenever the valuation describes the row (`Agrees`), the pair is the encoding
of the SQL value.  The assumptions are the ones the toolchain makes: non-nullable columns hold
no NULL (the schema), and every opaque pair is consistent (the side condition the encoder adds).

Not here yet: `in_group` (the depth-bounded unrolling over a hierarchy, SEMANTICS §7) and the
n-ary `and`/`or` of the JSON syntax, which the encoder folds with `Pair.andList`/`orList`
(`Pair.encode_andList` covers the fold).
-/
namespace MLS

/-- `now()` is the context value `ctx.now` (SEMANTICS §2). -/
def CONTEXT_NOW : String := "now"

/-- A value term: a column (NULL if nullable), a context value (never NULL), a constant
(text and uuid constants are interned to integers by the encoder). -/
inductive Term where
  | col (name : String)
  | ctx (name : String)
  | const (value : Int)

inductive Op where
  | eq | ne | lt | le | gt | ge

/-- The encoder's `core`: `=` / `not =` / `<` / `<=` / `<` swapped / `<=` swapped. -/
def Op.holds : Op → Int → Int → Bool
  | .eq, a, b => decide (a = b)
  | .ne, a, b => !decide (a = b)
  | .lt, a, b => decide (a < b)
  | .le, a, b => decide (a ≤ b)
  | .gt, a, b => decide (b < a)
  | .ge, a, b => decide (b ≤ a)

inductive Pred where
  | lit (b : Bool)
  | and (p q : Pred)
  | or (p q : Pred)
  | not (p : Pred)
  | isNull (t : Term)
  | cmp (l : Term) (op : Op) (r : Term)
  | member (rel : String) (key value : Term)
  | nowBetween (lo hi : Option Term)
  | opaqueRef (name : String)

/-! ## The SQL meaning -/

/-- A row and its context: what a predicate is evaluated against. -/
structure Env where
  /-- Column values; `none` is NULL. -/
  col : String → Option Int
  /-- Context values are never NULL. -/
  ctx : String → Int
  /-- Relations, as sets of (key, value) pairs. -/
  rel : String → Int → Int → Bool
  /-- An opaque block is an uninterpreted predicate: its value is given, as a pair. -/
  opaques : String → Pair

def ofBool (b : Bool) : TV := if b then .tt else .ff

def Term.value (e : Env) : Term → Option Int
  | .col n => e.col n
  | .ctx n => some (e.ctx n)
  | .const v => some v

/-- A comparison (or membership test) with a NULL operand is UNKNOWN. -/
def liftCmp (f : Int → Int → Bool) : Option Int → Option Int → TV
  | some a, some b => ofBool (f a b)
  | some _, none => .unk
  | none, _ => .unk

/-- `from <= now`, a NULL bound (or no bound) is open. -/
def Env.lower (e : Env) : Option Term → Bool
  | none => true
  | some t =>
    match t.value e with
    | none => true
    | some v => decide (v ≤ e.ctx CONTEXT_NOW)

/-- `now < until`, a NULL bound (or no bound) is open. -/
def Env.upper (e : Env) : Option Term → Bool
  | none => true
  | some t =>
    match t.value e with
    | none => true
    | some v => decide (e.ctx CONTEXT_NOW < v)

def Pred.eval (e : Env) : Pred → TV
  | .lit b => ofBool b
  | .and p q => TV.and (p.eval e) (q.eval e)
  | .or p q => TV.or (p.eval e) (q.eval e)
  | .not p => TV.not (p.eval e)
  | .isNull t => ofBool (t.value e).isNone
  | .cmp l op r => liftCmp op.holds (l.value e) (r.value e)
  | .member rel k v => liftCmp (e.rel rel) (k.value e) (v.value e)
  | .nowBetween lo hi => ofBool (e.lower lo && e.upper hi)
  | .opaqueRef n => Pair.decode (e.opaques n)

/-! ## The encoder -/

/-- The declared nullability of each column: the schema the encoder reads. -/
def Schema := String → Bool

/-- A model of the SMT variables the encoder introduces. -/
structure Valuation where
  /-- `row.col` -/
  val : String → Int
  /-- `row.col.null` -/
  null : String → Bool
  /-- `ctx.name` -/
  ctx : String → Int
  /-- `rel.name` -/
  rel : String → Int → Int → Bool
  /-- `opaque.name.T` -/
  opaqueT : String → Bool
  /-- `opaque.name.F` -/
  opaqueF : String → Bool

/-- `Encoder.term`: `(value, is_null)`; the null flag of a non-nullable column is `FALSE`. -/
def Term.tf (s : Schema) (σ : Valuation) : Term → Int × Bool
  | .col n => (σ.val n, if s n then σ.null n else false)
  | .ctx n => (σ.ctx n, false)
  | .const v => (v, false)

/-- `or_(fn, le(fv, now))` -/
def Term.lowerTF (s : Schema) (σ : Valuation) : Option Term → Bool
  | none => true
  | some t => (t.tf s σ).2 || decide ((t.tf s σ).1 ≤ σ.ctx CONTEXT_NOW)

/-- `or_(un, lt(now, uv))` -/
def Term.upperTF (s : Schema) (σ : Valuation) : Option Term → Bool
  | none => true
  | some t => (t.tf s σ).2 || decide (σ.ctx CONTEXT_NOW < (t.tf s σ).1)

/-- `Encoder.tf`, clause for clause. -/
def Pred.tf (s : Schema) (σ : Valuation) : Pred → Pair
  | .lit b => if b then ⟨true, false⟩ else ⟨false, true⟩
  | .and p q => Pair.and (p.tf s σ) (q.tf s σ)
  | .or p q => Pair.or (p.tf s σ) (q.tf s σ)
  | .not p => Pair.not (p.tf s σ)
  | .isNull t => ⟨(t.tf s σ).2, !(t.tf s σ).2⟩
  | .cmp l op r =>
    let ok := !((l.tf s σ).2 || (r.tf s σ).2)
    let core := op.holds (l.tf s σ).1 (r.tf s σ).1
    ⟨ok && core, ok && !core⟩
  | .member rel k v =>
    let ok := !((k.tf s σ).2 || (v.tf s σ).2)
    let fact := σ.rel rel (k.tf s σ).1 (v.tf s σ).1
    ⟨ok && fact, ok && !fact⟩
  | .nowBetween lo hi =>
    let t := Term.lowerTF s σ lo && Term.upperTF s σ hi
    ⟨t, !t⟩
  | .opaqueRef n => ⟨σ.opaqueT n, σ.opaqueF n⟩

/-! ## Soundness -/

/-- The valuation describes the row: a NULL column is a nullable column whose flag is set; a
non-NULL column has its value and, if nullable, a clear flag; context, relations and opaque
pairs coincide. -/
structure Agrees (s : Schema) (σ : Valuation) (e : Env) : Prop where
  null_of_none : ∀ n, e.col n = none → s n = true ∧ σ.null n = true
  val_of_some : ∀ n v, e.col n = some v → σ.val n = v
  flag_of_some : ∀ n v, e.col n = some v → s n = true → σ.null n = false
  ctx : ∀ n, σ.ctx n = e.ctx n
  rel : ∀ r k v, σ.rel r k v = e.rel r k v
  opaqueT : ∀ n, σ.opaqueT n = (e.opaques n).T
  opaqueF : ∀ n, σ.opaqueF n = (e.opaques n).F

variable {s : Schema} {σ : Valuation} {e : Env}

theorem Term.tf_null (h : Agrees s σ e) (t : Term) : (t.tf s σ).2 = (t.value e).isNone := by
  cases t with
  | col n =>
    dsimp only [Term.tf, Term.value]
    cases hc : e.col n with
    | none =>
      obtain ⟨hs, hn⟩ := h.null_of_none n hc
      simp [hs, hn]
    | some v =>
      cases hs : s n with
      | false => simp
      | true => simp [h.flag_of_some n v hc hs]
  | ctx n => rfl
  | const v => rfl

theorem Term.tf_val (h : Agrees s σ e) (t : Term) (v : Int) (hv : t.value e = some v) :
    (t.tf s σ).1 = v := by
  cases t with
  | col n => exact h.val_of_some n v hv
  | ctx n =>
    simp only [Term.value, Option.some.injEq] at hv
    show σ.ctx n = v
    rw [h.ctx n, hv]
  | const c =>
    simp only [Term.value, Option.some.injEq] at hv
    exact hv

theorem Term.lowerTF_sound (h : Agrees s σ e) (lo : Option Term) :
    Term.lowerTF s σ lo = e.lower lo := by
  cases lo with
  | none => rfl
  | some t =>
    dsimp only [Term.lowerTF, Env.lower]
    rw [Term.tf_null h t]
    cases ht : t.value e with
    | none => rfl
    | some v =>
      rw [Term.tf_val h t v ht, h.ctx]
      rfl

theorem Term.upperTF_sound (h : Agrees s σ e) (hi : Option Term) :
    Term.upperTF s σ hi = e.upper hi := by
  cases hi with
  | none => rfl
  | some t =>
    dsimp only [Term.upperTF, Env.upper]
    rw [Term.tf_null h t]
    cases ht : t.value e with
    | none => rfl
    | some v =>
      rw [Term.tf_val h t v ht, h.ctx]
      rfl

/-- The encoder is sound: under the schema and the opaque side conditions, the pair it emits
is the encoding of the SQL truth value. -/
theorem Pred.tf_sound (h : Agrees s σ e) (hop : ∀ n, (e.opaques n).consistent = true) :
    ∀ p : Pred, p.tf s σ = Pair.encode (p.eval e)
  | .lit b => by cases b <;> rfl
  | .and p q => by
    show Pair.and (p.tf s σ) (q.tf s σ) = Pair.encode (TV.and (p.eval e) (q.eval e))
    rw [Pred.tf_sound h hop p, Pred.tf_sound h hop q, Pair.encode_and]
  | .or p q => by
    show Pair.or (p.tf s σ) (q.tf s σ) = Pair.encode (TV.or (p.eval e) (q.eval e))
    rw [Pred.tf_sound h hop p, Pred.tf_sound h hop q, Pair.encode_or]
  | .not p => by
    show Pair.not (p.tf s σ) = Pair.encode (TV.not (p.eval e))
    rw [Pred.tf_sound h hop p, Pair.encode_not]
  | .isNull t => by
    show (⟨(t.tf s σ).2, !(t.tf s σ).2⟩ : Pair) = Pair.encode (ofBool (t.value e).isNone)
    rw [Term.tf_null h t]
    cases (t.value e).isNone <;> rfl
  | .cmp l op r => by
    show (⟨!((l.tf s σ).2 || (r.tf s σ).2) && op.holds (l.tf s σ).1 (r.tf s σ).1,
           !((l.tf s σ).2 || (r.tf s σ).2) && !op.holds (l.tf s σ).1 (r.tf s σ).1⟩ : Pair)
        = Pair.encode (liftCmp op.holds (l.value e) (r.value e))
    rw [Term.tf_null h l, Term.tf_null h r]
    cases hl : l.value e with
    | none => rfl
    | some a =>
      cases hr : r.value e with
      | none => rfl
      | some b =>
        rw [Term.tf_val h l a hl, Term.tf_val h r b hr]
        show _ = Pair.encode (ofBool (op.holds a b))
        cases op.holds a b <;> rfl
  | .member rel k v => by
    show (⟨!((k.tf s σ).2 || (v.tf s σ).2) && σ.rel rel (k.tf s σ).1 (v.tf s σ).1,
           !((k.tf s σ).2 || (v.tf s σ).2) && !σ.rel rel (k.tf s σ).1 (v.tf s σ).1⟩ : Pair)
        = Pair.encode (liftCmp (e.rel rel) (k.value e) (v.value e))
    rw [Term.tf_null h k, Term.tf_null h v]
    cases hk : k.value e with
    | none => rfl
    | some a =>
      cases hv : v.value e with
      | none => rfl
      | some b =>
        rw [Term.tf_val h k a hk, Term.tf_val h v b hv, h.rel]
        show _ = Pair.encode (ofBool (e.rel rel a b))
        cases e.rel rel a b <;> rfl
  | .nowBetween lo hi => by
    show (⟨Term.lowerTF s σ lo && Term.upperTF s σ hi,
           !(Term.lowerTF s σ lo && Term.upperTF s σ hi)⟩ : Pair)
        = Pair.encode (ofBool (e.lower lo && e.upper hi))
    rw [Term.lowerTF_sound h lo, Term.upperTF_sound h hi]
    cases (e.lower lo && e.upper hi) <;> rfl
  | .opaqueRef n => by
    show (⟨σ.opaqueT n, σ.opaqueF n⟩ : Pair) = Pair.encode (Pair.decode (e.opaques n))
    rw [h.opaqueT n, h.opaqueF n, Pair.encode_decode _ (hop n)]

/-- Admission, stated on the encoder's output: the row is admitted iff the `T` component holds. -/
theorem Pred.admitted (h : Agrees s σ e) (hop : ∀ n, (e.opaques n).consistent = true) (p : Pred) :
    (p.tf s σ).T = TV.isTrue (p.eval e) := by
  rw [Pred.tf_sound h hop p, Pair.isTrue_eq_T]

end MLS
