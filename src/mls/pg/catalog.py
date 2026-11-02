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


def snapshot(conn: psycopg.Connection, schema: str, tables: list[str], roles: list[str]) -> Snapshot:
    out: Snapshot = {"tables": {}, "roles": {}, "other_tables": []}
    with conn.cursor() as cur:
        cur.execute(psql.SQL("set local search_path to {}, pg_catalog").format(psql.Identifier(schema)))
        cur.execute("""select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
                       where n.nspname = %s and c.relkind in ('r', 'p') order by 1""", (schema,))
        present = [r[0] for r in cur.fetchall()]
        out["other_tables"] = sorted(set(present) - set(tables))
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
                                    from aclexplode(p.proacl) x where x.privilege_type = 'EXECUTE' order by 1)
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
            out["roles"][r] = None if row is None else {"bypassrls": row[0], "super": row[1], "login": row[2],
                                                        "can_set_replication_role": row[3],
                                                        "members": list(row[4])}
    return out
