"""
RLS demoted to one target among several.  The spec now has four kinds of construct:

    Policy      (ctx, row)   -> CREATE POLICY
    Check       (row)        -> CHECK ((p) IS TRUE)
    Inclusion   (row ⊆ rel)  -> FOREIGN KEY ... ON DELETE RESTRICT
    Transition  (old, new)   -> BEFORE UPDATE trigger (one fixed template)

Every proof obligation below is still quantifier-free or EPR over uninterpreted relations.

    python3 integrity.py                          # proofs only
    python3 integrity.py --pg <socket-dir> <port> # + differential test (UPDATE, DELETE, CHECK)
"""
import sys, random
import z3
from mls import *
from demo import UID, CLR, TENANT, member, GOOD, banner, verdict

SCHEMA = Schema(
    tables={"docs":        Table("docs", {"tenant_id": False, "owner_id": True, "level": False, "archived": True}),
            "admins":      Table("admins", {"user_id": False, "tenant_id": False}),
            "memberships": Table("memberships", {"user_id": False, "tenant_id": False})},
    rels={"memberships": Rel("memberships", "memberships", "user_id", "tenant_id"),
          "admins":      Rel("admins",      "admins",      "user_id", "tenant_id")},
    subsets=[("admins", "memberships")],
)

# ------------------------------------------------------------------ the integrity spec
LEVEL_RANGE  = Check("level_range", "docs", AND(Cmp(">=", Col("level"), Const(0)), Cmp("<=", Col("level"), Const(3))))
OWNER_MEMBER = Inclusion("owner_in_tenant", "docs", ("owner_id", "tenant_id"), "memberships")
ADMIN_MEMBER = Inclusion("admin_is_member", "admins", ("user_id", "tenant_id"), "memberships")
TENANT_FIXED = Transition("tenant_immutable", "docs", Cmp("=", New("tenant_id"), Old("tenant_id")))

CHECKS, INCLUSIONS, TRANSITIONS = [LEVEL_RANGE], [OWNER_MEMBER, ADMIN_MEMBER], [TENANT_FIXED]
# a CHECK we only use to demonstrate the NULL-normalization issue
ARCHIVED_FLAG = Check("archived_flag", "docs", OR(Cmp("=", Col("archived"), Const(0)), Cmp("=", Col("archived"), Const(1))))

def doc_invariant():
    return AND(LEVEL_RANGE.pred, OWNER_MEMBER.pred(SCHEMA))

def ddl():
    out = [check_sql(c, SCHEMA) for c in CHECKS]
    out += [inclusion_sql(i, SCHEMA) for i in INCLUSIONS]
    out += [transition_sql(t, SCHEMA) for t in TRANSITIONS]
    return out

# ------------------------------------------------------------------ theorems
def theorems():
    banner("integrity spec compiled to Postgres")
    for s in ddl(): print("  " + s.replace("\n", "\n  "))

    enc = Enc(SCHEMA); ax = enc.axioms()
    sel = effective(GOOD, "docs", "select", "using")
    upd_old = effective(GOOD, "docs", "update", "using")
    upd_new = effective(GOOD, "docs", "update", "check")

    banner("[1] CHECK vs RLS: the same predicate means different things under NULL")
    t, f = enc.tf(ARCHIVED_FLAG.pred, "r", "docs")
    ok, m = prove([z3.Not(f)], t)      # naive CHECK accepts iff predicate is not FALSE
    verdict(ok, "naive   CHECK (archived = 0 or archived = 1) only admits rows where it holds", m)
    ok, m = prove([t], t)              # normalized CHECK accepts iff predicate is TRUE
    verdict(ok, "normalized CHECK ((...) IS TRUE) only admits rows where it holds", m)

    banner("[2] tenant isolation across UPDATE, now with the transition trigger")
    base = ax + [enc.T(upd_old, "old", "docs"), enc.T(upd_new, "new", "docs")]
    same = z3.Int("old.tenant_id") == z3.Int("new.tenant_id")
    ok, m = prove(base, same)
    verdict(ok, "RLS alone:            UPDATE preserves tenant_id", m)
    ok, m = prove(base + [enc.T(TENANT_FIXED.pred, "_", "docs")], same)
    verdict(ok, "RLS + transition:     UPDATE preserves tenant_id")

    banner("[3] inductive invariant  I(row) = level_range ∧ owner_in_tenant  (and admins ⊆ memberships)")
    I = doc_invariant()
    # base case: the empty database satisfies I vacuously.
    # step: INSERT into docs.  The constraints enforce exactly I, so this is translation validation.
    enforced_new = [enc.T(LEVEL_RANGE.pred, "new", "docs"), enc.T(OWNER_MEMBER.pred(SCHEMA), "new", "docs")]
    ok, _ = prove(ax + enforced_new, enc.T(I, "new", "docs"))
    verdict(ok, "INSERT docs:   CHECK ∧ FK(new)                     => I(new)")
    ok, _ = prove(ax + [enc.T(I, "old", "docs")] + enforced_new + [enc.T(TENANT_FIXED.pred, "_", "docs")],
                  enc.T(I, "new", "docs"))
    verdict(ok, "UPDATE docs:   I(old) ∧ CHECK ∧ FK(new) ∧ T(old,new) => I(new)")

    # step: DELETE (u,t) FROM memberships.  The interesting one: a transition on a *different* table.
    big, small = Enc(SCHEMA, {"memberships": "big"}), Enc(SCHEMA, {"memberships": "small"})
    u, t_ = z3.Ints("del.user del.tenant")
    x, y = z3.Ints("dx dy")
    M, Ms = big.rel_fn("memberships"), small.rel_fn("memberships")
    A = big.rel_fn("admins")             # admins unchanged by this transition
    delete = z3.ForAll([x, y], Ms(x, y) == z3.And(M(x, y), z3.Not(z3.And(x == u, y == t_))))
    adm_inv = lambda R: z3.ForAll([x, y], z3.Implies(A(x, y), R(x, y)))
    pre = [delete, adm_inv(M), big.T(I, "r", "docs")]
    post = z3.And(small.T(I, "r", "docs"), adm_inv(Ms))

    ok, m = prove(pre, post)
    verdict(ok, "DELETE membership, unguarded:            I and admins⊆memberships survive", m)
    # ON DELETE RESTRICT, as seen by the solver: no referencing docs row, no referencing admins row
    owner_null = z3.Bool("r.owner_id.null")
    guard_docs = z3.Not(z3.And(z3.Not(owner_null), z3.Int("r.owner_id") == u, z3.Int("r.tenant_id") == t_))
    guard_adm = z3.Not(A(u, t_))
    ok, m = prove(pre + [guard_docs], post)
    verdict(ok, "DELETE membership, FK on docs only:      I and admins⊆memberships survive", m)
    ok, m = prove(pre + [guard_docs, guard_adm], post)
    verdict(ok, "DELETE membership, both FKs (RESTRICT):  I and admins⊆memberships survive", m)

