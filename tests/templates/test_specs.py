"""Every Postgres fixture builds and checks without a database.

The differential fixtures are derived from the example specs; an edit to an example can break a
fixture, and until this guard existed that showed only in the Postgres CI jobs (the must_allow
properties of D40 broke the pg_roles fixture that way).  `specs.make` runs the checker, so building
a fixture is the check.
"""
from __future__ import annotations

import inspect

import pytest

from mls.spec.model import Spec
from tests.templates import specs

BUILDERS = sorted(
    name for name, f in inspect.getmembers(specs, inspect.isfunction)
    if f.__module__ == specs.__name__ and (name.endswith("_spec") or name.startswith("v_"))
)


@pytest.mark.parametrize("name", BUILDERS)
def test_fixture_builds_and_checks(name):
    assert isinstance(getattr(specs, name)(), Spec)


def test_every_differential_case_is_guarded():
    from tests.templates.test_templates import CASES

    for build, _, _ in CASES.values():
        assert build.__name__ in BUILDERS, build.__name__
