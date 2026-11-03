import MLS.TVL
import MLS.Encoding
import MLS.Hierarchy

/-!
# Predicates: SQL meaning and the encoder (SEMANTICS §1–2, `src/mls/logic/tvl.py`)

Two readings of one predicate, both over a named row image (`"r"`, `"old"`, `"new"`: the
encoder's `row` argument; `old`/`new` references always read those two images):

* `Pred.eval`: the reference meaning over the row images (`Env`): comparison with a NULL
  operand is UNKNOWN, `IS NULL` is two-valued, `now_between` treats a NULL bound as open,
  `in_group` walks the hierarchy (`MLS.Hierarchy`), an opaque block is whatever consistent pair
  its definition gives.
* `Pred.tf`: the pair the Python encoder emits, over a valuation of the SMT variables it
  introduces (`row.col`, `row.col.null`, `ctx.name`, `rel.name`, `hier.name.has_parent/parent`,
  `opaque.name.T/F`).  Written clause for clause after `Encoder.tf`: a non-nullable column's null
  flag is the constant `FALSE`, a comparison is `(ok ∧ core, ok ∧ ¬core)` with
  `ok = ¬(lnull ∨ rnull)`, membership likewise over the uninterpreted relation, `in_group` is the
  unrolling to the declared depth, `now_between` is two-valued.

`Pred.tf_sound`: whenever the valuation describes the rows (`Agrees`), the pair is the encoding
of the SQL value.  The assumptions are the ones the toolchain makes: non-nullable columns hold
no NULL (the schema), every opaque pair is consistent (the side condition the encoder adds), and
the hierarchy FKs hold on stored data.

Not here: the n-ary `and`/`or` of the JSON syntax, which the encoder folds with
`Pair.andList`/`orList` (`Pair.encode_andList` covers the fold; `MLS.Policy` folds the
rule-level conjunctions).
-/
namespace MLS

/-- `now()` is the context value `ctx.now` (SEMANTICS §2). -/
def CONTEXT_NOW : String := "now"

/-- A value term: a column of the current image (NULL if nullable), a column of the `old` or
`new` image, a context value (never NULL), a constant (text and uuid constants are interned to
integers by the encoder). -/
inductive Term where
  | col (name : String)
  | old (name : String)
  | new (name : String)
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
  | inGroup (hierarchy : String) (key value : Term)
  | nowBetween (lo hi : Option Term)
  | opaqueRef (name : String)

/-- What a hierarchy declares (SEMANTICS §3.1): its members relation and the depth bound. -/
structure HierSpec where
  members : String
  maxDepth : Nat

/-- What the encoder and the evaluator read from the spec. -/
structure Schema where
  /-- Declared nullability per column. -/
  nullable : String → Bool
  hier : String → HierSpec

/-! ## The SQL meaning -/

/-- The row images and the context a predicate is evaluated against. -/
structure Env where
  /-- Column values per image; `none` is NULL. -/
  col : String → String → Option Int
  /-- Context values are never NULL. -/
  ctx : String → Int
  /-- Relations, as sets of (key, value) pairs. -/
  rel : String → Int → Int → Bool
  /-- The stored rows of each hierarchy table. -/
  forest : String → Forest
  /-- An opaque block is an uninterpreted predicate of the image: its value is given, as a pair. -/
  opaques : String → String → Pair

def ofBool (b : Bool) : TV := if b then .tt else .ff

theorem TV.isTrue_ofBool (b : Bool) : TV.isTrue (ofBool b) = b := by
  cases b <;> rfl

def Term.value (e : Env) (row : String) : Term → Option Int
  | .col n => e.col row n
  | .old n => e.col "old" n
  | .new n => e.col "new" n
  | .ctx n => some (e.ctx n)
  | .const v => some v

/-- A comparison (or membership test) with a NULL operand is UNKNOWN. -/
def liftCmp (f : Int → Int → Bool) : Option Int → Option Int → TV
  | some a, some b => ofBool (f a b)
  | some _, none => .unk
  | none, _ => .unk

/-- `from <= now`, a NULL bound (or no bound) is open. -/
def Env.lower (e : Env) (row : String) : Option Term → Bool
  | none => true
  | some t =>
    match t.value e row with
    | none => true
    | some v => decide (v ≤ e.ctx CONTEXT_NOW)

/-- `now < until`, a NULL bound (or no bound) is open. -/
def Env.upper (e : Env) (row : String) : Option Term → Bool
  | none => true
  | some t =>
    match t.value e row with
    | none => true
    | some v => decide (e.ctx CONTEXT_NOW < v)

/-- `in_group`: the walk over `max_depth + 1` nodes (`MLS.Hierarchy`). -/
def Env.inGroup (s : Schema) (e : Env) (hn : String) (k g : Int) : Bool :=
  (e.forest hn).walk (e.rel (s.hier hn).members) k g ((s.hier hn).maxDepth + 1)

def Pred.eval (s : Schema) (e : Env) (row : String) : Pred → TV
  | .lit b => ofBool b
  | .and p q => TV.and (p.eval s e row) (q.eval s e row)
  | .or p q => TV.or (p.eval s e row) (q.eval s e row)
  | .not p => TV.not (p.eval s e row)
  | .isNull t => ofBool (t.value e row).isNone
  | .cmp l op r => liftCmp op.holds (l.value e row) (r.value e row)
  | .member rel k v => liftCmp (e.rel rel) (k.value e row) (v.value e row)
  | .inGroup hn k v => liftCmp (e.inGroup s hn) (k.value e row) (v.value e row)
  | .nowBetween lo hi => ofBool (e.lower row lo && e.upper row hi)
  | .opaqueRef n => Pair.decode (e.opaques n row)

/-! ## The encoder -/

/-- A model of the SMT variables the encoder introduces. -/
structure Valuation where
  /-- `row.col` -/
  val : String → String → Int
  /-- `row.col.null` -/
  null : String → String → Bool
  /-- `ctx.name` -/
  ctx : String → Int
  /-- `rel.name` -/
  rel : String → Int → Int → Bool
  /-- `hier.name.has_parent` -/
  has : String → Int → Bool
  /-- `hier.name.parent` -/
  par : String → Int → Int
  /-- `opaque.name.T` on an image -/
  opaqueT : String → String → Bool
  /-- `opaque.name.F` on an image -/
  opaqueF : String → String → Bool

/-- `Encoder._col`: `(value, is_null)`; the null flag of a non-nullable column is `FALSE`. -/
def Term.colTF (s : Schema) (σ : Valuation) (img n : String) : Int × Bool :=
  (σ.val img n, if s.nullable n then σ.null img n else false)

/-- `Encoder.term` -/
def Term.tf (s : Schema) (σ : Valuation) (row : String) : Term → Int × Bool
  | .col n => Term.colTF s σ row n
  | .old n => Term.colTF s σ "old" n
  | .new n => Term.colTF s σ "new" n
  | .ctx n => (σ.ctx n, false)
  | .const v => (v, false)

/-- `or_(fn, le(fv, now))` -/
def Term.lowerTF (s : Schema) (σ : Valuation) (row : String) : Option Term → Bool
  | none => true
  | some t => (t.tf s σ row).2 || decide ((t.tf s σ row).1 ≤ σ.ctx CONTEXT_NOW)

/-- `or_(un, lt(now, uv))` -/
def Term.upperTF (s : Schema) (σ : Valuation) (row : String) : Option Term → Bool
  | none => true
  | some t => (t.tf s σ row).2 || decide (σ.ctx CONTEXT_NOW < (t.tf s σ row).1)

/-- `Encoder.tf`, clause for clause. -/
def Pred.tf (s : Schema) (σ : Valuation) (row : String) : Pred → Pair
  | .lit b => if b then ⟨true, false⟩ else ⟨false, true⟩
  | .and p q => Pair.and (p.tf s σ row) (q.tf s σ row)
  | .or p q => Pair.or (p.tf s σ row) (q.tf s σ row)
  | .not p => Pair.not (p.tf s σ row)
  | .isNull t => ⟨(t.tf s σ row).2, !(t.tf s σ row).2⟩
  | .cmp l op r =>
    let ok := !((l.tf s σ row).2 || (r.tf s σ row).2)
    let core := op.holds (l.tf s σ row).1 (r.tf s σ row).1
    ⟨ok && core, ok && !core⟩
  | .member rel k v =>
    let ok := !((k.tf s σ row).2 || (v.tf s σ row).2)
    let fact := σ.rel rel (k.tf s σ row).1 (v.tf s σ row).1
    ⟨ok && fact, ok && !fact⟩
  | .inGroup hn k v =>
    let ok := !((k.tf s σ row).2 || (v.tf s σ row).2)
    let hit := unrollPy (σ.has hn) (σ.par hn) (σ.rel (s.hier hn).members)
      (k.tf s σ row).1 (v.tf s σ row).1 (s.hier hn).maxDepth
    ⟨ok && hit, ok && !hit⟩
  | .nowBetween lo hi =>
    let t := Term.lowerTF s σ row lo && Term.upperTF s σ row hi
    ⟨t, !t⟩
  | .opaqueRef n => ⟨σ.opaqueT n row, σ.opaqueF n row⟩

/-! ## Soundness -/

/-- The valuation describes the rows: a NULL column is a nullable column whose flag is set; a
non-NULL column has its value and, if nullable, a clear flag; context, relations, hierarchy
functions and opaque pairs coincide. -/
structure Agrees (s : Schema) (σ : Valuation) (e : Env) : Prop where
  null_of_none : ∀ img n, e.col img n = none → s.nullable n = true ∧ σ.null img n = true
  val_of_some : ∀ img n v, e.col img n = some v → σ.val img n = v
  flag_of_some : ∀ img n v, e.col img n = some v → s.nullable n = true → σ.null img n = false
  ctx : ∀ n, σ.ctx n = e.ctx n
  rel : ∀ r, σ.rel r = e.rel r
  forest : ∀ hn, (e.forest hn).Encodes (σ.has hn) (σ.par hn)
  opaqueT : ∀ n img, σ.opaqueT n img = (e.opaques n img).T
  opaqueF : ∀ n img, σ.opaqueF n img = (e.opaques n img).F

/-- The stored data satisfies the hierarchy FKs. -/
def Env.hierarchiesWellFormed (s : Schema) (e : Env) : Prop :=
  ∀ hn, (e.forest hn).WellFormed (e.rel (s.hier hn).members)

variable {s : Schema} {σ : Valuation} {e : Env}

theorem Term.colTF_null (h : Agrees s σ e) (img n : String) :
    (Term.colTF s σ img n).2 = (e.col img n).isNone := by
  dsimp only [Term.colTF]
  cases hc : e.col img n with
  | none =>
    obtain ⟨hs, hn⟩ := h.null_of_none img n hc
    simp [hs, hn]
  | some v =>
    cases hs : s.nullable n with
    | false => simp
    | true => simp [h.flag_of_some img n v hc hs]

theorem Term.colTF_val (h : Agrees s σ e) (img n : String) (v : Int) (hv : e.col img n = some v) :
    (Term.colTF s σ img n).1 = v :=
  h.val_of_some img n v hv

theorem Term.tf_null (h : Agrees s σ e) (row : String) (t : Term) :
    (t.tf s σ row).2 = (t.value e row).isNone := by
  cases t with
  | col n => exact Term.colTF_null h row n
  | old n => exact Term.colTF_null h "old" n
  | new n => exact Term.colTF_null h "new" n
  | ctx n => rfl
  | const v => rfl

theorem Term.tf_val (h : Agrees s σ e) (row : String) (t : Term) (v : Int)
    (hv : t.value e row = some v) : (t.tf s σ row).1 = v := by
  cases t with
  | col n => exact Term.colTF_val h row n v hv
  | old n => exact Term.colTF_val h "old" n v hv
  | new n => exact Term.colTF_val h "new" n v hv
  | ctx n =>
    simp only [Term.value, Option.some.injEq] at hv
    show σ.ctx n = v
    rw [h.ctx n, hv]
  | const c =>
    simp only [Term.value, Option.some.injEq] at hv
    exact hv

theorem Term.lowerTF_sound (h : Agrees s σ e) (row : String) (lo : Option Term) :
    Term.lowerTF s σ row lo = e.lower row lo := by
  cases lo with
  | none => rfl
  | some t =>
    dsimp only [Term.lowerTF, Env.lower]
    rw [Term.tf_null h row t]
    cases ht : t.value e row with
    | none => rfl
    | some v =>
      rw [Term.tf_val h row t v ht, h.ctx]
      rfl

theorem Term.upperTF_sound (h : Agrees s σ e) (row : String) (hi : Option Term) :
    Term.upperTF s σ row hi = e.upper row hi := by
  cases hi with
  | none => rfl
  | some t =>
    dsimp only [Term.upperTF, Env.upper]
    rw [Term.tf_null h row t]
    cases ht : t.value e row with
    | none => rfl
    | some v =>
      rw [Term.tf_val h row t v ht, h.ctx]
      rfl

/-- The encoder is sound: under the schema, the opaque side conditions and the hierarchy FKs,
the pair it emits is the encoding of the SQL truth value. -/
theorem Pred.tf_sound (h : Agrees s σ e) (hop : ∀ n img, (e.opaques n img).consistent = true)
    (hw : e.hierarchiesWellFormed s) (row : String) :
    ∀ p : Pred, p.tf s σ row = Pair.encode (p.eval s e row)
  | .lit b => by cases b <;> rfl
  | .and p q => by
    show Pair.and (p.tf s σ row) (q.tf s σ row) = Pair.encode (TV.and (p.eval s e row) (q.eval s e row))
    rw [Pred.tf_sound h hop hw row p, Pred.tf_sound h hop hw row q, Pair.encode_and]
  | .or p q => by
    show Pair.or (p.tf s σ row) (q.tf s σ row) = Pair.encode (TV.or (p.eval s e row) (q.eval s e row))
    rw [Pred.tf_sound h hop hw row p, Pred.tf_sound h hop hw row q, Pair.encode_or]
  | .not p => by
    show Pair.not (p.tf s σ row) = Pair.encode (TV.not (p.eval s e row))
    rw [Pred.tf_sound h hop hw row p, Pair.encode_not]
  | .isNull t => by
    show (⟨(t.tf s σ row).2, !(t.tf s σ row).2⟩ : Pair) = Pair.encode (ofBool (t.value e row).isNone)
    rw [Term.tf_null h row t]
    cases (t.value e row).isNone <;> rfl
  | .cmp l op r => by
    show (⟨!((l.tf s σ row).2 || (r.tf s σ row).2) && op.holds (l.tf s σ row).1 (r.tf s σ row).1,
           !((l.tf s σ row).2 || (r.tf s σ row).2) && !op.holds (l.tf s σ row).1 (r.tf s σ row).1⟩ : Pair)
        = Pair.encode (liftCmp op.holds (l.value e row) (r.value e row))
    rw [Term.tf_null h row l, Term.tf_null h row r]
    cases hl : l.value e row with
    | none => rfl
    | some a =>
      cases hr : r.value e row with
      | none => rfl
      | some b =>
        rw [Term.tf_val h row l a hl, Term.tf_val h row r b hr]
        show _ = Pair.encode (ofBool (op.holds a b))
        cases op.holds a b <;> rfl
  | .member rel k v => by
    show (⟨!((k.tf s σ row).2 || (v.tf s σ row).2) && σ.rel rel (k.tf s σ row).1 (v.tf s σ row).1,
           !((k.tf s σ row).2 || (v.tf s σ row).2) && !σ.rel rel (k.tf s σ row).1 (v.tf s σ row).1⟩ : Pair)
        = Pair.encode (liftCmp (e.rel rel) (k.value e row) (v.value e row))
    rw [Term.tf_null h row k, Term.tf_null h row v]
    cases hk : k.value e row with
    | none => rfl
    | some a =>
      cases hv : v.value e row with
      | none => rfl
      | some b =>
        rw [Term.tf_val h row k a hk, Term.tf_val h row v b hv, h.rel]
        show _ = Pair.encode (ofBool (e.rel rel a b))
        cases e.rel rel a b <;> rfl
  | .inGroup hn k v => by
    show (⟨!((k.tf s σ row).2 || (v.tf s σ row).2)
             && unrollPy (σ.has hn) (σ.par hn) (σ.rel (s.hier hn).members)
                  (k.tf s σ row).1 (v.tf s σ row).1 (s.hier hn).maxDepth,
           !((k.tf s σ row).2 || (v.tf s σ row).2)
             && !unrollPy (σ.has hn) (σ.par hn) (σ.rel (s.hier hn).members)
                  (k.tf s σ row).1 (v.tf s σ row).1 (s.hier hn).maxDepth⟩ : Pair)
        = Pair.encode (liftCmp (e.inGroup s hn) (k.value e row) (v.value e row))
    rw [Term.tf_null h row k, Term.tf_null h row v]
    cases hk : k.value e row with
    | none => rfl
    | some a =>
      cases hv : v.value e row with
      | none => rfl
      | some b =>
        rw [Term.tf_val h row k a hk, Term.tf_val h row v b hv, h.rel, unrollPy_eq_unroll,
          unroll_eq_walk (h.forest hn) (hw hn)]
        show _ = Pair.encode (ofBool ((e.forest hn).walk (e.rel (s.hier hn).members) a b
          ((s.hier hn).maxDepth + 1)))
        cases (e.forest hn).walk (e.rel (s.hier hn).members) a b ((s.hier hn).maxDepth + 1) <;> rfl
  | .nowBetween lo hi => by
    show (⟨Term.lowerTF s σ row lo && Term.upperTF s σ row hi,
           !(Term.lowerTF s σ row lo && Term.upperTF s σ row hi)⟩ : Pair)
        = Pair.encode (ofBool (e.lower row lo && e.upper row hi))
    rw [Term.lowerTF_sound h row lo, Term.upperTF_sound h row hi]
    cases (e.lower row lo && e.upper row hi) <;> rfl
  | .opaqueRef n => by
    show (⟨σ.opaqueT n row, σ.opaqueF n row⟩ : Pair) = Pair.encode (Pair.decode (e.opaques n row))
    rw [h.opaqueT n row, h.opaqueF n row, Pair.encode_decode _ (hop n row)]

/-- Admission, stated on the encoder's output: the row is admitted iff the `T` component holds. -/
theorem Pred.admitted (h : Agrees s σ e) (hop : ∀ n img, (e.opaques n img).consistent = true)
    (hw : e.hierarchiesWellFormed s) (row : String) (p : Pred) :
    (p.tf s σ row).T = TV.isTrue (p.eval s e row) := by
  rw [Pred.tf_sound h hop hw row p, Pair.isTrue_eq_T]

end MLS
