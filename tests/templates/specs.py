"""Per-template specs for the differential and mutation tests.  Each variant of the example spec
isolates one template and the data shape its mutation needs (NULLs, column lists, bounds...)."""
from __future__ import annotations

import json

from mls.spec import check_spec
from mls.spec.model import Spec
from tests.conftest import EXAMPLE


def base() -> dict:
    return json.loads(EXAMPLE.read_text())


def make(d: dict) -> Spec:
    spec = Spec.model_validate(d)
    spec = check_spec(spec)
    return spec


def policy_spec() -> Spec:
    return make(base())


def in_spec() -> Spec:
    """A policy with `in` (expanded to an IN list): members see levels 2 and 3 whatever their
    clearance.  The mutation drops the last constant, hiding level 3 from low-clearance members.
    The admin policy (R2) and its lower bound go: in the generated data every member pair is
    also an admin pair, so with R2 the `in` policy would never decide anything."""
    d = base()
    d["rules"] = [r for r in d["rules"] if r["id"] != "R2"]
    d["properties"] = [p for p in d["properties"] if p["id"] != "P8"]
    d["rules"].append({"id": "R10", "kind": "policy", "name": "member_read_high", "table": "docs",
                       "role": "authenticated", "command": "select", "mode": "permissive",
                       "using": {"and": [{"member": {"relation": "member_of", "key": {"ctx": "uid"},
                                                     "value": {"col": "tenant_id"}}},
                                         {"in": [{"col": "level"}, [{"const": 2}, {"const": 3}]]}]}})
    return make(d)


def check_nullable_spec() -> Spec:
    d = base()
    d["tables"]["docs"]["columns"]["flag"] = {"type": "int", "nullable": True}
    d["rules"].append({"id": "C1", "kind": "check", "name": "flag_bit", "table": "docs",
                       "predicate": {"or": [{"cmp": [{"col": "flag"}, "=", {"const": 0}]},
                                            {"cmp": [{"col": "flag"}, "=", {"const": 1}]}]}})
    return make(d)


def transition_nullable_spec() -> Spec:
    d = base()
    d["rules"].append({"id": "T1", "kind": "transition", "name": "owner_immutable", "table": "docs",
                       "predicate": {"cmp": [{"new": "owner_id"}, "=", {"old": "owner_id"}]}})
    return make(d)


def fk_spec() -> Spec:
    return make(base())


def column_rule_spec() -> Spec:
    d = base()
    d["rules"].append({"id": "C1", "kind": "column_rule", "role": "authenticated", "table": "docs",
                       "columns": ["id", "tenant_id", "level"], "access": "read"})
    d["rules"].append({"id": "C2", "kind": "column_rule", "role": "authenticated", "table": "docs",
                       "columns": ["id", "tenant_id", "level", "owner_id"], "access": "write"})
    d["properties"] = []
    return make(d)


def time_spec() -> Spec:
    d = base()
    d["tables"]["docs"]["columns"]["valid_from"] = {"type": "timestamp", "nullable": True}
    d["tables"]["docs"]["columns"]["valid_until"] = {"type": "timestamp", "nullable": True}
    d["rules"].append({"id": "TB", "kind": "policy", "name": "currently_valid", "table": "docs",
                       "role": "authenticated", "command": "all", "mode": "restrictive",
                       "using": {"now_between": {"from": {"col": "valid_from"}, "until": {"col": "valid_until"}}}})
    return make(d)


def opaque_spec() -> Spec:
    d = base()
    d["opaques"] = {"low_or_cleared": {"table": "docs", "columns": ["level"], "context": ["clearance"],
                                       "sql": "level < 1 or {ctx:clearance} >= 3"}}
    d["rules"].append({"id": "OP", "kind": "policy", "name": "opaque_gate", "table": "docs",
                       "role": "authenticated", "command": "all", "mode": "restrictive",
                       "using": {"opaque": "low_or_cleared"}})
    return make(d)


