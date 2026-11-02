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