# ------------------------------------------------------------------ differential test
def differential_test(sockdir, port, model_trigger=True, model_sel_new=True, seed=11, quiet=False):
    import psycopg2
    rnd = random.Random(seed)
    adm = psycopg2.connect(host=sockdir, port=port, user="postgres", dbname="postgres"); adm.autocommit = True
    adm.cursor().execute("drop database if exists rlsint"); adm.cursor().execute("create database rlsint"); adm.close()
    db = psycopg2.connect(host=sockdir, port=port, user="postgres", dbname="rlsint"); db.autocommit = True
    c = db.cursor()
    c.execute("do $$ begin if not exists (select 1 from pg_roles where rolname='app') "
              "then create role app nologin; end if; end $$")
    c.execute("""create table memberships(user_id int, tenant_id int, primary key(user_id, tenant_id));
                 create table admins(user_id int not null, tenant_id int not null);
                 create table docs(id serial primary key, tenant_id int not null, owner_id int,
                                   level int not null, archived int);""")
    users, tenants = list(range(1, 9)), list(range(1, 6))
    mem = {(u, t) for u in users for t in tenants if rnd.random() < 0.5}
    admins = {p for p in mem if rnd.random() < 0.3}
    for p in mem: c.execute("insert into memberships values (%s,%s)", p)
    for p in admins: c.execute("insert into admins values (%s,%s)", p)
    rows = {}
    for i in range(1, 201):
        t = rnd.choice(tenants)
        owners = [u for u in users if (u, t) in mem]
        r = dict(id=i, tenant_id=t, owner_id=rnd.choice([None] + owners), level=rnd.randint(0, 3),
                 archived=rnd.choice([None, 0, 1]))
        rows[i] = r
        c.execute("insert into docs values (%(id)s,%(tenant_id)s,%(owner_id)s,%(level)s,%(archived)s)", r)
    c.execute("select setval('docs_id_seq', 100000)")
    for s in ddl(): c.execute(s)
    c.execute("grant select, insert, update on docs to app; grant select on memberships, admins to app")
    c.execute("alter table docs enable row level security")
    for p in GOOD: c.execute(create_policy_sql(p, SCHEMA))
    rels = {"memberships": mem, "admins": admins}

    sel = effective(GOOD, "docs", "select", "using")
    upd_old = effective(GOOD, "docs", "update", "using")
    upd_new = effective(GOOD, "docs", "update", "check")
    def holds(p, r, ctx): return eval_pred(p, r, ctx, rels) is True

    def model_update(old, new, ctx):
        # rows the UPDATE can see: UPDATE USING, plus SELECT USING because WHERE id = .. reads the row
        if not (holds(upd_old, old, ctx) and holds(sel, old, ctx)): return "filtered"
        ok = holds(upd_new, new, ctx) and holds(LEVEL_RANGE.pred, new, ctx) \
             and holds(OWNER_MEMBER.pred(SCHEMA), new, ctx)
        if model_sel_new: ok = ok and holds(sel, new, ctx)
        if model_trigger: ok = ok and holds(TENANT_FIXED.pred, {"__old": old, "__new": new}, ctx)
        return "ok" if ok else "error"

    mism, n, outcomes = 0, 0, {"filtered": 0, "ok": 0, "error": 0}
    for uid in users:
        for clr in range(0, 4):
            ctx = {"uid": uid, "clearance": clr}
            c.execute("set role app"); c.execute(f"set app.uid = {uid}; set app.clearance = {clr}")
            updatable = [r for r in rows.values() if holds(upd_old, r, ctx) and holds(sel, r, ctx)]
            for _ in range(40):
                # 75% of the time pick a row the model says is updatable (if any), else any row
                old = rnd.choice(updatable) if updatable and rnd.random() < 0.75 else rows[rnd.randint(1, 200)]
                new = dict(old)
                if rnd.random() < 0.4: new["tenant_id"] = rnd.choice(tenants)
                if rnd.random() < 0.5: new["owner_id"] = rnd.choice([None] + users)
                if rnd.random() < 0.5: new["level"] = rnd.randint(-1, 4)
                if rnd.random() < 0.3: new["archived"] = rnd.choice([None, 0, 1])
                want = model_update(old, new, ctx)
                c.execute("begin")
                try:
                    c.execute("update docs set tenant_id=%(tenant_id)s, owner_id=%(owner_id)s, "
                              "level=%(level)s, archived=%(archived)s where id=%(id)s", new)
                    got = "ok" if c.rowcount == 1 else "filtered"
                except psycopg2.Error:
                    got = "error"
                c.execute("rollback")
                n += 1; outcomes[got] += 1
                if got != want:
                    mism += 1
                    if not quiet and mism <= 5: print("  UPDATE MISMATCH", ctx, old, "->", new, "pg:", got, "model:", want)
            c.execute("reset role")
    print(f"  UPDATE: {n} statements across {len(users)*4} contexts, pg outcomes {outcomes}: {mism} mismatches")

    # DELETE from memberships: ON DELETE RESTRICT blocks iff some docs/admins row references the pair
    dm = 0
    for (u, t) in sorted(mem):
        want = not (any(r["owner_id"] == u and r["tenant_id"] == t for r in rows.values()) or (u, t) in admins)
        c.execute("begin")
        try:
            c.execute("delete from memberships where user_id=%s and tenant_id=%s", (u, t)); got = True
        except psycopg2.errors.ForeignKeyViolation:
            got = False
        c.execute("rollback")
        if got != want: dm += 1; print("  DELETE MISMATCH", (u, t), "pg:", got, "model:", want)
    print(f"  DELETE memberships: {len(mem)} deletes tried: {dm} mismatches")

    # realize the unguarded-delete counterexample in Postgres: drop the FK, delete a referenced pair
    victim = next((r["owner_id"], r["tenant_id"]) for r in rows.values()
                  if r["owner_id"] is not None and (r["owner_id"], r["tenant_id"]) not in admins)
    c.execute("begin")
    c.execute("alter table docs drop constraint owner_in_tenant")
    c.execute("delete from memberships where user_id=%s and tenant_id=%s", victim)
    c.execute("select count(*) from docs d where owner_id is not null and not exists "
              "(select 1 from memberships m where m.user_id=d.owner_id and m.tenant_id=d.tenant_id)")
    broken = c.fetchone()[0]
    c.execute("rollback")
    print(f"  without the FK, deleting membership {victim} leaves {broken} docs rows violating I  (solver predicted > 0)")

    # CHECK normalization, for real
    res = {}
    for norm in (False, True):
        c.execute("drop table if exists t_chk; create table t_chk(archived int)")
        c.execute(check_sql(Check("archived_flag", "t_chk", ARCHIVED_FLAG.pred), SCHEMA, normalize=norm))
        try: c.execute("insert into t_chk values (null)"); res[norm] = "accepted"
        except psycopg2.errors.CheckViolation: res[norm] = "rejected"
    print(f"  CHECK with archived = NULL: naive {res[False]}, normalized {res[True]}")
    return mism + dm

if __name__ == "__main__":
    theorems()
    if "--pg" in sys.argv:
        i = sys.argv.index("--pg"); sock, port = sys.argv[i + 1], int(sys.argv[i + 2])
        banner("differential test vs real PostgreSQL")
        bad = differential_test(sock, port)
        banner("mutation checks (each must produce mismatches)")
        print("  model without the trigger:");               differential_test(sock, port, model_trigger=False, quiet=True)
        print("  model without SELECT-policy check on new row:"); differential_test(sock, port, model_sel_new=False, quiet=True)
        sys.exit(1 if bad else 0)
