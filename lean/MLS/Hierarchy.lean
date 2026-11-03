/-!
# Hierarchies: the walk and its unrolling (SEMANTICS §2, §3.1; D5)

`in_group(hierarchy, key, value)` holds when `(key, g) ∈ members` for `g` = the row's group or one
of its ancestors.  Two renderings:

* `Forest.walk`: the reference meaning (`pg/model.py:in_group`): walk up from the group through
  stored rows, at most `max_depth + 1` nodes; a missing row or a root ends the walk.
* `unroll`: what the encoder emits (`Encoder._in_group`) over the uninterpreted `has_parent` and
  `parent` functions: `rel(k, g) ∨ has(g) ∧ rel(k, parent(g)) ∨ has(g) ∧ has(parent(g)) ∧ …`,
  `max_depth` parent steps.

`unroll_eq_walk`: they agree when the functions describe the forest and the FKs hold.
`walk_stable`: with the depth column bounded by `max_depth`, a longer walk finds nothing more, so
unrolling to `max_depth` is exact.
-/
namespace MLS

/-- The stored rows of a hierarchy table: which ids exist and their parent (`none` for a root). -/
structure Forest where
  stored : Int → Bool
  parent : Int → Option Int

/-- The reference walk: `fuel` nodes at most, starting at `node`. -/
def Forest.walk (f : Forest) (rel : Int → Int → Bool) (key : Int) : Int → Nat → Bool
  | _, 0 => false
  | node, fuel + 1 =>
    f.stored node && (rel key node || (f.parent node).any fun p => f.walk rel key p fuel)

/-- The encoder's alternatives, as the Python builds them: `chain ∧ rel(k, node)` for each node of
the parent chain, `chain` accumulating `has_parent` of the nodes passed. -/
def alts (has : Int → Bool) (par : Int → Int) (rel : Int → Int → Bool) (key : Int) :
    Int → Bool → Nat → List Bool
  | node, chain, 0 => [chain && rel key node]
  | node, chain, depth + 1 => (chain && rel key node) :: alts has par rel key (par node) (chain && has node) depth

/-- `or_(*alts)` -/
def unrollPy (has : Int → Bool) (par : Int → Int) (rel : Int → Int → Bool) (key node : Int)
    (depth : Nat) : Bool :=
  (alts has par rel key node true depth).any id

/-- The same, factored: `rel(k, node) ∨ has(node) ∧ unroll(parent(node), depth - 1)`. -/
def unroll (has : Int → Bool) (par : Int → Int) (rel : Int → Int → Bool) (key : Int) :
    Int → Nat → Bool
  | node, 0 => rel key node
  | node, depth + 1 => rel key node || (has node && unroll has par rel key (par node) depth)

theorem alts_any (has : Int → Bool) (par : Int → Int) (rel : Int → Int → Bool) (key : Int) :
    ∀ (depth : Nat) (node : Int) (chain : Bool),
      (alts has par rel key node chain depth).any id = (chain && unroll has par rel key node depth)
  | 0, node, chain => by simp [alts, unroll]
  | depth + 1, node, chain => by
    simp only [alts, unroll, List.any_cons, id]
    rw [alts_any has par rel key depth (par node) (chain && has node)]
    cases chain <;> cases rel key node <;> cases has node <;> simp

theorem unrollPy_eq_unroll (has : Int → Bool) (par : Int → Int) (rel : Int → Int → Bool)
    (key node : Int) (depth : Nat) :
    unrollPy has par rel key node depth = unroll has par rel key node depth := by
  unfold unrollPy
  rw [alts_any, Bool.true_and]

/-- The uninterpreted functions describe the forest: `has_parent(x)` iff `x` is stored and has a
parent, and `parent(x)` is that parent. -/
structure Forest.Encodes (f : Forest) (has : Int → Bool) (par : Int → Int) : Prop where
  has_eq : ∀ x, has x = (f.stored x && (f.parent x).isSome)
  par_eq : ∀ x p, f.parent x = some p → par x = p

