"""The loop (D16): pass 1 writes properties, pass 2 writes rules without seeing pass 1; the
checker and the solver judge; check errors and counterexamples (as English scenarios) drive a
bounded number of repair rounds; what is still refuted goes to a human as scenarios."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from mls import english
from mls.errors import CompileError, MlsError, SpecError
from mls.llm.client import Client, LlmError, Request
from mls.llm.prompts import (
    ROLE_PROPERTIES, ROLE_RULES, errors_text, system_prompt, user_repair, user_requirements,
)
from mls.llm.schemas import properties_schema, rules_schema
from mls.spec import check_spec
from mls.spec.check import pydantic_errors_to_mls
from mls.spec.model import Property, Rule, Spec
from mls.spec.syntax import Source, parse_section

DEFAULT_SYNTAX = "terse"  # D50 as amended 2026-11-05: the corpus run (docs/LLM.md) made terse the default


@dataclass
class Escalation:
    property_id: str
    statement: str           # the property in English
    verdict: str
    scenario: str            # the counterexample in English ("" for UNKNOWN)
    notes: str = ""          # what the rules pass said about the requirements

    def __str__(self) -> str:
        s = f"{self.property_id} ({self.verdict}): {self.statement}"
        if self.scenario:
            s += f"\n  counterexample: {self.scenario}"
        if self.notes:
            s += f"\n  the rules pass noted: {self.notes}"
        return s


@dataclass
class Step:
    stage: str               # properties | rules | repair:properties | repair:rules | check | prove
    detail: str


@dataclass
class LoopResult:
    status: str              # proved | escalated | invalid | failed
    spec: Spec | None
    properties: list[dict]
    rules: list[dict]
    repair_rounds: int
    escalations: list[Escalation] = field(default_factory=list)
    verdicts: dict[str, str] = field(default_factory=dict)
    errors: list[MlsError] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)
    syntax: str = "json"     # what the passes answered in (D50)

    @property
    def escalated(self) -> bool:
        return bool(self.escalations)

    def to_dict(self) -> dict:
        return {"status": self.status, "syntax": self.syntax, "repair_rounds": self.repair_rounds,
                "verdicts": self.verdicts, "escalations": [str(e) for e in self.escalations],
                "errors": [e.to_dict() for e in self.errors],
                "steps": [{"stage": s.stage, "detail": s.detail} for s in self.steps],
                "notes": self.notes,
                "spec": self.spec.model_dump(by_alias=True, exclude_none=True) if self.spec else None}


class _PropsWrap(BaseModel):
    properties: list[Property]


class _RulesWrap(BaseModel):
    rules: list[Rule]


_FENCE = re.compile(r"```[a-zA-Z]*\s*\n(.*?)\n\s*```", re.S)


def _decode(text: str):
    """The answer as JSON: the whole text, else a ```json``` block, else the first decodable
    object in the text (models sometimes explain before or after the JSON; decoding is
    unconstrained, D37)."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as first:
        err = first
    m = _FENCE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                return dec.raw_decode(text, i)[0]
            except json.JSONDecodeError:
                continue
    raise err


def _section_in(text: str, key: str) -> tuple[str, int, int] | None:
    """The first `key {` at a line start and its balanced closing brace (quotes and `#` comments
    skipped): (section text, start, end), or None."""
    m = re.search(rf"(?m)^[ \t]*{key}\s*\{{", text)
    if m is None:
        return None
    i, depth, quote = m.start(), 0, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#":
            i = text.find("\n", i)
            if i < 0:
                break
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[m.start():i + 1], m.start(), i + 1
        i += 1
    return None


