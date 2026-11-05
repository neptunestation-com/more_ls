"""The lifter (D46): the deparsed text of a policy's USING / WITH CHECK (pg_get_expr) or of a CHECK
constraint (pg_get_constraintdef) -> a verified-fragment predicate, else an opaque block.

The lifter reads the shapes the compiler itself emits (`compile/sql.py`): `col op (select <ctx>)`,
`col = any ((select mls_reach_<h>(<ctx>))::t[])`, `col = any (array(select value from rel where
key = <ctx>))`, `col = any (array[c1, ...])`, `col is null`, `(p) is true` at the top of a CHECK,
`and` / `or` / `not`; and a fixed list of hand-written idioms: a bare `auth.uid()`,
`current_setting('app.x')` or `current_user` without the InitPlan wrapper (lifted; a hint names
the index-friendly form), `col in (c1, ...)`, `col in (select value from rel where key = <ctx>)`,
the correlated `exists (select 1 from rel where rel.key = <ctx> and rel.value = t.col)` (lifted
to `member`; a hint names it as the slow shape of FINDINGS), a bare boolean column, and
`col like 'const'` (never in the fragment: it becomes an opaque block over the column alone, which
the leakproof lint reports).

The lifter is total.  It works at the granularity of the boolean connectives: a conjunct,
disjunct or negand it cannot read becomes an opaque block whose signature is the columns, context
sources and relations its text names (a conservative scan); an expression it cannot parse at all
is one opaque block.  Every lifted atom is validated with the spec checker before it is kept, so
the lifted spec always checks.  A user function is never inlined (D34): any call that is not one
of the identity sources or a compiler helper of a declared hierarchy is unreadable, however short
the function's body.  Deparsing differs across versions (D30): the parser reads the deparsed text
of every version in the matrix, and the tests pin shapes, not strings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import TypeAdapter

from mls.spec.model import CONTEXT_NOW, ColType, Pred, Spec

PG_TO_MLS: dict[str, ColType] = {"integer": "int", "text": "text", "boolean": "bool",
                                 "timestamp with time zone": "timestamp", "uuid": "uuid"}
CAST_TO_MLS: dict[str, ColType] = {"uuid": "uuid", "integer": "int", "int": "int", "int4": "int", "text": "text",
                                   "boolean": "bool", "bool": "bool", "timestamptz": "timestamp",
                                   "timestamp with time zone": "timestamp"}
SQL_TYPE_OF: dict[ColType, str] = {"int": "integer", "text": "text", "bool": "boolean", "timestamp": "timestamptz",
                                   "uuid": "uuid"}
FLIP = {"=": "=", "<>": "<>", "<": ">", "<=": ">=", ">": "<", ">=": "<="}
REACH_FN = re.compile(r"^mls_reach_([a-z_][a-z0-9_]*)$")
_PRED = TypeAdapter(Pred)


class Unreadable(Exception):
    """An expression the lifter does not read; the message says why, for the import notes."""


# ----------------------------------------------------------------------------- tokens
@dataclass(frozen=True)
class Tok:
    kind: str  # ident | qident | str | num | op | end
    text: str
    pos: int


_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<qident>"(?:[^"]|"")*")
  | (?P<str>'(?:[^']|'')*')
  | (?P<num>\d+(?:\.\d+)?)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<op>->>|->|::|<>|!=|<=|>=|!~~\*|!~~|~~\*|~~|\|\||[=<>()\[\],.+\-*/~])
""", re.VERBOSE)


def tokenize(text: str) -> list[Tok]:
    out: list[Tok] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None:
            raise Unreadable(f"unexpected character {text[pos]!r} at {pos}")
        kind = m.lastgroup
        if kind != "ws":
            out.append(Tok(kind, m.group(0), pos))
        pos = m.end()
    out.append(Tok("end", "", len(text)))
    return out


# ----------------------------------------------------------------------------- syntax tree
@dataclass
class Node:
    start: int = field(default=0, kw_only=True)
    end: int = field(default=0, kw_only=True)


@dataclass
class And(Node):
    items: list[Node]


@dataclass
class Or(Node):
    items: list[Node]


@dataclass
class Not(Node):
    x: Node


@dataclass
class IsNull(Node):
    x: Node
    negated: bool


@dataclass
class IsTrue(Node):
    x: Node
    value: bool  # IS TRUE / IS FALSE
    negated: bool


@dataclass
class Cmp(Node):
    l: Node
    op: str
    r: Node


@dataclass
class AnyOf(Node):
    l: Node
    op: str
    arr: Node
    quant: str = "any"  # `= ANY (...)`; `<> ALL (...)` is how NOT IN deparses


@dataclass
class InList(Node):
    l: Node
    items: list[Node]
    negated: bool


@dataclass
class InSelect(Node):
    l: Node
    select: "Select"
    negated: bool


@dataclass
class Like(Node):
    l: Node
    op: str
    r: Node


@dataclass
class BinOp(Node):
    l: Node
    op: str
    r: Node


@dataclass
class Exists(Node):
    select: "Select"


@dataclass
class Col(Node):
    qual: str | None
    name: str


@dataclass
class Func(Node):
    name: str  # possibly schema-qualified, lower-case
    args: list[Node]


@dataclass
class Str(Node):
    value: str


@dataclass
class Num(Node):
    text: str


@dataclass
class Bool(Node):
    value: bool


@dataclass
class Null(Node):
    pass


@dataclass
class Keyword(Node):
    name: str  # current_user, session_user, current_timestamp, ...


@dataclass
class Cast(Node):
    x: Node
    type: str  # lower-case, single-spaced


