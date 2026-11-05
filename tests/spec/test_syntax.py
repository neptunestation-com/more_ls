"""The terse parser (D50): hypothesis-generated specs round-trip through render and parse,
syntax errors carry file:line:col and the offending line, sections may come in any order,
comments and quoting work, and the JSON Schema is untouched by the parser."""
from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mls.errors import SpecError
from mls.spec import load_spec, spec_json_schema
from mls.spec.model import Spec
from mls.spec.syntax import parse, parse_data, tokenize
from mls.terse import RESERVED, render
from tests.conftest import EXAMPLE

# ----------------------------------------------------------------------------- a generator
NAMES = st.sampled_from(["id", "tenant_id", "level", "owner_id", "archived", "check", "from", "select", "two words",
                         'say "hi"', "parent", "depth", "members", "max", "all", "Ünïcode"])
TYPES = st.sampled_from(["int", "text", "bool", "timestamp", "uuid"])
CMDS = st.sampled_from(["select", "insert", "update", "delete"])
TEXT = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=12)


def terms(images: bool):
    consts = st.one_of(st.integers(-9, 9), st.booleans(), TEXT.map(lambda s: s))
    out = [NAMES.map(lambda c: {"col": c}), NAMES.map(lambda c: {"ctx": c}), consts.map(lambda v: {"const": v})]
    if images:
        out += [NAMES.map(lambda c: {"old": c}), NAMES.map(lambda c: {"new": c})]
    return st.one_of(*out)


def preds(images: bool = False):
    t = terms(images)
    atoms = st.one_of(
        st.booleans().map(lambda b: {"lit": b}),
        st.tuples(t, st.sampled_from(["=", "<>", "<", "<=", ">", ">="]), t).map(lambda x: {"cmp": list(x)}),
        st.tuples(t, st.lists(st.one_of(st.integers(-3, 3), TEXT).map(lambda v: {"const": v}), min_size=1, max_size=3))
        .map(lambda x: {"in": list(x)}),
        t.map(lambda x: {"is_null": x}),
        st.tuples(NAMES, t, t).map(lambda x: {"member": {"relation": x[0], "key": x[1], "value": x[2]}}),
        st.tuples(NAMES, t, t).map(lambda x: {"in_group": {"hierarchy": x[0], "key": x[1], "value": x[2]}}),
        st.tuples(st.none() | t, st.none() | t).map(
            lambda x: {"now_between": {k: v for k, v in (("from", x[0]), ("until", x[1])) if v is not None}}),
        NAMES.map(lambda n: {"opaque": n}),
    )
    return st.recursive(atoms, lambda sub: st.one_of(
        st.lists(sub, min_size=2, max_size=3).map(lambda xs: {"and": xs}),
        st.lists(sub, min_size=2, max_size=3).map(lambda xs: {"or": xs}),
        sub.map(lambda x: {"not": x})), max_leaves=6)


SELECTORS = st.recursive(
    st.one_of(st.tuples(NAMES, st.none() | TYPES).map(lambda x: {"has_column": [x[0]] + ([x[1]] if x[1] else [])}),
              st.lists(NAMES, min_size=1, max_size=2).map(lambda xs: {"named": xs}),
              NAMES.map(lambda n: {"tagged": n}), st.just({"all": True})),
    lambda sub: st.one_of(st.lists(sub, min_size=1, max_size=2).map(lambda xs: {"and": xs}),
                          st.lists(sub, min_size=1, max_size=2).map(lambda xs: {"or": xs})), max_leaves=4)


def rules():
    head = st.fixed_dictionaries({"id": st.sampled_from(["R1", "R2.docs", "rule", "x_1"]), "name": NAMES},
                                 optional={"requirement": TEXT})
    where = st.one_of(NAMES.map(lambda t: {"table": t}), SELECTORS.map(lambda s: {"applies_to": s}),
                      st.tuples(NAMES, SELECTORS).map(lambda x: {"table": x[0], "from": "R0"}))
    body = st.one_of(
        st.fixed_dictionaries({"kind": st.just("policy"), "role": NAMES,
                               "command": st.sampled_from(["select", "insert", "update", "delete", "all"]),
                               "mode": st.sampled_from(["permissive", "restrictive"])},
                              optional={"using": preds(), "check": preds()}),
        st.fixed_dictionaries({"kind": st.just("check"), "predicate": preds()}),
        st.fixed_dictionaries({"kind": st.just("transition"), "predicate": preds(images=True)}),
        st.fixed_dictionaries({"kind": st.just("inclusion"), "columns": st.tuples(NAMES, NAMES).map(list),
                               "relation": NAMES, "on_delete": st.just("restrict")}),
        st.fixed_dictionaries({"kind": st.just("cardinality"), "group": NAMES, "parent": NAMES,
                               "max": st.integers(1, 99)}),
        st.fixed_dictionaries({"kind": st.just("column_rule"), "role": NAMES,
                               "columns": st.lists(NAMES, min_size=1, max_size=3),
                               "access": st.sampled_from(["read", "write"])}),
    )
    return st.tuples(head, where, body).map(lambda x: {**x[0], **x[1], **x[2]})


