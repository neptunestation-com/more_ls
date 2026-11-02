"""
Demo: prove theorems about an RLS policy corpus, then differential-test the model against real Postgres.

    python3 demo.py                 # proofs only
    python3 demo.py --pg /tmp/pgrun 5544   # proofs + differential test (unix-socket dir, port)
"""
import sys, random
import z3
from mls import *

# ------------------------------------------------------------------ data model
SCHEMA = Schema(
    tables={"docs": Table("docs", {"tenant_id": False, "owner_id": True, "level": False, "archived": True})},
    rels={"memberships": Rel("memberships", "memberships", "user_id", "tenant_id"),
          "admins":      Rel("admins",      "admins",      "user_id", "tenant_id")},
    subsets=[("admins", "memberships")],      # axiom: every admin is a member
)
UID, CLR = Ctx("uid"), Ctx("clearance")
TENANT = Col("tenant_id")
def member(): return Member("memberships", UID, TENANT)
def admin():  return Member("admins", UID, TENANT)

# ------------------------------------------------------------------ policy corpora
GOOD = [
    Policy("member_read",   "docs", "select", "permissive", using=AND(member(), Cmp("<=", Col("level"), CLR))),
    Policy("admin_read",    "docs", "select", "permissive", using=admin()),
    Policy("member_insert", "docs", "insert", "permissive", check=AND(member(), Cmp("<=", Col("level"), CLR))),
    Policy("member_update", "docs", "update", "permissive", using=member(), check=member()),
    Policy("not_archived",  "docs", "all",    "restrictive", using=Not(Cmp("=", Col("archived"), Const(1)))),
]
BUGGY = GOOD + [
    # "owners can always read their own docs"  -- sounds harmless
    Policy("owner_read",    "docs", "select", "permissive", using=Cmp("=", Col("owner_id"), UID)),
    # copy-pasted legacy policy, duplicates member_read
    Policy("legacy_member_read", "docs", "select", "permissive",
           using=AND(member(), Cmp("<=", Col("level"), CLR))),
]
NONMONO = GOOD + [
    # "outsiders may read level-0 public docs"
    Policy("outsider_public", "docs", "select", "permissive",
           using=AND(Not(member()), Cmp("=", Col("level"), Const(0)))),
]

def banner(s): print("\n" + "=" * 78 + "\n" + s + "\n" + "=" * 78)
def verdict(ok, text, model=None):
    print(("  PROVED   " if ok else "  REFUTED  ") + text)
    if model is not None: print(show_model(model))

# ------------------------------------------------------------------ theorems
def theorems(name, corpus):
    banner(f"corpus: {name}   ({', '.join(p.name for p in corpus)})")
    enc = Enc(SCHEMA); ax = enc.axioms()
    sel = effective(corpus, "docs", "select", "using")
    ins = effective(corpus, "docs", "insert", "check")
    upd_old = effective(corpus, "docs", "update", "using")
    upd_new = effective(corpus, "docs", "update", "check")

    print("\n[1] fragment check (index-driven policies only)")
    bad = False
    for p in corpus:
        for e in (p.using, p.check):
            if e is not None:
                for v in check_fragment(e, SCHEMA, p.table):
                    bad = True; print(f"  {p.name}: {v}")
    if not bad: print("  all policies in the sargable fragment")

    print("\n[2] tenant isolation")
    isolated = member()
    ok, m = prove(ax + [enc.T(sel, "r", "docs")], enc.T(isolated, "r", "docs"))
    verdict(ok, "SELECT: every visible row's tenant is one the user belongs to", m)
    ok, m = prove(ax + [enc.T(ins, "r", "docs")], enc.T(isolated, "r", "docs"))
    verdict(ok, "INSERT: every insertable row's tenant is one the user belongs to", m)
    ok, m = prove(ax + [enc.T(upd_old, "old", "docs"), enc.T(upd_new, "new", "docs")],
                  enc.T(isolated, "new", "docs"))
    verdict(ok, "UPDATE: the new row's tenant is one the user belongs to", m)

    print("\n[3] can an UPDATE move a row between tenants?")
    ok, m = prove(ax + [enc.T(upd_old, "old", "docs"), enc.T(upd_new, "new", "docs")],
                  Int_eq("old.tenant_id", "new.tenant_id"))
    verdict(ok, "UPDATE preserves tenant_id", m)

    print("\n[4] NULL liveness (three-valued logic)")
    ok, m = prove(ax + [enc.T(sel, "r", "docs")], z3.Not(z3.Bool("r.archived.null")))
    verdict(ok, "no visible row has archived IS NULL  (NOT (NULL = 1) is NULL => denied)", m)

    print("\n[5] redundancy: does dropping one policy change the effective SELECT predicate?")
    for p in corpus:
        if p.cmd not in ("select", "all"): continue
        without = effective(corpus, "docs", "select", "using", skip=(p.name,))
        a, b = enc.T(sel, "r", "docs"), enc.T(without, "r", "docs")
        eq, _ = prove(ax, z3.And(z3.Implies(a, b), z3.Implies(b, a)))
        print(f"  {p.name:20s} {'REDUNDANT (removable)' if eq else 'needed'}")

    print("\n[6] monotonicity: shrinking `memberships` can never grant access")
    e1 = Enc(SCHEMA, {"memberships": "big"}); e2 = Enc(SCHEMA, {"memberships": "small"})
    u, t = z3.Ints("mu mt")
    shrink = z3.ForAll([u, t], z3.Implies(e2.rel_fn("memberships")(u, t), e1.rel_fn("memberships")(u, t)))
    ok, m = prove(e1.axioms() + e2.axioms() + [shrink, e2.T(sel, "r", "docs")], e1.T(sel, "r", "docs"))
    verdict(ok, "visible(small) => visible(big)", m)

