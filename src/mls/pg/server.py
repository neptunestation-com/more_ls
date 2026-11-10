"""Start throwaway Postgres servers in Docker (testcontainers) for the test harness and CLI."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

PG_VERSIONS = ("15", "16", "17", "18")


@contextmanager
def postgres_container(version: str) -> Iterator[str]:
    """Yield a superuser DSN for a fresh postgres:<version> container."""
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # older testcontainers
        from testcontainers.postgres import PostgresContainer

    with PostgresContainer(f"postgres:{version}", username="postgres", password="postgres",
                           dbname="postgres", driver=None) as pg:
        host = pg.get_container_host_ip()
        port = pg.get_exposed_port(5432)
        yield f"postgresql://postgres:postgres@{host}:{port}/postgres"


# ----------------------------------------------------------------------------- Supabase (D55)
SUPABASE_IMAGE = "public.ecr.aws/supabase/postgres:17.6.1.043"
_INIT = "/docker-entrypoint-initdb.d/init-scripts"

# What a Supabase project has in its database that a database created by the harness lacks: the
# public-schema grants and default privileges of the image's 00000000000000-initial-schema.sql
# (run there as postgres and, `for user supabase_admin`, as supabase_admin), and the auth.uid()
# and auth.jwt() that GoTrue's migrations install over the image's older auth.uid().
SUPABASE_DATABASE_SQL = [
    "grant usage on schema public to postgres, anon, authenticated, service_role",
    *[f"alter default privileges for role {owner} in schema public grant all on {kind} "
      f"to postgres, anon, authenticated, service_role"
      for owner in ("postgres", "supabase_admin") for kind in ("tables", "functions", "sequences")],
]
GOTRUE_SQL = [
    """create or replace function auth.uid() returns uuid language sql stable as $$
         select coalesce(nullif(current_setting('request.jwt.claim.sub', true), ''),
                         (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'))::uuid $$""",
    """create or replace function auth.jwt() returns jsonb language sql stable as $$
         select coalesce(nullif(current_setting('request.jwt.claim', true), ''),
                         nullif(current_setting('request.jwt.claims', true), ''))::jsonb $$""",
    "alter function auth.uid() owner to supabase_auth_admin",
    "alter function auth.jwt() owner to supabase_auth_admin",
    "grant execute on function auth.uid(), auth.jwt() to anon, authenticated, service_role",
]
# the image's migrations/10000000000000_demote-postgres.sql: `postgres` keeps every privilege on
# the auth schema (a project's `profiles.id references auth.users` needs REFERENCES)
AUTH_GRANTS_SQL = [f"grant all on {what} to postgres" for what in
                   ("schema auth", "all tables in schema auth", "all sequences in schema auth",
                    "all routines in schema auth")]


class SupabaseServer:
    """A `supabase/postgres` container.  `dsn` is the `postgres` role a Supabase user deploys as
    (not a superuser; BYPASSRLS, CREATEROLE, CREATEDB); `admin_dsn` is `supabase_admin`, used only
    to give each new database what the platform gives a project's database (`prepare`)."""

    def __init__(self, container, host: str, port: int):
        self.container = container
        self.dsn = f"postgresql://postgres:postgres@{host}:{port}/postgres"
        self.admin_dsn = f"postgresql://supabase_admin:postgres@{host}:{port}/postgres"
        code, out = container.exec(["cat", f"{_INIT}/00000000000001-auth-schema.sql"])
        assert code == 0, out
        # its roles are cluster-wide and exist already: replay only the per-database part
        self.auth_schema_sql = "\n".join(line for line in out.decode().splitlines()
                                         if not line.upper().startswith(("CREATE USER", "ALTER USER")))

    def prepare(self, dbname: str) -> None:
        import psycopg

        with psycopg.connect(psycopg.conninfo.make_conninfo(self.admin_dsn, dbname=dbname), autocommit=True) as c:
            c.execute(self.auth_schema_sql)
            for s in SUPABASE_DATABASE_SQL + GOTRUE_SQL + AUTH_GRANTS_SQL:
                c.execute(s)


@contextmanager
def supabase_container(image: str = SUPABASE_IMAGE) -> Iterator[SupabaseServer]:
    import time

    import psycopg
    from testcontainers.core.container import DockerContainer

    with DockerContainer(image).with_env("POSTGRES_PASSWORD", "postgres").with_exposed_ports(5432) as c:
        host, port = c.get_container_host_ip(), int(c.get_exposed_port(5432))
        deadline = time.monotonic() + 180
        while True:  # the image restarts once after its init scripts; wait for the real server
            try:
                with psycopg.connect(f"postgresql://supabase_admin:postgres@{host}:{port}/postgres",
                                     connect_timeout=3) as conn:
                    if conn.execute("select count(*) from pg_namespace where nspname = 'auth'").fetchone()[0]:
                        break
            except psycopg.Error:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"{image} did not become ready")
            time.sleep(2)
        yield SupabaseServer(c, host, port)
