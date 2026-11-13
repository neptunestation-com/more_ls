# Taking over an existing schema: the runbook

You have a Postgres database in production: tables, data, an application, and row-level security
policies written by hand over the years.  You want to keep the tables and the data, replace the
policies with rules that are proved, and know exactly what changes for whom on the day you
switch.  This is the runbook for that, step by step, on a real example.

The example is Supabase's team-and-projects starter (`tests/brownfield/starter_schema.sql`,
with the data of `tests/brownfield/starter_data.sql`): users in `auth.users`, `profiles`,
`teams`, a `team_members` membership table, `projects` of a team, `tasks` of a project, a
`project_overview` view and an `is_team_member` helper function.  Only `profiles` has policies
today; `teams`, `projects` and `tasks` have row-level security enabled and no policy, so app roles
read nothing there; `team_members` has row-level security off, so every app role with a grant reads
every row.  `tests/platform/test_runbook.py` replays this page on the `supabase/postgres` image:
every command below runs and prints the lines shown (`...` marks an elision), and the test then
runs the differential test against the deployed database.

`$DATABASE_URL` is a connection string for the role that owns the tables, `postgres` on
Supabase.  Nothing here needs a superuser.

The steps:

1. Back up
2. Record what the old policies allow
3. Import the skeleton
4. Review the notes, and close the bypass paths
5. Write the requirements
6. Write the rules and the properties
7. Prove
8. Compare with what the old policies allowed
9. See what the deploy will drop
10. Check the existing data
11. Deploy
12. Confirm: drift clean, every table proved
13. Rollback

## 1. Back up

    $ pg_dump --format=custom --file=backup.dump "$DATABASE_URL"

The deploy runs in one transaction and every step after it can be undone with MLS itself (step
13), but take the dump anyway: it is the rollback of last resort, `pg_restore --clean` of the
policies and grants it holds.

## 2. Record what the old policies allow

Import the schema as it is, policies included, before you change anything:

    $ uv run mls import --dsn "$DATABASE_URL" --out before.mls
    ...
    before.mls: imported 5 table(s), 49 note(s)

`before.mls` is a spec of today's behaviour: the hand-written policies are lifted into rules
where MLS can read them and kept as `opaque` blocks where it cannot (here it reads them all).
`team_members` is `observed` in it, because a table without row-level security has no policies
to describe.  You will use it twice: to compare (step 8) and to roll back (step 13).  Commit it.

## 3. Import the skeleton

Now import again, keeping the tables and discarding the policies:

    $ uv run mls import --dsn "$DATABASE_URL" --policies discard --out spec.mls
    ...
    spec.mls: imported 5 table(s), 28 note(s)

`spec.mls` is the starting point of the new spec.  Every table is `adopted`: MLS did not create
it, describes its columns and keys as the catalog has them, and will add rules to it.  The
identity adapter is `supabase` (`@uid` is `auth.uid()`), the app roles are `anon` and
`authenticated` (`service_role` bypasses row-level security on Supabase and is a trusted role,
which every proof report says), the membership table is a relation, and the two readable CHECKs
are rules:

    relation team_members = team_members(user_id, team_id)
    rules {
      R1 check tasks_status_check on tasks: status in ('todo', 'doing', 'done')
      R2 check team_members_role_check on team_members: role in ('owner', 'member')
    }

## 4. Review the notes, and close the bypass paths

The import prints its notes on stderr; read every one.  They say what the skeleton leaves out
and what the deploy will change:

    note: roles not taken as app roles: postgres, service_role (owners, superusers, public, MLS's internal role, or the platform's trusted roles)
    note: column projects.settings has type jsonb, not an MLS type: left out of the spec (drift lists it as foreign)
    note: table team_members has row level security disabled today: the deploy enables it, and until then every app role with a grant reads every row
    note: relation team_members proposed: (user_id, team_id) of team_members (membership table: (user_id, team_id) is a key, user_id refers to auth.users, team_id to teams)
    note: policy Public profiles are viewable by everyone. on profiles discarded (--policies discard; the deploy drops it): permissive for select to public using (true)
    note: policy Users can update own profile. on profiles discarded (--policies discard; the deploy drops it): permissive for update to public using ((( SELECT auth.uid() AS uid) = id))
    note: bypass path: view project_overview reaches table projects without its policies
    note: bypass path: function is_team_member(bigint) reaches table team_members without its policies

- **Discarded policies** are what the old rules said.  Each is quoted with its SQL, so the
  requirements you write in step 5 can say what should stay.
- **Assumptions**: none of the tables forces row-level security, so their owner bypasses every
  policy.  The skeleton records that as `assume ('rls not_forced')` on each table, and every proof
  report repeats it.  `alter table ... force row level security` and removing the assumption
  takes the owner out of the trusted roles.
- **Foreign objects**: the foreign keys onto `auth.users` and between the tables, the `jsonb`
  column and the indexes stay as they are; MLS lists them as `foreign` and never touches them.