/-- The FKs of SEMANTICS §3.1 on stored data: a member's group is a stored id, a parent is a
stored id. -/
structure Forest.WellFormed (f : Forest) (rel : Int → Int → Bool) : Prop where
  member_stored : ∀ k x, rel k x = true → f.stored x = true
  parent_stored : ∀ x p, f.stored x = true → f.parent x = some p → f.stored p = true

/-- The encoder's unrolling to `depth` parent steps is the walk over `depth + 1` nodes. -/
theorem unroll_eq_walk {f : Forest} {has : Int → Bool} {par : Int → Int} {rel : Int → Int → Bool}
    {key : Int} (hc : f.Encodes has par) (hw : f.WellFormed rel) :
    ∀ (depth : Nat) (node : Int), unroll has par rel key node depth = f.walk rel key node (depth + 1)
  | 0, node => by
    show rel key node = (f.stored node && (rel key node || (f.parent node).any fun p => f.walk rel key p 0))
    cases hr : rel key node with
    | true => simp [hw.member_stored _ _ hr]
    | false => cases f.parent node <;> simp [Forest.walk]
  | depth + 1, node => by
    show (rel key node || (has node && unroll has par rel key (par node) depth))
        = (f.stored node && (rel key node || (f.parent node).any fun p => f.walk rel key p (depth + 1)))
    rw [unroll_eq_walk hc hw depth (par node), hc.has_eq node]
    cases hr : rel key node with
    | true => simp [hw.member_stored _ _ hr]
    | false =>
      cases hp : f.parent node with
      | none => simp
      | some p => rw [hc.par_eq node p hp]; simp

/-- The depth column (SEMANTICS §3.1): a child's depth is its parent's plus one, and every stored
depth is at most `maxDepth`. -/
structure Forest.Depths (f : Forest) (depth : Int → Nat) (maxDepth : Nat) : Prop where
  child : ∀ x p, f.stored x = true → f.parent x = some p → depth x = depth p + 1
  bound : ∀ x, f.stored x = true → depth x ≤ maxDepth

/-- Past `depth node + 1` nodes the walk no longer changes. -/
theorem Forest.walk_stable {f : Forest} {rel : Int → Int → Bool} {key : Int} {depth : Int → Nat}
    {maxDepth : Nat} (hd : f.Depths depth maxDepth) :
    ∀ (fuel fuel' : Nat) (node : Int), depth node + 1 ≤ fuel → depth node + 1 ≤ fuel' →
      f.walk rel key node fuel = f.walk rel key node fuel'
  | 0, _, _, h, _ => absurd h (by omega)
  | _, 0, _, _, h' => absurd h' (by omega)
  | fuel + 1, fuel' + 1, node, h, h' => by
    show (f.stored node && (rel key node || (f.parent node).any fun p => f.walk rel key p fuel))
        = (f.stored node && (rel key node || (f.parent node).any fun p => f.walk rel key p fuel'))
    cases hs : f.stored node with
    | false => simp
    | true =>
      cases hp : f.parent node with
      | none => simp
      | some p =>
        have hdp := hd.child node p hs hp
        simp only [Option.any_some]
        rw [f.walk_stable hd fuel fuel' p (by omega) (by omega)]

/-- Unrolling to `maxDepth` is exact: the walk over `maxDepth + 1` nodes is the walk over any
larger number of nodes. -/
theorem Forest.walk_maxDepth {f : Forest} {rel : Int → Int → Bool} {key : Int} {depth : Int → Nat}
    {maxDepth : Nat} (hd : f.Depths depth maxDepth) (node : Int) (fuel : Nat)
    (hf : maxDepth + 1 ≤ fuel) :
    f.walk rel key node fuel = f.walk rel key node (maxDepth + 1) := by
  cases hs : f.stored node with
  | true =>
    have := hd.bound node hs
    exact f.walk_stable hd fuel (maxDepth + 1) node (by omega) (by omega)
  | false =>
    cases fuel with
    | zero => omega
    | succ fuel => simp [Forest.walk, hs]

end MLS
