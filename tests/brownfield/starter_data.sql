-- Data for the Supabase starter (starter_schema.sql), as a project has it before a takeover:
-- four users, two teams, a user in no team, profiles for three of them, projects and tasks
-- nested under the teams.  GUIDE §3 runs on it (tests/platform/test_guide_takeover.py).

insert into auth.users (id) values
  ('00000000-0000-0000-0000-00000000000a'),  -- alice: owner of Design
  ('00000000-0000-0000-0000-00000000000b'),  -- bob: member of Design
  ('00000000-0000-0000-0000-00000000000c'),  -- carol: owner of Platform
  ('00000000-0000-0000-0000-00000000000d');  -- dave: in no team

insert into profiles (id, username, full_name) values
  ('00000000-0000-0000-0000-00000000000a', 'alice', 'Alice Anders'),
  ('00000000-0000-0000-0000-00000000000b', 'bob', 'Bob Brandt'),
  ('00000000-0000-0000-0000-00000000000c', 'carol', 'Carol Chen');

insert into teams (id, name) overriding system value values (1, 'Design'), (2, 'Platform');
select setval(pg_get_serial_sequence('teams', 'id'), 2);

insert into team_members (team_id, user_id, role) values
  (1, '00000000-0000-0000-0000-00000000000a', 'owner'),
  (1, '00000000-0000-0000-0000-00000000000b', 'member'),
  (2, '00000000-0000-0000-0000-00000000000c', 'owner');

insert into projects (id, team_id, name, archived) overriding system value values
  (10, 1, 'Website', false),
  (11, 1, 'Old brand', true),
  (20, 2, 'API', false);
select setval(pg_get_serial_sequence('projects', 'id'), 20);

insert into tasks (id, project_id, title, status, assignee_id) overriding system value values
  (100, 10, 'Draft the landing page', 'doing', '00000000-0000-0000-0000-00000000000b'),
  (101, 10, 'Pick a typeface', 'done', '00000000-0000-0000-0000-00000000000a'),
  (110, 11, 'Archive the old logos', 'todo', null),
  (200, 20, 'Rate limits', 'todo', '00000000-0000-0000-0000-00000000000c');
select setval(pg_get_serial_sequence('tasks', 'id'), 200);