- **Bypass paths** are ways into a table that do not go through its policies.  The view runs as
  its owner (it is not `security_invoker`), and the function is `SECURITY DEFINER`, owned by
  `postgres`; both are callable by `anon` and `authenticated`.  Whatever the new policies say,
  `select * from project_overview` would still show every project and task to anyone.

Each bypass note ends with its fix.  Apply them:

    $ psql "$DATABASE_URL" -c "alter view project_overview set (security_invoker = true)"
    $ psql "$DATABASE_URL" -c "revoke execute on function is_team_member(bigint) from public, anon, authenticated"

The view now runs with the caller's rights and sees what the caller's policies admit; the helper
is no longer callable by app roles (nothing in the new rules needs it: membership is the relation).
When a path is meant to be there, a reporting view only the dashboard reads, assume it on the
table instead (`assume ('bypass view project_overview')` in its block): every proof report then says what it
does not cover.

## 5. Write the requirements

In plain sentences, one per grant, each saying who, which rows and what they may do.  These
are what a reviewer approves, and what `mls generate` turns into rules:

    Anyone, signed in or not, can read every profile.
    A signed-in user creates their own profile and edits only their own profile.
    A user sees the teams they belong to, and their own memberships.
    A member of a team reads the team's projects and creates projects in the team.
    A member reads, creates and edits the tasks of their team's projects.

The first two keep what the old `profiles` policies allowed.  The rest are new: today
`teams`, `projects` and `tasks` are closed to app roles, and `team_members` is open to all.

## 6. Write the rules and the properties

A task has no `team_id`: it reaches its team through its project.  A composed relation states
that path, `team_members` then `projects` through its key `id`:

    relation in_project = team_members . projects(team_id, id)

Then the rules, after the two imported CHECKs:

    R3 policy profiles_public on profiles: anon select permissive using true
    R4 policy profiles_signed_in on profiles: authenticated select permissive using true
    R5 policy profile_create_own on profiles: authenticated insert permissive check id = @uid
    R6 policy profile_edit_own on profiles: authenticated update permissive using id = @uid
    R7 policy teams_of_member on teams: authenticated select permissive using team_members(@uid, id)
    R8 policy own_memberships on team_members: authenticated select permissive using user_id = @uid
    R9 policy projects_of_team on projects: authenticated select permissive using team_members(@uid, team_id)
    R10 policy project_create on projects: authenticated insert permissive check team_members(@uid, team_id)
    R11 policy tasks_of_team on tasks: authenticated select permissive using in_project(@uid, project_id)
    R12 policy task_create on tasks: authenticated insert permissive check in_project(@uid, project_id)
    R13 policy task_edit on tasks: authenticated update permissive using in_project(@uid, project_id)

