"""Normalized catalog snapshots: what Postgres itself says the objects are (pg_get_expr,
pg_get_constraintdef, pg_get_triggerdef, pg_get_indexdef, aclexplode), never the strings we
generated.  Two snapshots taken on the same server (a shadow deploy and the live schema) are
comparable; the schema name is stripped from every definition.
"""
from __future__ import annotations

import re
from typing import Any

import psycopg
from psycopg import sql as psql

Snapshot = dict[str, Any]


def _strip_schema(s: str | None, schema: str) -> str | None:
    if s is None:
        return None
    return re.sub(rf'(?<![\w"])("?{re.escape(schema)}"?)\.', "", s)


def snapshot(conn: psycopg.Connection, schema: str, tables: list[str], roles: list[str],
             observed: list[str] = ()) -> Snapshot:
    """`tables` are snapshotted in full; `observed` ones (D45) only for their existence."""
    out: Snapshot = {"tables": {}, "roles": {}, "other_tables": [], "observed": {}}
    with conn.cursor() as cur:
        cur.execute(psql.SQL("set local search_path to {}, pg_catalog").format(psql.Identifier(schema)))
        cur.execute("""select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
                       where n.nspname = %s and c.relkind in ('r', 'p') order by 1""", (schema,))
        present = [r[0] for r in cur.fetchall()]
        out["other_tables"] = sorted(set(present) - set(tables) - set(observed))
        out["observed"] = {t: t in present for t in observed}
        # enum types (D59): the labels, in the type's order
        cur.execute("""select t.typname, array(select e.enumlabel from pg_enum e where e.enumtypid = t.oid
                                               order by e.enumsortorder)
                       from pg_type t join pg_namespace n on n.oid = t.typnamespace
                       where n.nspname = %s and t.typtype = 'e' order by 1""", (schema,))
        out["enums"] = {r[0]: list(r[1]) for r in cur.fetchall()}
        for t in tables:
            if t not in present:
                out["tables"][t] = None
                continue
            cur.execute("""select c.oid, c.relrowsecurity, c.relforcerowsecurity, pg_get_userbyid(c.relowner)
                           from pg_class c join pg_namespace n on n.oid = c.relnamespace
                           where n.nspname = %s and c.relname = %s""", (schema, t))
            oid, rls, force, owner = cur.fetchone()
            tb: dict[str, Any] = {"rls": rls, "force": force, "owner": owner}
            cur.execute("""select a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
                           from pg_attribute a where a.attrelid = %s and a.attnum > 0 and not a.attisdropped
                           order by a.attnum""", (oid,))
            tb["columns"] = {r[0]: {"type": r[1], "notnull": r[2]} for r in cur.fetchall()}
            cur.execute("""select conname, contype, pg_get_constraintdef(oid), convalidated
                           from pg_constraint where conrelid = %s order by 1""", (oid,))
            tb["constraints"] = {r[0]: {"type": r[1], "def": _strip_schema(r[2], schema), "validated": r[3]}
                                 for r in cur.fetchall()}
            cur.execute("""select polname, polcmd, polpermissive,
                                  array(select rolname from pg_roles where oid = any (polroles) order by 1),
                                  pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid)
                           from pg_policy where polrelid = %s order by 1""", (oid,))
            tb["policies"] = {r[0]: {"cmd": r[1], "permissive": r[2], "roles": list(r[3]),
                                     "using": _strip_schema(r[4], schema), "check": _strip_schema(r[5], schema)}
                              for r in cur.fetchall()}
            cur.execute("""select t.tgname, pg_get_triggerdef(t.oid), p.proname
                           from pg_trigger t join pg_proc p on p.oid = t.tgfoid
                           where t.tgrelid = %s and not t.tgisinternal order by 1""", (oid,))
            tb["triggers"] = {r[0]: {"def": _strip_schema(r[1], schema), "function": r[2]}
                              for r in cur.fetchall()}
            cur.execute("""select c2.relname, pg_get_indexdef(i.indexrelid)
                           from pg_index i join pg_class c2 on c2.oid = i.indexrelid
                           where i.indrelid = %s order by 1""", (oid,))
            tb["indexes"] = {r[0]: _strip_schema(r[1], schema) for r in cur.fetchall()}
            cur.execute("""select coalesce(nullif(x.grantee, 0)::regrole::text, 'public'), x.privilege_type
                           from pg_class c, aclexplode(c.relacl) x where c.oid = %s""", (oid,))
            acl = {(r[0].strip('"'), r[1], None) for r in cur.fetchall()}
            cur.execute("""select a.attname, coalesce(nullif(x.grantee, 0)::regrole::text, 'public'), x.privilege_type
                           from pg_attribute a, aclexplode(a.attacl) x
                           where a.attrelid = %s and a.attnum > 0 and not a.attisdropped""", (oid,))
            acl |= {(r[1].strip('"'), r[2], r[0]) for r in cur.fetchall()}
            tb["acl"] = sorted(e for e in acl if e[0] != owner)
            out["tables"][t] = tb
        # functions in the schema (ours are named mls_*): body, language, security, owner, EXECUTE acl
        cur.execute("""select p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
                              p.proname, p.prosrc, l.lanname, p.prosecdef, pg_get_userbyid(p.proowner),
                              array(select coalesce(nullif(x.grantee, 0)::regrole::text, 'public')
                                    from aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) x where x.privilege_type = 'EXECUTE' order by 1)
                       from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                       join pg_language l on l.oid = p.prolang
                       where n.nspname = %s order by 1""", (schema,))
        out["functions"] = {}
        for sig, name, src, lang, secdef, owner, execute in cur.fetchall():
            out["functions"][name] = {"signature": sig, "body": " ".join(src.split()), "language": lang,
                                      "security_definer": secdef, "owner": owner,
                                      "execute": sorted(g.strip('"') for g in execute if g.strip('"') != owner)}
        for r in roles:
            cur.execute("""select rolbypassrls, rolsuper, rolcanlogin,
                                  has_parameter_privilege(rolname, 'session_replication_role', 'SET'),
                                  array(select m.rolname from pg_auth_members am join pg_roles m on m.oid = am.member
                                        where am.roleid = pg_roles.oid order by 1)
                           from pg_roles where rolname = %s""", (r,))
            row = cur.fetchone()
            if row is None:
                out["roles"][r] = None
                continue
            # D47 hardening: functions (any schema) whose body sets the app.* namespace and that
            # the role may execute, and ALTER ROLE ... SET defaults for app.*
            cur.execute("""select n.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')'
                           from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                           where n.nspname not in ('pg_catalog', 'information_schema')
                             and (p.prosrc ~* $$set_config\\s*\\(\\s*'app\\.$$ or p.prosrc ~* $$\\mset\\s+(local\\s+|session\\s+)?app\\.$$)
                             and has_function_privilege(%s, p.oid, 'EXECUTE') order by 1""", (r,))
            setters = [x[0] for x in cur.fetchall()]
            cur.execute("""select array_agg(c order by c) from pg_db_role_setting s join pg_roles o on o.oid = s.setrole,
                           unnest(s.setconfig) c where o.rolname = %s and c like 'app.%%'""", (r,))
            defaults = cur.fetchone()[0] or []
            out["roles"][r] = {"bypassrls": row[0], "super": row[1], "login": row[2],
                               "can_set_replication_role": row[3], "members": list(row[4]),
                               "app_setters": setters, "app_defaults": list(defaults)}
    return out