def opaque_impl():
    return {"low_or_cleared": lambda row, ctx: row["level"] < 1 or ctx["clearance"] >= 3}


PG_ROLES = EXAMPLE.parent / "pg_roles_docs.mls.json"
SESSION = EXAMPLE.parent / "session_docs.mls.json"


def session_spec() -> Spec:
    """The example on the session_setting adapter (D47): the harness sets app.uid and
    app.clearance with set_config inside the transaction."""
    return make(json.loads(SESSION.read_text()))


def bigint_spec() -> Spec:
    """The session example with every key `bigint` (D53): the user id, the tenant ids and the
    document id, and the clearance a bigint context value compared with an int column.  The
    harness draws bigint values beyond 2³¹ (and user ids above 2³²), so an `integer` anywhere in
    the compiled script fails."""
    d = json.loads(SESSION.read_text())
    d["identity"]["context"]["uid"]["type"] = "bigint"
    d["identity"]["context"]["clearance"]["type"] = "bigint"
    for t, cols in (("memberships", ("user_id", "tenant_id")), ("admins", ("user_id", "tenant_id")),
                    ("docs", ("id", "tenant_id", "owner_id"))):
        for c in cols:
            d["tables"][t]["columns"][c]["type"] = "bigint"
    return make(d)


def pg_roles_spec() -> Spec:
    """The pg_roles rendering of the example (`examples/pg_roles_docs.mls.json`): same tables and
    rule shape (two permissive SELECT policies on docs, so the all-restrictive mutation is
    detectable), identity from `current_user`, no clearance claim."""
    return make(json.loads(PG_ROLES.read_text()))


# ----------------------------------------------------------------------------- versions (M2)
V2 = EXAMPLE.parent / "tenant_docs_v2.mls.json"


def v_a() -> Spec:
    """The example without the transition rule (no trigger)."""
    d = base()
    d["rules"] = [r for r in d["rules"] if r["id"] != "R9"]
    d["properties"] = [p for p in d["properties"] if p["id"] != "P2"]
    return make(d)


def v_b() -> Spec:
    return make(base())


def v_c() -> Spec:
    return make(json.loads(V2.read_text()))


def v_c_level() -> Spec:
    """v2 plus a permissive policy on `level` alone: the migration from v1 creates an index
    (`mls_ix_docs_level`) concurrently, and the migration back drops it.  (v2's policy on
    `owner_id` needs no index of its own: v1's inclusion index (owner_id, tenant_id) covers it.)"""
    d = json.loads(V2.read_text())
    d["rules"].append({"id": "R16", "kind": "policy", "name": "low_level_read", "table": "docs",
                       "role": "authenticated", "command": "select", "mode": "permissive",
                       "using": {"cmp": [{"col": "level"}, "<=", {"const": 0}]}})
    return make(d)


def v_d() -> Spec:
    """v2 plus: archived becomes NOT NULL, level becomes nullable, a unique (tenant_id, id), an FK."""
    d = json.loads(V2.read_text())
    d["tables"]["docs"]["columns"]["archived"]["nullable"] = False
    d["tables"]["docs"]["columns"]["level"]["nullable"] = True
    d["tables"]["docs"]["unique"] = [["tenant_id", "id"]]
    d["rules"].append({"id": "R15", "kind": "inclusion", "name": "owner_is_admin", "table": "docs",
                       "columns": ["owner_id", "tenant_id"], "relation": "admin_of", "on_delete": "restrict"})
    return make(d)


def v_tight() -> Spec:
    """The example with a stricter level range and a NULL-sensitive check on archived."""
    d = base()
    for r in d["rules"]:
        if r["id"] == "R6":
            r["predicate"] = {"and": [{"cmp": [{"col": "level"}, ">=", {"const": 0}]},
                                      {"cmp": [{"col": "level"}, "<=", {"const": 2}]}]}
    d["rules"].append({"id": "R14", "kind": "check", "name": "archived_bit", "table": "docs",
                       "predicate": {"or": [{"cmp": [{"col": "archived"}, "=", {"const": 0}]},
                                            {"cmp": [{"col": "archived"}, "=", {"const": 1}]}]}})
    return make(d)