def properties():
    d = st.fixed_dictionaries({"id": st.sampled_from(["P1", "P2", "prop"])}, optional={"description": TEXT})
    body = st.one_of(
        st.fixed_dictionaries({"template": st.just("tenant_isolation"), "table": NAMES, "column": NAMES,
                               "relation": NAMES, "key": terms(False), "commands": st.lists(CMDS, min_size=1, max_size=2)}),
        st.fixed_dictionaries({"template": st.just("column_immutable"), "table": NAMES, "column": NAMES}),
        st.fixed_dictionaries({"template": st.just("monotone_in"), "relation": NAMES}),
        st.fixed_dictionaries({"template": st.just("write_subset_read"), "table": NAMES}),
        st.fixed_dictionaries({"template": st.just("least_privilege"),
                               "allowed": st.dictionaries(NAMES, st.dictionaries(NAMES, st.lists(CMDS, max_size=2),
                                                                                 max_size=2), max_size=2)}),
        st.fixed_dictionaries({"template": st.just("invariant_preserved"), "invariants": st.lists(st.one_of(
            st.sampled_from(["R1", "R2.docs"]).map(lambda r: {"rule": r}),
            st.tuples(NAMES, preds()).map(lambda x: {"table": x[0], "predicate": x[1]})), min_size=1, max_size=3)}),
        st.fixed_dictionaries({"template": st.just("custom"), "table": NAMES, "role": NAMES, "command": CMDS,
                               "which": st.sampled_from(["old", "new"]), "goal": preds(images=True)}),
        st.fixed_dictionaries({"template": st.just("must_allow"), "table": NAMES, "role": NAMES, "command": CMDS,
                               "when": preds(images=True)}),
        st.fixed_dictionaries({"template": st.just("role_separation"), "table": NAMES, "command": CMDS,
                               "roles": st.just(["a", "b"])}),
    )
    return st.tuples(d, body).map(lambda x: {**x[0], **x[1]})


TABLES = st.fixed_dictionaries(
    {"columns": st.dictionaries(NAMES, st.fixed_dictionaries({"type": TYPES, "nullable": st.booleans()}), max_size=4),
     "mode": st.sampled_from(["owned", "adopted", "observed"])},
    optional={"primary_key": st.lists(NAMES, min_size=1, max_size=2), "unique": st.lists(st.lists(NAMES, min_size=1, max_size=2), max_size=2),
              "tags": st.lists(NAMES, max_size=2),
              "assume": st.lists(st.one_of(NAMES.map(lambda r: {"owner": r}), NAMES.map(lambda r: {"bypassrls": r}),
                                           st.just({"rls": "not_forced"})), max_size=2)})

SPECS = st.fixed_dictionaries(
    {"mls_version": st.just("0.1"),
     "identity": st.fixed_dictionaries({"adapter": st.sampled_from(["supabase", "pg_roles", "session_setting"]),
                                        "context": st.dictionaries(NAMES, st.fixed_dictionaries(
                                            {"source": st.sampled_from(["auth.uid", "jwt.claims.org_id", "current_user", "app.t"]),
                                             "type": TYPES}), max_size=2)}),
     "roles": st.lists(NAMES, min_size=1, max_size=2),
     "tables": st.dictionaries(NAMES, TABLES, max_size=3),
     "relations": st.dictionaries(NAMES, st.fixed_dictionaries({"table": NAMES, "key": NAMES, "value": NAMES}), max_size=2),
     "hierarchies": st.dictionaries(NAMES, st.fixed_dictionaries(
         {"table": NAMES, "id": NAMES, "parent": NAMES, "depth": NAMES, "max_depth": st.integers(1, 16),
          "members": NAMES}), max_size=1),
     "opaques": st.dictionaries(NAMES, st.fixed_dictionaries(
         {"table": NAMES, "columns": st.lists(NAMES, max_size=2), "context": st.lists(NAMES, max_size=2),
          "relations": st.lists(NAMES, max_size=2), "sql": st.one_of(TEXT, st.just("x $$ y"), st.just("$mls$ z"))}),
         max_size=2),
     "rules": st.lists(rules(), max_size=3),
     "properties": st.lists(properties(), max_size=3)},
    optional={"$comment": TEXT})


