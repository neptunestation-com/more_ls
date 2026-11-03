import MLS.TVL
import MLS.Pred

/-!
# Policy combination and the admission rule per command (SEMANTICS §5)

`effective = (OR of permissive) AND (AND of restrictive)`; no permissive policy → deny; `WITH
CHECK` defaults to `USING`.  Transcribed from `props/effective.py` (`_and`, `_or`, `policy_expr`,
`policies_for`, `effective`) and proved to mean what §5 says: a row is admitted iff some
permissive policy is TRUE on it and every restrictive policy is TRUE on it.  UNKNOWN never
admits, on either side (the NULL findings in FINDINGS), adding a permissive policy only grants,
adding a restrictive policy only denies.

Then the statement outcomes of §5 (`pg/model.py`: filtered / ok / error) in the logic layer —
policies, stored-row constraints, transition rules — and their relation to what the proofs
assume (`props/obligations.py:update_assumptions` etc.) and to the one meaning of "admitted" the
behaviour diff and `must_allow` share (`net_terms`, D38/D40):

* `update_ok_assumptions` (and insert/delete/select): every statement the model executes
  satisfies the obligation's assumptions, so a property proved under them holds of every
  executed statement.
* `update_ok_iff_net` (and the others): for a statement that reads no columns, the model
  executes it iff the net-effect predicate admits it.

Column privileges (`unwritable_unchanged`, the last conjunct of `net_terms`), grants, uniqueness,
FK existence and cardinality are outside this layer and not modelled here.
-/
namespace MLS

inductive Command where
  | select | insert | update | delete | all
  deriving DecidableEq, Repr

inductive Mode where
  | permissive | restrictive
  deriving DecidableEq, Repr

/-- Which clause: `USING` or `WITH CHECK`. -/
inductive Which where
  | using_ | check
  deriving DecidableEq, Repr

structure Policy where
  table : String
  role : String
  command : Command
  mode : Mode
  using_ : Option Pred
  check : Option Pred

/-- `policy_expr`: `WITH CHECK` defaults to `USING`. -/
def Policy.expr (p : Policy) : Which → Option Pred
  | .using_ => p.using_
  | .check => p.check.or p.using_

/-- `policies_for`: the policies of the table and role whose command is `cmd` or `ALL`. -/
def policiesFor (ps : List Policy) (table role : String) (cmd : Command) : List Policy :=
  ps.filter fun p => p.table == table && p.role == role && (p.command == cmd || p.command == .all)

/-! ## `_and` / `_or` -/

def Pred.isLit (b : Bool) : Pred → Bool
  | .lit b' => b == b'
  | _ => false

/-- A chain of binary conjunctions (the n-ary `And` of the JSON syntax). -/
def andChain : List Pred → Pred
  | [] => .lit true
  | [p] => p
  | p :: ps => .and p (andChain ps)

def orChain : List Pred → Pred
  | [] => .lit false
  | [p] => p
  | p :: ps => .or p (orChain ps)

/-- `_and`: drop TRUE literals; a FALSE literal makes the whole FALSE; empty is TRUE; a single
predicate stands alone; otherwise the conjunction. -/
def conj (ps : List Pred) : Pred :=
  let ps := ps.filter fun p => !p.isLit true
  if ps.any (·.isLit false) then .lit false else andChain ps

/-- `_or`, dually. -/
def disj (ps : List Pred) : Pred :=
  let ps := ps.filter fun p => !p.isLit false
  if ps.any (·.isLit true) then .lit true else orChain ps

/-- `effective` -/
def exprs (ps : List Policy) (m : Mode) (which : Which) : List Pred :=
  (ps.filter fun p => p.mode == m).filterMap fun p => p.expr which

def effective (ps : List Policy) (table role : String) (cmd : Command) (which : Which) : Pred :=
  let sel := policiesFor ps table role cmd
  let perm := exprs sel .permissive which
  let rest := exprs sel .restrictive which
  if perm.isEmpty then .lit false else conj (disj perm :: rest)

/-! ## The meaning of the combinators -/

variable {s : Schema} {e : Env} {row : String}

theorem Pred.isLit_eq {b : Bool} {p : Pred} (h : p.isLit b = true) : p = .lit b := by
  cases p <;> simp [Pred.isLit] at h
  rw [h]

theorem andChain_eval : ∀ ps : List Pred,
    (andChain ps).eval s e row = TV.andList (ps.map (Pred.eval s e row))
  | [] => rfl
  | [p] => (TV.and_tt _).symm
  | p :: q :: ps => by
    show TV.and (p.eval s e row) ((andChain (q :: ps)).eval s e row)
        = TV.and (p.eval s e row) (TV.andList ((q :: ps).map (Pred.eval s e row)))
    rw [andChain_eval (q :: ps)]

