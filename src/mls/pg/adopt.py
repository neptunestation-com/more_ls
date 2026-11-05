"""`mls import` (D45): read a live schema into a spec that is a starting point for a human or
the loop, and `mls check --dsn`: verify a spec's adopted tables against the catalog.

Every table becomes `adopted` when it has row level security enabled and `observed` otherwise
(a policy on a table without RLS means nothing, so nothing is assumed about it).  Columns,
primary key and unique constraints are read from the catalog (MLS types only; others are left
out and noted).  The identity adapter is guessed from the policy expressions and reported when
ambiguous.  Relations are proposed for every (key, value) pair an inclusion-shaped foreign key
targets, and for every two-column primary key a policy's SQL reads.  Every policy becomes a rule
whose predicate is an opaque block with the policy's catalog signature: the columns it depends
on (pg_depend) or names, the context values whose source it reads, the proposed relations whose
table it names.  The lifter (D46) later turns the blocks it can read into the fragment.  Column
privileges become column rules; a foreign key of the inclusion shape becomes an inclusion rule;
CHECKs, triggers, other foreign keys and table-level grants with no policy are listed, not
imported (ROADMAP M5: a CHECK is adopted as a stored-row assumption only once the lifter reads it).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql as psql

from mls.compile.internal import INTERNAL_ROLE
from mls.errors import MlsError, SpecError
from mls.pg.catalog import _strip_schema
from mls.spec.check import IDENT
from mls.spec.model import ColType, Spec

PG_TO_MLS: dict[str, ColType] = {"integer": "int", "text": "text", "boolean": "bool",
                                 "timestamp with time zone": "timestamp", "uuid": "uuid"}
CAST_TO_MLS: dict[str, ColType] = {"uuid": "uuid", "integer": "int", "int": "int", "int4": "int", "text": "text",
                                   "boolean": "bool", "bool": "bool", "timestamptz": "timestamp",
                                   "timestamp with time zone": "timestamp"}
CMD = {"*": "all", "r": "select", "a": "insert", "w": "update", "d": "delete"}
MARKERS = {"supabase": re.compile(r"\bauth\.(uid|jwt)\(\)"),
           "session_setting": re.compile(r"current_setting\('app\."),
           "pg_roles": re.compile(r"\bcurrent_user\b|\bsession_user\b")}
# the deparser writes the claim name as 'name'::text and the cast of the whole as ((...))::integer
JWT_CLAIM = re.compile(r"auth\.jwt\(\)\s*->>\s*'([A-Za-z_][A-Za-z0-9_]*)'(?:::text)?\s*\)*\s*(?:::([a-z ]+))?")
# bare `current_setting('app.x', true)::t` and the compiler's `(NULLIF(current_setting('app.x'::text, true), ''::text))::t`
APP_SETTING = re.compile(r"current_setting\('app\.([a-z_][a-z0-9_]*)'(?:::text)?[^)]*\)(?:,\s*''(?:::text)?\))?"
                         r"\s*\)*\s*(?:::([a-z ]+))?")


@dataclass
class ImportResult:
    spec: dict
    notes: list[str] = field(default_factory=list)


def ident(name: str) -> str:
    """An MLS identifier for a catalog name (lower-case, underscores, at most 63 characters)."""
    s = re.sub(r"[^a-z0-9_]", "_", name.lower())
    if not s or not re.match(r"[a-z_]", s[0]):
        s = "_" + s
    return s[:63]


# ----------------------------------------------------------------------------- catalog reads
def _read(conn: psycopg.Connection, schema: str) -> dict[str, Any]:
    cat: dict[str, Any] = {"tables": {}, "superusers": set(), "roles": {}}
    with conn.cursor() as cur:
        cur.execute(psql.SQL("set local search_path to {}, pg_catalog").format(psql.Identifier(schema)))
        cur.execute("select rolname, rolsuper, rolcanlogin, rolbypassrls from pg_roles")
        for name, sup, login, bypass in cur.fetchall():
            cat["roles"][name] = {"super": sup, "login": login, "bypassrls": bypass}
            if sup:
                cat["superusers"].add(name)
        cur.execute("""select c.oid, c.relname, c.relrowsecurity, c.relforcerowsecurity, pg_get_userbyid(c.relowner)
                       from pg_class c join pg_namespace n on n.oid = c.relnamespace
                       where n.nspname = %s and c.relkind in ('r', 'p') order by c.relname""", (schema,))
        tables = cur.fetchall()
        for oid, name, rls, force, owner in tables:
            t: dict[str, Any] = {"oid": oid, "rls": rls, "force": force, "owner": owner}
            cur.execute("""select a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull
                           from pg_attribute a where a.attrelid = %s and a.attnum > 0 and not a.attisdropped
                           order by a.attnum""", (oid,))
            t["columns"] = [{"num": r[0], "name": r[1], "type": r[2], "notnull": r[3]} for r in cur.fetchall()]
            cur.execute("""select conname, contype, pg_get_constraintdef(oid), conkey, confrelid, confkey,
                                  confupdtype, confdeltype, confmatchtype
                           from pg_constraint where conrelid = %s order by conname""", (oid,))
            t["constraints"] = [{"name": r[0], "type": r[1], "def": _strip_schema(r[2], schema), "cols": list(r[3] or []),
                                 "ref": r[4], "refcols": list(r[5] or []), "upd": r[6], "del": r[7], "match": r[8]}
                                for r in cur.fetchall()]
            cur.execute("""select p.oid, p.polname, p.polcmd, p.polpermissive,
                                  array(select rolname from pg_roles where oid = any (p.polroles) order by 1),
                                  0 = any (p.polroles),
                                  pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid),
                                  array(select d.refobjsubid from pg_depend d
                                        where d.classid = 'pg_policy'::regclass and d.objid = p.oid
                                          and d.refobjid = p.polrelid and d.refobjsubid > 0)
                           from pg_policy p where p.polrelid = %s order by p.polname""", (oid,))
            t["policies"] = [{"name": r[1], "cmd": CMD[r[2]], "permissive": r[3], "roles": list(r[4]), "public": r[5],
                              "using": _strip_schema(r[6], schema), "check": _strip_schema(r[7], schema),
                              "depcols": list(r[8])} for r in cur.fetchall()]
            cur.execute("""select t.tgname from pg_trigger t where t.tgrelid = %s and not t.tgisinternal order by 1""",
                        (oid,))
            t["triggers"] = [r[0] for r in cur.fetchall()]
            cur.execute("""select coalesce(nullif(x.grantee, 0)::regrole::text, 'public'), x.privilege_type
                           from pg_class c, aclexplode(c.relacl) x where c.oid = %s""", (oid,))
            t["acl"] = [(r[0].strip('"'), r[1].lower(), None) for r in cur.fetchall()]
            cur.execute("""select a.attname, coalesce(nullif(x.grantee, 0)::regrole::text, 'public'), x.privilege_type
                           from pg_attribute a, aclexplode(a.attacl) x
                           where a.attrelid = %s and a.attnum > 0 and not a.attisdropped""", (oid,))
            t["acl"] += [(r[1].strip('"'), r[2].lower(), r[0]) for r in cur.fetchall()]
            cat["tables"][name] = t
    oids = {t["oid"]: name for name, t in cat["tables"].items()}
    for t in cat["tables"].values():
        t["colname"] = {c["num"]: c["name"] for c in t["columns"]}
    for t in cat["tables"].values():
        for con in t["constraints"]:
            con["colnames"] = [t["colname"][n] for n in con["cols"]]
            ref = oids.get(con["ref"])
            con["reftable"] = ref
            con["refcolnames"] = [cat["tables"][ref]["colname"][n] for n in con["refcols"]] if ref else []
    return cat


# ----------------------------------------------------------------------------- the import
def import_schema(conn: psycopg.Connection, schema: str = "public", roles: list[str] | None = None) -> ImportResult:
    with conn.transaction():
        cat = _read(conn, schema)
    notes: list[str] = []
    tables = cat["tables"]
    if not tables:
        raise SpecError([MlsError("$.tables", f"schema '{schema}' has no tables", "pass --schema", "import")])

    # --- roles: what the policies and grants name, minus owners, superusers and the internal role
    if roles is None:
        found: set[str] = set()
        for name, t in tables.items():
            for p in t["policies"]:
                found.update(p["roles"])
            found.update(g for g, _, _ in t["acl"])
        owners = {t["owner"] for t in tables.values()}
        skipped = sorted(r for r in found if r in owners or r in cat["superusers"] or r == INTERNAL_ROLE
                         or r.startswith("pg_") or r == "public")
        roles = sorted(r for r in found if r not in skipped and r in cat["roles"])
        if skipped:
            notes.append(f"roles not taken as app roles: {', '.join(skipped)} (owners, superusers, public, "
                         f"or MLS's internal role)")
    if not roles:
        raise SpecError([MlsError("$.roles", "no app role was found in the policies and grants of schema "
                                             f"'{schema}'", "pass --role NAME for each app role", "import")])

    # --- identity: guessed from the policy expressions
    texts = [e for t in tables.values() for p in t["policies"] for e in (p["using"], p["check"]) if e]
    hits = {a: sum(1 for e in texts if m.search(e)) for a, m in MARKERS.items()}
    ranked = sorted((n, a) for a, n in hits.items() if n)
    if not ranked:
        adapter = "pg_roles"
        notes.append("identity: no policy reads auth.uid()/auth.jwt(), current_setting('app.*') or current_user; "
                     "the adapter defaults to pg_roles")
    else:
        adapter = ranked[-1][1]
        if len(ranked) > 1:
            notes.append(f"identity: ambiguous, policies read " + ", ".join(f"{a} ({n})" for n, a in ranked[::-1])
                         + f"; the adapter is {adapter} (the most read); the others' policies stay opaque")
    context: dict[str, dict] = {}
    markers: dict[str, re.Pattern] = {}  # context name -> what a policy text must contain to read it
    if adapter == "supabase":
        if any("auth.uid()" in e for e in texts):
            context["uid"] = {"source": "auth.uid", "type": "uuid"}
            markers["uid"] = re.compile(r"auth\.uid\(\)")
        for e in texts:
            for claim, cast in JWT_CLAIM.findall(e):
                name = ident(claim)
                ty = CAST_TO_MLS.get((cast or "").strip())
                if ty is None:
                    ty = "text"
                    if cast:
                        notes.append(f"identity: claim '{claim}' is cast to {cast.strip()}, not an MLS type; "
                                     f"declared as text")
                if name not in context:
                    context[name] = {"source": f"jwt.claims.{claim}", "type": ty}
                    markers[name] = re.compile(r"auth\.jwt\(\)\s*->>\s*'" + re.escape(claim) + "'")
    elif adapter == "session_setting":
        for e in texts:
            for name, cast in APP_SETTING.findall(e):
                ty = CAST_TO_MLS.get((cast or "").strip())
                if ty is None:
                    ty = "text"
                    if cast:
                        notes.append(f"identity: app.{name} is cast to {cast.strip()}, not an MLS type; declared as text")
                if name not in context:
                    context[name] = {"source": f"app.{name}", "type": ty}
                    markers[name] = re.compile(r"current_setting\('app\." + re.escape(name) + "'")
    else:
        context["role"] = {"source": "current_user", "type": "text"}
        markers["role"] = MARKERS["pg_roles"]

    # --- tables
    spec_tables: dict[str, dict] = {}
    modelled: dict[str, set[str]] = {}
    for name, t in tables.items():
        cols: dict[str, dict] = {}
        for c in t["columns"]:
            ty = PG_TO_MLS.get(c["type"])
            if ty is None:
                notes.append(f"column {name}.{c['name']} has type {c['type']}, not an MLS type: left out of the "
                             f"spec (drift lists it as foreign)")
                continue
            cols[c["name"]] = {"type": ty, "nullable": not c["notnull"]}
        modelled[name] = set(cols)
        tb: dict[str, Any] = {"columns": cols, "mode": "adopted" if t["rls"] else "observed"}
        if not t["rls"]:
            notes.append(f"table {name} has row level security disabled: observed, not adopted (enable it and "
                         f"set mode to adopted to govern it)")
        else:  # the hardening state, recorded as assumptions (D45): what drift would otherwise report
            assume: list[dict] = []
            if t["owner"] in roles:
                assume.append({"owner": t["owner"]})
            if not t["force"]:
                assume.append({"rls": "not_forced"})
            assume += [{"bypassrls": r} for r in roles if cat["roles"][r]["bypassrls"]]
            if assume:
                tb["assume"] = assume
                notes.append(f"table {name}: assumed " + ", ".join(
                    f"owner {a['owner']} is trusted" if "owner" in a else
                    f"row level security is not forced" if "rls" in a else
                    f"{a['bypassrls']} has BYPASSRLS" for a in assume) + " (fix the hardening and remove the assumption)")
        for con in t["constraints"]:
            if con["type"] == "p":
                if set(con["colnames"]) <= set(cols):
                    tb["primary_key"] = con["colnames"]
                else:
                    notes.append(f"primary key of {name} uses a column that is not modelled; left out")
                if con["name"] != f"{name}_pkey":
                    notes.append(f"primary key of {name} is named {con['name']}, MLS expects {name}_pkey "
                                 f"(drift reports the name)")
            elif con["type"] == "u":
                if set(con["colnames"]) <= set(cols):
                    tb.setdefault("unique", []).append(con["colnames"])
                    expect = f"{name}_{'_'.join(con['colnames'])}_key"
                    if con["name"] != expect:
                        notes.append(f"unique constraint {con['name']} on {name} is named differently from MLS's "
                                     f"{expect} (drift reports the name)")
                else:
                    notes.append(f"unique constraint {con['name']} on {name} uses a column that is not modelled; left out")
        spec_tables[name] = tb
    governed = {n for n, tb in spec_tables.items() if tb["mode"] == "adopted"}

    def key_sets(table: str) -> list[list[str]]:
        tb = spec_tables[table]
        return ([tb["primary_key"]] if tb.get("primary_key") else []) + tb.get("unique", [])

    # --- relations: inclusion-shaped foreign keys, and two-column keys a policy reads
    relations: dict[str, dict] = {}
    rel_by_pair: dict[tuple[str, str, str], str] = {}

    def propose(table: str, key: str, value: str, why: str) -> str | None:
        if table not in governed:
            notes.append(f"relation on {table} ({key}, {value}) not proposed: the table is observed ({why})")
            return None
        pair = (table, key, value)
        if pair in rel_by_pair:
            return rel_by_pair[pair]
        name = ident(table) if ident(table) not in relations else ident(f"{table}_{key}_{value}")
        relations[name] = {"table": table, "key": key, "value": value}
        rel_by_pair[pair] = name
        notes.append(f"relation {name} proposed: ({key}, {value}) of {table} ({why})")
        return name

    inclusions: list[dict] = []
    for name, t in tables.items():
        if name not in governed:
            continue
        for con in t["constraints"]:
            if con["type"] != "f":
                continue
            shape_ok = (len(con["colnames"]) == 2 and con["reftable"] in spec_tables
                        and con["refcolnames"] in key_sets(con["reftable"]) and con["match"] == "s"
                        and con["upd"] == "r" and con["del"] == "r"
                        and set(con["colnames"]) <= modelled[name])
            if not shape_ok:
                notes.append(f"foreign key {con['name']} on {name} is not an inclusion (two columns onto a key pair, "
                             f"MATCH SIMPLE, ON UPDATE/DELETE RESTRICT): listed as foreign, not imported")
                continue
            rel = propose(con["reftable"], con["refcolnames"][0], con["refcolnames"][1], f"foreign key {con['name']}")
            if rel is None:
                continue
            inclusions.append({"kind": "inclusion", "name": ident(con["name"]), "table": name,
                               "columns": con["colnames"], "relation": rel, "on_delete": "restrict"})
    for name, t in tables.items():
        for p in t["policies"]:
            for e in (p["using"], p["check"]):
                if not e:
                    continue
                for other in tables:
                    if other == name or not re.search(r'(?<![\w."])"?' + re.escape(other) + r'"?(?![\w"])', e):
                        continue
                    ks = key_sets(other) if other in spec_tables else []
                    for k in ks:
                        if len(k) == 2:
                            propose(other, k[0], k[1], f"policy {p['name']} on {name} reads it")
                            break

    # --- policies as opaque blocks, column privileges as column rules
    opaques: dict[str, dict] = {}
    rules: list[dict] = list(inclusions)
    for name, t in tables.items():
        if name not in governed:
            if t["policies"]:
                notes.append(f"table {name} is observed: its {len(t['policies'])} policies are not imported")
            continue
        for p in t["policies"]:
            pol_roles = list(p["roles"])
            if p["public"]:
                pol_roles = list(roles)
                notes.append(f"policy {p['name']} on {name} applies to public: imported once per app role")
            pol_roles = [r for r in pol_roles if r in roles]
            if not pol_roles:
                notes.append(f"policy {p['name']} on {name} applies to no app role ({', '.join(p['roles'])}): not imported")
                continue
            if len(pol_roles) > 1:
                notes.append(f"policy {p['name']} on {name} applies to {', '.join(pol_roles)}: imported as one rule per "
                             f"role (MLS policies have one role; after a deploy the catalog holds one policy per rule)")
            preds: dict[str, dict] = {}
            for which, text in (("using", p["using"]), ("check", p["check"])):
                if text is None:
                    continue
                if text.strip().lower() in ("true", "false"):  # a literal needs no block
                    preds[which] = {"lit": text.strip().lower() == "true"}
                    continue
                oname = ident(f"{p['name']}_{which}")
                cols = sorted({t["colname"][n] for n in p["depcols"] if n in t["colname"]} |
                              {c for c in t["colname"].values()
                               if re.search(r'(?<![\w."])"?' + re.escape(c) + r'"?(?![\w"])', text)})
                cols = [c for c in cols if c in modelled[name]]
                ctx = [n for n, m in markers.items() if m.search(text)]
                rels = [rn for rn, r in relations.items()
                        if re.search(r'(?<![\w."])"?' + re.escape(r["table"]) + r'"?(?![\w"])', text)]
                opaques[oname] = {"table": name, "columns": cols, "context": ctx, "relations": rels, "sql": text}
                preds[which] = {"opaque": oname}
            if p["cmd"] in ("update", "all") and p["check"] is not None and p["check"] == p["using"]:
                del preds["check"]
                opaques.pop(ident(f"{p['name']}_check"), None)
            if p["cmd"] != "insert" and "using" not in preds:
                preds["using"] = {"lit": True}
            if p["cmd"] == "insert" and "check" not in preds:
                preds["check"] = {"lit": True}
            if ident(p["name"]) != p["name"]:
                notes.append(f"policy \"{p['name']}\" on {name} is imported as rule {ident(p['name'])} (MLS names are "
                             f"identifiers); until a deploy renames it, drift shows the old name as unexpected")
            for i, role in enumerate(pol_roles):
                rname = ident(p["name"]) if i == 0 else ident(f"{p['name']}_{role}")
                rule = {"kind": "policy", "name": rname, "table": name, "role": role, "command": p["cmd"],
                        "mode": "permissive" if p["permissive"] else "restrictive", **preds}
                rules.append(rule)
        permissive = {(r["role"], c) for r in rules if r["kind"] == "policy" and r["table"] == name
                      and r["mode"] == "permissive"
                      for c in (("select", "insert", "update", "delete") if r["command"] == "all" else (r["command"],))}
        grants: dict[tuple[str, str], set[str] | None] = {}
        for grantee, priv, col in t["acl"]:
            if grantee not in roles or priv not in ("select", "insert", "update", "delete"):
                continue
            cur = grants.get((grantee, priv), set())
            grants[(grantee, priv)] = None if col is None or cur is None else cur | {col}
        for (role, priv), cols in sorted(grants.items(), key=lambda kv: kv[0]):
            if (role, priv) not in permissive:
                notes.append(f"{role} has {priv} on {name} but no permissive {priv} policy: not imported (revoke it, "
                             f"or add the policy)")
                continue
            if cols is None:
                continue
            access = "read" if priv == "select" else "write"
            existing = next((r for r in rules if r["kind"] == "column_rule" and r["table"] == name
                             and r["role"] == role and r["access"] == access), None)
            if existing is not None:
                if set(existing["columns"]) != cols:
                    notes.append(f"{role}'s insert and update column grants on {name} differ; the write column rule "
                                 f"takes their union")
                    existing["columns"] = sorted(set(existing["columns"]) | cols)
                continue
            if priv == "delete":
                continue
            rules.append({"kind": "column_rule", "name": ident(f"{name}_{role}_{access}"), "table": name,
                          "role": role, "columns": sorted(cols), "access": access})
        for con in t["constraints"]:
            if con["type"] == "c":
                notes.append(f"CHECK {con['name']} on {name} is listed, not imported: {con['def']}")
        for tg in t["triggers"]:
            notes.append(f"trigger {tg} on {name} is listed, not imported")
    for i, r in enumerate(rules, 1):
        r_ = {"id": f"R{i}"}
        r_.update(r)
        rules[i - 1] = r_

    n_adopted = len(governed)
    comment = (f"imported by mls import from schema '{schema}' on {dt.date.today().isoformat()}: "
               f"{len(spec_tables)} tables ({n_adopted} adopted, {len(spec_tables) - n_adopted} observed), "
               f"{len(opaques)} policy expressions as opaque blocks; see the import notes for what was left out")
    spec = {"$comment": comment, "mls_version": "0.1", "identity": {"adapter": adapter, "context": context},
            "roles": roles, "tables": spec_tables, "relations": relations, "opaques": opaques, "rules": rules,
            "properties": []}
    from mls.spec.check import check_spec

    try:
        check_spec(Spec.model_validate(spec))
    except SpecError as e:
        raise SpecError([MlsError(x.path, f"the imported spec does not check (an import bug): {x.message}",
                                  x.fix, "import") for x in e.errors]) from None
    return ImportResult(spec, notes)


# ----------------------------------------------------------------------------- check --dsn
def verify_adopted(conn: psycopg.Connection, spec: Spec, schema: str = "public") -> list[MlsError]:
    """`mls check --dsn`: every adopted table exists with the columns and types the spec declares,
    every observed table exists, and no owned table is owned by an app role (D45)."""
    errors: list[MlsError] = []
    with conn.transaction():
        cat = _read(conn, schema)
    for name, t in spec.tables.items():
        path = f"$.tables.{name}"
        live = cat["tables"].get(name)
        if t.mode == "owned":
            if live is not None and live["owner"] in spec.roles:
                errors.append(MlsError(f"{path}.mode", f"owned table '{name}' is owned by app role '{live['owner']}' "
                                                       f"in schema '{schema}'; an app-role owner can alter the "
                                                       f"policies, which the compiler refuses to rely on",
                                       f"change the owner outside MLS, or set mode to \"adopted\" and add "
                                       f"{{\"owner\": \"{live['owner']}\"}} to assume", "check"))
            continue
        if live is None:
            errors.append(MlsError(path, f"{t.mode} table '{name}' does not exist in schema '{schema}'",
                                   "create it outside MLS, set mode to \"owned\" so that MLS creates it, or remove it",
                                   "check"))
            continue
        if t.mode == "observed":
            continue
        live_cols = {c["name"]: c for c in live["columns"]}
        for c, col in t.columns.items():
            lc = live_cols.get(c)
            if lc is None:
                errors.append(MlsError(f"{path}.columns.{c}", f"adopted table '{name}' has no column '{c}'",
                                       f"remove it from the spec, or add it outside MLS; the table has: "
                                       f"{', '.join(live_cols)}", "check"))
            elif PG_TO_MLS.get(lc["type"]) != col.type:
                errors.append(MlsError(f"{path}.columns.{c}.type", f"column '{name}.{c}' is {lc['type']} in the "
                                                                   f"catalog, declared {col.type}",
                                       f"declare it as {PG_TO_MLS.get(lc['type'], 'a type MLS does not model')}",
                                       "check"))
    return errors