def _terse_answer(text: str, key: str):
    """A terse answer (D50) -> (section text, notes, Parsed) or (None, "", errors).  The section
    is the fenced block that parses, else the first `key {` section at a line start (balanced
    braces), else the whole text; the prose outside it is the notes.  When nothing parses, the
    error of the candidate that was a section (a syntax error inside it, with its line:col)
    beats "no section found"."""
    candidates = [(m.group(1), text[:m.start()] + text[m.end():]) for m in _FENCE.finditer(text)]
    found = _section_in(text, key)
    if found is not None:
        body, s, e = found
        candidates.append((body, text[:s] + text[e:]))
    candidates.append((text, ""))
    inside: tuple[str, SpecError] | None = None
    for body, notes in candidates:
        try:
            parsed = parse_section(body, key, "<answer>")
        except SpecError as err:
            if inside is None and body.lstrip().startswith(key):
                inside = (body, err)
            continue
        return body, re.sub(r"\n{3,}", "\n\n", notes.strip()), parsed
    if inside is not None:
        return inside[0], "", inside[1].errors
    return None, "", [MlsError("$", f"the answer holds no `{key} {{ ... }}` section",
                               f"answer with one `{key} {{ ... }}` section (the grammar is in the system prompt); "
                               "any notes in prose outside it")]


def _parse_terse(text: str, key: str):
    """Terse text -> (items, notes, errors, (section text, Source) | None); items is None when
    the text is not usable (the section text is still kept when a section was found, so the
    repair quotes what the model wrote and the line:col of the syntax error point into it).
    Schema errors are located in the section text."""
    body, notes, res = _terse_answer(text, key)
    if isinstance(res, list):
        return None, "", res, ((body, Source("<answer>", body)) if body is not None else None)
    items = res.data[key]
    source = Source("<answer>", body, res.spans)
    wrap = _PropsWrap if key == "properties" else _RulesWrap
    try:
        wrap.model_validate({key: items})
    except ValidationError as e:
        return items, notes, source.locate(pydantic_errors_to_mls(e)), (body, source)
    return items, notes, [], (body, source)


def _parse(text: str, key: str) -> tuple[list[dict] | None, str, list[MlsError]]:
    """JSON text -> (items, notes, errors); items is None when the text is not usable."""
    try:
        data = _decode(text)
    except json.JSONDecodeError as e:
        return None, "", [MlsError("$", f"the answer is not valid JSON: {e.msg}", "answer with a single JSON object")]
    if not isinstance(data, dict) or key not in data or not isinstance(data[key], list):
        return None, "", [MlsError("$", f"the answer must be an object with a {key!r} array",
                                   f'{{"{key}": [...], "notes": "..."}}')]
    notes = str(data.get("notes") or "")
    wrap = _PropsWrap if key == "properties" else _RulesWrap
    try:
        wrap.model_validate({key: data[key]})
    except ValidationError as e:
        return data[key], notes, pydantic_errors_to_mls(e)
    return data[key], notes, []