theorem orChain_eval : ∀ ps : List Pred,
    (orChain ps).eval s e row = TV.orList (ps.map (Pred.eval s e row))
  | [] => rfl
  | [p] => (TV.or_ff _).symm
  | p :: q :: ps => by
    show TV.or (p.eval s e row) ((orChain (q :: ps)).eval s e row)
        = TV.or (p.eval s e row) (TV.orList ((q :: ps).map (Pred.eval s e row)))
    rw [orChain_eval (q :: ps)]

theorem andList_filter_true : ∀ ps : List Pred,
    TV.andList ((ps.filter fun p => !p.isLit true).map (Pred.eval s e row))
      = TV.andList (ps.map (Pred.eval s e row))
  | [] => rfl
  | p :: ps => by
    cases hl : p.isLit true with
    | true =>
      obtain rfl := Pred.isLit_eq hl
      rw [List.filter_cons_of_neg (by simp [Pred.isLit]), andList_filter_true ps, List.map_cons,
        TV.andList_cons]
      show _ = TV.and (ofBool true) _
      rw [show ofBool true = TV.tt from rfl, TV.tt_and]
    | false =>
      rw [List.filter_cons_of_pos (by simp [hl]), List.map_cons, List.map_cons, TV.andList_cons,
        TV.andList_cons, andList_filter_true ps]

theorem orList_filter_false : ∀ ps : List Pred,
    TV.orList ((ps.filter fun p => !p.isLit false).map (Pred.eval s e row))
      = TV.orList (ps.map (Pred.eval s e row))
  | [] => rfl
  | p :: ps => by
    cases hl : p.isLit false with
    | true =>
      obtain rfl := Pred.isLit_eq hl
      rw [List.filter_cons_of_neg (by simp [Pred.isLit]), orList_filter_false ps, List.map_cons,
        TV.orList_cons]
      show _ = TV.or (ofBool false) _
      rw [show ofBool false = TV.ff from rfl, TV.ff_or]
    | false =>
      rw [List.filter_cons_of_pos (by simp [hl]), List.map_cons, List.map_cons, TV.orList_cons,
        TV.orList_cons, orList_filter_false ps]

theorem andList_any_false : ∀ ps : List Pred, ps.any (·.isLit false) = true →
    TV.andList (ps.map (Pred.eval s e row)) = .ff
  | [], h => by simp at h
  | p :: ps, h => by
    simp only [List.any_cons, Bool.or_eq_true] at h
    simp only [List.map_cons, TV.andList_cons]
    cases h with
    | inl hp => rw [Pred.isLit_eq hp]; rfl
    | inr hps => rw [andList_any_false ps hps, TV.and_ff]

theorem orList_any_true : ∀ ps : List Pred, ps.any (·.isLit true) = true →
    TV.orList (ps.map (Pred.eval s e row)) = .tt
  | [], h => by simp at h
  | p :: ps, h => by
    simp only [List.any_cons, Bool.or_eq_true] at h
    simp only [List.map_cons, TV.orList_cons]
    cases h with
    | inl hp => rw [Pred.isLit_eq hp]; rfl
    | inr hps => rw [orList_any_true ps hps, TV.or_tt]

/-- `_and` is the conjunction. -/
theorem conj_eval (ps : List Pred) :
    (conj ps).eval s e row = TV.andList (ps.map (Pred.eval s e row)) := by
  rw [← andList_filter_true ps]
  simp only [conj]
  generalize (ps.filter fun p => !p.isLit true) = qs
  cases ha : qs.any (·.isLit false) with
  | true => simp only [↓reduceIte]; rw [andList_any_false qs ha]; rfl
  | false => simp only [Bool.false_eq_true, ↓reduceIte]; exact andChain_eval qs

/-- `_or` is the disjunction. -/
theorem disj_eval (ps : List Pred) :
    (disj ps).eval s e row = TV.orList (ps.map (Pred.eval s e row)) := by
  rw [← orList_filter_false ps]
  simp only [disj]
  generalize (ps.filter fun p => !p.isLit false) = qs
  cases ha : qs.any (·.isLit true) with
  | true => simp only [↓reduceIte]; rw [orList_any_true qs ha]; rfl
  | false => simp only [Bool.false_eq_true, ↓reduceIte]; exact orChain_eval qs

/-! ## The meaning of `effective` -/

