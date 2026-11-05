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

    @property
    def escalated(self) -> bool:
        return bool(self.escalations)

    def to_dict(self) -> dict:
        return {"status": self.status, "repair_rounds": self.repair_rounds,
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
                 timeout_ms: int = 20000):
        self.requirements = requirements
        self.skeleton = skeleton
        self.client = client
        self.max_repairs = max_repairs
        self.timeout_ms = timeout_ms
        self.rounds = 0
        self.steps: list[Step] = []
        self.notes: dict[str, str] = {}
        self._sys = {"properties": system_prompt(ROLE_PROPERTIES, skeleton, properties_schema()),
                     "rules": system_prompt(ROLE_RULES, skeleton, rules_schema())}
        self._schema = {"properties": properties_schema(), "rules": rules_schema()}

    # --- one pass, with bounded structural repair
    def _ask(self, key: str, previous: list[dict] | None, feedback: str | None) -> tuple[list[dict], list[MlsError]]:
        if previous is None:
            req = Request(key, self._sys[key], user_requirements(self.requirements), self._schema[key])
        else:
            req = Request(f"repair:{key}", self._sys[key], user_repair(self.requirements, previous, feedback or "", key),
                          self._schema[key])
            self.rounds += 1
        text = self.client.complete(req)
        items, notes, errors = _parse(text, key)
        if notes:
            self.notes[key] = notes
        self.steps.append(Step(req.name, f"{len(items) if items is not None else 0} {key}; "
                                         f"{len(errors)} structural error(s)"))
        if items is None:  # unusable answer: keep the previous one so the next repair sees it
            items = previous or []
        return items, errors

    def _pass(self, key: str) -> tuple[list[dict], list[MlsError]]:
        items, errors = self._ask(key, None, None)
        while errors and self.rounds < self.max_repairs:
            items, errors = self._ask(key, items, errors_text(errors))
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
            return LoopResult("failed", None, [], [], self.rounds, errors=[MlsError("$", str(e))], steps=self.steps)
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
                                              errors=errors, steps=self.steps, notes=self.notes)
                        return LoopResult("invalid", spec, props, rules, self.rounds, errors=errors,
                                          steps=self.steps, notes=self.notes)
                    perr = [e for e in errors if e.path.startswith("$.properties")]
                    rerr = [e for e in errors if not e.path.startswith("$.properties")]
                    errors = []
                    if perr:
                        props, errors = self._ask("properties", props, errors_text(perr))
                    if rerr and self.rounds < self.max_repairs:
                        rules, more = self._ask("rules", rules, errors_text(rerr))
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
                                      steps=self.steps, notes=self.notes)
                last = (spec, rules, bad, verdicts)
                if self.rounds >= self.max_repairs:
                    esc = [self._escalation(spec, r) for r in bad]
                    return LoopResult("escalated", spec, props, rules, self.rounds, esc, verdicts,
                                      steps=self.steps, notes=self.notes)
                before = rules
                rules, errors = self._ask("rules", rules, self._counterexamples(spec, bad) + self._hints(spec))
                if not errors and rules == before:
                    # the rules pass stands by its rules (typically: it judges the property wrong);
                    # more rounds would only repeat the exchange, so escalate now
                    self.steps.append(Step("stop", "the rules pass returned its rules unchanged"))
                    esc = [self._escalation(spec, r) for r in bad]
                    return LoopResult("escalated", spec, props, rules, self.rounds, esc, verdicts,
                                      steps=self.steps, notes=self.notes)
        except LlmError as e:
            return LoopResult("failed", spec, props, rules, self.rounds, errors=[MlsError("$", str(e))],
                              steps=self.steps, notes=self.notes)

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
             start_rules: list[dict] | None = None, start_properties: list[dict] | None = None) -> LoopResult:
    """Run the loop.  `start_rules` / `start_properties` skip the corresponding pass (used to
    evaluate repair from a given rule set)."""
    return Loop(requirements, skeleton, client, max_repairs, timeout_ms).run(start_rules, start_properties)