# D54: the views and functions through which an app role reaches a table without its policies
_BYPASS_VIEWS = r"""
with recursive dep(view, tbl) as (
  select distinct r.ev_class, t.relname
  from pg_class t join pg_namespace tn on tn.oid = t.relnamespace
  join pg_depend d on d.refclassid = 'pg_class'::regclass and d.refobjid = t.oid
  join pg_rewrite r on d.classid = 'pg_rewrite'::regclass and r.oid = d.objid
  where tn.nspname = %(schema)s and t.relname = any(%(tables)s) and r.ev_class <> t.oid
  union
  select r.ev_class, dep.tbl
  from dep join pg_depend d on d.refclassid = 'pg_class'::regclass and d.refobjid = dep.view
  join pg_rewrite r on d.classid = 'pg_rewrite'::regclass and r.oid = d.objid
  where r.ev_class <> dep.view
)
select dep.tbl, vn.nspname, v.relname, v.relkind,
       array(select ro.rolname from pg_roles ro
             where ro.rolname = any(%(roles)s)
               and has_table_privilege(ro.oid, v.oid, 'SELECT, INSERT, UPDATE, DELETE') order by 1)
from dep join pg_class v on v.oid = dep.view join pg_namespace vn on vn.oid = v.relnamespace
where v.relkind = 'm'
   or (v.relkind = 'v' and not coalesce((select o.option_value::boolean from pg_options_to_table(v.reloptions) o
                                         where o.option_name = 'security_invoker'), false))
order by 1, 2, 3"""

