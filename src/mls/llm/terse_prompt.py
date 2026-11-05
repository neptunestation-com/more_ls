"""The terse-syntax material of the LLM prompts (D50, #16): the language reference in terse
notation, the grammar, and three complete examples that parse, check and prove (the fast suite
holds them to that).  The examples are purpose-made and share no table with the eval corpus."""
from __future__ import annotations

LANGUAGE_TERSE = """
# MLS in brief

An MLS spec states who may see and change which rows of which tables, and which invariants the
data must satisfy.  Everything is three-valued (SQL semantics): a predicate that is UNKNOWN (NULL)
denies in a policy and passes in a check.  The toolchain proves properties about the rules with
an SMT solver and compiles the rules to PostgreSQL (RLS policies, CHECK constraints, foreign keys,
triggers, grants).  You never write SQL.

## Terms
- name                       a column of the row (in transitions: old.name / new.name)
- @name                      a context value of the current user (declared in identity)
- 3 | -1 | 'x' | true        a constant (no timestamp constants; a '' inside a string doubles the quote)

## Predicates
- a op b                     op in = <> < <= > >=; the right-hand side is a context value or a constant
- a in (c1, c2, ...)         a equals one of the constants (sugar for an "or" of =)
- R(@c, v)                   (@c, v) is a pair of relation R (the user @c is a member of v)
- in_group(H, @c, v)         v is one of the user's groups in hierarchy H or lies below one (policies only)
- now_between(from, until)   from <= now < until; `-` for an open bound
- a is null, p and q, p or q, not p, true, false, opaque(name); parentheses group; not binds
  tighter than and, and tighter than or
- Negated R(...) / in_group is not allowed in policies.  Context values are not allowed in checks.
- A name that is a keyword (check, select, ...) or not an identifier is written "quoted".

## Rules: one per line inside `rules { }`, `ID kind name on table: body requirement 'REQ'`
- R1 policy name on t: role command mode using p check p
  command select|insert|update|delete|all, mode permissive|restrictive.  Permissive policies of a
  command are OR-ed, restrictive ones AND-ed on top (PostgreSQL semantics).  insert has only check
  (new rows it may write); select and delete only using (rows it may touch); update may have both.
- R2 check name on t: p                      every stored row satisfies p (p may be UNKNOWN)
- R3 inclusion name on t: (a, b) in R on_delete restrict   (a, b) of every row is a pair of R (NULLs
  pass); the referenced pair cannot be deleted
- R4 transition name on t: p over old.c and new.c          every update satisfies p
- R5 cardinality name on t: col -> parent max N            at most N rows per value of col (parent
  is the table col refers to)
- R6 column_rule name on t: role read|write c1, c2         the role may read or write only these
  columns of the table (without a column rule the role's policies cover all columns)
  A column rule is per role, not per row: a column some users may see only on some rows (their own
  salary) is its own table, one row per parent row, with its own policies; there is no masking construct.
A role has a privilege on a table only through its policies; a command with no policy is denied.
One implicit privilege: a role whose policy uses a relation R (R(...) / in_group) can thereby read
the key and value columns of R's table (the membership lookup runs as the user), and nothing else.

## Properties: one per line inside `properties { }`, `ID template ...: ... description 'English'`
- P1 tenant_isolation t: col in R(@c) for select, insert      every row the role can touch has col in R(@c)
- P2 column_immutable t: col                                  no admitted update changes the column
- P3 monotone_in: R                                           removing a pair from R never grants access
- P4 write_subset_read t                                      whatever a role can insert or update it can also read
- P5 least_privilege: role t[select, insert]; role t2[select]  no role has more privileges than listed
  (include select on the tables of the relations the role's policies will need; see the implicit
  privilege above)
- P6 invariant_preserved: R2; t: p                            every admitted change keeps the
  invariants (rule ids or table: predicate; inductive proof)
- P7 custom t role command old|new: p                         every row the role can <command>
  satisfies p (an upper bound on access)
- P8 role_separation t command: a | b                         no row is admitted to <command> for
  both roles (the same user under either role)
- P9 must_allow t role command: p                             every valid row where p holds is
  admitted by the role's <command> (a lower bound: catches policies, transitions and column rules
  that deny too much).  The rows are assumed to satisfy the table's checks and inclusions, so p
  states what the user can do, not the integrity rules.  For update p ranges over old.c / new.c (a
  bare c is the new image); state the whole change, e.g. the status goes from open to closed and
  the other columns stay the same.  A p that no valid row can meet refutes the property.
""".strip()

GRAMMAR = """
rules      := 'rules' '{' rule* '}'          properties := 'properties' '{' property* '}'
rule       := ID KIND [NAME] 'on' TABLE ':' body ['requirement' STRING]
body       := ROLE COMMAND MODE ['using' pred] ['check' pred]      (policy)
            | pred                                                 (check, transition)
            | '(' COL ',' COL ')' 'in' RELATION 'on_delete' 'restrict'   (inclusion)
            | COL '->' TABLE 'max' INT                             (cardinality)
            | ROLE ('read' | 'write') COL (',' COL)*               (column_rule)
property   := ID TEMPLATE ... ['description' STRING]              (forms as listed above)
term       := column | old.column | new.column | @context | 'text' | 123 | true | false
atom       := term op term | term in (const, ...) | term is null | relation(key, value)
            | in_group(h, key, value) | now_between(from | -, until | -) | opaque(name) | true | false
pred       := atom | not pred | pred and pred | pred or pred   (not > and > or; parentheses group)
STRING     := '...' with '' for a quote;  # comments run to the end of the line
""".strip()

