"""Shared fixtures.  Postgres tests need Docker; set MLS_PG_VERSIONS=16 to test one version,
MLS_PG_DSN=postgresql://postgres:...@host:port/postgres to use an existing superuser server, or
MLS_SKIP_PG=1 to skip them."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mls.pg.server import PG_VERSIONS

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "tenant_docs.mls.json"
ORG_TREE = ROOT / "examples" / "org_tree.mls.json"

_versions = os.environ.get("MLS_PG_VERSIONS", ",".join(PG_VERSIONS)).split(",")
_dsn = os.environ.get("MLS_PG_DSN")
_params = ["dsn"] if _dsn else _versions


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="MLS_SKIP_PG set")
    for item in items:
        if "pg" in item.keywords and os.environ.get("MLS_SKIP_PG"):
            item.add_marker(skip)


@pytest.fixture(scope="session", params=_params, ids=lambda v: f"pg{v}")
def pg_dsn(request):
    """Superuser DSN of a Postgres server (one container per major version)."""
    if _dsn:
        yield _dsn
        return
    from mls.pg.server import postgres_container

    with postgres_container(request.param) as dsn:
        yield dsn


@pytest.fixture(scope="session")
def example_spec():
    from mls.spec import check_spec, load_spec

    spec = load_spec(EXAMPLE)
    spec = check_spec(spec)
    return spec


@pytest.fixture(scope="session")
def org_spec():
    from mls.spec import check_spec, load_spec

    spec = load_spec(ORG_TREE)
    spec = check_spec(spec)
    return spec
