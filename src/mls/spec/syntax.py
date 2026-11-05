"""The terse syntax as an input (D50): a recursive-descent parser over the grammar of
`mls/terse.py` into the JSON shape of the model, with a source span per node.

`parse(text)` returns the validated `Spec`; `parse_data(text)` the JSON-shaped dict and the
spans (`{json_path: (line, col)}`) the checker can quote (D50, source-span errors).  The
grammar is not the definition of the language: the model is, and this parser only spells its
JSON.  Round trip is the evidence: `parse(render(spec)) == spec` (tests/test_terse.py,
tests/spec/test_syntax.py).  Sections may come in any order; each of `identity`, `roles`,
`rules`, `properties` at most once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from mls.errors import MlsError, SpecError
from mls.spec.model import Spec

KINDS = ("policy", "check", "inclusion", "transition", "cardinality", "column_rule")
TEMPLATES = ("tenant_isolation", "column_immutable", "monotone_in", "write_subset_read", "least_privilege",
             "invariant_preserved", "custom", "must_allow", "role_separation")
COMMANDS = ("select", "insert", "update", "delete", "all")
MODES = ("permissive", "restrictive")
TYPES = ("int", "text", "bool", "timestamp", "uuid")
TABLE_MODES = ("owned", "adopted", "observed")
CMP_OPS = ("=", "<>", "<", "<=", ">", ">=")


@dataclass
class Tok:
    kind: str   # ident | qident | str | num | dollar | op | eof
    text: str   # the source text (quotes stripped for qident/str; the SQL for dollar)
    line: int
    col: int


_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>\#[^\n]*)
  | (?P<dollar>\$(?P<tag>[A-Za-z0-9_]*)\$)
  | (?P<qident>"(?:[^"]|"")*")
  | (?P<str>'(?:[^']|'')*')
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>->|<>|<=|>=|[=<>(){}\[\],;:.@|\-])
""", re.VERBOSE)


class ParseError(Exception):
    def __init__(self, message: str, line: int, col: int, path: str):
        super().__init__(message)
        self.message, self.line, self.col, self.path = message, line, col, path


def tokenize(text: str) -> list[Tok]:
    out: list[Tok] = []
    pos, line, line_start = 0, 1, 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None:
            raise ParseError(f"unexpected character {text[pos]!r}", line, pos - line_start + 1, "$")
        kind = m.lastgroup if m.lastgroup not in ("tag",) else "dollar"
        if m.group("dollar") is not None:
            kind = "dollar"
        s = m.group(kind) if kind in ("ws", "comment", "qident", "str", "num", "ident", "op") else m.group("dollar")
        col = pos - line_start + 1
        if kind == "dollar":
            close = text.find(s, m.end())
            if close < 0:
                raise ParseError(f"unterminated {s} ... {s}", line, col, "$")
            body = text[m.end():close]
            if body.startswith(" "):
                body = body[1:]
            if body.endswith(" "):
                body = body[:-1]
            out.append(Tok("dollar", body, line, col))
            consumed = text[pos:close + len(s)]
            pos = close + len(s)
        else:
            consumed = s
            pos = m.end()
            if kind == "qident":
                out.append(Tok("qident", s[1:-1].replace('""', '"'), line, col))
            elif kind == "str":
                out.append(Tok("str", s[1:-1].replace("''", "'"), line, col))
            elif kind not in ("ws", "comment"):
                out.append(Tok(kind, s, line, col))
        nl = consumed.count("\n")
        if nl:
            line += nl
            line_start = pos - (len(consumed) - consumed.rfind("\n") - 1)
    out.append(Tok("eof", "", line, pos - line_start + 1))
    return out


@dataclass
class Parsed:
    data: dict[str, Any]
    spans: dict[str, tuple[int, int]] = field(default_factory=dict)


