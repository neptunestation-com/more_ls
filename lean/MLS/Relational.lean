import MLS.TVL
import MLS.Pred
import MLS.Policy
import MLS.Normalize

/-!
# The CHECK and FK templates against a relational model (D11, step 4)

A database state is the set of stored rows of each table and the facts of each relation; a
relation declared over a table is *backed* by it (its facts are the (key, value) pairs of the
stored rows, SEMANTICS §2).  Statements are relations between states: a row inserted into,
deleted from or replaced in one table, every other table unchanged.  Rows are sets, not bags:
two identical rows are one row, which changes nothing for these invariants.

* CHECK template: `CHECK ((p) IS TRUE)` on a table keeps the invariant "every stored row is
  admitted by `p`" across inserts, updates and deletes (`check_insert`, `check_update`,
  `check_delete`), because a CHECK predicate reads only its row (`Pred.rowOnly`,
  `eval_rowOnly`).  Without the `IS TRUE`, a row with a NULL passes the constraint although the
  admission rule rejects it (`check_naive_admits_null`; FINDINGS "NULL semantics").
* FK template: `FOREIGN KEY (c1, c2) REFERENCES u (k, v) MATCH SIMPLE ON UPDATE RESTRICT ON
  DELETE RESTRICT` keeps the invariant "every stored row of `t` satisfies MATCH SIMPLE against
  the relation" (`Inclusion`) across every statement on either table: writes to `t` are checked
  on the new row (`fk_insert_referencing`, `fk_update_referencing`, `fk_delete_referencing`),
  inserts into `u` only add facts (`fk_insert_referenced`), and deletes from and updates of `u`
  are guarded by RESTRICT (`fk_delete_referenced`, `fk_update_referenced`): the guard is the one
  `props/obligations.py:_restrict_guard` gives the solver.  The relation-change axioms the
  obligations add (`_relation_change`) follow from the model (`rel_after_insert`,
  `rel_after_delete`).  Self-referencing FKs (`u = t`) are outside these theorems.
-/
namespace MLS

abbrev Row := String → Option Int

/-- A database state: the stored rows of each table and the facts of each relation. -/
structure State where
  rows : String → Row → Prop
  rel : String → Int → Int → Bool

/-- A relation declared over `table` with key and value columns. -/
structure RelDecl where
  table : String
  key : String
  value : String

/-- Relation `r` is backed by the stored rows of its table. -/
def State.Backed (st : State) (r : String) (d : RelDecl) : Prop :=
  ∀ a b, st.rel r a b = true ↔ ∃ ρ, st.rows d.table ρ ∧ ρ d.key = some a ∧ ρ d.value = some b