def Int_eq(a, b): return z3.Int(a) == z3.Int(b)

# ------------------------------------------------------------------ differential test vs real Postgres
def differential_test(sockdir, port, corpus, seed=7):
    import psycopg2
    rnd = random.Random(seed)
    adm = psycopg2.connect(host=sockdir, port=port, user="postgres", dbname="postgres")
    adm.autocommit = True
    cur = adm.cursor()
    cur.execute("drop database if exists mls"); cur.execute("create database mls")
    adm.close()
    db = psycopg2.connect(host=sockdir, port=port, user="postgres", dbname="mls"); db.autocommit = True
    c = db.cursor()
    c.execute("do $$ begin if not exists (select 1 from pg_roles where rolname='app') "
              "then create role app nologin; end if; end $$")
    c.execute("""create table docs(id serial primary key, tenant_id int not null, owner_id int,
                 level int not null, archived int);
                 create table memberships(user_id int, tenant_id int);
                 create table admins(user_id int, tenant_id int);""")
    users, tenants = range(1, 7), range(1, 6)
    mem = {(u, t) for u in users for t in tenants if rnd.random() < 0.4}
    adm_ = {(u, t) for (u, t) in mem if rnd.random() < 0.3}          # admins subset of memberships
    for (u, t) in mem:  c.execute("insert into memberships values (%s,%s)", (u, t))
    for (u, t) in adm_: c.execute("insert into admins values (%s,%s)", (u, t))
    rows = []
    for i in range(1, 301):
        r = dict(id=i, tenant_id=rnd.choice(list(tenants)),
                 owner_id=rnd.choice([None] + list(users)), level=rnd.randint(0, 3),
                 archived=rnd.choice([None, 0, 1]))
        rows.append(r)
        c.execute("insert into docs(id,tenant_id,owner_id,level,archived) values (%(id)s,%(tenant_id)s,%(owner_id)s,%(level)s,%(archived)s)", r)
    c.execute("select setval('docs_id_seq', 100000)")      # ids were inserted explicitly
    c.execute("grant select, insert on docs, memberships, admins to app; grant usage on all sequences in schema public to app;")
    c.execute("alter table docs enable row level security")
    for p in corpus: c.execute(create_policy_sql(p, SCHEMA))
    rels = {"memberships": mem, "admins": adm_}

    sel = effective(corpus, "docs", "select", "using")
    ins = effective(corpus, "docs", "insert", "check")
    mism = n_sel = n_ins = 0
    for uid in users:
        for clr in range(0, 4):
            ctx = {"uid": uid, "clearance": clr}
            c.execute("set role app"); c.execute(f"set app.uid = {uid}; set app.clearance = {clr}")
            # --- SELECT
            c.execute("select id from docs order by id")
            pg = [x[0] for x in c.fetchall()]
            model = [r["id"] for r in rows if eval_pred(sel, r, ctx, rels) is True]
            n_sel += len(rows)
            if pg != model:
                mism += 1; print("  SELECT MISMATCH", ctx, "pg-only:", sorted(set(pg) - set(model)),
                                 "model-only:", sorted(set(model) - set(pg)))
            # --- INSERT (try 40 random candidate rows, each in a rolled-back savepoint)
            for _ in range(40):
                cand = dict(tenant_id=rnd.choice(list(tenants)), owner_id=rnd.choice([None] + list(users)),
                            level=rnd.randint(0, 3), archived=rnd.choice([None, 0, 1]))
                want = eval_pred(ins, cand, ctx, rels) is True
                c.execute("begin"); c.execute("savepoint s")
                try:
                    c.execute("insert into docs(tenant_id,owner_id,level,archived) values (%(tenant_id)s,%(owner_id)s,%(level)s,%(archived)s)", cand)
                    got = True
                except psycopg2.errors.InsufficientPrivilege:
                    got = False
                c.execute("rollback")
                n_ins += 1
                if got != want:
                    mism += 1; print("  INSERT MISMATCH", ctx, cand, "pg:", got, "model:", want)
            c.execute("reset role")
    print(f"  compared {n_sel} (user,clearance,row) SELECT decisions and {n_ins} INSERT decisions "
          f"across {len(users)*4} contexts: {mism} mismatches")
    return mism

if __name__ == "__main__":
    for name, corpus in (("GOOD", GOOD), ("BUGGY (owner_read + legacy dup)", BUGGY),
                         ("NON-MONOTONE (outsider_public)", NONMONO)):
        theorems(name, corpus)
    if "--pg" in sys.argv:
        i = sys.argv.index("--pg"); sock, port = sys.argv[i + 1], int(sys.argv[i + 2])
        banner("differential test: DSL semantics vs real PostgreSQL (GOOD corpus, generated SQL)")
        print("\n".join("  " + create_policy_sql(p, SCHEMA) for p in GOOD))
        sys.exit(1 if differential_test(sock, port, GOOD) else 0)