/-- Admission (SEMANTICS §5). -/
def admits (s : Schema) (e : Env) (row : String) (p : Pred) : Bool := TV.isTrue (p.eval s e row)

/-- A row is admitted iff some permissive policy is TRUE on it and every restrictive policy is
TRUE on it. -/
theorem effective_admits (ps : List Policy) (table role : String) (cmd : Command) (which : Which) :
    admits s e row (effective ps table role cmd which)
      = ((exprs (policiesFor ps table role cmd) .permissive which).any (admits s e row)
         && (exprs (policiesFor ps table role cmd) .restrictive which).all (admits s e row)) := by
  simp only [effective]
  cases hperm : exprs (policiesFor ps table role cmd) .permissive which with
  | nil => simp [admits, Pred.eval, ofBool, TV.isTrue]
  | cons p ps' =>
    simp only [List.isEmpty_cons, Bool.false_eq_true, ↓reduceIte, admits]
    rw [conj_eval, List.map_cons, TV.andList_cons, disj_eval, TV.isTrue_and, TV.isTrue_orList,
      TV.isTrue_andList, List.any_map, List.all_map]
    rfl

/-- No permissive policy: deny. -/
theorem effective_no_permissive (ps : List Policy) (table role : String) (cmd : Command)
    (which : Which) (h : exprs (policiesFor ps table role cmd) .permissive which = []) :
    admits s e row (effective ps table role cmd which) = false := by
  rw [effective_admits, h]
  rfl

/-- A restrictive policy that is not TRUE on the row — FALSE or UNKNOWN — denies, whatever the
permissive policies say (FINDINGS: `NOT (archived = 1)` hides every row with `archived IS NULL`). -/
theorem restrictive_denies (ps : List Policy) (table role : String) (cmd : Command) (which : Which)
    (q : Pred) (hq : q ∈ exprs (policiesFor ps table role cmd) .restrictive which)
    (hv : admits s e row q = false) :
    admits s e row (effective ps table role cmd which) = false := by
  rw [effective_admits]
  have : (exprs (policiesFor ps table role cmd) .restrictive which).all (admits s e row) = false := by
    rw [Bool.eq_false_iff, Ne, List.all_eq_true]
    intro hall
    exact absurd (hall q hq) (by simp [hv])
  rw [this, Bool.and_false]

/-- Only a permissive policy that is TRUE grants; one that is UNKNOWN on the row does not. -/
theorem permissive_unknown_denies (ps : List Policy) (table role : String) (cmd : Command)
    (which : Which)
    (h : ∀ p ∈ exprs (policiesFor ps table role cmd) .permissive which, admits s e row p = false) :
    admits s e row (effective ps table role cmd which) = false := by
  rw [effective_admits]
  have : (exprs (policiesFor ps table role cmd) .permissive which).any (admits s e row) = false := by
    rw [Bool.eq_false_iff, Ne, List.any_eq_true]
    rintro ⟨p, hp, hv⟩
    exact absurd hv (by simp [h p hp])
  rw [this, Bool.false_and]

theorem policiesFor_append (ps qs : List Policy) (table role : String) (cmd : Command) :
    policiesFor (ps ++ qs) table role cmd = policiesFor ps table role cmd ++ policiesFor qs table role cmd :=
  List.filter_append ..

theorem exprs_append (ps qs : List Policy) (m : Mode) (which : Which) :
    exprs (ps ++ qs) m which = exprs ps m which ++ exprs qs m which := by
  simp only [exprs, List.filter_append, List.filterMap_append]

/-- Adding a permissive policy never takes a row away. -/
theorem permissive_monotone (ps : List Policy) (table role : String) (cmd : Command) (which : Which)
    (p : Policy) (hp : p.mode = .permissive)
    (h : admits s e row (effective ps table role cmd which) = true) :
    admits s e row (effective (ps ++ [p]) table role cmd which) = true := by
  rw [effective_admits] at h ⊢
  rw [policiesFor_append, exprs_append, exprs_append, List.any_append, List.all_append]
  have hrest : exprs (policiesFor [p] table role cmd) .restrictive which = [] := by
    simp only [exprs, policiesFor, List.filter_cons, List.filter_nil]
    split <;> simp [hp]
  rw [hrest, List.all_nil, Bool.and_true]
  simp only [Bool.and_eq_true] at h
  rw [h.1, h.2, Bool.true_or]
  rfl