# ----------------------------------------------------------------------------- M3
ORG = EXAMPLE.parent / "org_tree.mls.json"


def org_base() -> dict:
    return json.loads(ORG.read_text())


def hierarchy_spec() -> Spec:
    """org_tree where authenticated may also insert and update groups (so the depth bound and the
    parent immutability are exercised by app roles)."""
    d = org_base()
    d["rules"] += [
        {"id": "G1", "kind": "policy", "name": "group_insert", "table": "groups", "role": "authenticated",
         "command": "insert", "mode": "permissive", "check": {"lit": True}},
        {"id": "G2", "kind": "policy", "name": "group_update", "table": "groups", "role": "authenticated",
         "command": "update", "mode": "permissive", "using": {"lit": True}},
        {"id": "G3", "kind": "policy", "name": "member_insert", "table": "group_members", "role": "authenticated",
         "command": "insert", "mode": "permissive", "check": {"lit": True}},
        {"id": "G4", "kind": "policy", "name": "member_delete", "table": "group_members", "role": "authenticated",
         "command": "delete", "mode": "permissive", "using": {"lit": True}}]
    d["properties"] = [p for p in d["properties"] if p["id"] != "P5"]
    return make(d)


def cardinality_spec() -> Spec:
    """org_tree with a tight limit (2 files per tenant) and file deletes."""
    d = org_base()
    for r in d["rules"]:
        if r["id"] == "R5":
            r["max"] = 2
    d["rules"].append({"id": "F1", "kind": "policy", "name": "file_delete", "table": "files",
                       "role": "authenticated", "command": "delete", "mode": "permissive",
                       "using": {"in_group": {"hierarchy": "org", "key": {"ctx": "uid"}, "value": {"col": "group_id"}}}})
    d["properties"] = [p for p in d["properties"] if p["id"] != "P5"]
    return make(d)


def org_base_spec() -> Spec:
    return make(org_base())


def org_flat_spec() -> Spec:
    """org_tree without the hierarchy and the cardinality limit: direct membership only, every
    group a root (so a migration to org_tree finds consistent depths)."""
    d = org_base()
    del d["hierarchies"]
    for r in d["rules"]:
        for k in ("using", "check"):
            if "in_group" in r.get(k, {}):
                ig = r[k]["in_group"]
                r[k] = {"member": {"relation": "group_member", "key": ig["key"], "value": ig["value"]}}
    d["rules"] = [r for r in d["rules"] if r["kind"] != "cardinality"]
    d["rules"].append({"id": "X1", "kind": "check", "name": "groups_are_roots", "table": "groups",
                       "predicate": {"and": [{"is_null": {"col": "parent_id"}},
                                             {"cmp": [{"col": "depth"}, "=", {"const": 0}]}]}})
    d["properties"] = [p for p in d["properties"] if p["id"] in ("P4", "P5")]
    return make(d)


# ----------------------------------------------------------------------------- M5 (brownfield, D45)
def adopted_spec(assume: list[dict] | None = None) -> Spec:
    """The example with `docs` adopted (the harness creates it as the pre-existing table) and the
    optional hardening assumptions."""
    d = base()
    d["tables"]["docs"]["mode"] = "adopted"
    if assume:
        d["tables"]["docs"]["assume"] = assume
    return make(d)


def observed_spec() -> Spec:
    """The example plus an observed table nothing names."""
    d = base()
    d["tables"]["audit"] = {"columns": {"id": {"type": "int", "nullable": False}}, "mode": "observed",
                            "tags": ["legacy"]}
    return make(d)