@dataclass
class Select(Node):
    target: Node
    alias: str | None
    table: str | None
    table_alias: str | None
    where: Node | None


@dataclass
class ArrayLit(Node):
    items: list[Node]


@dataclass
class ArrayQuery(Node):
    select: Select


_KW_CONST = {"current_user", "session_user", "current_role", "current_timestamp", "current_date", "localtimestamp"}


class Parser:
    def __init__(self, text: str):
        self.text = text
        self.toks = tokenize(text)
        self.i = 0

    # --- helpers
    def peek(self, k: int = 0) -> Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def is_kw(self, *words: str, k: int = 0) -> bool:
        t = self.peek(k)
        return t.kind == "ident" and t.text.lower() in words

    def is_op(self, *ops: str, k: int = 0) -> bool:
        t = self.peek(k)
        return t.kind == "op" and t.text in ops

    def take(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect_op(self, op: str) -> Tok:
        if not self.is_op(op):
            raise Unreadable(f"expected '{op}' at {self.peek().pos}, found {self.peek().text!r}")
        return self.take()

    def expect_kw(self, word: str) -> Tok:
        if not self.is_kw(word):
            raise Unreadable(f"expected {word.upper()} at {self.peek().pos}, found {self.peek().text!r}")
        return self.take()

    def name(self) -> str:
        t = self.take()
        if t.kind == "ident":
            return t.text.lower()
        if t.kind == "qident":
            return t.text[1:-1].replace('""', '"')
        raise Unreadable(f"expected a name at {t.pos}, found {t.text!r}")

    def span(self, node: Node, start: int) -> Node:
        node.start = start
        node.end = self.toks[self.i - 1].pos + len(self.toks[self.i - 1].text)
        return node

    # --- grammar
    def parse(self) -> Node:
        e = self.expr()
        if self.peek().kind != "end":
            raise Unreadable(f"unexpected {self.peek().text!r} at {self.peek().pos}")
        return e

    def expr(self) -> Node:
        start = self.peek().pos
        items = [self.and_expr()]
        while self.is_kw("or"):
            self.take()
            items.append(self.and_expr())
        return items[0] if len(items) == 1 else self.span(Or(items), start)

    def and_expr(self) -> Node:
        start = self.peek().pos
        items = [self.not_expr()]
        while self.is_kw("and"):
            self.take()
            items.append(self.not_expr())
        return items[0] if len(items) == 1 else self.span(And(items), start)

    def not_expr(self) -> Node:
        start = self.peek().pos
        if self.is_kw("not"):
            self.take()
            return self.span(Not(self.not_expr()), start)
        return self.is_expr()

    def is_expr(self) -> Node:
        start = self.peek().pos
        e = self.cmp_expr()
        while self.is_kw("is"):
            self.take()
            neg = False
            if self.is_kw("not"):
                self.take()
                neg = True
            if self.is_kw("null"):
                self.take()
                e = self.span(IsNull(e, neg), start)
            elif self.is_kw("true", "false"):
                e = self.span(IsTrue(e, self.take().text.lower() == "true", neg), start)
            elif self.is_kw("distinct"):
                raise Unreadable("IS DISTINCT FROM is not in the fragment")
            else:
                raise Unreadable(f"unexpected IS at {start}")
        return e

    def cmp_expr(self) -> Node:
        start = self.peek().pos
        l = self.add_expr()
        if self.is_op("=", "<>", "!=", "<", "<=", ">", ">="):
            op = self.take().text
            op = "<>" if op == "!=" else op
            if self.is_kw("any", "all"):
                which = self.take().text.lower()
                self.expect_op("(")
                arr = self.expr()
                self.expect_op(")")
                return self.span(AnyOf(l, op, arr, which), start)
            r = self.add_expr()
            return self.span(Cmp(l, op, r), start)
        neg = False
        if self.is_kw("not") and self.is_kw("in", "like", "ilike", k=1):
            self.take()
            neg = True
        if self.is_kw("in"):
            self.take()
            self.expect_op("(")
            if self.is_kw("select"):
                sel = self.select()
                self.expect_op(")")
                return self.span(InSelect(l, sel, neg), start)
            items = self.expr_list()
            self.expect_op(")")
            return self.span(InList(l, items, neg), start)
        if self.is_kw("like", "ilike") or self.is_op("~~", "~~*", "!~~", "!~~*"):
            t = self.take().text.lower()
            op = {"like": "~~", "ilike": "~~*"}.get(t, t)
            if neg:
                op = "!" + op
            r = self.add_expr()
            return self.span(Like(l, op, r), start)
        if neg:
            raise Unreadable(f"unexpected NOT at {start}")
        return l

    def add_expr(self) -> Node:
        start = self.peek().pos
        l = self.unary()
        while self.is_op("+", "-", "*", "/", "||", "->>", "->", "~"):
            op = self.take().text
            r = self.unary()
            l = self.span(BinOp(l, op, r), start)
        return l

    def unary(self) -> Node:
        start = self.peek().pos
        if self.is_op("-"):
            self.take()
            x = self.unary()
            if isinstance(x, Num):
                return self.span(Num("-" + x.text), start)
            return self.span(BinOp(Num("0"), "-", x), start)
        return self.postfix()

    def postfix(self) -> Node:
        start = self.peek().pos
        e = self.primary()
        while self.is_op("::"):
            self.take()
            e = self.span(Cast(e, self.typename()), start)
        return e

    def typename(self) -> str:
        words = [self.name()]
        while self.peek().kind == "ident" and not self.is_kw("and", "or", "is", "in", "like", "ilike", "not", "as",
                                                              "from", "where", "any", "all"):
            words.append(self.take().text.lower())
        if self.is_op("("):
            self.take()
            while not self.is_op(")"):
                self.take()
            self.take()
        t = " ".join(words)
        while self.is_op("["):
            self.take()
            self.expect_op("]")
            t += "[]"
        return t

    def expr_list(self) -> list[Node]:
        items = [self.expr()]
        while self.is_op(","):
            self.take()
            items.append(self.expr())
        return items

    def primary(self) -> Node:
        start = self.peek().pos
        t = self.peek()
        if t.kind == "op" and t.text == "(":
            self.take()
            if self.is_kw("select"):
                sel = self.select()
                self.expect_op(")")
                sel.start, sel.end = start, self.toks[self.i - 1].pos + 1
                return sel
            e = self.expr()
            self.expect_op(")")
            e.start, e.end = start, self.toks[self.i - 1].pos + 1
            return e
        if t.kind == "str":
            self.take()
            return self.span(Str(t.text[1:-1].replace("''", "'")), start)
        if t.kind == "num":
            self.take()
            return self.span(Num(t.text), start)
        if t.kind == "ident":
            w = t.text.lower()
            if w in ("true", "false"):
                self.take()
                return self.span(Bool(w == "true"), start)
            if w == "null":
                self.take()
                return self.span(Null(), start)
            if w in _KW_CONST:
                self.take()
                return self.span(Keyword(w), start)
            if w == "array":
                self.take()
                if self.is_op("["):
                    self.take()
                    items = [] if self.is_op("]") else self.expr_list()
                    self.expect_op("]")
                    return self.span(ArrayLit(items), start)
                self.expect_op("(")
                sel = self.select()
                self.expect_op(")")
                return self.span(ArrayQuery(sel), start)
            if w == "exists":
                self.take()
                self.expect_op("(")
                sel = self.select()
                self.expect_op(")")
                return self.span(Exists(sel), start)
            if w in ("select", "case", "cast"):
                raise Unreadable(f"{w.upper()} is not in the fragment")
        if t.kind in ("ident", "qident"):
            parts = [self.name()]
            while self.is_op(".") and self.peek(1).kind in ("ident", "qident"):
                self.take()
                parts.append(self.name())
            if self.is_op("("):
                self.take()
                args = [] if self.is_op(")") else self.expr_list()
                self.expect_op(")")
                return self.span(Func(".".join(parts), args), start)
            if len(parts) > 2:
                raise Unreadable(f"a three-part name at {start}")
            if len(parts) == 2:
                return self.span(Col(parts[0], parts[1]), start)
            return self.span(Col(None, parts[0]), start)
        raise Unreadable(f"unexpected {t.text!r} at {t.pos}")

    def select(self) -> Select:
        start = self.peek().pos
        self.expect_kw("select")
        if self.is_op("*"):
            self.take()
            target: Node = Num("*")
        else:
            target = self.expr()
        alias = None
        if self.is_kw("as"):
            self.take()
            alias = self.name()
        table = table_alias = None
        where = None
        if self.is_kw("from"):
            self.take()
            parts = [self.name()]
            while self.is_op("."):
                self.take()
                parts.append(self.name())
            table = parts[-1]
            if self.is_kw("as"):
                self.take()
                table_alias = self.name()
            elif self.peek().kind in ("ident", "qident") and not self.is_kw("where"):
                table_alias = self.name()
            if self.is_kw("join", "inner", "left", "right", "cross", "natural") or self.is_op(","):
                raise Unreadable("a subquery over more than one table")
            if self.is_kw("where"):
                self.take()
                where = self.expr()
            if self.is_kw("group", "order", "limit", "having", "union", "intersect", "except"):
                raise Unreadable(f"{self.peek().text.upper()} in a subquery")
        return self.span(Select(target, alias, table, table_alias, where), start)


def parse(text: str) -> Node:
    return Parser(text).parse()


def strip_check(constraint_def: str) -> str:
    """The expression of a `CHECK (...)` definition from pg_get_constraintdef."""
    s = constraint_def.strip()
    if not s.upper().startswith("CHECK"):
        raise Unreadable("not a CHECK constraint")
    s = s[5:].strip()
    if not (s.startswith("(") and s.endswith(")")):
        raise Unreadable("a CHECK definition without parentheses")
    return s[1:-1]


# ----------------------------------------------------------------------------- environment
@dataclass
class Env:
    """What the lifter reads an expression against: the spec so far.  `tables` maps a table to
    its modelled columns (type, nullable); `context` a context name to (source, type);
    `relations` a name to (table, key, value); `hierarchies` a name to its declaration; `keys`
    a table to its primary key and unique column sets (a relation must be one of them)."""
    adapter: str
    context: dict[str, tuple[str, ColType]]
    tables: dict[str, dict[str, tuple[ColType, bool]]]
    governed: set[str]
    keys: dict[str, list[frozenset[str]]] = field(default_factory=dict)  # table -> its PK and unique column sets
    roles: list[str] = field(default_factory=list)
    relations: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    hierarchies: dict[str, dict] = field(default_factory=dict)
    opaques: dict[str, dict] = field(default_factory=dict)
    _spec: Spec | None = field(default=None, repr=False)

    @classmethod
    def of_spec(cls, spec: Spec) -> "Env":
        return cls(adapter=spec.identity.adapter,
                   context={n: (cv.source, cv.type) for n, cv in spec.identity.context.items()},
                   tables={t: {c: (col.type, col.nullable) for c, col in tb.columns.items()}
                           for t, tb in spec.tables.items()},
                   governed=set(spec.governed()), roles=list(spec.roles),
                   keys={t: [frozenset(k) for k in ([tb.primary_key] if tb.primary_key else []) + list(tb.unique)]
                         for t, tb in spec.tables.items()},
                   relations={n: (r.table, r.key, r.value) for n, r in spec.relations.items()},
                   hierarchies={n: h.model_dump() for n, h in spec.hierarchies.items()},
                   opaques={n: o.model_dump() for n, o in spec.opaques.items()})

    def ctx_by_source(self, source: str) -> str | None:
        for n, (s, _) in self.context.items():
            if s == source:
                return n
        return None

    def invalidate(self) -> None:
        self._spec = None

    def spec(self) -> Spec:
        """A minimal spec for the checker: the declarations, no rules."""
        if self._spec is None:
            d = {"mls_version": "0.1", "identity": {"adapter": self.adapter,
                                                    "context": {n: {"source": s, "type": t}
                                                                for n, (s, t) in self.context.items()}},
                 "roles": self.roles or ["r"],
                 "tables": {t: {"columns": {c: {"type": ty, "nullable": nu} for c, (ty, nu) in cols.items()},
                                "mode": "adopted" if t in self.governed else "observed"}
                            for t, cols in self.tables.items()},
                 "relations": {n: {"table": t, "key": k, "value": v} for n, (t, k, v) in self.relations.items()},
                 "hierarchies": self.hierarchies,
                 "opaques": self.opaques, "rules": [], "properties": []}
            self._spec = Spec.model_validate(d)
        return self._spec

    def validate(self, pred: dict, table: str, scope: str, negated: bool) -> None:
        """Raise Unreadable with the checker's message when `pred` is outside the fragment."""
        from mls.spec.check import Checker, check_scope, policy_scope, transition_scope

        ck = Checker(self.spec())
        sc = {"policy": policy_scope, "check": check_scope, "transition": transition_scope}[scope](table)
        ck.pred(_PRED.validate_python(pred), sc, "$", negated)
        if ck.sink.errors:
            e = ck.sink.errors[0]
            raise Unreadable(e.message + (f" ({e.fix})" if e.fix else ""))


# ----------------------------------------------------------------------------- the lifter
@dataclass
class LiftResult:
    pred: dict | None  # None when the text could not be parsed and no opaque was made
    kind: str  # lifted | partial | opaque
    reasons: list[str] = field(default_factory=list)  # why a part is opaque: "<sql>: <reason>"
    hints: list[str] = field(default_factory=list)  # index-friendly forms, slow shapes, LIKE
    opaques: list[str] = field(default_factory=list)  # names of the blocks made
    columns: set[str] = field(default_factory=set)  # columns the text names (conservative)


MakeOpaque = Callable[[str, list[str], list[str], list[str]], str]  # (sql, columns, context, relations) -> name
MakeRelation = Callable[[str, str, str], "str | None"]  # (table, key, value) -> name, None if refused


class Lifter:
    def __init__(self, env: Env, table: str, scope: str, make_opaque: MakeOpaque, make_relation: MakeRelation):
        self.env = env
        self.table = table
        self.scope = scope
        self.make_opaque = make_opaque
        self.make_relation = make_relation
        self.markers = context_markers(env)
        self.result = LiftResult(None, "opaque")
        self.hinted: set[str] = set()
        self._text = ""

    # --- entry points
    def lift(self, text: str, top_is_true: bool = False) -> LiftResult:
        self._text = text
        self.result.columns = scan_columns(text, self.env.tables[self.table])
        try:
            tree = parse(text)
        except Unreadable as e:
            self.result.reasons.append(f"{text.strip()}: {e}")
            self.result.pred = {"opaque": self.opaque(text, 0, len(text))}
            return self.result
        if top_is_true and isinstance(tree, IsTrue) and tree.value and not tree.negated:
            tree = tree.x
        try:
            self.result.pred = self.pred(tree, False)
        except Unreadable as e:
            self.result.reasons.append(f"{text.strip()}: {e}")
            self.result.pred = {"opaque": self.opaque(text, 0, len(text))}
            self.result.kind = "opaque"
            return self.result
        self.result.kind = "partial" if self.result.opaques else "lifted"
        self.result.pred = now_between_shape(self.result.pred)
        return self.result

    def opaque(self, text: str, start: int, end: int) -> str:
        sql = text[start:end].strip()
        cols = sorted(scan_columns(sql, self.env.tables[self.table]))
        ctx = [n for n, m in self.markers.items() if m.search(sql)]
        rels = [rn for rn, (t, _, _) in self.env.relations.items()
                if re.search(r'(?<![\w."])"?' + re.escape(t) + r'"?(?![\w"])', sql)]
        name = self.make_opaque(sql, cols, ctx, rels)
        self.result.opaques.append(name)
        self.env.invalidate()
        return name

    def hint(self, text: str) -> None:
        if text not in self.hinted:
            self.hinted.add(text)
            self.result.hints.append(text)

    # --- predicates
    def part(self, node: Node, negated: bool) -> dict:
        """A conjunct / disjunct / negand: lifted, or an opaque block of its own text."""
        try:
            return self.pred(node, negated)
        except Unreadable as e:
            self.result.reasons.append(f"{self.src(node)}: {e}")
            return {"opaque": self.opaque(self.src_text, node.start, node.end)}

    def pred(self, node: Node, negated: bool) -> dict:
        if isinstance(node, Bool):
            return {"lit": node.value}
        if isinstance(node, And):
            return {"and": [self.part(x, negated) for x in node.items]}
        if isinstance(node, Or):
            return {"or": [self.part(x, negated) for x in node.items]}
        if isinstance(node, Not):
            return {"not": self.part(node.x, not negated)}
        if isinstance(node, IsNull):
            p = {"is_null": self.term(node.x)[0]}
            self.check(p, negated)
            return {"not": p} if node.negated else p
        if isinstance(node, IsTrue):
            raise Unreadable("IS TRUE / IS FALSE inside an expression (the fragment is three-valued; only a CHECK's "
                             "outer `(p) IS TRUE` is read)")
        if isinstance(node, Cmp):
            return self.cmp(node, negated)
        if isinstance(node, AnyOf):
            return self.any_of(node, negated)
        if isinstance(node, InList):
            if node.negated:
                return {"not": self.in_list(node, not negated)}
            return self.in_list(node, negated)
        if isinstance(node, InSelect):
            if node.negated:
                raise Unreadable("NOT IN (subquery) is an anti-join (restate positively, or it stays opaque)")
            p = self.member(node.l, node.select, correlated=False)
            self.check(p, negated)
            self.hint(f"`{self.src(node)}` is lifted to a membership; the compiler writes it as "
                      f"`col = any (array(select ...))`, the index-driven shape")
            return p
        if isinstance(node, Exists):
            p = self.exists(node)
            self.check(p, negated)
            self.hint(f"`{self.src(node)}` is a correlated EXISTS, the per-row shape FINDINGS measures as the slow "
                      f"one; it is lifted to a membership and the compiler writes the array shape")
            return p
        if isinstance(node, Like):
            self.term(node.l)  # must be a readable term, so the block's signature is exact
            raise Unreadable("LIKE is not in the fragment (its operator is not leakproof: the leakproof lint "
                             "reports it); the comparison stays an opaque block over the column")
        if isinstance(node, Col):
            t, ty = self.term(node)
            if ty != "bool":
                raise Unreadable(f"a bare column of type {ty} is not a predicate")
            p = {"cmp": [t, "=", {"const": True}]}
            self.check(p, negated)
            return p
        if isinstance(node, Func):
            raise Unreadable(self.func_reason(node))
        if isinstance(node, (Cast, Select, BinOp, Keyword, Str, Num, Null, ArrayLit, ArrayQuery)):
            raise Unreadable(f"`{self.src(node)}` is not a predicate the fragment has")
        raise Unreadable(f"unknown node {type(node).__name__}")

    def check(self, pred: dict, negated: bool) -> None:
        self.env.validate(pred, self.table, self.scope, negated)

    def cmp(self, node: Cmp, negated: bool) -> dict:
        (l, lt), (r, rt) = self.term(node.l), self.term(node.r)
        op = node.op
        if not _is_col(l) and _is_col(r):  # the fragment puts the column on the left
            l, r, op = r, l, FLIP[op]
        p = {"cmp": [l, op, r]}
        self.check(p, negated)
        return p

    def any_of(self, node: AnyOf, negated: bool) -> dict:
        arr = node.arr
        if node.quant == "all":
            if node.op == "<>" and isinstance(arr, ArrayLit):  # NOT IN (c1, ...) deparses as <> ALL (ARRAY[...])
                l, _ = self.term(node.l)
                p = {"in": [l, [self.const(x)[0] for x in arr.items]]}
                self.check(p, not negated)
                return {"not": p}
            raise Unreadable(f"{node.op} ALL is not in the fragment")
        if node.op != "=":
            raise Unreadable(f"{node.op} ANY is not in the fragment")
        if isinstance(arr, ArrayLit):
            l, _ = self.term(node.l)
            p = {"in": [l, [self.const(x)[0] for x in arr.items]]}
            self.check(p, negated)
            return p
        if isinstance(arr, ArrayQuery):
            p = self.member(node.l, arr.select, correlated=False)
            self.check(p, negated)
            return p
        if isinstance(arr, Cast) and isinstance(arr.x, Select) and arr.x.table is None:
            # col = any ((select mls_reach_<h>(<ctx>))::t[]): the compiled in_group
            sel = arr.x
            if isinstance(sel.target, Func):
                m = REACH_FN.match(sel.target.name)
                if m and m.group(1) in self.env.hierarchies and len(sel.target.args) == 1:
                    key, _ = self.term(sel.target.args[0])
                    value, _ = self.term(node.l)
                    p = {"in_group": {"hierarchy": m.group(1), "key": key, "value": value}}
                    self.check(p, negated)
                    return p
                if m:
                    raise Unreadable(f"calls {sel.target.name}(), a hierarchy helper of a hierarchy the spec does not "
                                     f"declare (declare it to lift the policy)")
                raise Unreadable(self.func_reason(sel.target))
        raise Unreadable(f"`{self.src(node)}`: = ANY over something that is not a constant array, a membership "
                         f"subquery or a hierarchy helper")

    def in_list(self, node: InList, negated: bool) -> dict:
        l, _ = self.term(node.l)
        p = {"in": [l, [self.const(x)[0] for x in node.items]]}
        self.check(p, negated)
        return p

    # --- memberships
    def member(self, value_node: Node, sel: Select, correlated: bool, outer_value: Node | None = None) -> dict:
        """`select V from R where R.K = <key>`: a relation (R, K, V) and a member atom."""
        if sel.table is None:
            raise Unreadable("a membership subquery needs a FROM table")
        rel_table = sel.table
        if rel_table not in self.env.tables:
            raise Unreadable(f"subquery reads table {rel_table}, which is not in the spec")
        inner = InnerScope(self, rel_table, sel.table_alias)
        if correlated:
            if sel.where is None:
                raise Unreadable("EXISTS without a WHERE clause")
            conds = sel.where.items if isinstance(sel.where, And) else [sel.where]
            key_col = key_term = val_col = outer_col = None
            for c in conds:
                if not isinstance(c, Cmp) or c.op != "=":
                    raise Unreadable(f"`{self.src(c)}`: EXISTS reads more than `rel.key = <ctx> and rel.value = col`")
                sides = [inner.classify(c.l), inner.classify(c.r)]
                kinds = sorted(k for k, _ in sides)
                if kinds == ["inner", "outer"]:
                    i = sides[0][1] if sides[0][0] == "inner" else sides[1][1]
                    o = sides[0][1] if sides[0][0] == "outer" else sides[1][1]
                    if val_col is not None:
                        raise Unreadable("EXISTS correlates on two columns")
                    val_col, outer_col = i, o
                elif kinds == ["inner", "key"]:
                    i = sides[0][1] if sides[0][0] == "inner" else sides[1][1]
                    k = sides[0][1] if sides[0][0] == "key" else sides[1][1]
                    if key_col is not None:
                        raise Unreadable("EXISTS filters the relation on two keys")
                    key_col, key_term = i, k
                else:
                    raise Unreadable(f"`{self.src(c)}`: EXISTS reads more than `rel.key = <ctx> and rel.value = col`")
            if key_col is None or val_col is None:
                raise Unreadable("EXISTS without both a key filter and a correlation on the row")
            value_json = {"col": outer_col}
        else:
            if not isinstance(sel.target, Col):
                raise Unreadable("the membership subquery must select one column")
            kind, val_col = inner.classify(sel.target)
            if kind != "inner":
                raise Unreadable("the membership subquery must select a column of its table")
            if sel.where is None:
                raise Unreadable("a membership subquery without a WHERE clause is not a relation lookup")
            if not isinstance(sel.where, Cmp) or sel.where.op != "=":
                raise Unreadable(f"`{self.src(sel.where)}`: the membership WHERE is not `key = <ctx>`")
            sides = [inner.classify(sel.where.l), inner.classify(sel.where.r)]
            kinds = sorted(k for k, _ in sides)
            if kinds != ["inner", "key"]:
                raise Unreadable(f"`{self.src(sel.where)}`: the membership WHERE is not `key = <ctx>`")
            key_col = sides[0][1] if sides[0][0] == "inner" else sides[1][1]
            key_term = sides[0][1] if sides[0][0] == "key" else sides[1][1]
            value_json, _ = self.term(value_node)
        if rel_table not in self.env.governed:
            raise Unreadable(f"the relation table {rel_table} is observed (not governed): no relation can be declared "
                             f"on it")
        if frozenset((key_col, val_col)) not in self.env.keys.get(rel_table, []):
            raise Unreadable(f"({key_col}, {val_col}) is not a primary key or unique constraint of {rel_table}; a "
                             f"relation is a set of pairs (add a unique constraint on the pair to lift this)")
        name = self.make_relation(rel_table, key_col, val_col)
        if name is None:
            raise Unreadable(f"no relation ({key_col}, {val_col}) of {rel_table} can be declared")
        if name not in self.env.relations:
            self.env.relations[name] = (rel_table, key_col, val_col)
            self.env.invalidate()
        return {"member": {"relation": name, "key": key_term, "value": value_json}}

    def exists(self, node: Exists) -> dict:
        return self.member(node, node.select, correlated=True)

    # --- terms
    def term(self, node: Node) -> tuple[dict, ColType | None]:
        """A term of the fragment: a column of the table, a context value or a constant."""
        if isinstance(node, Col):
            cols = self.env.tables[self.table]
            if self.scope == "transition" and node.qual in ("old", "new"):
                if node.name not in cols:
                    raise Unreadable(f"column {node.name} is not modelled on {self.table}")
                return {node.qual: node.name}, cols[node.name][0]
            if node.qual is not None and node.qual != self.table:
                raise Unreadable(f"column {node.qual}.{node.name} is not of table {self.table}")
            if node.name not in cols:
                raise Unreadable(f"column {node.name} is not modelled on {self.table}")
            return {"col": node.name}, cols[node.name][0]
        ctx = self.context(node)
        if ctx is not None:
            name, ty = ctx
            return {"ctx": name}, ty
        try:
            return self.const(node)
        except Unreadable:
            pass
        if isinstance(node, Func):
            raise Unreadable(self.func_reason(node))
        if isinstance(node, Select):
            raise Unreadable(f"`{self.src(node)}` is a subquery the fragment has no term for")
        raise Unreadable(f"`{self.src(node)}` is not a term of the fragment")

    def const(self, node: Node) -> tuple[dict, ColType | None]:
        if isinstance(node, Cast):
            ty = CAST_TO_MLS.get(node.type)
            if ty is None:
                raise Unreadable(f"a cast to {node.type}, not an MLS type")
            inner, _ = self.const(node.x)
            v = inner["const"]
            if ty == "int":
                if isinstance(v, bool) or not isinstance(v, (int, str)) or (isinstance(v, str)
                                                                            and not re.fullmatch(r"-?\d+", v)):
                    raise Unreadable(f"`{self.src(node)}` is not an integer constant")
                return {"const": int(v)}, "int"
            if ty == "bool":
                if isinstance(v, bool):
                    return {"const": v}, "bool"
                raise Unreadable(f"`{self.src(node)}` is not a boolean constant")
            if ty == "timestamp":
                raise Unreadable("timestamp constants are not supported (compare with now)")
            if not isinstance(v, str):
                raise Unreadable(f"`{self.src(node)}` is not a {ty} constant")
            return {"const": v}, ty
        if isinstance(node, Str):
            return {"const": node.value}, "text"
        if isinstance(node, Num):
            if not re.fullmatch(r"-?\d+", node.text):
                raise Unreadable(f"{node.text} is not an integer")
            return {"const": int(node.text)}, "int"
        if isinstance(node, Bool):
            return {"const": node.value}, "bool"
        if isinstance(node, Null):
            raise Unreadable("a NULL constant (use IS NULL)")
        raise Unreadable(f"`{self.src(node)}` is not a constant")

    def context(self, node: Node) -> tuple[str, ColType] | None:
        """The context value an expression reads, with the InitPlan wrapper stripped."""
        wrapped = False
        if isinstance(node, Select) and node.table is None:
            node = node.target
            wrapped = True
        found = self._context_source(node)
        if found is None:
            return None
        source, cast = found
        if source == CONTEXT_NOW:
            name, ty = CONTEXT_NOW, "timestamp"
        else:
            name = self.env.ctx_by_source(source)
            if name is None:
                raise Unreadable(f"reads {source}, which is not a declared context value (declare it under "
                                 f"identity.context)")
            ty = self.env.context[name][1]
            if cast is not None and cast != ty:
                raise Unreadable(f"reads {source} as {SQL_TYPE_OF[cast]}, but context value {name} is declared "
                                 f"{ty}")
            if cast is None and ty != "text" and source != "auth.uid":
                raise Unreadable(f"reads {source} as text, but context value {name} is declared {ty}")
        if not wrapped and name != CONTEXT_NOW:
            self.hint(f"`{self.src(node)}` is evaluated per row; the index-friendly form is `(select ...)`, "
                      f"an InitPlan the compiler writes on deploy")
        return name, ty

    def _context_source(self, node: Node) -> tuple[str, ColType | None] | None:
        """(source, cast type) of an identity expression, or None when it is not one."""
        cast: ColType | None = None
        if isinstance(node, Cast):
            if node.type == "text" and isinstance(node.x, (Keyword, Func, BinOp, Cast)):
                cast = "text"
            else:
                cast = CAST_TO_MLS.get(node.type)
                if cast is None:
                    return None
            node = node.x
        if isinstance(node, Func) and not node.args and node.name in ("now", "transaction_timestamp"):
            return CONTEXT_NOW, None
        if isinstance(node, Keyword) and node.name in ("current_timestamp", "localtimestamp"):
            return CONTEXT_NOW, None
        a = self.env.adapter
        if a == "supabase":
            if isinstance(node, Func) and node.name == "auth.uid" and not node.args:
                return "auth.uid", cast or "uuid"
            if isinstance(node, BinOp) and node.op == "->>" and isinstance(node.l, Func) and node.l.name == "auth.jwt" \
                    and not node.l.args:
                claim = node.r.x if isinstance(node.r, Cast) and node.r.type == "text" else node.r
                if isinstance(claim, Str):
                    return f"jwt.claims.{claim.value}", cast
            return None
        if a == "session_setting":
            inner = node
            if isinstance(inner, Func) and inner.name == "nullif" and len(inner.args) == 2 \
                    and self._is_empty_string(inner.args[1]):
                inner = inner.args[0]
            if isinstance(inner, Func) and inner.name == "current_setting" and inner.args:
                arg = inner.args[0]
                if isinstance(arg, Cast) and arg.type == "text":
                    arg = arg.x
                if isinstance(arg, Str) and arg.value.startswith("app."):
                    return arg.value, cast
            return None
        if a == "pg_roles":
            if isinstance(node, Keyword) and node.name in ("current_user", "current_role"):
                return "current_user", cast or "text"
            return None
        return None

    @staticmethod
    def _is_empty_string(node: Node) -> bool:
        if isinstance(node, Cast) and node.type == "text":
            node = node.x
        return isinstance(node, Str) and node.value == ""

    def func_reason(self, node: Func) -> str:
        return (f"calls {node.name}(), a function the lifter does not read: user functions are never inlined "
                f"(D34), whatever their body")

    # --- source text
    @property
    def src_text(self) -> str:
        return self._text

    def src(self, node: Node) -> str:
        return self._text[node.start:node.end].strip()


class InnerScope:
    """Name resolution inside a one-table subquery: a column of the subquery's table, a column
    of the outer row, or a key (context value / constant)."""

    def __init__(self, lifter: Lifter, table: str, alias: str | None):
        self.lifter = lifter
        self.table = table
        self.alias = alias
        self.cols = lifter.env.tables[table]

    def classify(self, node: Node) -> tuple[str, Any]:
        if isinstance(node, Col):
            if node.qual in (self.table, self.alias) and node.qual is not None:
                if node.name not in self.cols:
                    raise Unreadable(f"column {self.table}.{node.name} is not modelled")
                return "inner", node.name
            if node.qual == self.lifter.table:
                return "outer", self.lifter.term(Col(None, node.name))[0]["col"]
            if node.qual is None:
                if node.name in self.cols:
                    return "inner", node.name
                return "outer", self.lifter.term(node)[0]["col"]
            raise Unreadable(f"column {node.qual}.{node.name} is of neither table")
        ctx = self.lifter.context(node)
        if ctx is not None:
            return "key", {"ctx": ctx[0]}
        return "key", self.lifter.const(node)[0]


def lift(text: str, table: str, env: Env, scope: str, make_opaque: MakeOpaque, make_relation: MakeRelation,
         top_is_true: bool = False) -> LiftResult:
    """Lift one expression.  `scope` is "policy" or "check"; `top_is_true` strips the CHECK
    template's outer `IS TRUE`."""
    return Lifter(env, table, scope, make_opaque, make_relation).lift(text, top_is_true)


# ----------------------------------------------------------------------------- helpers
def _is_col(term: dict) -> bool:
    return "col" in term or "old" in term or "new" in term


TRANSITION_BODY = re.compile(
    r"^\s*begin\s+if not \((.*) is true\) then\s+raise exception 'MLS transition \w+ on \w+ violated' "
    r"using errcode = 'check_violation';\s+end if;\s+return new;\s+end\s*$", re.DOTALL)
TRANSITION_TRIGGER = re.compile(r'^CREATE TRIGGER "?(\w+)"? BEFORE UPDATE ON "?(\w+)"? FOR EACH ROW EXECUTE '
                                r'(?:FUNCTION|PROCEDURE) "?(mls_tr_\w+)"?\(\)$')


def transition_expr(trigger_def: str, function: str, body: str) -> str | None:
    """The predicate text of a trigger the transition template emitted (`compile/transition.py`):
    `BEFORE UPDATE ... FOR EACH ROW EXECUTE FUNCTION mls_tr_<table>_<name>()` whose body is the
    template's `if not ((p) is true) then raise`.  None for any other trigger: a hand-written
    trigger is never read (D34)."""
    m = TRANSITION_TRIGGER.match(trigger_def.strip())
    if m is None or m.group(3) != function or m.group(3) != f"mls_tr_{m.group(2)}_{m.group(1)}":
        return None
    b = TRANSITION_BODY.match(body)
    return b.group(1) if b else None


def scan_columns(sql: str, cols: dict[str, Any]) -> set[str]:
    """The modelled columns the text names (word boundaries; `table.col` and bare `col`)."""
    return {c for c in cols if re.search(r'(?<![\w."])"?' + re.escape(c) + r'"?(?![\w"])', sql)}


def context_markers(env: Env) -> dict[str, re.Pattern]:
    """What a text must contain to read each context value (the opaque signature scan)."""
    out: dict[str, re.Pattern] = {}
    for n, (source, _) in env.context.items():
        if source == "auth.uid":
            out[n] = re.compile(r"auth\.uid\(\)")
        elif source.startswith("jwt.claims."):
            out[n] = re.compile(r"auth\.jwt\(\)\s*->>\s*'" + re.escape(source[len("jwt.claims."):]) + "'")
        elif source.startswith("app."):
            out[n] = re.compile(r"current_setting\('" + re.escape(source) + "'")
        elif source == "current_user":
            out[n] = re.compile(r"\bcurrent_user\b|\bsession_user\b", re.IGNORECASE)
    return out


def columns_of(node: Node, out: set[str]) -> set[str]:
    """Every column name a parsed tree references (any qualification)."""
    if isinstance(node, Col):
        out.add(node.name)
    for f in getattr(node, "__dataclass_fields__", {}):
        v = getattr(node, f)
        if isinstance(v, Node):
            columns_of(v, out)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, Node):
                    columns_of(x, out)
    return out