EXAMPLE_1 = """mls 0.1
comment 'Projects of organisations: members read and edit the boards of their organisations.'

identity supabase { uid: auth.uid uuid }
roles authenticated

table boards owned {
  id int not null,
  org_id int not null,
  name text,
  archived bool not null;
  primary_key (id)
}

table org_members owned {
  user_id uuid not null,
  org_id int not null;
  primary_key (user_id, org_id)
}

relation member_of = org_members(user_id, org_id)

rules {
  R1 policy member_read on boards: authenticated select permissive using member_of(@uid, org_id) requirement 'REQ-1'
  R2 policy member_insert on boards: authenticated insert permissive check member_of(@uid, org_id) and archived = false requirement 'REQ-2'
  R3 policy member_update on boards: authenticated update permissive using member_of(@uid, org_id) check member_of(@uid, org_id) requirement 'REQ-2'
  R4 transition org_immutable on boards: new.org_id = old.org_id requirement 'REQ-3'
  R5 check has_name on boards: not name is null requirement 'REQ-4'
}

properties {
  P1 tenant_isolation boards: org_id in member_of(@uid) for select, insert, update description 'a user only touches boards of organisations they belong to'
  P2 column_immutable boards: org_id description 'a board never changes organisation'
  P3 monotone_in: member_of description 'leaving an organisation never grants access'
  P4 write_subset_read boards description 'whatever a user writes, they can read back'
  P5 least_privilege: authenticated boards[select, insert, update]; authenticated org_members[select] description 'users touch boards and look up their memberships, nothing else'
  P6 must_allow boards authenticated select: member_of(@uid, org_id) description 'a member reads every board of their organisations'
  P7 must_allow boards authenticated insert: member_of(@uid, org_id) and archived = false description 'a member creates an unarchived board in their organisation'
}
"""

EXAMPLE_2 = """mls 0.1
comment 'Squads form a tree; a user sees the chores of their squads and of the squads below; a squad holds at most 100 chores.'

identity session_setting { uid: app.uid uuid }
roles app_user

table squads owned {
  id int not null,
  parent_id int,
  depth int not null;
  primary_key (id)
}

table squad_members owned {
  user_id uuid not null,
  squad_id int not null;
  primary_key (user_id, squad_id)
}

table chores owned {
  id int not null,
  squad_id int not null,
  title text;
  primary_key (id)
}

relation squad_of = squad_members(user_id, squad_id)

hierarchy squad_tree = squads id id parent parent_id depth depth max_depth 4 members squad_of

rules {
  R1 policy subtree_read on chores: app_user select permissive using in_group(squad_tree, @uid, squad_id) requirement 'REQ-1'
  R2 policy member_insert on chores: app_user insert permissive check squad_of(@uid, squad_id) requirement 'REQ-2'
  R3 cardinality chores_per_squad on chores: squad_id -> squads max 100 requirement 'REQ-3'
}

properties {
  P1 custom chores app_user select old: in_group(squad_tree, @uid, squad_id) description 'a user reads only chores of their squads and of the squads below them'
  P2 monotone_in: squad_of description 'leaving a squad never grants access'
  P3 invariant_preserved: R3 description 'a squad never holds more than 100 chores'
  P4 must_allow chores app_user insert: squad_of(@uid, squad_id) description 'a member adds a chore to their squad'
  P5 least_privilege: app_user chores[select, insert]; app_user squad_members[select]; app_user squads[select] description 'users touch chores and look up the squad tree, nothing else'
}
"""

EXAMPLE_3 = """mls 0.1
comment 'Invoices: a clerk handles their own invoices while they are open, an auditor reads every invoice in its validity window; a paid invoice is final.'

identity pg_roles { user: current_user text }
roles clerk, auditor

table invoices owned {
  id int not null,
  owner text not null,
  status text not null,
  total int not null,
  valid_from timestamp,
  valid_until timestamp;
  primary_key (id)
}

rules {
  R1 policy clerk_own on invoices: clerk all permissive using owner = @user check owner = @user requirement 'REQ-1'
  R2 policy auditor_window on invoices: auditor select permissive using now_between(valid_from, valid_until) requirement 'REQ-2'
  R3 check status_known on invoices: status in ('draft', 'sent', 'paid') requirement 'REQ-3'
  R4 check total_not_negative on invoices: total >= 0 requirement 'REQ-3'
  R5 transition paid_is_final on invoices: not old.status = 'paid' or new.status = 'paid' requirement 'REQ-4'
  R6 column_rule clerk_edits on invoices: clerk write status, total requirement 'REQ-5'
}

properties {
  P1 column_immutable invoices: owner description 'an invoice never changes owner'
  P2 custom invoices clerk select old: owner = @user description 'a clerk reads only their own invoices'
  P3 role_separation invoices update: clerk | auditor description 'nobody updates an invoice as both clerk and auditor'
  P4 invariant_preserved: R3; R4 description 'the status stays known and the total non-negative'
  P5 must_allow invoices clerk select: owner = @user description 'a clerk reads every invoice of theirs'
  P6 must_allow invoices clerk update: old.owner = @user and new.owner = @user and old.status = 'draft' and new.status = 'sent' and new.total = old.total and new.id = old.id and new.valid_from = old.valid_from and new.valid_until = old.valid_until description 'a clerk sends their draft invoice'
}
"""

EXAMPLES = (EXAMPLE_1, EXAMPLE_2, EXAMPLE_3)


def terse_output_reference() -> str:
    """The output contract of a terse pass: the grammar and the three examples."""
    return "\n\n".join(["# Output syntax (MLS terse, D50)\n" + GRAMMAR,
                        "# Three complete examples (schema and the two sections together)"]
                       + [f"```mls\n{e.rstrip()}\n```" for e in EXAMPLES])