def composed_spec() -> Spec:
    """The team_tasks example (D56): in_project (one hop, member_of . projects) and in_task (two,
    . tasks), without R1, so that projects are readable only through the hop's visibility policy,
    and with an update policy on tasks (filtered updates, a nested WITH CHECK).  The mutation drops
    the hops' visibility policies: the nested subqueries read no project as the role and every
    task and comment disappears."""
    d = json.loads((EXAMPLE.parent / "team_tasks.mls.json").read_text())
    d["rules"] = [r for r in d["rules"] if r["id"] != "R1"]
    member = {"member": {"relation": "in_project", "key": {"ctx": "uid"}, "value": {"col": "project_id"}}}
    d["rules"].append({"id": "R5", "kind": "policy", "name": "member_updates_tasks", "table": "tasks",
                       "role": "authenticated", "command": "update", "mode": "permissive",
                       "using": member, "check": member})
    return make(d)


def owner_spec() -> Spec:
    """D60: the composed fixture plus the owner of each project.  `owns = projects(owner_id, id)`
    is a relation because `id` is the primary key; the owner reads and edits the tasks of their
    projects (R6, R7) and reads the comments on them through `owns . tasks(project_id, id)` (R8),
    whether or not they are in the project's team.  The mutation makes every policy restrictive,
    so an owner outside the team reads nothing."""
    d = json.loads((EXAMPLE.parent / "team_tasks.mls.json").read_text())
    d["rules"] = [r for r in d["rules"] if r["id"] != "R1"]
    d["tables"]["projects"]["columns"]["owner_id"] = {"type": "uuid", "nullable": False}
    d["relations"]["owns"] = {"table": "projects", "key": "owner_id", "value": "id"}
    d["relations"]["owns_task"] = {"of": "owns", "through": [{"table": "tasks", "key": "project_id", "value": "id"}]}
    owner = {"member": {"relation": "owns", "key": {"ctx": "uid"}, "value": {"col": "project_id"}}}
    d["rules"] += [
        {"id": "R6", "kind": "policy", "name": "owner_reads_tasks", "table": "tasks", "role": "authenticated",
         "command": "select", "mode": "permissive", "using": owner},
        {"id": "R7", "kind": "policy", "name": "owner_edits_tasks", "table": "tasks", "role": "authenticated",
         "command": "update", "mode": "permissive", "using": owner, "check": owner},
        {"id": "R8", "kind": "policy", "name": "owner_reads_comments", "table": "comments", "role": "authenticated",
         "command": "select", "mode": "permissive",
         "using": {"member": {"relation": "owns_task", "key": {"ctx": "uid"}, "value": {"col": "task_id"}}}},
    ]
    d["properties"] = [p for p in d["properties"] if p["template"] != "tenant_isolation"] + [
        {"id": "P6", "template": "must_allow", "table": "tasks", "role": "authenticated", "command": "select",
         "when": owner, "description": "the owner of a project reads its tasks"},
        {"id": "P7", "template": "must_allow", "table": "comments", "role": "authenticated", "command": "select",
         "when": {"member": {"relation": "owns_task", "key": {"ctx": "uid"}, "value": {"col": "task_id"}}},
         "description": "the owner of a project reads the comments on its tasks"},
    ]
    return make(d)


