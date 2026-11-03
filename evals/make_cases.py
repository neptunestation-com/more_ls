"""Seed corpus generator (D6).  Each case below is a real-world access-control pattern written as
requirements + reference spec + intended verdicts; the solver must confirm every intended verdict
or the generator fails.  Run `uv run python evals/make_cases.py` to (re)write evals/cases/*.json and
evals/cases/specs/*.mls.json.  The four hand-written tenant_docs cases are kept as they are.

Patterns: multi-tenant documents with clearance (tenant_docs), group hierarchies (org_tree), a
help desk (customers / agents / ticket status), projects with assignees, time-bounded invites,
pg_roles identity, a marketplace (public listings, private orders), a classroom (enrolled /
teaching, publication), an append-only audit log, soft deletion, and column rules (salaries).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mls.props.runner import run_all  # noqa: E402
from mls.spec import check_spec  # noqa: E402
from mls.spec.model import Spec  # noqa: E402

OUT = ROOT / "evals" / "cases"


# ----------------------------------------------------------------------------- predicate helpers
def col(c): return {"col": c}
def ctx(c): return {"ctx": c}
def const(v): return {"const": v}
def old(c): return {"old": c}
def new(c): return {"new": c}
def cmp(a, op, b): return {"cmp": [a, op, b]}
def eq(a, b): return cmp(a, "=", b)
def member(rel, value, key="uid"): return {"member": {"relation": rel, "key": ctx(key), "value": col(value)}}
def in_group(h, value, key="uid"): return {"in_group": {"hierarchy": h, "key": ctx(key), "value": col(value)}}
def and_(*ps): return {"and": list(ps)}
def or_(*ps): return {"or": list(ps)}
def not_(p): return {"not": p}
def is_null(t): return {"is_null": t}
def now_between(from_=None, until=None): return {"now_between": {"from": from_, "until": until}}


def policy(id, name, table, command, mode="permissive", using=None, check=None, role="authenticated", req=None):
    r = {"id": id, "kind": "policy", "name": name, "table": table, "role": role, "command": command, "mode": mode}
    if using is not None:
        r["using"] = using
    if check is not None:
        r["check"] = check
    if req:
        r["requirement"] = req
    return r


def check(id, name, table, predicate, req=None):
    return {"id": id, "kind": "check", "name": name, "table": table, "predicate": predicate, **({"requirement": req} if req else {})}


def inclusion(id, name, table, columns, relation, req=None):
    return {"id": id, "kind": "inclusion", "name": name, "table": table, "columns": columns, "relation": relation,
            "on_delete": "restrict", **({"requirement": req} if req else {})}


def transition(id, name, table, predicate, req=None):
    return {"id": id, "kind": "transition", "name": name, "table": table, "predicate": predicate, **({"requirement": req} if req else {})}


def column_rule(id, table, role, columns, access):
    return {"id": id, "kind": "column_rule", "name": f"{role}_{access}_{table}", "table": table, "role": role,
            "columns": columns, "access": access}


def immutable(id, name, table, c):
    return transition(id, name, table, or_(and_(is_null(old(c)), is_null(new(c))), eq(new(c), old(c))))


# ----------------------------------------------------------------------------- property helpers
def isolation(id, desc, table, column, relation, commands):
    return {"id": id, "template": "tenant_isolation", "description": desc, "table": table, "column": column,
            "relation": relation, "key": ctx("uid"), "commands": commands}


def col_immutable(id, desc, table, column):
    return {"id": id, "template": "column_immutable", "description": desc, "table": table, "column": column}


def monotone(id, desc, relation):
    return {"id": id, "template": "monotone_in", "description": desc, "relation": relation}


def write_subset_read(id, desc, table):
    return {"id": id, "template": "write_subset_read", "description": desc, "table": table}


def least_privilege(id, desc, allowed):
    return {"id": id, "template": "least_privilege", "description": desc, "allowed": allowed}


def invariants(id, desc, *refs):
    return {"id": id, "template": "invariant_preserved", "description": desc,
            "invariants": [{"rule": r} if isinstance(r, str) else {"table": r[0], "predicate": r[1]} for r in refs]}


def custom(id, desc, table, role, command, goal, which="new"):
    return {"id": id, "template": "custom", "description": desc, "table": table, "role": role, "command": command,
            "which": which, "goal": goal}


def must_allow(id, desc, table, role, command, when):
    """The lower bound (D40): every row meeting `when` is admitted to the command, all layers together."""
    return {"id": id, "template": "must_allow", "description": desc, "table": table, "role": role, "command": command,
            "when": when}


def member_old(rel, value, key="uid"):
    return {"member": {"relation": rel, "key": ctx(key), "value": old(value)}}


def same(*cols):
    """new = old for non-nullable columns (an update that keeps them)."""
    return [eq(new(c), old(c)) for c in cols]


# ----------------------------------------------------------------------------- schema helpers
SUPABASE = {"adapter": "supabase", "context": {"uid": {"source": "auth.uid", "type": "uuid"}}}


def T(columns, pk, unique=None):
    t = {"columns": {c: {"type": ty, "nullable": nul} for c, (ty, nul) in columns.items()}, "primary_key": pk}
    if unique:
        t["unique"] = unique
    return t


def pairs(user="user_id", other="tenant_id", other_type="int"):
    return T({user: ("uuid", False), other: (other_type, False)}, [user, other])


def spec(comment, tables, relations, rules, properties, roles=("authenticated",), identity=SUPABASE, **extra):
    return {"$comment": comment, "mls_version": "0.1", "identity": identity, "roles": list(roles), "tables": tables,
            "relations": relations, **extra, "rules": rules, "properties": properties}


CASES: list[dict] = []


def case(id, requirements, expected_properties, spec_data, verdicts, loop=None):
    CASES.append({"id": id, "requirements": requirements, "expected_properties": expected_properties,
                  "spec": spec_data, "verdicts": verdicts, "loop": loop})


# ============================================================================= F1 tenant_docs variants
TD = json.loads((ROOT / "examples" / "tenant_docs.mls.json").read_text())
TD_REQ = json.loads((OUT / "good_tenant_docs.json").read_text())["requirements"]
TD_PROPS = json.loads((OUT / "good_tenant_docs.json").read_text())["expected_properties"]
TD_OK = {f"P{i}": "PROVED" for i in range(1, 11)}


def td(rules=None, props=None, comment=None):
    d = json.loads(json.dumps(TD))
    if comment:
        d["$comment"] = comment
    if rules is not None:
        d["rules"] = rules
    if props is not None:
        d["properties"] = props
    return d


admin = member("admin_of", "tenant_id")
r = td()["rules"]
case("td_admin_delete",
     TD_REQ + " Tenant admins may also delete documents of their tenants.",
     TD_PROPS[:5] + ["The application role has no privilege beyond select/insert/update/delete on docs and reading memberships and admins."] + TD_PROPS[6:],
     td(rules=r + [policy("R10", "admin_delete", "docs", "delete", using=admin, req="admins delete")],
        props=TD["properties"][:5] + [{**TD["properties"][5], "allowed": {"authenticated": {"docs": ["select", "insert", "update", "delete"], "memberships": ["select"], "admins": ["select"]}}}] + TD["properties"][6:],
        comment="tenant_docs plus admin delete"),
     TD_OK)

r = td()["rules"]
for x in r:
    if x["id"] == "R3":
        x["check"] = member("member_of", "tenant_id")  # no clearance bound on writes
case("td_clearance_write_gap",
     TD_REQ.replace("Members may create documents in their tenants at a level within their clearance, and may update any document of their tenants as long as the updated document's level is within their clearance.",
                    "Members may create documents in their tenants at any level, and may update any document of their "
                    "tenants as long as the updated document's level is within their clearance."),
     TD_PROPS,
     td(rules=r, comment="tenant_docs where inserts are not bounded by clearance: write ⊆ read fails"),
     {**TD_OK, "P5": "REFUTED"})

case("td_tenant_move",
     TD_REQ.replace(" A document never changes tenant.", ""),
     TD_PROPS,
     td(rules=[x for x in TD["rules"] if x["id"] != "R9"], comment="tenant_docs without the tenant-immutability transition"),
     {**TD_OK, "P2": "REFUTED"})

# ============================================================================= F2 org_tree variants
ORG = json.loads((ROOT / "examples" / "org_tree.mls.json").read_text())
ORG_REQ = ("Users belong to groups; groups form a tree at most 3 levels deep (a group has at most one parent, "
           "which never changes). A user may read, create and update the files of the groups they belong to "
           "and of every group below those. Users may see which groups they belong to (directly or below). "
           "A tenant holds at most 5 files. File sizes are never negative.")
ORG_PROPS = ["A user only reads files of groups they belong to or that lie below them.",
             "Leaving a group never grants access.",
             "A tenant never holds more than 5 files; sizes stay non-negative.",
             "Whatever a user writes, they can read back.",
             "The application role may only select/insert/update files and select groups.",
             "A user can read the files of the groups they belong to.", "A user can add a file to a group they belong to."]
case("org_good", ORG_REQ, ORG_PROPS, ORG, {f"P{i}": "PROVED" for i in range(1, 8)})

d = json.loads(json.dumps(ORG))
d["$comment"] = "org_tree where the read policy says in_group but the property asks for direct membership"
d["properties"][0] = custom("P1", "a user only reads files of groups they are a direct member of", "files",
                            "authenticated", "select", member("group_member", "group_id"))
case("org_subtree_leak",  # the requirement grants the subtree; the reviewer's property asks for direct membership
     ORG_REQ,
     ["A user only reads files of groups they are a direct member of."] + ORG_PROPS[1:],
     d, {"P1": "REFUTED", "P2": "PROVED", "P3": "PROVED", "P4": "PROVED", "P5": "PROVED", "P6": "PROVED", "P7": "PROVED"})

d = json.loads(json.dumps(ORG))
d["$comment"] = "org_tree with direct membership only (no subtree access)"
for x in d["rules"]:
    for k in ("using", "check"):
        if k in x and "in_group" in x[k]:
            x[k] = member("group_member", x[k]["in_group"]["value"]["col"])
d["properties"][0] = custom("P1", "a user only reads files of groups they are a direct member of", "files",
                            "authenticated", "select", member("group_member", "group_id"))
# direct membership compiles to a membership subquery of the app role (D21): group_members becomes readable
d["properties"][4]["allowed"]["authenticated"]["group_members"] = ["select"]
case("org_direct_only",
     ORG_REQ.replace("of the groups they belong to and of every group below those", "of exactly the groups they belong to")
     .replace("Users may see which groups they belong to (directly or below).", "Users may see the groups they belong to."),
     ["A user only reads files of groups they are a direct member of."] + ORG_PROPS[1:],
     d, {f"P{i}": "PROVED" for i in range(1, 8)})

# ============================================================================= F3 help desk
HD_TABLES = {
    "orgs": T({"id": ("int", False)}, ["id"]),
    "agents": pairs(other="org_id"),
    "tickets": T({"id": ("int", False), "org_id": ("int", False), "customer_id": ("uuid", False),
                  "status": ("text", False), "assignee_id": ("uuid", True)}, ["id"]),
}
HD_REL = {"agent_of": {"table": "agents", "key": "user_id", "value": "org_id"}}
agent = member("agent_of", "org_id")
mine = eq(col("customer_id"), ctx("uid"))
HD_RULES = [
    policy("R1", "customer_read", "tickets", "select", using=mine, req="customers see their own tickets"),
    policy("R2", "customer_open", "tickets", "insert", check=and_(mine, eq(col("status"), const("open"))), req="customers open tickets"),
    policy("R3", "agent_read", "tickets", "select", using=agent, req="agents see their org's tickets"),
    policy("R4", "agent_update", "tickets", "update", using=agent, check=agent, req="agents work their org's tickets"),
    policy("R5", "closed_frozen", "tickets", "update", mode="restrictive", using=cmp(col("status"), "<>", const("closed")),
           check={"lit": True}, req="closed tickets are immutable"),  # the new row may be the closing one
    check("R6", "status_values", "tickets", or_(eq(col("status"), const("open")), eq(col("status"), const("closed"))), req="statuses"),
    immutable("R7", "customer_immutable", "tickets", "customer_id"),
    immutable("R8", "org_immutable", "tickets", "org_id"),
]
HD_PROPS = [
    custom("P1", "a ticket is visible only to its customer and to agents of its org", "tickets", "authenticated", "select",
           or_(mine, agent)),
    col_immutable("P2", "a ticket's customer never changes", "tickets", "customer_id"),
    invariants("P3", "a ticket's status is open or closed", "R6"),
    custom("P4", "closed tickets are never modified", "tickets", "authenticated", "update",
           cmp(col("status"), "<>", const("closed")), which="old"),
    custom("P5", "an agent never moves a ticket to another org", "tickets", "authenticated", "update", agent),
    monotone("P6", "removing an agent from an org never grants access", "agent_of"),
    write_subset_read("P7", "whatever is written can be read back", "tickets"),
    least_privilege("P8", "only select/insert/update on tickets and select on agents",
                    {"authenticated": {"tickets": ["select", "insert", "update"], "agents": ["select"]}}),
    must_allow("P9", "a customer reads their own tickets", "tickets", "authenticated", "select", mine),
    must_allow("P10", "a customer opens a ticket in their own name", "tickets", "authenticated", "insert",
               and_(mine, eq(col("status"), const("open")))),
    must_allow("P11", "an agent reads the tickets of their organizations", "tickets", "authenticated", "select", agent),
    must_allow("P12", "an agent of the ticket's organization closes an open ticket", "tickets", "authenticated", "update",
               and_(member_old("agent_of", "org_id"), eq(old("status"), const("open")), eq(new("status"), const("closed")),
                    *same("id", "org_id", "customer_id"),
                    or_(and_(is_null(old("assignee_id")), is_null(new("assignee_id"))), eq(new("assignee_id"), old("assignee_id"))))),
]
HD_REQ = ("Customers open support tickets and see only their own tickets; a new ticket is 'open'. Agents belong to "
          "organizations (agents table) and may read and work on every ticket of their organizations, but cannot move "
          "a ticket to another organization. A ticket's status is 'open' or 'closed'; once closed, a ticket is never "
          "modified again. A ticket's customer never changes. Nobody deletes tickets.")
HD_ENG = ["A ticket is visible only to its customer and to agents of its organization.",
          "A ticket's customer never changes.", "A ticket's status is always open or closed.",
          "Closed tickets are never modified.", "An agent never moves a ticket to another organization.",
          "Removing an agent from an organization never grants access.",
          "Whatever is written can be read back.",
          "The application role may only select/insert/update tickets and select agents.",
          "A customer can read their own tickets.", "A customer can open a ticket in their own name.",
          "An agent can read the tickets of their organizations.",
          "An agent of the ticket's organization can close an open ticket."]
HD_OK = {f"P{i}": "PROVED" for i in range(1, 13)}
case("hd_good", HD_REQ, HD_ENG, spec("help desk: customers, agents, ticket status", HD_TABLES, HD_REL, HD_RULES, HD_PROPS), HD_OK)
case("hd_reopen", HD_REQ.replace(" once closed, a ticket is never modified again.", ""), HD_ENG,
     spec("help desk without the closed-ticket freeze", HD_TABLES, HD_REL, [x for x in HD_RULES if x["id"] != "R5"], HD_PROPS),
     {**HD_OK, "P4": "REFUTED"})
case("hd_cross_org_agent", HD_REQ.replace(", but cannot move a ticket to another organization", ", and may move a ticket to any organization"), HD_ENG,
     spec("help desk where agent updates are not checked on the new row", HD_TABLES, HD_REL,
          [x if x["id"] != "R4" else policy("R4", "agent_update", "tickets", "update", using=agent, check={"lit": True})
           for x in HD_RULES if x["id"] != "R8"], HD_PROPS),
     {**HD_OK, "P5": "REFUTED", "P7": "REFUTED"})

# ============================================================================= F4 projects and tasks
PJ_TABLES = {
    "projects": T({"id": ("int", False), "owner_id": ("uuid", False)}, ["id"]),
    "project_members": pairs(other="project_id"),
    "tasks": T({"id": ("int", False), "project_id": ("int", False), "assignee_id": ("uuid", True), "done": ("bool", False)}, ["id"]),
}
PJ_REL = {"member_of": {"table": "project_members", "key": "user_id", "value": "project_id"}}
pm = member("member_of", "project_id")
assignee = eq(col("assignee_id"), ctx("uid"))
PJ_RULES = [
    policy("R1", "member_read", "tasks", "select", using=pm, req="members see tasks of their projects"),
    policy("R2", "member_insert", "tasks", "insert", check=pm, req="members add tasks"),
    policy("R3", "assignee_update", "tasks", "update", using=and_(pm, assignee), check=pm, req="assignees update their tasks"),
    inclusion("R4", "assignee_is_member", "tasks", ["assignee_id", "project_id"], "member_of", req="assignees are members"),
]
PJ_PROPS = [
    isolation("P1", "tasks are only seen, added and changed by members of their project", "tasks", "project_id", "member_of",
              ["select", "insert", "update"]),
    custom("P2", "only the assignee updates a task", "tasks", "authenticated", "update", assignee, which="old"),
    monotone("P3", "leaving a project never grants access", "member_of"),
    invariants("P4", "an assignee is always a member of the task's project", "R4"),
    write_subset_read("P5", "whatever is written can be read back", "tasks"),
    least_privilege("P6", "only select/insert/update on tasks and select on project_members",
                    {"authenticated": {"tasks": ["select", "insert", "update"], "project_members": ["select"]}}),
    must_allow("P7", "a member reads the tasks of their projects", "tasks", "authenticated", "select", pm),
    must_allow("P8", "a member adds an unassigned task to their project", "tasks", "authenticated", "insert",
               and_(pm, is_null(col("assignee_id")))),
    must_allow("P9", "an assignee marks their task done", "tasks", "authenticated", "update",
               and_(member_old("member_of", "project_id"), eq(old("assignee_id"), ctx("uid")), eq(new("assignee_id"), old("assignee_id")),
                    *same("id", "project_id"), eq(new("done"), const(True)))),
]
PJ_REQ = ("Projects have members (project_members). Members see the tasks of their projects and may add tasks to them. "
          "A task may be assigned to a user, who must be a member of the task's project; only the assignee may update "
          "a task, and it stays in a project the assignee belongs to. Nobody deletes tasks.")
PJ_ENG = ["Tasks are only seen, added and changed by members of their project.", "Only the assignee updates a task.",
          "Leaving a project never grants access.", "An assignee is always a member of the task's project.",
          "Whatever is written can be read back.", "The application role may only select/insert/update tasks and select project_members.",
          "A member can read the tasks of their projects.", "A member can add an unassigned task to their project.",
          "An assignee can mark their task done."]
PJ_OK = {f"P{i}": "PROVED" for i in range(1, 10)}
case("pj_good", PJ_REQ, PJ_ENG, spec("projects, members, assignees", PJ_TABLES, PJ_REL, PJ_RULES, PJ_PROPS), PJ_OK)
case("pj_assignee_outsider",
     PJ_REQ.replace(", who must be a member of the task's project", ", who need not be a member of the project")
     .replace(", and it stays in a project the assignee belongs to", ", and may move it to any project"), PJ_ENG,
     spec("projects where the assignee need not be a member and updates only check the assignee", PJ_TABLES, PJ_REL,
          [policy("R1", "member_read", "tasks", "select", using=pm), policy("R2", "member_insert", "tasks", "insert", check=pm),
           policy("R3", "assignee_update", "tasks", "update", using=assignee, check=assignee)],
          [p for p in PJ_PROPS if p["id"] != "P4"]),
     {"P1": "REFUTED", "P2": "PROVED", "P3": "PROVED", "P5": "REFUTED", "P6": "PROVED", "P7": "PROVED", "P8": "PROVED", "P9": "PROVED"})
case("pj_owner_closes", PJ_REQ + " Additionally a project owner (owners are recorded in task_owners and must be members of "
                                  "the project) may delete tasks of their project.",
     PJ_ENG[:5] + ["The application role may only select/insert/update/delete tasks and select project_members and projects."]
     + PJ_ENG[6:] + ["A project owner can delete any task of their project."],
     spec("projects where owners delete tasks", {**PJ_TABLES, "task_owners": pairs(other="project_id")},
          {**PJ_REL, "owner_of": {"table": "task_owners", "key": "user_id", "value": "project_id"}},
          PJ_RULES + [policy("R5", "owner_delete", "tasks", "delete", using=member("owner_of", "project_id"), req="owners delete"),
                      inclusion("R6", "owners_are_members", "task_owners", ["user_id", "project_id"], "member_of")],
          [{**PJ_PROPS[0], "commands": ["select", "insert", "update", "delete"]}, *PJ_PROPS[1:3],
           invariants("P4", "assignees and owners are members", "R4", "R6"), PJ_PROPS[4],
           least_privilege("P6", "only select/insert/update/delete on tasks, select on members and owners",
                           {"authenticated": {"tasks": ["select", "insert", "update", "delete"], "project_members": ["select"],
                                              "task_owners": ["select"]}}),
           *PJ_PROPS[6:],
           must_allow("P10", "a project owner deletes any task of their project", "tasks", "authenticated", "delete",
                      member("owner_of", "project_id"))]),
     {**PJ_OK, "P10": "PROVED"})

# ============================================================================= F5 time-bounded invites
INV_TABLES = {"memberships": pairs(),
              "invites": T({"id": ("int", False), "tenant_id": ("int", False), "inviter_id": ("uuid", False),
                            "expires_at": ("timestamp", True)}, ["id"])}
INV_REL = {"member_of": {"table": "memberships", "key": "user_id", "value": "tenant_id"}}
mem = member("member_of", "tenant_id")
live = now_between(None, col("expires_at"))
INV_RULES = [
    policy("R1", "member_read_live", "invites", "select", using=and_(mem, live), req="members see unexpired invites"),
    policy("R2", "member_invite", "invites", "insert", check=and_(mem, eq(col("inviter_id"), ctx("uid"))), req="members invite"),
    policy("R3", "inviter_revoke", "invites", "delete", using=and_(mem, eq(col("inviter_id"), ctx("uid"))), req="inviters revoke"),
]
INV_PROPS = [
    isolation("P1", "invites are only seen, created and revoked within the member's tenants", "invites", "tenant_id", "member_of",
              ["select", "insert", "delete"]),
    custom("P2", "an expired invite is never visible", "invites", "authenticated", "select", live),
    custom("P3", "only the inviter revokes an invite", "invites", "authenticated", "delete", eq(col("inviter_id"), ctx("uid")), which="old"),
    monotone("P4", "leaving a tenant never grants access", "member_of"),
    least_privilege("P5", "only select/insert/delete on invites and select on memberships",
                    {"authenticated": {"invites": ["select", "insert", "delete"], "memberships": ["select"]}}),
    must_allow("P6", "a member reads the unexpired invites of their tenants", "invites", "authenticated", "select", and_(mem, live)),
    must_allow("P7", "a member creates an invite in their own name", "invites", "authenticated", "insert",
               and_(mem, eq(col("inviter_id"), ctx("uid")))),
    must_allow("P8", "the inviter, while a member, revokes their invite", "invites", "authenticated", "delete",
               and_(mem, eq(col("inviter_id"), ctx("uid")))),
]
INV_REQ = ("Members of a tenant may invite others: an invite belongs to the tenant, records who created it and may carry "
           "an expiry time. Members see the invites of their tenants, but never an expired one. Only the inviter, while "
           "still a member of the tenant, may revoke (delete) an invite. Invites are never edited.")
INV_ENG = ["Invites are only seen, created and revoked within the member's tenants.", "An expired invite is never visible.",
           "Only the inviter revokes an invite.", "Leaving a tenant never grants access.",
           "The application role may only select/insert/delete invites and select memberships.",
           "A member can read the unexpired invites of their tenants.", "A member can create an invite in their own name.",
           "The inviter, while a member, can revoke their invite."]
INV_OK = {f"P{i}": "PROVED" for i in range(1, 9)}
case("inv_good", INV_REQ, INV_ENG, spec("time-bounded invites", INV_TABLES, INV_REL, INV_RULES, INV_PROPS), INV_OK)
case("inv_expired_visible", INV_REQ.replace(", but never an expired one", ""), INV_ENG,
     spec("invites visible after expiry", INV_TABLES, INV_REL,
          [policy("R1", "member_read", "invites", "select", using=mem)] + INV_RULES[1:], INV_PROPS),
     {**INV_OK, "P2": "REFUTED"})

# ============================================================================= F6 pg_roles identity
PGR = json.loads((ROOT / "examples" / "pg_roles_docs.mls.json").read_text())
PGR_REQ = ("Users belong to tenants (memberships) and connect with their own database role (identity is current_user; "
           "there are no clearance levels). A user may read, create and update the documents of the tenants they belong "
           "to; tenant admins may read every document of their tenants. A document is archived unless its archived "
           "column holds a value other than 1 (a missing value counts as archived); archived documents are hidden from "
           "everyone: nobody can read, create, change or delete them. Levels are 0..3. A document's owner, if set, must "
           "be a member of the document's tenant; admins must be members. A document never changes tenant.")
PGR_ENG = TD_PROPS[:6] + ["A member can read any unarchived document of their tenants.",
                         "A member can create an unarchived document in their tenant.",
                         "A member can update an unarchived document of their tenant, keeping its tenant."]
case("pgr_good", PGR_REQ, PGR_ENG, PGR, {f"P{i}": "PROVED" for i in range(1, 10)})

# ============================================================================= F7 marketplace
MK_TABLES = {
    "listings": T({"id": ("int", False), "seller_id": ("uuid", False), "status": ("text", False), "price": ("int", False)}, ["id"]),
    "orders": T({"id": ("int", False), "listing_id": ("int", False), "buyer_id": ("uuid", False)}, ["id"]),
}
seller = eq(col("seller_id"), ctx("uid"))
buyer = eq(col("buyer_id"), ctx("uid"))
active = eq(col("status"), const("active"))
MK_RULES = [
    policy("R1", "public_active", "listings", "select", using=active, req="active listings are public"),
    policy("R2", "seller_read", "listings", "select", using=seller, req="sellers see their listings"),
    policy("R3", "seller_insert", "listings", "insert", check=seller, req="sellers create listings"),
    policy("R4", "seller_update", "listings", "update", using=seller, check=seller, req="sellers edit their listings"),
    immutable("R5", "seller_immutable", "listings", "seller_id"),
    check("R6", "price_non_negative", "listings", cmp(col("price"), ">=", const(0)), req="prices"),
    policy("R7", "buyer_read", "orders", "select", using=buyer, req="buyers see their orders"),
    policy("R8", "buyer_order", "orders", "insert", check=buyer, req="buyers order"),
]
price_ok = cmp(col("price"), ">=", const(0))
MK_PROPS = [
    custom("P1", "a listing is visible only when active or to its seller", "listings", "authenticated", "select", or_(active, seller)),
    col_immutable("P2", "a listing's seller never changes", "listings", "seller_id"),
    invariants("P3", "prices are never negative", ("listings", price_ok)),
    custom("P4", "an order is visible only to its buyer", "orders", "authenticated", "select", buyer),
    custom("P5", "only the seller changes a listing", "listings", "authenticated", "update", seller, which="old"),
    write_subset_read("P6", "whatever is written can be read back", "listings"),
    least_privilege("P7", "only select/insert/update on listings and select/insert on orders",
                    {"authenticated": {"listings": ["select", "insert", "update"], "orders": ["select", "insert"]}}),
    must_allow("P8", "every signed-in user reads active listings", "listings", "authenticated", "select", active),
    must_allow("P9", "a seller reads their own listings", "listings", "authenticated", "select", seller),
    must_allow("P10", "a seller creates a listing with a non-negative price", "listings", "authenticated", "insert", and_(seller, price_ok)),
    must_allow("P11", "a seller reprices their listing", "listings", "authenticated", "update",
               and_(eq(old("seller_id"), ctx("uid")), *same("id", "seller_id", "status"), cmp(new("price"), ">=", const(0)))),
    must_allow("P12", "a buyer places an order in their own name", "orders", "authenticated", "insert", buyer),
    must_allow("P13", "a buyer reads their own orders", "orders", "authenticated", "select", buyer),
]
MK_REQ = ("Sellers create listings with a status and a price; a listing's seller never changes and prices are never "
          "negative. Every signed-in user sees active listings; sellers also see and edit their own listings whatever "
          "the status. Buyers place orders on listings and see only their own orders. Nothing is deleted.")
MK_ENG = ["A listing is visible only when active or to its seller.", "A listing's seller never changes.",
          "Prices are never negative.", "An order is visible only to its buyer.", "Only the seller changes a listing.",
          "Whatever is written can be read back.", "The application role may only select/insert/update listings and select/insert orders.",
          "Every signed-in user can read active listings.", "A seller can read their own listings.",
          "A seller can create a listing with a non-negative price.", "A seller can reprice their listing.",
          "A buyer can place an order in their own name.", "A buyer can read their own orders."]
MK_OK = {f"P{i}": "PROVED" for i in range(1, 14)}
case("mk_good", MK_REQ, MK_ENG, spec("marketplace", MK_TABLES, {}, MK_RULES, MK_PROPS), MK_OK)
case("mk_price_unchecked", MK_REQ.replace(" and prices are never negative", ""), MK_ENG,
     spec("marketplace without the price check", MK_TABLES, {}, [x for x in MK_RULES if x["id"] != "R6"], MK_PROPS),
     {**MK_OK, "P3": "REFUTED"})
# without the transition the seller is still immutable: the update policy pins seller_id to the user on
# both images (the solver proves it); only a hand-over rule breaks P2 (and then write ⊆ read)
case("mk_seller_change", MK_REQ.replace("a listing's seller never changes and ", "") + " A seller may hand a listing over to another seller.", MK_ENG,
     spec("marketplace where sellers hand listings over", MK_TABLES, {},
          [x if x["id"] != "R4" else policy("R4", "seller_update", "listings", "update", using=seller, check={"lit": True})
           for x in MK_RULES if x["id"] != "R5"], MK_PROPS),
     {**MK_OK, "P2": "REFUTED", "P6": "REFUTED"})

# ============================================================================= F8 classroom
CL_TABLES = {
    "courses": T({"id": ("int", False)}, ["id"]),
    "enrollments": pairs(other="course_id"),
    "teaching": pairs(other="course_id"),
    "assignments": T({"id": ("int", False), "course_id": ("int", False), "published": ("bool", False)}, ["id"]),
    "submissions": T({"id": ("int", False), "assignment_id": ("int", False), "course_id": ("int", False),
                      "student_id": ("uuid", False)}, ["id"]),
}
CL_REL = {"enrolled": {"table": "enrollments", "key": "user_id", "value": "course_id"},
          "teaches": {"table": "teaching", "key": "user_id", "value": "course_id"}}
enrolled = member("enrolled", "course_id")
teaches = member("teaches", "course_id")
published = eq(col("published"), const(True))
student = eq(col("student_id"), ctx("uid"))
CL_RULES = [
    policy("R1", "student_read_published", "assignments", "select", using=and_(enrolled, published), req="students see published assignments"),
    policy("R2", "teacher_read", "assignments", "select", using=teaches, req="teachers see their assignments"),
    policy("R3", "teacher_insert", "assignments", "insert", check=teaches, req="teachers create assignments"),
    policy("R4", "teacher_update", "assignments", "update", using=teaches, check=teaches, req="teachers edit assignments"),
    policy("R5", "student_submit", "submissions", "insert", check=and_(student, enrolled), req="students submit"),
    policy("R6", "student_read_own", "submissions", "select", using=student, req="students see their submissions"),
    policy("R7", "teacher_read_submissions", "submissions", "select", using=teaches, req="teachers grade"),
    inclusion("R8", "submitter_enrolled", "submissions", ["student_id", "course_id"], "enrolled", req="submitters are enrolled"),
]
CL_PROPS = [
    custom("P1", "an assignment is visible to its teachers, and to enrolled students once published", "assignments",
           "authenticated", "select", or_(teaches, and_(enrolled, published))),
    custom("P2", "a submission is created only by an enrolled student, in their own name", "submissions", "authenticated",
           "insert", and_(student, enrolled)),
    custom("P3", "a submission is visible only to its student and the course's teachers", "submissions", "authenticated",
           "select", or_(student, teaches)),
    monotone("P4", "dropping a course never grants access", "enrolled"),
    invariants("P5", "every submitter is enrolled in the course", "R8"),
    least_privilege("P6", "only select/insert/update on assignments, select/insert on submissions, select on enrollments and teaching",
                    {"authenticated": {"assignments": ["select", "insert", "update"], "submissions": ["select", "insert"],
                                       "enrollments": ["select"], "teaching": ["select"]}}),
    must_allow("P7", "a teacher reads the assignments of their courses", "assignments", "authenticated", "select", teaches),
    must_allow("P8", "an enrolled student reads the published assignments of their courses", "assignments", "authenticated", "select",
               and_(enrolled, published)),
    must_allow("P9", "a teacher creates an assignment in their course", "assignments", "authenticated", "insert", teaches),
    must_allow("P10", "a teacher publishes an assignment of their course", "assignments", "authenticated", "update",
               and_(member_old("teaches", "course_id"), *same("id", "course_id"), eq(new("published"), const(True)))),
    must_allow("P11", "an enrolled student submits work in their own name", "submissions", "authenticated", "insert", and_(student, enrolled)),
    must_allow("P12", "a student reads their own submissions", "submissions", "authenticated", "select", student),
    must_allow("P13", "a teacher reads every submission of their courses", "submissions", "authenticated", "select", teaches),
]
CL_REQ = ("Users are enrolled in courses (enrollments) or teach them (teaching). Teachers see, create and edit the "
          "assignments of their courses; enrolled students see only published assignments. Students submit work for "
          "courses they are enrolled in, in their own name, and see their own submissions; teachers see every submission "
          "of their courses. Submissions and assignments are never deleted; submissions are never edited. A submission's "
          "student stays enrolled in its course: an enrollment with submissions cannot be removed.")
CL_ENG = ["An assignment is visible to its teachers, and to enrolled students once published.",
          "A submission is created only by an enrolled student, in their own name.",
          "A submission is visible only to its student and the course's teachers.", "Dropping a course never grants access.",
          "Every submitter is enrolled in the course.",
          "The application role may only select/insert/update assignments, select/insert submissions, and select enrollments and teaching.",
          "A teacher can read the assignments of their courses.", "An enrolled student can read the published assignments of their courses.",
          "A teacher can create an assignment in their course.", "A teacher can publish an assignment of their course.",
          "An enrolled student can submit work in their own name.", "A student can read their own submissions.",
          "A teacher can read every submission of their courses."]
CL_OK = {f"P{i}": "PROVED" for i in range(1, 14)}
case("cl_good", CL_REQ, CL_ENG, spec("classroom", CL_TABLES, CL_REL, CL_RULES, CL_PROPS), CL_OK)
case("cl_unpublished_leak", CL_REQ.replace("enrolled students see only published assignments", "enrolled students see the assignments"), CL_ENG,
     spec("classroom where students see unpublished assignments", CL_TABLES, CL_REL,
          [policy("R1", "student_read", "assignments", "select", using=enrolled)] + CL_RULES[1:], CL_PROPS),
     {**CL_OK, "P1": "REFUTED"})
case("cl_submit_for_other", CL_REQ.replace(", in their own name", ", on behalf of any student (the submission names the "
                                                                 "student, not necessarily the submitter)"), CL_ENG,
     spec("classroom where a student may submit under another name", CL_TABLES, CL_REL,
          [x if x["id"] != "R5" else policy("R5", "student_submit", "submissions", "insert", check=enrolled) for x in CL_RULES], CL_PROPS),
     {**CL_OK, "P2": "REFUTED"})

# ============================================================================= F9 append-only audit log
AU_TABLES = {"memberships": pairs(), "admins": pairs(),
             "events": T({"id": ("int", False), "tenant_id": ("int", False), "actor_id": ("uuid", False), "kind": ("text", False)}, ["id"])}
AU_REL = {"member_of": {"table": "memberships", "key": "user_id", "value": "tenant_id"},
          "admin_of": {"table": "admins", "key": "user_id", "value": "tenant_id"}}
actor = eq(col("actor_id"), ctx("uid"))
AU_RULES = [
    policy("R1", "member_record", "events", "insert", check=and_(mem, actor), req="members record events"),
    policy("R2", "admin_read", "events", "select", using=member("admin_of", "tenant_id"), req="admins audit"),
    inclusion("R3", "admins_are_members", "admins", ["user_id", "tenant_id"], "member_of", req="admins are members"),
]
AU_PROPS = [
    isolation("P1", "events are only recorded and read within the user's tenants", "events", "tenant_id", "member_of", ["select", "insert"]),
    least_privilege("P2", "the log is append-only: no update or delete on events",
                    {"authenticated": {"events": ["select", "insert"], "memberships": ["select"], "admins": ["select"]}}),
    monotone("P3", "losing a membership never grants access", "member_of"),
    custom("P4", "an event is recorded in the actor's own name", "events", "authenticated", "insert", actor),
    invariants("P5", "admins are members", "R3"),
    must_allow("P6", "a member records an event for their tenant in their own name", "events", "authenticated", "insert", and_(mem, actor)),
    must_allow("P7", "an admin reads the events of their tenants", "events", "authenticated", "select", member("admin_of", "tenant_id")),
]
AU_REQ = ("Members of a tenant record audit events for their tenant, always in their own name. Tenant admins (who must "
          "be members) read the events of their tenants. The log is append-only: events are never updated or deleted.")
AU_ENG = ["Events are only recorded and read within the user's tenants.", "The log is append-only: no update or delete on events.",
          "Losing a membership never grants access.", "An event is recorded in the actor's own name.", "Admins are members.",
          "A member can record an event for their tenant in their own name.", "An admin can read the events of their tenants."]
AU_OK = {f"P{i}": "PROVED" for i in range(1, 8)}
case("au_good", AU_REQ, AU_ENG, spec("append-only audit log", AU_TABLES, AU_REL, AU_RULES, AU_PROPS), AU_OK)
case("au_update_allowed", AU_REQ.replace(" The log is append-only: events are never updated or deleted.",
                                         " Admins may correct events of their tenants (a correction never moves an event to another tenant)."),
     AU_ENG + ["An admin can correct the kind of an event of their tenant."],
     spec("audit log where admins may update events", AU_TABLES, AU_REL,
          AU_RULES + [policy("R4", "admin_fix", "events", "update", using=member("admin_of", "tenant_id"), check=member("admin_of", "tenant_id")),
                      immutable("R5", "tenant_immutable", "events", "tenant_id")],
          AU_PROPS + [must_allow("P8", "an admin corrects the kind of an event of their tenant", "events", "authenticated", "update",
                                 and_(member_old("admin_of", "tenant_id"), *same("id", "tenant_id", "actor_id")))]),
     {**AU_OK, "P2": "REFUTED", "P8": "PROVED"})

# ============================================================================= F10 soft deletion
SD_TABLES = {"memberships": pairs(),
             "notes": T({"id": ("int", False), "tenant_id": ("int", False), "deleted_at": ("timestamp", True), "body": ("text", False)}, ["id"])}
SD_REL = {"member_of": {"table": "memberships", "key": "user_id", "value": "tenant_id"}}
alive = is_null(col("deleted_at"))
SD_RULES = [
    policy("R1", "member_read", "notes", "select", using=mem, req="members read"),
    policy("R2", "hide_deleted", "notes", "select", mode="restrictive", using=alive, req="deleted notes are hidden"),
    policy("R3", "member_insert", "notes", "insert", check=and_(mem, alive), req="members write"),
    policy("R4", "member_update", "notes", "update", using=and_(mem, alive), check=mem, req="members edit and soft-delete"),
    immutable("R5", "tenant_immutable", "notes", "tenant_id"),
]
SD_PROPS = [
    isolation("P1", "notes are only seen and changed within the member's tenants", "notes", "tenant_id", "member_of", ["select", "insert", "update"]),
    custom("P2", "a deleted note is never visible", "notes", "authenticated", "select", alive),
    custom("P3", "a deleted note is never modified", "notes", "authenticated", "update", alive, which="old"),
    monotone("P4", "leaving a tenant never grants access", "member_of"),
    least_privilege("P5", "no hard delete", {"authenticated": {"notes": ["select", "insert", "update"], "memberships": ["select"]}}),
    must_allow("P6", "a member reads the live notes of their tenants", "notes", "authenticated", "select", and_(mem, alive)),
    must_allow("P7", "a member creates a live note in their tenant", "notes", "authenticated", "insert", and_(mem, alive)),
    must_allow("P8", "a member soft-deletes a live note of their tenant", "notes", "authenticated", "update",
               and_(member_old("member_of", "tenant_id"), is_null(old("deleted_at")), not_(is_null(new("deleted_at"))),
                    *same("id", "tenant_id", "body"))),
]
SD_REQ = ("Members of a tenant read, create and edit the tenant's notes. A note is deleted by setting its deleted_at "
          "time (soft delete): a deleted note is invisible and can no longer be modified. Notes are never hard-deleted; a "
          "new note is not deleted, and a note never changes tenant.")
SD_ENG = ["Notes are only seen and changed within the member's tenants.", "A deleted note is never visible.",
          "A deleted note is never modified.", "Leaving a tenant never grants access.", "There is no hard delete.",
          "A member can read the live notes of their tenants.", "A member can create a live note in their tenant.",
          "A member can soft-delete a live note of their tenant."]
SD_OK = {f"P{i}": "PROVED" for i in range(1, 9)}
case("sd_good", SD_REQ, SD_ENG, spec("soft deletion", SD_TABLES, SD_REL, SD_RULES, SD_PROPS), SD_OK)
case("sd_deleted_visible", SD_REQ.replace("a deleted note is invisible and ", "a deleted note "), SD_ENG,
     spec("soft deletion without the hiding policy", SD_TABLES, SD_REL, [x for x in SD_RULES if x["id"] != "R2"], SD_PROPS),
     {**SD_OK, "P2": "REFUTED"})

# ============================================================================= F11 column rules (salaries)
CR_TABLES = {"memberships": pairs(),
             "employees": T({"id": ("int", False), "tenant_id": ("int", False), "user_id": ("uuid", False),
                             "title": ("text", False), "salary": ("int", False)}, ["id"])}
CR_REL = {"member_of": {"table": "memberships", "key": "user_id", "value": "tenant_id"}}
CR_RULES = [
    policy("R1", "staff_read", "employees", "select", using=mem, role="staff", req="staff see the directory"),
    column_rule("R2", "employees", "staff", ["id", "tenant_id", "user_id", "title"], "read"),
    policy("R3", "hr_read", "employees", "select", using=mem, role="hr", req="HR sees everything"),
    policy("R4", "hr_update", "employees", "update", using=mem, check=mem, role="hr", req="HR sets salaries"),
    column_rule("R5", "employees", "hr", ["salary", "title"], "write"),
    check("R6", "salary_non_negative", "employees", cmp(col("salary"), ">=", const(0)), req="salaries"),
    policy("R7", "staff_members", "memberships", "select", using=eq(col("user_id"), ctx("uid")), role="staff", req="own memberships"),
    policy("R8", "hr_members", "memberships", "select", using=eq(col("user_id"), ctx("uid")), role="hr", req="own memberships"),
]
CR_PROPS = [
    isolation("P1", "employee records are only seen and changed within the user's tenants", "employees", "tenant_id", "member_of", ["select", "update"]),
    least_privilege("P2", "staff only read employees (not salaries); HR reads and updates", 
                    {"staff": {"employees": ["select"], "memberships": ["select"]}, "hr": {"employees": ["select", "update"], "memberships": ["select"]}}),
    invariants("P3", "salaries are never negative", "R6"),
    monotone("P4", "leaving a tenant never grants access", "member_of"),
    must_allow("P5", "staff read the employee records of their tenants", "employees", "staff", "select", mem),
    must_allow("P6", "HR read the employee records of their tenants", "employees", "hr", "select", mem),
    must_allow("P7", "HR set a non-negative salary on an employee of their tenants", "employees", "hr", "update",
               and_(member_old("member_of", "tenant_id"), *same("id", "tenant_id", "user_id", "title"), cmp(new("salary"), ">=", const(0)))),
    must_allow("P8", "a user reads their own memberships", "memberships", "staff", "select", eq(col("user_id"), ctx("uid"))),
]
CR_REQ = ("Two application roles: staff and hr. Both see the employee records of the tenants they belong to, but staff "
          "never see salaries (only id, tenant, user and title). HR may update salaries and titles; nobody else changes "
          "employee records, and nothing is inserted or deleted through the application. Salaries are never negative. "
          "Users see their own memberships.")
CR_ENG = ["Employee records are only seen and changed within the user's tenants.",
          "Staff only read employees (never salaries); HR reads and updates.", "Salaries are never negative.",
          "Leaving a tenant never grants access.", "Staff can read the employee records of their tenants.",
          "HR can read the employee records of their tenants.", "HR can set a non-negative salary on an employee of their tenants.",
          "A user can read their own memberships."]
CR_OK = {f"P{i}": "PROVED" for i in range(1, 9)}
case("cr_good", CR_REQ, CR_ENG, spec("column rules: salaries", CR_TABLES, CR_REL, CR_RULES, CR_PROPS, roles=("staff", "hr")), CR_OK)
case("cr_staff_update", CR_REQ.replace("HR may update salaries and titles; nobody else changes employee records", "HR may update salaries and titles; staff may update titles"), CR_ENG,
     spec("column rules where staff may also update", CR_TABLES, CR_REL,
          CR_RULES + [policy("R9", "staff_update", "employees", "update", using=mem, check=mem, role="staff"),
                      column_rule("R10", "employees", "staff", ["title"], "write")], CR_PROPS, roles=("staff", "hr")),
     {**CR_OK, "P2": "REFUTED"})


# ----------------------------------------------------------------------------- write and verify
def main() -> int:
    (OUT / "specs").mkdir(parents=True, exist_ok=True)
    bad = 0
    for c in CASES:
        spec_obj = Spec.model_validate(c["spec"])
        check_spec(spec_obj)
        verdicts = {r.id: r.verdict for r in run_all(spec_obj)}
        if verdicts != c["verdicts"]:
            print(f"{c['id']}: intended {c['verdicts']} but the solver says {verdicts}")
            bad += 1
            continue
        spec_path = OUT / "specs" / f"{c['id']}.mls.json"
        spec_path.write_text(json.dumps(c["spec"], indent=1) + "\n")
        # the requirements are self-consistent in every case, so the loop should prove its own
        # properties; the reference verdicts (REFUTED included) are what the reference properties
        # must say about the generated rules (D38)
        status = c["loop"] or "proved"
        (OUT / f"{c['id']}.json").write_text(json.dumps({
            "id": c["id"], "origin": "seed", "requirements": c["requirements"],
            "expected_properties": c["expected_properties"], "reference_spec": f"specs/{c['id']}.mls.json",
            "expected_verdicts": verdicts, "expected_loop": {"status": status}}, indent=1) + "\n")
        print(f"{c['id']}: ok ({status})")
    print(f"{len(CASES) - bad}/{len(CASES)} cases written")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
