-- A hand-written Supabase-style schema (Milestone 5 acceptance fixture): bare auth.uid() in
-- policies, correlated EXISTS and IN (subquery) memberships, a JWT claim, a policy for two roles,
-- a restrictive policy, a CHECK, a trigger, single-column foreign keys, bigint columns MLS does
-- not model, column-level grants, a grant with no policy, and a table without RLS.
create schema if not exists auth;
create or replace function auth.jwt() returns jsonb language sql stable as $$
  select coalesce(nullif(current_setting('request.jwt.claim', true), ''),
                  nullif(current_setting('request.jwt.claims', true), ''))::jsonb $$;
create or replace function auth.uid() returns uuid language sql stable as $$
  select coalesce(nullif(current_setting('request.jwt.claim.sub', true), ''),
                  (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'))::uuid $$;
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
end $$;
grant usage on schema auth to authenticated, anon;
grant execute on function auth.jwt(), auth.uid() to authenticated, anon;

create table profiles (
  id uuid primary key,
  display_name text not null,
  bio text,
  plan text not null default 'free',
  credits bigint not null default 0,
  created_at timestamptz not null default now()
);
alter table profiles enable row level security;
create policy "profiles are viewable by everyone" on profiles for select to authenticated, anon using (true);
create policy "users update own profile" on profiles for update to authenticated
  using (auth.uid() = id) with check (auth.uid() = id);

create table projects (
  id uuid primary key,
  owner uuid not null references profiles (id),
  name text not null check (name <> ''),
  archived boolean not null default false
);
alter table projects enable row level security;

create table project_members (
  project_id uuid not null references projects (id) on delete cascade,
  user_id uuid not null references profiles (id),
  role text not null default 'member',
  primary key (project_id, user_id)
);
alter table project_members enable row level security;

create policy "members see their projects" on projects for select to authenticated
  using (exists (select 1 from project_members m where m.project_id = projects.id and m.user_id = auth.uid()));
create policy "owners manage projects" on projects for all to authenticated
  using (owner = auth.uid()) with check (owner = auth.uid());
create policy "members see memberships" on project_members for select to authenticated, anon
  using (user_id = auth.uid());

create table tasks (
  id bigserial primary key,
  project_id uuid not null references projects (id),
  title text not null,
  done boolean not null default false,
  assignee uuid references profiles (id),
  priority integer not null default 0 check (priority between 0 and 5)
);
alter table tasks enable row level security;
create policy "tasks of my projects" on tasks for select to authenticated
  using (project_id in (select project_id from project_members where user_id = auth.uid()));
create policy "assignee updates" on tasks for update to authenticated
  using (assignee = auth.uid() and (auth.jwt() ->> 'role') = 'editor');
create policy "no deletes of done tasks" on tasks as restrictive for delete to authenticated using (not done);
create policy "insert into own projects" on tasks for insert to authenticated
  with check (project_id in (select id from projects where owner = auth.uid()));
create function tasks_touch() returns trigger language plpgsql as $$ begin return new; end $$;
create trigger tasks_touch before update on tasks for each row execute function tasks_touch();

create table audit_log (id bigserial primary key, at timestamptz not null default now(), what text not null);

grant select, update on profiles to authenticated;
grant select (id, display_name) on profiles to anon;
grant select, insert, update, delete on projects to authenticated;
grant select on project_members to authenticated, anon;
grant insert on project_members to authenticated;
grant select, insert, update, delete on tasks to authenticated;
grant select on audit_log to authenticated;