/-- Adding a restrictive policy never grants a row. -/
theorem restrictive_antitone (ps : List Policy) (table role : String) (cmd : Command) (which : Which)
    (p : Policy) (hp : p.mode = .restrictive)
    (h : admits s e row (effective ps table role cmd which) = false) :
    admits s e row (effective (ps ++ [p]) table role cmd which) = false := by
  rw [effective_admits] at h ⊢
  rw [policiesFor_append, exprs_append, exprs_append, List.any_append, List.all_append]
  have hperm : exprs (policiesFor [p] table role cmd) .permissive which = [] := by
    simp only [exprs, policiesFor, List.filter_cons, List.filter_nil]
    split <;> simp [hp]
  rw [hperm, List.any_nil, Bool.or_false]
  simp only [Bool.and_eq_false_iff] at h
  cases h with
  | inl h => rw [h, Bool.false_and]
  | inr h => rw [h, Bool.false_and, Bool.and_false]

/-! ## Statement outcomes (SEMANTICS §5, `pg/model.py`) -/

inductive Outcome where
  | filtered | ok | error
  deriving DecidableEq, Repr

/-- The rule layers of one table, as the obligations and the evaluator see them. -/
structure Rules where
  policies : List Policy
  /-- CHECKs, FK (MATCH SIMPLE) predicates and the hierarchy depth bound: `row_constraints`. -/
  constraints : List Pred
  /-- Transition rules over `old`/`new`, the implicit ones included: `transition_constraints`. -/
  transitions : List Pred

namespace Rules

def eff (r : Rules) (table role : String) (cmd : Command) (which : Which) : Pred :=
  effective r.policies table role cmd which

/-- SELECT: the row `r` is visible iff SELECT-USING is TRUE on it. -/
def visible (r : Rules) (s : Schema) (table role : String) (e : Env) : Bool :=
  admits s e "r" (r.eff table role .select .using_)

/-- INSERT: errors unless INSERT-CHECK(new) and the constraints hold on `new`. -/
def insert (r : Rules) (s : Schema) (table role : String) (e : Env) : Outcome :=
  if !admits s e "new" (r.eff table role .insert .check) then .error
  else if !admits s e "new" (conj r.constraints) then .error
  else .ok

/-- UPDATE: the row is targeted iff UPDATE-USING(old) and, when the statement reads columns,
SELECT-USING(old); it errors unless UPDATE-CHECK(new) and, when reading, SELECT-USING(new),
and the constraints and transitions hold. -/
def update (r : Rules) (s : Schema) (table role : String) (readsColumns : Bool) (e : Env) : Outcome :=
  let sel := r.eff table role .select .using_
  if !admits s e "old" (r.eff table role .update .using_) then .filtered
  else if readsColumns && !admits s e "old" sel then .filtered
  else if !admits s e "new" (r.eff table role .update .check) then .error
  else if readsColumns && !admits s e "new" sel then .error
  else if !admits s e "new" (conj r.constraints) then .error
  else if !admits s e "new" (conj r.transitions) then .error
  else .ok

/-- DELETE: the row is targeted iff DELETE-USING(old) and, when reading, SELECT-USING(old). -/
def delete (r : Rules) (s : Schema) (table role : String) (readsColumns : Bool) (e : Env) : Outcome :=
  if !admits s e "old" (r.eff table role .delete .using_) then .filtered
  else if readsColumns && !admits s e "old" (r.eff table role .select .using_) then .filtered
  else .ok

/-! ### What the proofs assume (`props/obligations.py`) -/

def selectAssumptions (r : Rules) (s : Schema) (table role : String) (e : Env) : Bool :=
  admits s e "r" (conj r.constraints) && admits s e "r" (r.eff table role .select .using_)

def insertAssumptions (r : Rules) (s : Schema) (table role : String) (e : Env) : Bool :=
  admits s e "new" (r.eff table role .insert .check) && admits s e "new" (conj r.constraints)

def updateAssumptions (r : Rules) (s : Schema) (table role : String) (e : Env) : Bool :=
  admits s e "old" (conj r.constraints)
    && admits s e "old" (r.eff table role .update .using_)
    && admits s e "new" (r.eff table role .update .check)
    && admits s e "new" (conj r.constraints)
    && admits s e "new" (conj r.transitions)

def deleteAssumptions (r : Rules) (s : Schema) (table role : String) (e : Env) : Bool :=
  admits s e "old" (conj r.constraints) && admits s e "old" (r.eff table role .delete .using_)

/-! ### The one meaning of "admitted" (`net_terms`, D38/D40), without the column layer -/

def netSelect (r : Rules) (s : Schema) (table role : String) (e : Env) : Bool :=
  admits s e "r" (r.eff table role .select .using_)

