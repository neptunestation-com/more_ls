"""D66/D67 on real Postgres.  A lookup reads its table under that table's SELECT policies; when
those lead back to the policy's own table, whose policies contain a subquery (every compiled policy
does: the context value is `(select auth.uid())`), Postgres refuses the statement with 'infinite
recursion detected in policy'.  That happens whatever the first policy's command is (a delete, an
update, a WITH CHECK), through two tables or three, and on one table; not for a chain that does not
come back, nor for one that comes back only through another role's policies.  The compiler routes
exactly the lookups that would recurse through a helper (`spec/recursion.py`): every shape
deploys and runs as the role without the error, and compiled with plain subqueries exactly the
routed shapes hit it.  The helper's results are the differential fixture `specs.lookup_spec`'s."""
from __future__ import annotations

import psycopg
import pytest

from mls.compile import compile_spec
from mls.pg.harness import Harness
from mls.spec import check_spec
from mls.spec.recursion import routed
from mls.spec.syntax import parse

pytestmark = pytest.mark.pg

SCHEMA = """mls 0.1
identity supabase { uid: auth.uid uuid }
roles anon, authenticated

table a owned {
  id uuid not null,
  owner uuid not null,
  b_id uuid not null;
  primary_key (id)
}

table b owned {
  id uuid not null,
  owner uuid not null,
  a_id uuid not null;
  primary_key (id)
}

table c owned {
  id uuid not null,
  owner uuid not null,
  a_id uuid not null;
  primary_key (id)
}

relation rel_a = a(owner, id)
relation rel_b = b(owner, id)
relation rel_c = c(owner, id)

rules {
%s
}
"""

OWN = "  R0%s policy own_%s on %s: authenticated select permissive using owner = @uid"
SHAPES = {
    "select cycle": (True, ["R1 policy p on a: authenticated select permissive using rel_b(@uid, b_id)",
                            "R2 policy q on b: authenticated select permissive using rel_a(@uid, a_id)"]),
    "three tables": (True, ["R1 policy p on a: authenticated select permissive using rel_b(@uid, b_id)",
                            "R2 policy q on b: authenticated select permissive using rel_c(@uid, id)",
                            "R3 policy r on c: authenticated select permissive using rel_a(@uid, a_id)"]),
    "restrictive": (True, ["R1 policy p on a: authenticated select permissive using rel_b(@uid, b_id)",
                           "R2 policy q on b: authenticated select restrictive using owner = @uid or rel_a(@uid, a_id)"]),
    "one table": (True, ["R1 policy p on a: authenticated select permissive using rel_a(@uid, id)"]),
    "delete closes it": (True, ["R1 policy p on a: authenticated delete permissive using rel_b(@uid, b_id)",
                                "R2 policy q on b: authenticated select permissive using rel_a(@uid, a_id)"]),
    "check closes it": (True, ["R1 policy p on a: authenticated all permissive using owner = @uid "
                               "check rel_b(@uid, b_id)",
                               "R2 policy q on b: authenticated select permissive using rel_a(@uid, a_id)"]),
    "write reads own table": (True, ["R1 policy p on a: authenticated delete permissive using rel_a(@uid, b_id)"]),
    "chain": (False, ["R1 policy p on a: authenticated select permissive using rel_b(@uid, b_id)",
                      "R2 policy q on b: authenticated select permissive using rel_c(@uid, id)",
                      "R3 policy r on c: authenticated delete permissive using owner = @uid"]),
    "back edge for another role": (False, [
        "R1 policy p on a: authenticated select permissive using rel_b(@uid, b_id)",
        "R2 policy q on b: anon select permissive using rel_a(@uid, a_id)"]),
    "back edge is no lookup": (False, [
        "R1 policy p on a: authenticated delete permissive using rel_b(@uid, b_id)",
        "R2 policy q on b: authenticated select permissive using a_id = @uid"]),
}
STATEMENTS = ["select count(*) from a", "select count(*) from b", "select count(*) from c",
              "delete from a", "update a set b_id = b_id"]
UID = "00000000-0000-0000-0000-000000000001"


def _spec_text(rules: list[str]) -> str:
    own = [OWN % (t, t, t) for t in "abc"]
    return SCHEMA % "\n".join(own + [f"  {r}" for r in rules])


def _recursion(spec, script, pg_dsn: str, db: str) -> bool:
    h = Harness(spec, script, pg_dsn, db)
    h.deploy()
    try:
        with h.conn.cursor() as cur:
            cur.execute(f"insert into a values ('{UID}', '{UID}', '{UID}')")
            cur.execute(f"insert into b values ('{UID}', '{UID}', '{UID}')")
            cur.execute(f"insert into c values ('{UID}', '{UID}', '{UID}')")
        found = False
        for sql in STATEMENTS:
            with h.conn.cursor() as cur:
                h._begin_ctx(cur, "authenticated", {"uid": UID})
                try:
                    cur.execute(sql)
                except psycopg.errors.InsufficientPrivilege:
                    pass  # a command the role has no grant for
                except psycopg.errors.StatementTooComplex:
                    found = True  # a helper that runs as the caller calls itself through the policies
                except psycopg.Error as e:
                    if "infinite recursion detected in policy" not in str(e):
                        raise
                    found = True
                cur.execute("rollback")
        return found
    finally:
        h.close()


@pytest.mark.parametrize("shape", list(SHAPES), ids=list(SHAPES))
def test_a_lookup_that_would_recurse_goes_through_a_helper(shape, pg_dsn, monkeypatch):
    """Routed, the shape runs; compiled with plain subqueries everywhere, exactly the routed
    shapes hit the error (the measurement the routing rests on, FINDINGS)."""
    from mls.compile import internal
    from mls.spec import recursion

    recurses, rules = SHAPES[shape]
    spec = check_spec(parse(_spec_text(rules)))
    assert bool(routed(spec)) == recurses
    assert not _recursion(spec, compile_spec(spec), pg_dsn, "mls_recursion")
    plain = check_spec(parse(_spec_text(rules)))
    monkeypatch.setattr(recursion, "routed", lambda spec: frozenset())
    monkeypatch.setattr(internal, "routed", lambda spec: frozenset())
    assert _recursion(plain, compile_spec(plain, prove_soundness=False), pg_dsn, "mls_recursion_plain") == recurses