def condition_spec() -> Spec:
    """D61: the composed fixture with a nullable `team_members.role`.  `lead_of` keeps the rows
    whose role is 'a' or 'b' (an `in`, expanded inside the relations; a NULL role is UNKNOWN, so
    not a lead): a lead edits their
    teams' projects (R6, R7) and deletes the tasks of those projects through `lead_of .
    projects(team_id, id)` (R8).  The mutation drops the condition from the subquery, so every
    member, NULL role included, edits and deletes."""
    d = json.loads((EXAMPLE.parent / "team_tasks.mls.json").read_text())
    d["tables"]["team_members"]["columns"]["role"] = {"type": "text", "nullable": True}
    d["relations"]["lead_of"] = {"table": "team_members", "key": "user_id", "value": "team_id",
                                 "where": {"in": [{"col": "role"}, [{"const": "a"}, {"const": "b"}]]}}
    d["relations"]["leads_project"] = {"of": "lead_of",
                                       "through": [{"table": "projects", "key": "team_id", "value": "id"}]}
    lead = {"member": {"relation": "lead_of", "key": {"ctx": "uid"}, "value": {"col": "team_id"}}}
    leads = {"member": {"relation": "leads_project", "key": {"ctx": "uid"}, "value": {"col": "project_id"}}}
    d["rules"] += [
        {"id": "R6", "kind": "policy", "name": "lead_updates_projects", "table": "projects", "role": "authenticated",
         "command": "update", "mode": "permissive", "using": lead, "check": lead},
        {"id": "R7", "kind": "policy", "name": "lead_deletes_projects", "table": "projects", "role": "authenticated",
         "command": "delete", "mode": "permissive", "using": lead},
        {"id": "R8", "kind": "policy", "name": "lead_deletes_tasks", "table": "tasks", "role": "authenticated",
         "command": "delete", "mode": "permissive", "using": leads},
    ]
    d["properties"] += [
        {"id": "P6", "template": "must_allow", "table": "tasks", "role": "authenticated", "command": "delete",
         "when": leads, "description": "a lead deletes every task of their teams' projects"},
    ]
    return make(d)


def lookup_spec() -> Spec:
    """D67: team_tasks where the membership table's own policies read its relations, whose plain
    subqueries Postgres would refuse as infinite recursion: a member reads every membership of
    their teams (R5, `member_of` on `team_members`), a lead adds and removes members of their
    teams (R6, R7, `lead_of`, with a condition on a nullable `role`), and projects are readable
    only through `in_project`, whose hop runs through `projects` itself (R8, a composed helper).
    Those lookups call `mls_lookup_<relation>`; the others keep their subqueries.  The mutation
    drops the helpers' key: every user reads every membership and every project."""
    d = json.loads((EXAMPLE.parent / "team_tasks.mls.json").read_text())
    d["rules"] = [r for r in d["rules"] if r["id"] != "R1"]
    d["tables"]["team_members"]["columns"]["role"] = {"type": "text", "nullable": True}
    d["relations"]["lead_of"] = {"table": "team_members", "key": "user_id", "value": "team_id",
                                 "where": {"cmp": [{"col": "role"}, "=", {"const": "lead"}]}}
    member = {"member": {"relation": "member_of", "key": {"ctx": "uid"}, "value": {"col": "team_id"}}}
    lead = {"member": {"relation": "lead_of", "key": {"ctx": "uid"}, "value": {"col": "team_id"}}}
    d["rules"] += [
        {"id": "R5", "kind": "policy", "name": "member_reads_memberships", "table": "team_members",
         "role": "authenticated", "command": "select", "mode": "permissive", "using": member},
        {"id": "R6", "kind": "policy", "name": "lead_adds_members", "table": "team_members",
         "role": "authenticated", "command": "insert", "mode": "permissive", "check": lead},
        {"id": "R7", "kind": "policy", "name": "lead_removes_members", "table": "team_members",
         "role": "authenticated", "command": "delete", "mode": "permissive", "using": lead},
        {"id": "R8", "kind": "policy", "name": "member_reads_projects", "table": "projects",
         "role": "authenticated", "command": "select", "mode": "permissive",
         "using": {"member": {"relation": "in_project", "key": {"ctx": "uid"}, "value": {"col": "id"}}}},
    ]
    d["properties"] += [
        {"id": "P6", "template": "must_allow", "table": "team_members", "role": "authenticated",
         "command": "select", "when": member, "description": "a member reads every membership of their teams"},
        {"id": "P7", "template": "must_allow", "table": "team_members", "role": "authenticated",
         "command": "delete", "when": lead, "description": "a lead removes any member of their teams"},
    ]
    return make(d)


# ----------------------------------------------------------------------------- case-sensitive names (D58)
MIXED_CASE = {"memberships": "Membership", "admins": "Admin", "docs": "Doc", "user_id": "userId",
              "tenant_id": "tenantId", "owner_id": "ownerId", "archived": "isArchived",
              "authenticated": "DocReader"}