def netInsert (r : Rules) (s : Schema) (table role : String) (e : Env) : Bool :=
  admits s e "new" (r.eff table role .insert .check) && admits s e "new" (conj r.constraints)

def netUpdate (r : Rules) (s : Schema) (table role : String) (e : Env) : Bool :=
  admits s e "old" (r.eff table role .update .using_)
    && admits s e "new" (r.eff table role .update .check)
    && admits s e "new" (conj r.constraints)
    && admits s e "new" (conj r.transitions)

def netDelete (r : Rules) (s : Schema) (table role : String) (e : Env) : Bool :=
  admits s e "old" (r.eff table role .delete .using_)

variable {r : Rules} {s : Schema} {table role : String} {e : Env}

/-! ### Executed statements satisfy the assumptions

`hstored`: the old image is a stored row, so it satisfies the constraints (CHECKs and FKs are
validated). -/

theorem select_visible_assumptions (hstored : admits s e "r" (conj r.constraints) = true)
    (h : r.visible s table role e = true) : r.selectAssumptions s table role e = true := by
  simp only [selectAssumptions, hstored, Bool.true_and]
  exact h

theorem insert_ok_assumptions (h : r.insert s table role e = .ok) :
    r.insertAssumptions s table role e = true := by
  simp only [Rules.insert] at h
  split at h
  · cases h
  split at h
  · cases h
  simp_all [insertAssumptions]

theorem update_ok_assumptions (hstored : admits s e "old" (conj r.constraints) = true)
    (readsColumns : Bool) (h : r.update s table role readsColumns e = .ok) :
    r.updateAssumptions s table role e = true := by
  simp only [Rules.update] at h
  split at h
  · cases h
  split at h
  · cases h
  split at h
  · cases h
  split at h
  · cases h
  split at h
  · cases h
  split at h
  · cases h
  simp_all [updateAssumptions]

theorem delete_ok_assumptions (hstored : admits s e "old" (conj r.constraints) = true)
    (readsColumns : Bool) (h : r.delete s table role readsColumns e = .ok) :
    r.deleteAssumptions s table role e = true := by
  simp only [Rules.delete] at h
  split at h
  · cases h
  split at h
  · cases h
  simp_all [deleteAssumptions]

/-- An update that would hide the row from its author is an error, never ok (FINDINGS). -/
theorem update_ok_visible_after (h : r.update s table role true e = .ok) :
    admits s e "new" (r.eff table role .select .using_) = true := by
  simp only [Rules.update] at h
  split at h
  · cases h
  split at h
  · cases h
  split at h
  · cases h
  split at h
  · cases h
  simp_all

/-! ### Executed iff admitted by the net-effect predicate -/

theorem select_visible_iff_net : r.visible s table role e = r.netSelect s table role e := rfl

theorem insert_ok_iff_net : r.insert s table role e = .ok ↔ r.netInsert s table role e = true := by
  constructor
  · intro h
    simp only [Rules.insert] at h
    split at h
    · cases h
    split at h
    · cases h
    simp_all [netInsert]
  · intro h
    simp only [netInsert, Bool.and_eq_true] at h
    simp [Rules.insert, h.1, h.2]

theorem update_ok_iff_net :
    r.update s table role false e = .ok ↔ r.netUpdate s table role e = true := by
  constructor
  · intro h
    simp only [Rules.update] at h
    split at h
    · cases h
    split at h
    · cases h
    split at h
    · cases h
    split at h
    · cases h
    split at h
    · cases h
    split at h
    · cases h
    simp_all [netUpdate]
  · intro h
    simp only [netUpdate, Bool.and_eq_true] at h
    simp [Rules.update, h.1.1.1, h.1.1.2, h.1.2, h.2]

/-- With column reads, the net-effect predicate still follows from an executed update. -/
theorem update_ok_net (readsColumns : Bool) (h : r.update s table role readsColumns e = .ok) :
    r.netUpdate s table role e = true := by
  simp only [Rules.update] at h
  split at h
  · cases h
  split at h
  · cases h
  split at h
  · cases h
  split at h
  · cases h
  split at h
  · cases h
  split at h
  · cases h
  simp_all [netUpdate]

theorem delete_ok_iff_net :
    r.delete s table role false e = .ok ↔ r.netDelete s table role e = true := by
  constructor
  · intro h
    simp only [Rules.delete] at h
    split at h
    · cases h
    split at h
    · cases h
    simp_all [netDelete]
  · intro h
    simp [Rules.delete, netDelete] at h ⊢
    exact h

end Rules

end MLS