class Parser:
    def __init__(self, text: str):
        self.toks = tokenize(text)
        self.i = 0
        self.spans: dict[str, tuple[int, int]] = {}
        self.path = "$"

    # ------------------------------------------------------------------ token helpers
    @property
    def tok(self) -> Tok:
        return self.toks[self.i]

    def peek(self, k: int = 1) -> Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def fail(self, expected: str, tok: Tok | None = None) -> ParseError:
        t = tok or self.tok
        found = "end of input" if t.kind == "eof" else repr(t.text)
        return ParseError(f"expected {expected}, found {found}", t.line, t.col, self.path)

    def at(self, text: str, kind: str = "op") -> bool:
        return self.tok.kind == kind and self.tok.text == text

    def at_word(self, *words: str) -> bool:
        return self.tok.kind == "ident" and self.tok.text in words

    def eat(self, text: str, kind: str = "op") -> Tok:
        if not self.at(text, kind):
            raise self.fail(repr(text))
        return self.advance()

    def word(self, *words: str) -> str:
        if not self.at_word(*words):
            raise self.fail(" or ".join(repr(w) for w in words))
        return self.advance().text

    def advance(self) -> Tok:
        t = self.tok
        self.i += 1
        return t

    def mark(self, path: str) -> None:
        self.spans[path] = (self.tok.line, self.tok.col)

    def name(self, what: str = "a name") -> str:
        """A bare identifier or a "quoted" one."""
        if self.tok.kind in ("ident", "qident"):
            return self.advance().text
        raise self.fail(what)

    def dotted(self, what: str = "a name") -> str:
        """ident ('.' ident)*, as rule ids (`R2.docs`) and identity sources (`auth.uid`) are."""
        if self.tok.kind == "qident":
            return self.advance().text
        if self.tok.kind != "ident":
            raise self.fail(what)
        s = self.advance().text
        while self.at(".") and self.peek().kind == "ident":
            self.advance()
            s += "." + self.advance().text
        return s

    def string(self, what: str = "a 'string'") -> str:
        if self.tok.kind != "str":
            raise self.fail(what)
        return self.advance().text

    def integer(self, what: str = "a number") -> int:
        if self.tok.kind != "num" or "." in self.tok.text:
            raise self.fail(what)
        return int(self.advance().text)

    def names(self, close: str = ")") -> list[str]:
        out: list[str] = []
        if self.at(close):
            return out
        out.append(self.name())
        while self.at(","):
            self.advance()
            out.append(self.name())
        return out

    # ------------------------------------------------------------------ spec
    def spec(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        tables: dict[str, Any] = {}
        relations: dict[str, Any] = {}
        hierarchies: dict[str, Any] = {}
        opaques: dict[str, Any] = {}
        seen: set[str] = set()
        while self.tok.kind != "eof":
            if self.tok.kind != "ident":
                raise self.fail("a section (mls, comment, identity, roles, table, relation, hierarchy, opaque, "
                                "rules, properties)")
            w = self.tok.text
            if w in ("mls", "comment", "identity", "roles", "rules", "properties") and w in seen:
                raise ParseError(f"'{w}' appears twice", self.tok.line, self.tok.col, "$")
            seen.add(w)
            if w == "mls":
                self.path = "$.mls_version"
                self.mark(self.path)
                self.advance()
                if self.tok.kind != "num":
                    raise self.fail("the version (0.1)")
                d["mls_version"] = self.advance().text
            elif w == "comment":
                self.path = "$.$comment"
                self.mark(self.path)
                self.advance()
                d["$comment"] = self.string()
            elif w == "identity":
                d["identity"] = self.identity()
            elif w == "roles":
                self.path = "$.roles"
                self.mark(self.path)
                self.advance()
                roles = [self.name("a role")]
                while self.at(","):
                    self.advance()
                    roles.append(self.name("a role"))
                d["roles"] = roles
            elif w == "table":
                name, t = self.table()
                tables[name] = t
            elif w == "relation":
                name, r = self.relation()
                relations[name] = r
            elif w == "hierarchy":
                name, h = self.hierarchy()
                hierarchies[name] = h
            elif w == "opaque":
                name, o = self.opaque()
                opaques[name] = o
            elif w == "rules":
                d["rules"] = self.rules()
            elif w == "properties":
                d["properties"] = self.properties()
            else:
                raise self.fail("a section (mls, comment, identity, roles, table, relation, hierarchy, opaque, "
                                "rules, properties)")
        self.path = "$"
        d["tables"] = tables
        if relations:
            d["relations"] = relations
        if hierarchies:
            d["hierarchies"] = hierarchies
        if opaques:
            d["opaques"] = opaques
        return d

    def identity(self) -> dict[str, Any]:
        self.path = "$.identity"
        self.mark(self.path)
        self.advance()
        adapter = self.name("an adapter")
        self.eat("{")
        ctx: dict[str, Any] = {}
        while not self.at("}"):
            n = self.name("a context value name")
            self.mark(f"$.identity.context.{n}")
            self.eat(":")
            source = self.dotted("a source (auth.uid, jwt.claims.<name>, current_user, app.<name>)")
            ctx[n] = {"source": source, "type": self.word(*TYPES)}
            if not self.at("}"):
                self.eat(";")
        self.eat("}")
        return {"adapter": adapter, "context": ctx}

    def table(self) -> tuple[str, dict[str, Any]]:
        self.advance()
        name = self.name("a table name")
        self.path = f"$.tables.{name}"
        self.mark(self.path)
        t: dict[str, Any] = {"columns": {}, "mode": self.word(*TABLE_MODES)}
        self.eat("{")
        options = ("primary_key", "unique", "tags", "assume")
        while not self.at("}") and not self.at(";") and not self.at_word(*options):
            c = self.name("a column name")
            self.mark(f"{self.path}.columns.{c}")
            col = {"type": self.word(*TYPES), "nullable": True}
            if self.at_word("not"):
                self.advance()
                self.word("null")
                col["nullable"] = False
            t["columns"][c] = col
            if self.at(","):
                self.advance()
            elif not (self.at("}") or self.at(";")):
                raise self.fail("',' or ';' or '}'")
        while self.at(";") or self.at_word(*options):
            if self.at(";"):
                self.advance()
            opt = self.word(*options)
            self.mark(f"{self.path}.{opt}")
            self.eat("(")
            if opt == "assume":
                items = [self.string("an assumption ('owner r', 'bypassrls r', 'rls not_forced')")]
                while self.at(","):
                    self.advance()
                    items.append(self.string())
                t["assume"] = []
                for s in items:
                    parts = s.split(" ", 1)
                    if len(parts) != 2 or parts[0] not in ("owner", "bypassrls", "rls") or not parts[1]:
                        raise ParseError(f"an assumption is 'owner <role>', 'bypassrls <role>' or 'rls not_forced', "
                                         f"not {s!r}", self.tok.line, self.tok.col, f"{self.path}.assume")
                    t["assume"].append({parts[0]: parts[1]})
            elif opt == "unique":
                t.setdefault("unique", []).append(self.names())
            else:
                t[opt] = self.names()
            self.eat(")")
        self.eat("}")
        return name, t

    def relation(self) -> tuple[str, dict[str, Any]]:
        self.advance()
        name = self.name("a relation name")
        self.path = f"$.relations.{name}"
        self.mark(self.path)
        self.eat("=")
        table = self.name("a table")
        self.eat("(")
        key = self.name("the key column")
        self.eat(",")
        value = self.name("the value column")
        self.eat(")")
        return name, {"table": table, "key": key, "value": value}

    def hierarchy(self) -> tuple[str, dict[str, Any]]:
        self.advance()
        name = self.name("a hierarchy name")
        self.path = f"$.hierarchies.{name}"
        self.mark(self.path)
        self.eat("=")
        h: dict[str, Any] = {"table": self.name("a table")}
        for kw in ("id", "parent", "depth"):
            self.word(kw)
            h[kw] = self.name(f"the {kw} column")
        self.word("max_depth")
        h["max_depth"] = self.integer()
        self.word("members")
        h["members"] = self.name("a relation")
        return name, h

    def opaque(self) -> tuple[str, dict[str, Any]]:
        self.advance()
        name = self.name("an opaque block name")
        self.path = f"$.opaques.{name}"
        self.mark(self.path)
        self.word("on")
        o: dict[str, Any] = {"table": self.name("a table"), "columns": [], "context": []}
        self.eat("(")
        while not self.at(")"):
            if self.at("@"):
                self.advance()
                o["context"].append(self.name("a context value"))
            else:
                o["columns"].append(self.name("a column or @context"))
            if self.at(","):
                self.advance()
            elif not self.at(")"):
                raise self.fail("',' or ')'")
        self.eat(")")
        self.word("relations")
        self.eat("(")
        o["relations"] = self.names()
        self.eat(")")
        self.word("sql")
        if self.tok.kind != "dollar":
            raise self.fail("$$ sql $$")
        o["sql"] = self.advance().text
        return name, o

    # ------------------------------------------------------------------ rules
    def rules(self) -> list[dict[str, Any]]:
        self.advance()
        self.eat("{")
        out: list[dict[str, Any]] = []
        while not self.at("}"):
            self.path = f"$.rules[{len(out)}]"
            self.mark(self.path)
            out.append(self.rule())
        self.eat("}")
        return out

    def rule(self) -> dict[str, Any]:
        base = self.path
        r: dict[str, Any] = {"id": self.dotted("a rule id"), "kind": self.word(*KINDS)}
        if not self.at_word("on", "applies_to"):
            r["name"] = self.name("the rule's name, 'on' or 'applies_to'")
        if self.at_word("on"):
            self.advance()
            r["table"] = self.name("a table")
        else:
            self.word("applies_to")
            self.mark(f"{base}.applies_to")
            r["applies_to"] = self.selector()
        if self.at_word("from"):
            self.advance()
            r["from"] = self.dotted("the selector rule's id")
        self.eat(":")
        kind = r["kind"]
        if kind == "policy":
            r["role"] = self.name("a role")
            r["command"] = self.word(*COMMANDS)
            r["mode"] = self.word(*MODES)
            if self.at_word("using"):
                self.advance()
                self.mark(f"{base}.using")
                r["using"] = self.pred(f"{base}.using")
            if self.at_word("check"):
                self.advance()
                self.mark(f"{base}.check")
                r["check"] = self.pred(f"{base}.check")
        elif kind in ("check", "transition"):
            self.mark(f"{base}.predicate")
            r["predicate"] = self.pred(f"{base}.predicate")
        elif kind == "inclusion":
            self.eat("(")
            c1 = self.name("a column")
            self.eat(",")
            c2 = self.name("a column")
            self.eat(")")
            self.word("in")
            r["columns"] = [c1, c2]
            r["relation"] = self.name("a relation")
            self.word("on_delete")
            r["on_delete"] = self.word("restrict")
        elif kind == "cardinality":
            r["group"] = self.name("the group column")
            self.eat("->")
            r["parent"] = self.name("the parent table")
            self.word("max")
            r["max"] = self.integer()
        else:  # column_rule
            r["role"] = self.name("a role")
            r["access"] = self.word("read", "write")
            r["columns"] = [self.name("a column")]
            while self.at(","):
                self.advance()
                r["columns"].append(self.name("a column"))
        if self.at_word("requirement"):
            self.advance()
            r["requirement"] = self.string()
        return r

    def selector(self) -> dict[str, Any]:
        w = self.word("has_column", "named", "tagged", "all", "and", "or")
        if w == "all":
            return {"all": True}
        self.eat("(")
        if w == "has_column":
            cols = [self.name("a column")]
            if self.at_word(*TYPES):
                cols.append(self.advance().text)
            sel: dict[str, Any] = {"has_column": cols}
        elif w == "named":
            sel = {"named": self.names()}
        elif w == "tagged":
            sel = {"tagged": self.name("a tag")}
        else:
            subs = [self.selector()]
            while self.at(","):
                self.advance()
                subs.append(self.selector())
            sel = {w: subs}
        self.eat(")")
        return sel

    # ------------------------------------------------------------------ properties
    def properties(self) -> list[dict[str, Any]]:
        self.advance()
        self.eat("{")
        out: list[dict[str, Any]] = []
        while not self.at("}"):
            self.path = f"$.properties[{len(out)}]"
            self.mark(self.path)
            out.append(self.property())
        self.eat("}")
        return out

    def property(self) -> dict[str, Any]:
        base = self.path
        p: dict[str, Any] = {"id": self.dotted("a property id"), "template": self.word(*TEMPLATES)}
        t = p["template"]
        if t == "tenant_isolation":
            p["table"] = self.name("a table")
            self.eat(":")
            p["column"] = self.name("a column")
            self.word("in")
            p["relation"] = self.name("a relation")
            self.eat("(")
            p["key"] = self.term(f"{base}.key")
            self.eat(")")
            self.word("for")
            p["commands"] = [self.word(*COMMANDS[:4])]
            while self.at(","):
                self.advance()
                p["commands"].append(self.word(*COMMANDS[:4]))
        elif t == "column_immutable":
            p["table"] = self.name("a table")
            self.eat(":")
            p["column"] = self.name("a column")
        elif t == "monotone_in":
            self.eat(":")
            p["relation"] = self.name("a relation")
        elif t == "write_subset_read":
            p["table"] = self.name("a table")
        elif t == "least_privilege":
            self.eat(":")
            allowed: dict[str, dict[str, list[str]]] = {}
            if self.at("["):  # no role at all
                self.advance()
                self.eat("]")
            while self.tok.kind in ("ident", "qident") and not self.at_word("description"):
                role = self.name("a role")
                if self.at("["):  # a role with no table
                    self.advance()
                    self.eat("]")
                    allowed.setdefault(role, {})
                    if self.at(";"):
                        self.advance()
                        continue
                    break
                table = self.name("a table")
                self.eat("[")
                cmds: list[str] = []
                while not self.at("]"):
                    cmds.append(self.word(*COMMANDS[:4]))
                    if self.at(","):
                        self.advance()
                self.eat("]")
                allowed.setdefault(role, {})[table] = cmds
                if self.at(";"):
                    self.advance()
                else:
                    break
            p["allowed"] = allowed
        elif t == "invariant_preserved":
            self.eat(":")
            invs: list[dict[str, Any]] = []
            while True:
                self.mark(f"{base}.invariants[{len(invs)}]")
                if self.peek().kind == "op" and self.peek().text == ":":
                    table = self.name("a table")
                    self.eat(":")
                    invs.append({"table": table, "predicate": self.pred(f"{base}.invariants[{len(invs)}].predicate")})
                else:
                    invs.append({"rule": self.dotted("a rule id or 'table: predicate'")})
                if self.at(";"):
                    self.advance()
                else:
                    break
            p["invariants"] = invs
        elif t == "custom":
            p["table"] = self.name("a table")
            p["role"] = self.name("a role")
            p["command"] = self.word(*COMMANDS[:4])
            p["which"] = self.word("old", "new")
            self.eat(":")
            p["goal"] = self.pred(f"{base}.goal")
        elif t == "must_allow":
            p["table"] = self.name("a table")
            p["role"] = self.name("a role")
            p["command"] = self.word(*COMMANDS[:4])
            self.eat(":")
            p["when"] = self.pred(f"{base}.when")
        else:  # role_separation
            p["table"] = self.name("a table")
            p["command"] = self.word(*COMMANDS[:4])
            self.eat(":")
            a = self.name("a role")
            self.eat("|")
            p["roles"] = [a, self.name("a role")]
        if self.at_word("description"):
            self.advance()
            p["description"] = self.string()
        return p

    # ------------------------------------------------------------------ predicates
    def pred(self, path: str) -> dict[str, Any]:
        self.spans.setdefault(path, (self.tok.line, self.tok.col))
        return self.or_expr(path)

    def or_expr(self, path: str) -> dict[str, Any]:
        parts = [self.and_expr(path)]
        while self.at_word("or"):
            self.advance()
            parts.append(self.and_expr(path))
        return parts[0] if len(parts) == 1 else {"or": parts}

    def and_expr(self, path: str) -> dict[str, Any]:
        parts = [self.not_expr(path)]
        while self.at_word("and"):
            self.advance()
            parts.append(self.not_expr(path))
        return parts[0] if len(parts) == 1 else {"and": parts}

    def not_expr(self, path: str) -> dict[str, Any]:
        if self.at_word("not"):
            self.advance()
            return {"not": self.not_expr(path)}
        return self.atom(path)

    def atom(self, path: str) -> dict[str, Any]:
        t = self.tok
        if self.at("("):
            self.advance()
            p = self.or_expr(path)
            self.eat(")")
            return p
        if t.kind == "ident" and self.peek().kind == "op" and self.peek().text == "(" \
                and t.text in ("in_group", "now_between", "opaque"):
            self.advance()
            self.eat("(")
            if t.text == "opaque":
                p = {"opaque": self.name("an opaque block")}
            elif t.text == "in_group":
                h = self.name("a hierarchy")
                self.eat(",")
                key = self.term(path)
                self.eat(",")
                p = {"in_group": {"hierarchy": h, "key": key, "value": self.term(path)}}
            else:
                lo = self.bound(path)
                self.eat(",")
                hi = self.bound(path)
                body: dict[str, Any] = {}
                if lo is not None:
                    body["from"] = lo
                if hi is not None:
                    body["until"] = hi
                p = {"now_between": body}
            self.eat(")")
            return p
        if t.kind in ("ident", "qident") and self.peek().kind == "op" and self.peek().text == "(" \
                and t.text not in ("old", "new"):
            rel = self.name()
            self.eat("(")
            key = self.term(path)
            self.eat(",")
            value = self.term(path)
            self.eat(")")
            return {"member": {"relation": rel, "key": key, "value": value}}
        if t.kind == "ident" and t.text in ("true", "false") and not self._term_follows():
            self.advance()
            return {"lit": t.text == "true"}
        left = self.term(path)
        if self.at_word("is"):
            self.advance()
            self.word("null")
            return {"is_null": left}
        if self.at_word("in"):
            self.advance()
            self.eat("(")
            consts = [self.const()]
            while self.at(","):
                self.advance()
                consts.append(self.const())
            self.eat(")")
            return {"in": [left, consts]}
        if self.tok.kind == "op" and self.tok.text in CMP_OPS:
            op = self.advance().text
            return {"cmp": [left, op, self.term(path)]}
        raise self.fail("a comparison operator, 'is null' or 'in (...)'")

    def _term_follows(self) -> bool:
        n = self.peek()
        return (n.kind == "op" and n.text in CMP_OPS) or (n.kind == "ident" and n.text in ("is", "in"))

    def bound(self, path: str) -> dict[str, Any] | None:
        if self.at("-") and self.peek().kind == "op" and self.peek().text in (",", ")"):
            self.advance()
            return None
        return self.term(path)

    def const(self) -> dict[str, Any]:
        t = self.term("$")
        if "const" not in t:
            raise ParseError("an in-list holds constants only", self.tok.line, self.tok.col, self.path)
        return t

    def term(self, path: str) -> dict[str, Any]:
        t = self.tok
        if t.kind == "str":
            self.advance()
            return {"const": t.text}
        if t.kind == "num" or (self.at("-") and self.peek().kind == "num"):
            neg = self.at("-")
            if neg:
                self.advance()
            n = self.advance()
            if "." in n.text:
                raise ParseError("the fragment has no decimal numbers", n.line, n.col, path)
            return {"const": -int(n.text) if neg else int(n.text)}
        if self.at("@"):
            self.advance()
            return {"ctx": self.name("a context value")}
        if t.kind == "ident" and t.text in ("old", "new") and self.peek().kind == "op" and self.peek().text == ".":
            self.advance()
            self.advance()
            return {t.text: self.name("a column")}
        if t.kind == "ident" and t.text in ("true", "false"):
            self.advance()
            return {"const": t.text == "true"}
        if t.kind in ("ident", "qident"):
            self.advance()
            return {"col": t.text}
        raise self.fail("a term (column, old.column, new.column, @context, 'text', a number, true, false)")


def parse_data(text: str, filename: str = "<terse>") -> Parsed:
    """The JSON shape and the spans; syntax errors as SpecError with file:line:col."""
    try:
        p = Parser(text)
        data = p.spec()
    except ParseError as e:
        line = text.split("\n")[e.line - 1] if 0 < e.line <= text.count("\n") + 1 else ""
        raise SpecError([MlsError(path=e.path, message=f"{filename}:{e.line}:{e.col}: {e.message}\n  {line.rstrip()}",
                                  fix="see the grammar (REFERENCE §13)", code="syntax")]) from None
    return Parsed(data, p.spans)


def parse(text: str, filename: str = "<terse>") -> Spec:
    """A terse spec, validated at the schema level (run check_spec for types and the fragment)."""
    from pydantic import ValidationError

    from mls.spec.check import pydantic_errors_to_mls

    parsed = parse_data(text, filename)
    try:
        return Spec.model_validate(parsed.data)
    except ValidationError as e:
        raise SpecError(pydantic_errors_to_mls(e)) from None