def mixed_case_spec() -> Spec:
    """The pg_roles example with the names a Prisma schema has: PascalCase tables, camelCase
    columns and a mixed-case role, all quoted wherever MLS emits them.  The tables also have
    lower-case `tenantid`, `isarchived` and `userid` holding other data, so a reference emitted unquoted
    (Postgres folds `tenantId` to `tenantid`) reads the wrong column rather than failing (mutation
    `IDENT_UNQUOTED`)."""
    import re

    from mls.spec.syntax import parse

    text = PG_ROLES.with_suffix("").read_text()
    for old, new in MIXED_CASE.items():
        text = re.sub(rf"\b{old}\b", new, text)
    text = text.replace("  tenantId int not null,\n  ownerId", "  tenantId int not null,\n  tenantid int,\n  ownerId", 1)
    text = text.replace("  isArchived int;", "  isArchived int,\n  isarchived int;", 1)
    text = text.replace("  userId text not null,\n", "  userId text not null,\n  userid text,\n")  # Membership, Admin
    assert "tenantid int," in text and "isarchived int;" in text and text.count("userid text,") == 2
    return check_spec(parse(text))


ENUM_LABELS = ["Draft", "In review", "it's", "Published"]


def enum_spec(labels: list[str] | None = None) -> Spec:
    return make(enum_dict(labels))


def enum_dict(labels: list[str] | None = None) -> dict:
    """D59: `docs` adopted with a column of a mixed-case enum type whose labels need quoting.
    Members read the documents in the listed states whatever their clearance (an `in` the
    mutation shortens, hiding 'Published' ones), nobody reads one in review, and a draft stays at
    level 0 or 1 (a check rule).  P11 holds only because the column holds nothing but its labels:
    `status <> 'In review'` leaves exactly the states R10 lists.  R2 and P8 go as in `in_spec`; P5
    and P7 go because R11 hides documents in review from their writers and their members."""
    d = base()
    d["enums"] = {"DocStatus": labels or ENUM_LABELS}
    d["tables"]["docs"]["mode"] = "adopted"
    d["tables"]["docs"]["columns"]["status"] = {"type": "enum", "enum": "DocStatus", "nullable": True}
    d["rules"] = [r for r in d["rules"] if r["id"] != "R2"]
    d["properties"] = [p for p in d["properties"] if p["id"] not in ("P5", "P7", "P8")]
    st = {"col": "status"}
    d["rules"] += [
        {"id": "R10", "kind": "policy", "name": "member_read_states", "table": "docs", "role": "authenticated",
         "command": "select", "mode": "permissive",
         "using": {"and": [{"member": {"relation": "member_of", "key": {"ctx": "uid"}, "value": {"col": "tenant_id"}}},
                           {"in": [st, [{"const": "Draft"}, {"const": "it's"}, {"const": "Published"}]]}]}},
        {"id": "R11", "kind": "policy", "name": "hide_in_review", "table": "docs", "role": "authenticated",
         "command": "select", "mode": "restrictive",
         "using": {"or": [{"is_null": st}, {"cmp": [st, "<>", {"const": "In review"}]}]}},
        {"id": "C1", "kind": "check", "name": "drafts_low", "table": "docs",
         "predicate": {"or": [{"is_null": st}, {"cmp": [st, "<>", {"const": "Draft"}]},
                              {"cmp": [{"col": "level"}, "<=", {"const": 1}]}]}},
    ]
    d["properties"].append(
        {"id": "P11", "template": "must_allow", "table": "docs", "role": "authenticated", "command": "select",
         "when": {"and": [{"member": {"relation": "member_of", "key": {"ctx": "uid"},
                                           "value": {"col": "tenant_id"}}},
                               {"not": {"is_null": st}}, {"cmp": [st, "<>", {"const": "In review"}]},
                               {"not": {"is_null": {"col": "archived"}}},
                               {"cmp": [{"col": "archived"}, "<>", {"const": 1}]}]},
         "description": "a member reads every unarchived document of their tenant that is not in review"})
    return d
