import MLS.Pred
import MLS.Policy

/-!
# Building environments from literal data (for `MLS.Vectors`)

`tests/lean/vectors.py` generates `MLS/Vectors.lean`: concrete predicates, rows, contexts,
relation facts and hierarchy rows, together with the truth value the Python evaluator
(`pg/model.py:eval_pred`) computed, the pair the Python encoder (`logic/tvl.py`) emitted under
the matching valuation, and the statement outcome the oracle (`pg/model.py:Oracle`) gave.  Each
vector is a `#guard`: the build fails when a transcription in `MLS.Pred` or `MLS.Policy` and
its Python original disagree on the vector.  The helpers here turn the literal data into an
`Env`, a `Valuation` and a `Schema` the same way the Python test harness does (a NULL column is
value 0 with the flag set; an absent context value is 0; `has_parent(x)` iff `x` is stored and
has a parent; `parent(x)` is 0 where it is meaningless).
-/
namespace MLS.Vec

/-- Column values per (image, column). -/
abbrev Cols := List (String × String × Option Int)
abbrev Ctx := List (String × Int)
/-- Relation facts: (relation, key, value). -/
abbrev Facts := List (String × Int × Int)
/-- Hierarchy tables: (hierarchy, [(id, parent)]). -/
abbrev Hiers := List (String × List (Int × Option Int))

def cols (l : Cols) : String → String → Option Int :=
  fun img n =>
    match l.find? (fun x => x.1 == img && x.2.1 == n) with
    | some x => x.2.2
    | none => none

def ints (l : Ctx) : String → Int :=
  fun n =>
    match l.find? (fun x => x.1 == n) with
    | some x => x.2
    | none => 0

def facts (l : Facts) : String → Int → Int → Bool :=
  fun r a b => l.any fun x => x.1 == r && x.2.1 == a && x.2.2 == b

/-- A hierarchy table: (id, parent). -/
def forest (nodes : List (Int × Option Int)) : Forest :=
  ⟨fun x => nodes.any fun n => n.1 == x,
   fun x =>
     match nodes.find? (fun n => n.1 == x) with
     | some n => n.2
     | none => none⟩

def forests (l : Hiers) : String → Forest :=
  fun h =>
    match l.find? (fun x => x.1 == h) with
    | some x => forest x.2
    | none => ⟨fun _ => false, fun _ => none⟩

def schema (nullable : List String) (hiers : List (String × String × Nat)) : Schema :=
  { nullable := fun c => nullable.contains c,
    hier := fun h =>
      match hiers.find? (fun x => x.1 == h) with
      | some x => ⟨x.2.1, x.2.2⟩
      | none => ⟨"", 0⟩ }

def env (c : Cols) (ctx : Ctx) (f : Facts) (hs : Hiers) : Env :=
  { col := cols c, ctx := ints ctx, rel := facts f, forest := forests hs,
    opaques := fun _ _ => ⟨false, false⟩ }

/-- The valuation the Python test harness builds from the same data (`tests/logic/test_tvl.py`). -/
def val (c : Cols) (ctx : Ctx) (f : Facts) (hs : Hiers) : Valuation :=
  { val := fun img n => (cols c img n).getD 0,
    null := fun img n => (cols c img n).isNone,
    ctx := ints ctx,
    rel := facts f,
    has := fun h x => (forests hs h).stored x && ((forests hs h).parent x).isSome,
    par := fun h x => ((forests hs h).parent x).getD 0,
    opaqueT := fun _ _ => false,
    opaqueF := fun _ _ => false }

def policy (command : Command) (mode : Mode) (using_ check : Option Pred) : Policy :=
  { table := "t", role := "app", command := command, mode := mode, using_ := using_, check := check }

end MLS.Vec