/-- `st'` is `st` with `ρ` added to `t`. -/
def Inserted (st st' : State) (t : String) (ρ : Row) : Prop :=
  ∀ u ρ', st'.rows u ρ' ↔ st.rows u ρ' ∨ (u = t ∧ ρ' = ρ)

/-- `st'` is `st` with `ρ` removed from `t`. -/
def Deleted (st st' : State) (t : String) (ρ : Row) : Prop :=
  ∀ u ρ', st'.rows u ρ' ↔ st.rows u ρ' ∧ ¬ (u = t ∧ ρ' = ρ)

/-- `st'` is `st` with `ρ` replaced by `ρ'` in `t`. -/
def Updated (st st' : State) (t : String) (ρ ρ' : Row) : Prop :=
  ∀ u ρ'', st'.rows u ρ'' ↔ (st.rows u ρ'' ∧ ¬ (u = t ∧ ρ'' = ρ)) ∨ (u = t ∧ ρ'' = ρ')

/-- What a stored row is evaluated with besides itself and the relations. -/
structure Extra where
  ctx : String → Int
  forest : String → Forest
  opaques : String → String → Pair

/-- The environment of one stored row: the row under every image name, the state's relations. -/
def Env.ofRow (st : State) (x : Extra) (ρ : Row) : Env :=
  { col := fun _ => ρ, ctx := x.ctx, rel := st.rel, forest := x.forest, opaques := x.opaques }

/-! ## A CHECK reads only its row -/

def Term.isRow : Term → Bool
  | .col _ => true
  | .const _ => true
  | _ => false

/-- The fragment of a CHECK predicate (SEMANTICS §3: QF over the row): columns of the row and
constants, no context, relations, hierarchies, time or opaque blocks. -/
def Pred.rowOnly : Pred → Bool
  | .lit _ => true
  | .and p q => p.rowOnly && q.rowOnly
  | .or p q => p.rowOnly && q.rowOnly
  | .not p => p.rowOnly
  | .isNull t => t.isRow
  | .cmp l _ r => l.isRow && r.isRow
  | _ => false

variable {s : Schema} {e e' : Env} {row : String}

theorem Term.value_isRow (t : Term) (ht : t.isRow = true) (hcol : e.col row = e'.col row) :
    t.value e row = t.value e' row := by
  cases t with
  | col n => show e.col row n = e'.col row n; rw [hcol]
  | const v => rfl
  | _ => simp [Term.isRow] at ht

theorem eval_rowOnly (hcol : e.col row = e'.col row) :
    ∀ p : Pred, p.rowOnly = true → p.eval s e row = p.eval s e' row
  | .lit _, _ => rfl
  | .and p q, hp => by
    simp only [Pred.rowOnly, Bool.and_eq_true] at hp
    show TV.and (p.eval s e row) (q.eval s e row) = TV.and (p.eval s e' row) (q.eval s e' row)
    rw [eval_rowOnly hcol p hp.1, eval_rowOnly hcol q hp.2]
  | .or p q, hp => by
    simp only [Pred.rowOnly, Bool.and_eq_true] at hp
    show TV.or (p.eval s e row) (q.eval s e row) = TV.or (p.eval s e' row) (q.eval s e' row)
    rw [eval_rowOnly hcol p hp.1, eval_rowOnly hcol q hp.2]
  | .not p, hp => by
    show TV.not (p.eval s e row) = TV.not (p.eval s e' row)
    rw [eval_rowOnly hcol p hp]
  | .isNull t, hp => by
    show ofBool (t.value e row).isNone = ofBool (t.value e' row).isNone
    rw [Term.value_isRow t hp hcol]
  | .cmp l op r, hp => by
    simp only [Pred.rowOnly, Bool.and_eq_true] at hp
    show liftCmp op.holds (l.value e row) (r.value e row) = liftCmp op.holds (l.value e' row) (r.value e' row)
    rw [Term.value_isRow l hp.1 hcol, Term.value_isRow r hp.2 hcol]
  | .member _ _ _, hp => by simp [Pred.rowOnly] at hp
  | .inGroup _ _ _, hp => by simp [Pred.rowOnly] at hp
  | .nowBetween _ _, hp => by simp [Pred.rowOnly] at hp
  | .opaqueRef _, hp => by simp [Pred.rowOnly] at hp

/-! ## The CHECK template -/

/-- Every stored row of `t` is admitted by `p`. -/
def CheckHolds (s : Schema) (x : Extra) (st : State) (t : String) (p : Pred) : Prop :=
  ∀ ρ, st.rows t ρ → admits s (Env.ofRow st x ρ) "r" p = true

variable {x : Extra} {st st' : State} {t : String} {p : Pred}

theorem admits_ofRow (hp : p.rowOnly = true) (ρ : Row) :
    admits s (Env.ofRow st' x ρ) "r" p = admits s (Env.ofRow st x ρ) "r" p := by
  unfold admits
  rw [eval_rowOnly (e := Env.ofRow st' x ρ) (e' := Env.ofRow st x ρ) rfl p hp]

/-- INSERT: the new row passed `CHECK ((p) IS TRUE)`. -/
theorem check_insert (hp : p.rowOnly = true) (hi : Inserted st st' t ρ) (hI : CheckHolds s x st t p)
    (hnew : checkPasses (TV.isTrueOp (p.eval s (Env.ofRow st x ρ) "r")) = true) :
    CheckHolds s x st' t p := by
  intro ρ' h'
  rw [admits_ofRow hp]
  rw [hi] at h'
  rcases h' with h' | ⟨_, rfl⟩
  · exact hI ρ' h'
  · rw [check_normalized] at hnew
    exact hnew

/-- UPDATE: the new image passed `CHECK ((p) IS TRUE)`. -/
theorem check_update (hp : p.rowOnly = true) (hu : Updated st st' t ρ ρ') (hI : CheckHolds s x st t p)
    (hnew : checkPasses (TV.isTrueOp (p.eval s (Env.ofRow st x ρ') "r")) = true) :
    CheckHolds s x st' t p := by
  intro ρ'' h''
  rw [admits_ofRow hp]
  rw [hu] at h''
  rcases h'' with ⟨h'', _⟩ | ⟨_, rfl⟩
  · exact hI ρ'' h''
  · rw [check_normalized] at hnew
    exact hnew

/-- DELETE: nothing to check. -/
theorem check_delete (hp : p.rowOnly = true) (hd : Deleted st st' t ρ) (hI : CheckHolds s x st t p) :
    CheckHolds s x st' t p := by
  intro ρ' h'
  rw [admits_ofRow hp]
  exact hI ρ' ((hd t ρ').mp h').1

/-- Without the normalization: `CHECK (a = 0)` accepts a row with `a` NULL, which the admission
rule rejects. -/
theorem check_naive_admits_null (s : Schema) (x : Extra) (st : State) :
    checkPasses ((Pred.cmp (.col "a") .eq (.const 0)).eval s (Env.ofRow st x fun _ => none) "r") = true
      ∧ admits s (Env.ofRow st x fun _ => none) "r" (Pred.cmp (.col "a") .eq (.const 0)) = false :=
  ⟨rfl, rfl⟩

/-! ## Relations under statements (`_relation_change`) -/

variable {R : String} {d : RelDecl}

/-- An insert anywhere only adds facts. -/
theorem rel_grows {u : String} {ρu : Row} (hi : Inserted st st' u ρu) (hb : st.Backed R d)
    (hb' : st'.Backed R d) (a b : Int) (h : st.rel R a b = true) : st'.rel R a b = true := by
  obtain ⟨ρ, hρ, hk, hv⟩ := (hb a b).mp h
  exact (hb' a b).mpr ⟨ρ, (hi d.table ρ).mpr (Or.inl hρ), hk, hv⟩

/-- Inserting `ρu` into the backing table: `after(x, y) ⇔ before(x, y) ∨ (x, y) = (new.k, new.v)`. -/
theorem rel_after_insert {ρu : Row} (hi : Inserted st st' d.table ρu) (hb : st.Backed R d)
    (hb' : st'.Backed R d) (a b : Int) :
    st'.rel R a b = true ↔ st.rel R a b = true ∨ (ρu d.key = some a ∧ ρu d.value = some b) := by
  rw [hb' a b, hb a b]
  constructor
  · rintro ⟨ρ, hρ, hk, hv⟩
    rcases (hi d.table ρ).mp hρ with hρ | ⟨_, rfl⟩
    · exact Or.inl ⟨ρ, hρ, hk, hv⟩
    · exact Or.inr ⟨hk, hv⟩
  · rintro (⟨ρ, hρ, hk, hv⟩ | ⟨hk, hv⟩)
    · exact ⟨ρ, (hi d.table ρ).mpr (Or.inl hρ), hk, hv⟩
    · exact ⟨ρu, (hi d.table ρu).mpr (Or.inr ⟨rfl, rfl⟩), hk, hv⟩

/-- Deleting from the backing table only removes facts. -/
theorem rel_after_delete {ρu : Row} (hd : Deleted st st' d.table ρu) (hb : st.Backed R d)
    (hb' : st'.Backed R d) (a b : Int) (h : st'.rel R a b = true) : st.rel R a b = true := by
  obtain ⟨ρ, hρ, hk, hv⟩ := (hb' a b).mp h
  exact (hb a b).mpr ⟨ρ, ((hd d.table ρ).mp hρ).1, hk, hv⟩

/-- A statement on another table leaves the relation unchanged. -/
theorem rel_unchanged (hb : st.Backed R d) (hb' : st'.Backed R d)
    (hrows : ∀ ρ, st'.rows d.table ρ ↔ st.rows d.table ρ) : st'.rel R = st.rel R := by
  funext a b
  apply Bool.eq_iff_iff.mpr
  rw [hb' a b, hb a b]
  simp only [hrows]

/-! ## The FK template -/

/-- Every stored row of `t` satisfies MATCH SIMPLE against relation `R`. -/
def Inclusion (st : State) (t c1 c2 R : String) : Prop :=
  ∀ ρ, st.rows t ρ → fkMatchSimple (st.rel R) (ρ c1) (ρ c2) = true

/-- Row `ρ` of the referencing table references row `ρu` of the referenced table. -/
def References (ρ : Row) (c1 c2 : String) (ρu : Row) (d : RelDecl) : Prop :=
  ∃ a b, ρ c1 = some a ∧ ρ c2 = some b ∧ ρu d.key = some a ∧ ρu d.value = some b

variable {c1 c2 : String}

/-- The inclusion survives when no row is added to `t` and every fact a stored row relies on
survives. -/
theorem inclusion_of (hrows : ∀ ρ, st'.rows t ρ → st.rows t ρ) (hI : Inclusion st t c1 c2 R)
    (hkeep : ∀ ρ, st.rows t ρ → ∀ a b, ρ c1 = some a → ρ c2 = some b →
      st.rel R a b = true → st'.rel R a b = true) :
    Inclusion st' t c1 c2 R := by
  intro ρ h'
  have h := hrows ρ h'
  have hI' := hI ρ h
  cases hc1 : ρ c1 with
  | none => rfl
  | some a =>
    cases hc2 : ρ c2 with
    | none => rfl
    | some b =>
      rw [hc1, hc2] at hI'
      exact hkeep ρ h a b hc1 hc2 hI'

/-- INSERT into the referencing table: the new row passed the FK check. -/
theorem fk_insert_referencing {ρ : Row} (hi : Inserted st st' t ρ) (hb : st.Backed R d)
    (hb' : st'.Backed R d) (hne : d.table ≠ t) (hI : Inclusion st t c1 c2 R)
    (hnew : fkMatchSimple (st.rel R) (ρ c1) (ρ c2) = true) : Inclusion st' t c1 c2 R := by
  have hrel : st'.rel R = st.rel R :=
    rel_unchanged hb hb' fun ρ' => by rw [hi]; simp [hne]
  intro ρ' h'
  rw [hrel]
  rcases (hi t ρ').mp h' with h' | ⟨_, rfl⟩
  · exact hI ρ' h'
  · exact hnew

/-- UPDATE of the referencing table: the new image passed the FK check. -/
theorem fk_update_referencing {ρ ρ' : Row} (hu : Updated st st' t ρ ρ') (hb : st.Backed R d)
    (hb' : st'.Backed R d) (hne : d.table ≠ t) (hI : Inclusion st t c1 c2 R)
    (hnew : fkMatchSimple (st.rel R) (ρ' c1) (ρ' c2) = true) : Inclusion st' t c1 c2 R := by
  have hrel : st'.rel R = st.rel R :=
    rel_unchanged hb hb' fun ρ'' => by rw [hu]; simp [hne]
  intro ρ'' h''
  rw [hrel]
  rcases (hu t ρ'').mp h'' with ⟨h'', _⟩ | ⟨_, rfl⟩
  · exact hI ρ'' h''
  · exact hnew

/-- DELETE from the referencing table: nothing to check. -/
theorem fk_delete_referencing {ρ : Row} (hd : Deleted st st' t ρ) (hb : st.Backed R d)
    (hb' : st'.Backed R d) (hne : d.table ≠ t) (hI : Inclusion st t c1 c2 R) :
    Inclusion st' t c1 c2 R := by
  have hrel : st'.rel R = st.rel R :=
    rel_unchanged hb hb' fun ρ' => by rw [hd]; simp [hne]
  intro ρ' h'
  rw [hrel]
  exact hI ρ' ((hd t ρ').mp h').1

/-- INSERT into the referenced table: facts are only added. -/
theorem fk_insert_referenced {ρu : Row} (hi : Inserted st st' d.table ρu) (hb : st.Backed R d)
    (hb' : st'.Backed R d) (hne : d.table ≠ t) (hI : Inclusion st t c1 c2 R) :
    Inclusion st' t c1 c2 R := by
  apply inclusion_of ?_ hI
  · intro ρ _ a b _ _ h
    exact rel_grows hi hb hb' a b h
  · intro ρ h
    rcases (hi t ρ).mp h with h | ⟨h, _⟩
    · exact h
    · exact absurd h.symm hne

/-- DELETE from the referenced table under ON DELETE RESTRICT: no stored row of `t` references
the deleted row (`_restrict_guard`). -/
theorem fk_delete_referenced {ρu : Row} (hd : Deleted st st' d.table ρu) (hb : st.Backed R d)
    (hb' : st'.Backed R d) (hI : Inclusion st t c1 c2 R)
    (guard : ∀ ρ, st.rows t ρ → ¬ References ρ c1 c2 ρu d) : Inclusion st' t c1 c2 R := by
  apply inclusion_of (fun ρ h => ((hd t ρ).mp h).1) hI
  intro ρ hρ a b h1 h2 hrel
  obtain ⟨ρ'', hρ'', hk, hv⟩ := (hb a b).mp hrel
  refine (hb' a b).mpr ⟨ρ'', (hd d.table ρ'').mpr ⟨hρ'', ?_⟩, hk, hv⟩
  rintro ⟨_, rfl⟩
  exact guard ρ hρ ⟨a, b, h1, h2, hk, hv⟩

/-- UPDATE of the referenced table under ON UPDATE RESTRICT: the pair is unchanged, or no stored
row of `t` references the old image (`_restrict_guard`). -/
theorem fk_update_referenced {ρu ρu' : Row} (hu : Updated st st' d.table ρu ρu') (hb : st.Backed R d)
    (hb' : st'.Backed R d) (hne : d.table ≠ t) (hI : Inclusion st t c1 c2 R)
    (guard : (ρu' d.key = ρu d.key ∧ ρu' d.value = ρu d.value)
      ∨ ∀ ρ, st.rows t ρ → ¬ References ρ c1 c2 ρu d) : Inclusion st' t c1 c2 R := by
  apply inclusion_of ?_ hI
  · intro ρ hρ a b h1 h2 hrel
    obtain ⟨ρ'', hρ'', hk, hv⟩ := (hb a b).mp hrel
    by_cases heq : ρ'' = ρu
    · subst heq
      rcases guard with ⟨gk, gv⟩ | guard
      · exact (hb' a b).mpr ⟨ρu', (hu d.table ρu').mpr (Or.inr ⟨rfl, rfl⟩), gk ▸ hk, gv ▸ hv⟩
      · exact absurd ⟨a, b, h1, h2, hk, hv⟩ (guard ρ hρ)
    · exact (hb' a b).mpr ⟨ρ'', (hu d.table ρ'').mpr (Or.inl ⟨hρ'', fun h => heq h.2⟩), hk, hv⟩
  · intro ρ h
    rcases (hu t ρ).mp h with ⟨h, _⟩ | ⟨h, _⟩
    · exact h
    · exact absurd h.symm hne

end MLS