@given(SPECS)
@settings(max_examples=150, deadline=None)
def test_generated_specs_round_trip(data):
    spec = Spec.model_validate(data)
    text = render(spec)
    back = parse(text)
    assert back == spec
    assert render(back) == text


def test_reserved_words_are_the_grammars():
    """Every keyword the parser dispatches on is reserved, so a name spelled like one is quoted
    and reads back as a name."""
    from mls.spec import syntax

    words = set(syntax.KINDS) | set(syntax.TEMPLATES) | set(syntax.COMMANDS) | set(syntax.MODES) | \
        set(syntax.TABLE_MODES) | {"mls", "comment", "identity", "roles", "table", "relation", "hierarchy", "opaque",
                                   "rules", "properties", "on", "applies_to", "from", "requirement", "description",
                                   "using", "check", "and", "or", "not", "is", "null", "true", "false", "in", "in_group",
                                   "now_between", "old", "new", "for", "max", "on_delete", "restrict", "max_depth",
                                   "primary_key", "unique", "tags", "assume", "sql", "relations", "has_column",
                                   "named", "tagged", "all", "read", "write"}
    assert words <= RESERVED


# ----------------------------------------------------------------------------- the text side
TERSE = """
# a comment line
rules {
  R1 policy member_read on docs: authenticated select permissive using "check" = 1 and owner_id = @uid  # trailing
}
roles authenticated, "select"
mls 0.1
table docs owned { id int not null, "check" int, owner_id uuid; primary_key (id) }
identity supabase { uid: auth.uid uuid }
properties { }
comment 'it''s fine'
"""


def test_sections_in_any_order_comments_and_quoting():
    spec = parse(TERSE)
    assert spec.roles == ["authenticated", "select"] and spec.comment == "it's fine"
    assert list(spec.tables["docs"].columns) == ["id", "check", "owner_id"]
    assert spec.rules[0].using.and_[0].cmp[0].col == "check"
    assert "R1 policy member_read on docs: authenticated select permissive using \"check\" = 1 and owner_id = @uid" \
        in render(spec)


def test_tokens():
    toks = tokenize("a \"b\"\"c\" 'd''e' 12 -> <> $$ x $$ $t$ y $$ $t$ # c\n@")
    assert [(t.kind, t.text) for t in toks][:-1] == [
        ("ident", "a"), ("qident", 'b"c'), ("str", "d'e"), ("num", "12"), ("op", "->"), ("op", "<>"),
        ("dollar", "x"), ("dollar", "y $$"), ("op", "@")]
    assert toks[-1].kind == "eof" and toks[-1].line == 2


@pytest.mark.parametrize("text,where,msg", [
    ("mls 0.1\nroles a\nrules {\n  R1 policy p on t: a select permissive using id = = 1\n}", "4:52", "expected a term"),
    ("mls 0.1\nroles a\ntable t owned { id int not null; primary_key (id }", "3:50", "expected ')'"),
    ("mls 0.1\nroles a\nrules {\n  R1 policy p on t: a select permissive using opaque(x) requirement 1\n}", "4:69", "expected a 'string'"),
    ("mls 0.1\nmls 0.1", "2:1", "'mls' appears twice"),
    ("mls 0.1\nroles a\ntable t owned { x int; assume ('something odd') }", "3:", "an assumption is"),
    ("mls 0.1\nopaque o on t (a) relations () sql $$ never closed", "2:36", "unterminated"),
    ("mls 0.1\nrules {\n  R1 policy p on t: a select permissive using level = 1.5\n}", "3:55", "no decimal"),
    ("mls 0.1\nbogus x", "2:1", "expected a section"),
])
def test_syntax_errors_name_the_place(text, where, msg, tmp_path):
    p = tmp_path / "s.mls"
    p.write_text(text)
    with pytest.raises(SpecError) as ei:
        load_spec(p)
    e = ei.value.errors[0]
    assert e.code == "syntax" and msg in e.message and e.path.startswith("$")
    assert f"{p}:{where}" in str(e.span) and e.span.text == text.split("\n")[e.span.line - 1]
    assert f"({p}:{where}" in str(e) and f"\n    {e.span.text.strip()}\n" in str(e)  # the offending line
    assert e.to_dict()["span"]["line"] == e.span.line