class Loop:
    def __init__(self, requirements: str, skeleton: dict, client: Client, max_repairs: int = 3,
                 timeout_ms: int = 20000, syntax: str = DEFAULT_SYNTAX):
        if syntax not in ("json", "terse"):
            raise ValueError(f"syntax is json or terse, not {syntax!r}")
        self.requirements = requirements
        self.skeleton = skeleton
        self.client = client
        self.max_repairs = max_repairs
        self.timeout_ms = timeout_ms
        self.syntax = syntax
        self.rounds = 0
        self.steps: list[Step] = []
        self.notes: dict[str, str] = {}
        self._sys = {"properties": system_prompt(ROLE_PROPERTIES, skeleton, properties_schema(), syntax),
                     "rules": system_prompt(ROLE_RULES, skeleton, rules_schema(), syntax)}
        self._schema = {"properties": properties_schema(), "rules": rules_schema()} if syntax == "json" else {}
        self._texts: dict[str, str] = {}       # terse: the last usable section text per pass, as written
        self._sources: dict[str, Source] = {}  # terse: its spans, to locate the checker's errors in it

    # --- one pass, with bounded structural repair
    def _quoted(self, key: str, previous: list[dict]) -> list[dict] | str:
        """What a repair request quotes as the previous answer: the JSON, or (terse) the section
        as the model wrote it, else rendered from the items (the repair benchmark's start rules)."""
        if self.syntax == "json":
            return previous
        if key in self._texts:
            return self._texts[key]
        from mls.terse import section

        wrap = _PropsWrap if key == "properties" else _RulesWrap
        try:
            return section(key, getattr(wrap.model_validate({key: previous}), key))
        except ValidationError:
            return json.dumps(previous, indent=1)

    def _ask(self, key: str, previous: list[dict] | None, feedback: str | None) -> tuple[list[dict], list[MlsError]]:
        schema = self._schema.get(key)
        if previous is None:
            req = Request(key, self._sys[key], user_requirements(self.requirements), schema)
        else:
            req = Request(f"repair:{key}", self._sys[key],
                          user_repair(self.requirements, self._quoted(key, previous), feedback or "", key), schema)
            self.rounds += 1
        text = self.client.complete(req)
        if self.syntax == "json":
            items, notes, errors = _parse(text, key)
        else:
            items, notes, errors, src = _parse_terse(text, key)
            if src is not None:
                self._texts[key], self._sources[key] = src
        if notes:
            self.notes[key] = notes
        self.steps.append(Step(req.name, f"{len(items) if items is not None else 0} {key}; "
                                         f"{len(errors)} structural error(s)"))
        if items is None:  # unusable answer: keep the previous one so the next repair sees it
            items = previous or []
        return items, errors

    def _feedback(self, key: str, errors: list[MlsError]) -> str:
        """The errors as feedback; on a terse answer, located in the section the model wrote."""
        if key in self._sources:
            errors = self._sources[key].locate(errors)
        return errors_text(errors)

    def _pass(self, key: str) -> tuple[list[dict], list[MlsError]]:
        items, errors = self._ask(key, None, None)
        while errors and self.rounds < self.max_repairs:
            items, errors = self._ask(key, items, self._feedback(key, errors))
        return items, errors

    # --- assemble and judge
    def _spec(self, rules: list[dict], props: list[dict]) -> tuple[Spec | None, list[MlsError]]:
        data = {**self.skeleton, "rules": rules, "properties": props}
        try:
            spec = Spec.model_validate(data)
        except ValidationError as e:
            return None, pydantic_errors_to_mls(e)
        try:
            spec = check_spec(spec)
        except SpecError as e:
            return spec, e.errors
        from mls.compile.plan import soundness_errors
        try:
            errs = soundness_errors(spec)
        except CompileError as e:
            errs = e.errors
        return spec, errs

    def run(self, start_rules: list[dict] | None = None, start_properties: list[dict] | None = None) -> LoopResult:
        try:
            props, perrs = (start_properties, []) if start_properties is not None else self._pass("properties")
            rules, rerrs = (start_rules, []) if start_rules is not None else self._pass("rules")
        except LlmError as e:
            return LoopResult("failed", None, [], [], self.rounds, errors=[MlsError("$", str(e))], steps=self.steps,
                              syntax=self.syntax)
        errors = perrs + rerrs
        spec: Spec | None = None
        last = None  # the last checked spec that was proved against: (spec, rules, bad, verdicts)
        try:
            while True:
                if not errors:
                    spec, errors = self._spec(rules, props)
                    self.steps.append(Step("check", f"{len(errors)} error(s)"))
                if errors:
                    if self.rounds >= self.max_repairs:
                        if last is not None:
                            # the final repair did not check: hand the human the last rules that did
                            self.steps.append(Step("stop", "the last repair does not check; escalating the "
                                                           "last checked rules"))
                            spec, rules, bad, verdicts = last
                            esc = [self._escalation(spec, r) for r in bad]
                            return LoopResult("escalated", spec, props, rules, self.rounds, esc, verdicts,
                                              errors=errors, steps=self.steps, notes=self.notes, syntax=self.syntax)
                        return LoopResult("invalid", spec, props, rules, self.rounds, errors=errors,
                                          steps=self.steps, notes=self.notes, syntax=self.syntax)
                    perr = [e for e in errors if e.path.startswith("$.properties")]
                    rerr = [e for e in errors if not e.path.startswith("$.properties")]
                    errors = []
                    if perr:
                        props, errors = self._ask("properties", props, self._feedback("properties", perr))
                    if rerr and self.rounds < self.max_repairs:
                        rules, more = self._ask("rules", rules, self._feedback("rules", rerr))
                        errors += more
                    continue
                assert spec is not None
                from mls.props.runner import run_all
                results = run_all(spec, self.timeout_ms)
                verdicts = {r.id: r.verdict for r in results}
                bad = [r for r in results if r.verdict != "PROVED"]
                self.steps.append(Step("prove", ", ".join(f"{k}={v}" for k, v in verdicts.items())))
                if not bad:
                    return LoopResult("proved", spec, props, rules, self.rounds, verdicts=verdicts,
                                      steps=self.steps, notes=self.notes, syntax=self.syntax)
                last = (spec, rules, bad, verdicts)
                if self.rounds >= self.max_repairs:
                    esc = [self._escalation(spec, r) for r in bad]
                    return LoopResult("escalated", spec, props, rules, self.rounds, esc, verdicts,
                                      steps=self.steps, notes=self.notes, syntax=self.syntax)
                before = rules
                rules, errors = self._ask("rules", rules, self._counterexamples(spec, bad) + self._hints(spec))
                if not errors and rules == before:
                    # the rules pass stands by its rules (typically: it judges the property wrong);
                    # more rounds would only repeat the exchange, so escalate now
                    self.steps.append(Step("stop", "the rules pass returned its rules unchanged"))
                    esc = [self._escalation(spec, r) for r in bad]
                    return LoopResult("escalated", spec, props, rules, self.rounds, esc, verdicts,
                                      steps=self.steps, notes=self.notes, syntax=self.syntax)
        except LlmError as e:
            return LoopResult("failed", spec, props, rules, self.rounds, errors=[MlsError("$", str(e))],
                              steps=self.steps, notes=self.notes, syntax=self.syntax)

    # --- feedback
    def _counterexamples(self, spec: Spec, bad) -> str:
        lines = ["The rules violate these properties (each property restates a requirement; the rules "
                 "must be changed so that every property holds):"]
        for r in bad:
            lines.append(f"\n## {r.id}: {self._statement(spec, r.id)}  — {r.verdict}")
            for o in r.obligations:
                if o.verdict == "PROVED":
                    continue
                lines.append(f"- {o.name}: {o.verdict}" + (f" — {o.message}" if o.message else ""))
                if o.scenario:
                    lines.append(f"  counterexample: {english.scenario(spec, o.scenario)}")
        return "\n".join(lines)

    def _hints(self, spec: Spec) -> str:
        """Lint findings (D49) as hints: hygiene the model may act on, never a violation."""
        from mls.props.lint import lint_spec

        findings = lint_spec(spec, self.timeout_ms)
        if not findings:
            return ""
        return ("\n\n# Hints (lint findings: hygiene, not violations; act on them only where it serves "
                "the properties)\n" + "\n".join(f"- {f}" for f in findings))

    @staticmethod
    def _statement(spec: Spec, pid: str) -> str:
        p = next(p for p in spec.properties if p.id == pid)
        return english.prop(spec, p)

    def _escalation(self, spec: Spec, r) -> Escalation:
        o = next((o for o in r.obligations if o.verdict != "PROVED"), None)
        sc = english.scenario(spec, o.scenario) if o is not None and o.scenario else ""
        return Escalation(r.id, self._statement(spec, r.id), r.verdict, sc, self.notes.get("rules", ""))


def generate(requirements: str, skeleton: dict, client: Client, max_repairs: int = 3, timeout_ms: int = 20000,
             start_rules: list[dict] | None = None, start_properties: list[dict] | None = None,
             syntax: str = DEFAULT_SYNTAX) -> LoopResult:
    """Run the loop.  `start_rules` / `start_properties` skip the corresponding pass (used to
    evaluate repair from a given rule set).  `syntax` is what the passes answer in (D50)."""
    return Loop(requirements, skeleton, client, max_repairs, timeout_ms, syntax).run(start_rules, start_properties)