`R8` is the user's own rows, not `team_members(@uid, team_id)`: a policy on `team_members` that
reads the relation backed by `team_members` would make Postgres apply the table's policies
inside its own policy, and Postgres refuses every query on such a table ("infinite recursion
detected in policy").  `mls check` refuses that rule with the fix.

And the properties, one upper bound per table and a lower bound per grant (`must_allow`):

    P1 tenant_isolation projects: team_id in team_members(@uid) for select, insert description 'a user only reads and creates projects of their own teams'
    P2 tenant_isolation tasks: project_id in in_project(@uid) for select, insert, update description 'a user only reads, creates and edits tasks of their own teams'' projects'
    P3 must_allow projects authenticated select: team_members(@uid, team_id) description 'a member reads every project of their teams'
    P4 must_allow tasks authenticated select: in_project(@uid, project_id) description 'a member reads every task of their teams'' projects'
    P5 must_allow profiles anon select: true description 'anyone, signed in or not, reads every profile'
    P6 custom profiles authenticated update old: id = @uid description 'a user only edits their own profile'
    P7 tenant_isolation teams: id in team_members(@uid) for select description 'a user only reads the teams they belong to'
    P8 tenant_isolation team_members: team_id in team_members(@uid) for select description 'a user only sees memberships of their own teams'
    P9 write_subset_read tasks description 'a member can read back every task they create or edit'

`examples/starter_takeover.mls` is `spec.mls` with these lines added.

Or let the LLM loop write them: `uv run mls generate requirements.txt --skeleton spec.mls --out
spec.mls` proposes properties for you to approve, then rules, and repairs them until the
properties are proved (`docs/LLM.md`).  The two imported CHECKs are kept as they are: both
passes are shown them as rules the spec already has, the model numbers its own rules from R3,
and the result is its rules followed by R1 and R2.

## 7. Prove

    $ uv run mls prove spec.mls
    # MLS proof report
    **9 proved, 0 refuted, 0 unknown**
    ...
    - assumed: row level security is not forced on adopted table `profiles`; its owner bypasses every policy and is trusted
    ...
    ### P2 (tenant_isolation): PROVED — a user only reads, creates and edits tasks of their own teams' projects

A REFUTED property comes with a counterexample (guide, section 8); fix the rule or the
requirement, never the property, until everything is proved.  Read the assumptions: they are
the conditions under which the proofs say anything.

## 8. Compare with what the old policies allowed

    $ uv run mls diff before.mls spec.mls
    # MLS access diff
    **10 access change(s), 0 integrity change(s), 8 privilege change(s), 0 undecided**
    ...
    - table 'team_members' is observed in the old version: what roles can do there is not modelled (row level security is off, or the table is not governed), so its access, integrity and privileges are not compared
    ...
    - anon can no longer update some existing rows of profiles that were allowed before
    ...
    - authenticated can now read rows of tasks that were not allowed before
      - before: `false`
      - after: `in_project(@uid, project_id)`

Every change in effect, with an example.  Go through them with whoever owns the requirements;
each one should be a sentence of step 5 or a mistake.  Here they are what was intended.  The
old update policy on `profiles` was `to public`, so it also named `anon`, and the new rules give
`anon` no update (with Supabase's anon key `auth.uid()` is NULL, so in practice the old policy
admitted nothing for `anon` either, but the model does not assume what the API layer sends); the
rest are the new grants.  The exit code is 2 when
anything changed, so a CI job can require a reviewed diff.

`team_members` is not compared: today it has no row-level security, so every app role with a
grant reads all of it, and after the deploy a user reads only their own memberships.  That is
the one change the diff cannot show.

## 9. See what the deploy will drop

    $ uv run mls drift spec.mls --dsn "$DATABASE_URL"
    ...
    unexpected: policy Public profiles are viewable by everyone. on profiles
    unexpected: policy Users can update own profile. on profiles
    ...
    drift: 90 item(s) (5 assumed, 9 foreign)

Before the deploy, drift is the list of what the deploy changes: the policies it drops
(`unexpected`), the ones it creates (`missing`), the grants it takes back, the CHECKs it rewrites
to MLS's `IS TRUE` form (`differs`).  It exits 1.  Had you skipped the fixes of step 4, it would
also list the bypass paths as `hardening`.

## 10. Check the existing data

    $ uv run mls preflight spec.mls --dsn "$DATABASE_URL"
    pre-flight: OK, no existing row violates the new constraints

A row that violates a CHECK the deploy creates would make the deploy fail; preflight finds
those rows first, by key.

## 11. Deploy

    $ uv run mls compile spec.mls --out deploy.sql
    $ psql "$DATABASE_URL" --single-transaction -v ON_ERROR_STOP=1 -f deploy.sql
    NOTICE:  MLS drops policy Public profiles are viewable by everyone. on profiles: the spec does not declare it
    NOTICE:  MLS drops policy Users can update own profile. on profiles: the spec does not declare it
    ...

One transaction: the old policies are dropped and the new ones created together, so there is
no moment when a table is open or closed by accident.  On an adopted table the script replaces
the whole policy set, says which policies it drops, and replaces a CHECK of the same name whose
definition differs, with a notice that quotes both.  On Supabase, run it as `postgres` (the SQL
editor, or a migration).

## 12. Confirm: drift clean, every table proved

    $ uv run mls drift spec.mls --dsn "$DATABASE_URL"
    ...
    foreign: constraint tasks_project_id_fkey on tasks
    assumed: table tasks: row level security is not forced; the owner postgres bypasses the policies and is trusted
    ...
    drift: none, the live schema matches the spec (5 assumed, 9 foreign)
    $ uv run mls status spec.mls --dsn "$DATABASE_URL"
    profiles: proved (P5, P6)
    projects: proved (P1, P3)
    tasks: proved (P2, P4, P9)
    team_members: proved (P8)
    teams: proved (P7)
    status: 5 proved

Drift lists what the spec assumes and what it leaves alone, and exits 0.  Run it from CI against
production from now on: a policy someone adds by hand, a view created without `security_invoker`,
a grant, all show up.  From here on the spec is the source of truth, and changes go through
`mls diff` and `mls migrate` (guide, section 11).

## 13. Rollback

The old behaviour is `before.mls`.  Deploying it restores the old policies (under the names MLS
gives them, with the same effect) and drops the new ones:

    $ uv run mls compile before.mls --out rollback.sql
    $ psql "$DATABASE_URL" --single-transaction -v ON_ERROR_STOP=1 -f rollback.sql
    ...
    NOTICE:  MLS drops policy tasks_of_team on tasks: the spec does not declare it
    ...

`team_members` was not governed before, so `rollback.sql` leaves it alone: put it back as it was.

    $ psql "$DATABASE_URL" -c "drop policy own_memberships on team_members; drop policy mls_rel_team_members_authenticated_1 on team_members; alter table team_members disable row level security"
    $ uv run mls drift before.mls --dsn "$DATABASE_URL"
    ...
    drift: none, the live schema matches the spec (4 assumed, 9 foreign)

The fixes of step 4 stay: they close holes the old setup had.  To deploy again, fix what made you
roll back and start from step 7; the script is safe to rerun on the adopted tables.  If the
database is in a state none of this describes, restore the policies and grants from the dump of
step 1.
