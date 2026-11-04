"""`in` with a constant list (ROADMAP Milestone 5): expanded by the checker, compiled as one IN
list, required as an index condition; no Postgres."""
from __future__ import annotations

import json

from pydantic import BaseModel, TypeAdapter

from mls.compile import Mutation, compile_spec
from mls.compile.sql import SqlCompiler
from mls.identity import get_adapter
from mls.pg.explain import index_atoms
from mls.props.effective import effective
from mls.spec import check_spec
from mls.spec.expand import as_in_list, expand_pred
from mls.spec.model import Cmp, In, Or, Pred, Spec
from tests.conftest import EXAMPLE
from tests.templates import specs

P = TypeAdapter(Pred)


def test_expansion_is_a_disjunction_of_equalities():
    p = expand_pred(P.validate_python({"in": [{"col": "level"}, [{"const": 2}, {"const": 3}]]}))
    assert p == P.validate_python({"or": [{"cmp": [{"col": "level"}, "=", {"const": 2}]},
                                          {"cmp": [{"col": "level"}, "=", {"const": 3}]}]})
    one = expand_pred(P.validate_python({"in": [{"col": "level"}, [{"const": 2}]]}))
    assert one == P.validate_python({"cmp": [{"col": "level"}, "=", {"const": 2}]})
    nested = expand_pred(P.validate_python({"not": {"and": [{"lit": True}, {"in": [{"ctx": "uid"}, [{"const": "a"}]]}]}}))
    assert nested == P.validate_python({"not": {"and": [{"lit": True}, {"cmp": [{"ctx": "uid"}, "=", {"const": "a"}]}]}})


def _walk(p, out):
    out.append(p)
    for name in type(p).model_fields:
        v = getattr(p, name)
        for x in (v if isinstance(v, (list, tuple)) else [v]):
            if isinstance(x, BaseModel):
                _walk(x, out)
    return out


def test_check_spec_returns_the_expanded_spec():
    spec = specs.in_spec()
    nodes = _walk(spec, [])
    assert not any(isinstance(n, In) for n in nodes)
    using = spec.rule("R10").using
    assert isinstance(using.and_[1], Or) and all(isinstance(c, Cmp) for c in using.and_[1].or_)
    # `in` is accepted wherever a constant comparison is: a check rule, a must_allow condition
    d = json.loads(EXAMPLE.read_text())
    d["rules"].append({"id": "C9", "kind": "check", "name": "level_set", "table": "docs",
                       "predicate": {"in": [{"col": "level"}, [{"const": 0}, {"const": 1}]]}})
    d["properties"].append({"id": "P99", "template": "must_allow", "table": "docs", "role": "authenticated",
                            "command": "select", "when": {"in": [{"col": "level"}, [{"const": 2}]]}})
    spec = check_spec(Spec.model_validate(d))
    assert isinstance(spec.rule("C9").predicate, Or)
    assert isinstance(spec.properties[-1].when, Cmp)


def test_in_list_shape():
    assert as_in_list(P.validate_python({"or": [{"cmp": [{"col": "a"}, "=", {"const": 1}]},
                                                {"cmp": [{"col": "a"}, "=", {"const": 2}]}]})) is not None
    for d in [{"or": [{"cmp": [{"col": "a"}, "=", {"const": 1}]}]},                       # one disjunct
              {"or": [{"cmp": [{"col": "a"}, "=", {"const": 1}]}, {"cmp": [{"col": "b"}, "=", {"const": 2}]}]},
              {"or": [{"cmp": [{"col": "a"}, "=", {"const": 1}]}, {"cmp": [{"col": "a"}, "<>", {"const": 2}]}]},
              {"or": [{"cmp": [{"col": "a"}, "=", {"const": 1}]}, {"cmp": [{"col": "a"}, "=", {"ctx": "uid"}]}]},
              {"or": [{"cmp": [{"const": 1}, "=", {"const": 1}]}, {"cmp": [{"const": 1}, "=", {"const": 2}]}]},
              {"or": [{"cmp": [{"col": "a"}, "=", {"const": 1}]}, {"is_null": {"col": "a"}}]}]:
        assert as_in_list(P.validate_python(d)) is None, d


def test_compiled_as_one_in_list():
    spec = specs.in_spec()
    sc = SqlCompiler(spec, get_adapter(spec))
    assert sc.pred(spec.rule("R10").using, "docs").endswith('and ("level" in (2, 3)))')
    text = compile_spec(spec).sql()
    assert '("level" in (2, 3))' in text and '"level" = 2' not in text
    mutated = compile_spec(spec, {Mutation.IN_DROP_LAST}).sql()
    assert '("level" in (2))' in mutated
    # uuid constants keep their cast; a hand-written disjunction of the same shape is the same SQL
    d = json.loads(EXAMPLE.read_text())
    d["rules"].append({"id": "R11", "kind": "policy", "name": "two_owners", "table": "docs",
                       "role": "authenticated", "command": "select", "mode": "permissive",
                       "using": {"or": [{"cmp": [{"col": "owner_id"}, "=", {"const": "00000000-0000-0000-0000-000000000001"}]},
                                        {"cmp": [{"col": "owner_id"}, "=", {"const": "00000000-0000-0000-0000-000000000002"}]}]}})
    spec2 = check_spec(Spec.model_validate(d))
    assert SqlCompiler(spec2, get_adapter(spec2)).pred(spec2.rule("R11").using, "docs") == \
        '("owner_id" in (\'00000000-0000-0000-0000-000000000001\'::uuid, \'00000000-0000-0000-0000-000000000002\'::uuid))'


def test_in_list_is_a_required_index_atom():
    spec = specs.in_spec()
    atoms = index_atoms(effective(spec, "docs", "authenticated", "select", "using"), [])
    assert ("in", "level") in atoms and ("cmp", "level") in atoms and ("member", "tenant_id") in atoms
    names = {f"{t}({', '.join(c)})" for _, t, c in __import__("mls.compile.index", fromlist=["index_specs"]).index_specs(spec)}
    assert "docs(tenant_id, level)" in names