def test_schema_errors_keep_the_json_path(tmp_path):
    p = tmp_path / "s.mls"
    p.write_text("mls 0.1\nidentity supabase { }\nroles a\ntable t owned { }\nrules {\n  R1 cardinality c on t: g -> p max 0\n}\n"
                 "properties { }")
    with pytest.raises(SpecError) as ei:
        load_spec(p)
    j = tmp_path / "s.mls.json"
    j.write_text(json.dumps({"mls_version": "0.1", "identity": {"adapter": "supabase", "context": {}}, "roles": ["a"],
                             "tables": {"t": {"columns": {}}}, "rules": [{"id": "R1", "kind": "cardinality", "name": "c", "table": "t", "group": "g",
                                        "parent": "p", "max": 0}], "properties": []}))
    with pytest.raises(SpecError) as ej:
        load_spec(j)
    et, ej = ei.value.errors[0], ej.value.errors[0]
    assert (et.path, et.message, et.fix, et.code) == (ej.path, ej.message, ej.fix, ej.code)  # the model's error
    assert et.path == "$.rules[0].max" and ej.span is None
    assert et.span.line == 6 and et.span.text.strip().startswith("R1 cardinality")  # located at the rule
    d = parse_data("mls 0.1\nroles a\nrules {\n  R1 check c on t: x = 1\n}")
    assert d.spans["$.rules[0]"] == (4, 3) and d.spans["$.rules[0].predicate"] == (4, 20)
    assert d.spans["$.roles"] == (2, 1)


def test_json_syntax_errors_name_the_place_too(tmp_path):
    p = tmp_path / "s.mls.json"
    p.write_text('{"mls_version": "0.1",')
    with pytest.raises(SpecError) as ei:
        load_spec(p)
    e = ei.value.errors[0]
    assert e.code == "syntax" and f"{p}:1:" in str(e.span) and e.span.text == '{"mls_version": "0.1",'


def test_check_errors_on_terse_input_carry_spans(tmp_path):
    """#15: a checker error from a `.mls` input has the JSON path, `file:line:col` at the node
    and the offending line; the same spec as JSON has the same path and message and no span."""
    from mls.llm.prompts import errors_text
    from mls.spec import load_checked, locate, spec_to_json

    text = EXAMPLE.with_suffix("").read_text().replace("level <= @clearance", "nope <= @clearance", 1)
    p = tmp_path / "s.mls"
    p.write_text(text)
    with pytest.raises(SpecError) as ei:
        load_checked(p)
    e = ei.value.errors[0]
    assert e.path == "$.rules[0].using.and[1].cmp[0]" and "unknown column 'nope'" in e.message
    assert e.span.file == str(p) and e.span.text == text.split("\n")[e.span.line - 1]
    assert e.span.text[e.span.col - 1:].startswith("nope <= @clearance")  # the atom, not just the rule
    assert str(e).startswith(f"error at {e.path} ({p}:{e.span.line}:{e.span.col}): unknown column")
    assert errors_text([e]).startswith(f"- at {e.path} ({e.span.line}:{e.span.col}: R1 policy member_read")
    j = tmp_path / "s.mls.json"
    j.write_text(spec_to_json(parse(text)))
    with pytest.raises(SpecError) as ej:
        load_checked(j)
    f = ej.value.errors[0]
    assert (f.path, f.message, f.fix, f.code) == (e.path, e.message, e.fix, e.code) and f.span is None
    assert errors_text([f]) == f"- at {f.path}: {f.message} (fix: {f.fix})"
    assert locate(j, [f])[0].span is None  # JSON input: nothing to locate
    assert locate(p, [MlsErrorForTest("$.roles")])[0].span.text == "roles authenticated"  # any path the source knows


class MlsErrorForTest:
    def __init__(self, path):
        self.path, self.span = path, None


def test_the_schema_is_untouched():
    """The parser adds nothing to the model: `mls schema` is the model's schema, with no span or
    syntax field anywhere (D50: JSON stays canonical, the grammar is not the language)."""
    schema = spec_json_schema()
    assert schema == Spec.model_json_schema()
    text = json.dumps(schema).lower()
    assert "span" not in text and '"line"' not in text and "syntax" not in text and "tok" not in schema["$defs"]
    assert set(schema["properties"]) == {"$comment", "mls_version", "identity", "roles", "tables", "relations",
                                         "hierarchies", "opaques", "rules", "properties"}


def test_the_example_twins_agree():
    spec = load_spec(EXAMPLE)
    twin = load_spec(EXAMPLE.with_suffix(""))  # tenant_docs.mls
    assert twin == spec