def nullable_columns(text: str, cols: dict[str, tuple[ColType, bool]]) -> set[str] | None:
    """The nullable columns a bare CHECK expression references, or None when it does not parse."""
    try:
        tree = parse(text)
    except Unreadable:
        return None
    return {c for c in columns_of(tree, set()) if c in cols and cols[c][1]}


def now_between_shape(p: dict) -> dict:
    """The compiled now_between is `(f is null or f <= now) and (u is null or now < u)`; fold the
    lifted `or`s back into the construct, anywhere in the predicate."""
    if "and" in p:
        out: list[dict] = []
        pending: dict | None = None  # a `from` half waiting for its `until`
        for x in p["and"]:
            f, u = _half(x)
            if f is None and u is None:
                if pending is not None:
                    out.append(pending)
                    pending = None
                out.append(now_between_shape(x))
            elif f is not None:
                if pending is not None:
                    out.append(pending)
                pending = {"now_between": {"from": f, "until": None}}
            else:
                if pending is not None:
                    pending["now_between"]["until"] = u
                    out.append(pending)
                    pending = None
                else:
                    out.append({"now_between": {"from": None, "until": u}})
        if pending is not None:
            out.append(pending)
        return out[0] if len(out) == 1 else {"and": out}
    if "or" in p:
        f, u = _half(p)
        if f is not None or u is not None:
            return {"now_between": {"from": f, "until": u}}
        return {"or": [now_between_shape(x) for x in p["or"]]}
    if "not" in p:
        return {"not": now_between_shape(p["not"])}
    return p


def _half(p: dict | None) -> tuple[dict | None, dict | None]:
    """(from, until) when `p` is `{or: [{is_null: c}, {cmp: [c, <=, now]}]}` / `{cmp: [c, >, now]}`."""
    if p is None or "or" not in p or len(p["or"]) != 2:
        return None, None
    a, b = p["or"]
    if "is_null" not in a or "cmp" not in b:
        return None, None
    c = a["is_null"]
    l, op, r = b["cmp"]
    if l != c or r != {"ctx": CONTEXT_NOW} or "col" not in c:
        return None, None
    if op == "<=":
        return c, None
    if op == ">":
        return None, c
    return None, None