_BYPASS_FUNCTIONS = r"""
select t.relname, n.nspname, p.proname, oidvectortypes(p.proargtypes), pg_get_userbyid(p.proowner),
       array(select ro.rolname from pg_roles ro
             where ro.rolname = any(%(roles)s) and has_function_privilege(ro.oid, p.oid, 'EXECUTE') order by 1)
from pg_proc p join pg_namespace n on n.oid = p.pronamespace join pg_roles o on o.oid = p.proowner,
     pg_class t join pg_namespace tn on tn.oid = t.relnamespace
where p.prosecdef and n.nspname not in ('pg_catalog', 'information_schema')
  and tn.nspname = %(schema)s and t.relname = any(%(tables)s) and t.relkind in ('r', 'p')
  and (o.rolsuper or o.rolbypassrls or (pg_has_role(o.oid, t.relowner, 'USAGE') and not t.relforcerowsecurity))
  and p.prosrc ~* ('\m' || regexp_replace(t.relname, '([^a-zA-Z0-9_])', '\\\1', 'g') || '\M')
order by 1, 2, 3, 4"""


def bypass_paths(conn: psycopg.Connection, schema: str, tables: list[str], roles: list[str]) -> dict[str, list[dict]]:
    """Per table, the bypass paths (D54) the app roles `roles` may use: every view over the table
    (directly or through other views) that is not `security_invoker`, and every materialized view,
    on which an app role holds a privilege (a view runs as its owner, whom the table's policies
    usually do not bind; a materialized view was filled by its owner); and every SECURITY DEFINER
    function an app role may execute whose owner bypasses the table's policies (a superuser, a
    BYPASSRLS role, or the table's owner while row level security is not forced) and whose body
    names the table (a conservative text match, as for D47's setters).  Each is `{"path": "view v"
    | "function f(types)", "kind", "roles", "owner"?}`; a name outside `schema` is qualified."""
    out: dict[str, list[dict]] = {t: [] for t in tables}
    args = {"schema": schema, "tables": list(tables), "roles": list(roles)}

    def part(n: str) -> str:  # as SQL spells it, so the fix in the drift report runs as printed
        return n if re.fullmatch(r"[a-z_][a-z0-9_$]*", n) else '"' + n.replace('"', '""') + '"'

    def name(nsp: str, rel: str) -> str:
        return part(rel) if nsp == schema else f"{part(nsp)}.{part(rel)}"

    with conn.cursor() as cur:
        cur.execute(_BYPASS_VIEWS, args)
        for tbl, nsp, rel, kind, using in cur.fetchall():
            if using:
                out[tbl].append({"path": f"view {name(nsp, rel)}", "roles": list(using),
                                 "kind": "materialized view" if kind == "m" else "view"})
        cur.execute(_BYPASS_FUNCTIONS, args)
        for tbl, nsp, fn, types, owner, executing in cur.fetchall():
            if executing:
                out[tbl].append({"path": f"function {name(nsp, fn)}({types})", "roles": list(executing),
                                 "kind": "function", "owner": owner})
    return out


FOREIGN_KEYS = """
select c.conname, t.relname, a.attname, pn.nspname, p.relname, pa.attname
from pg_constraint c
join pg_class t on t.oid = c.conrelid join pg_namespace n on n.oid = t.relnamespace
join pg_class p on p.oid = c.confrelid join pg_namespace pn on pn.oid = p.relnamespace
join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1]
join pg_attribute pa on pa.attrelid = c.confrelid and pa.attnum = c.confkey[1]
where c.contype = 'f' and n.nspname = %s and t.relname = any(%s) and cardinality(c.conkey) = 1
order by t.relname, c.conname
"""


def foreign_keys(conn: psycopg.Connection, schema: str, tables: list[str], owned: set[str]) -> list[dict[str, str]]:
    """The single-column foreign keys on `tables` whose names are not in `owned` (the ones the
    spec declares): {name, table, column, parent, parent_column}, the parent schema-qualified
    when it is in another schema (`auth.users`).  The differential harness reads them so that its
    data satisfies the schema's own keys (D45: a foreign constraint is not modelled)."""
    out = []
    for name, table, col, pschema, parent, pcol in conn.execute(FOREIGN_KEYS, (schema, tables)).fetchall():
        if name in owned:
            continue
        out.append({"name": name, "table": table, "column": col,
                    "parent": parent if pschema == schema else f"{pschema}.{parent}", "parent_column": pcol})
    return out
