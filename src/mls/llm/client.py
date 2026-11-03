"""LLM clients.  `AnthropicClient` calls the Claude API with structured outputs constrained by
the JSON Schema of the pass (D15/D16); `ScriptedClient` replays canned answers for tests and
offline evaluation.  Every client returns the raw JSON text of one completion."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_MODEL = "claude-opus-5-5"


class LlmError(Exception):
    pass


@dataclass
class Request:
    name: str                  # "properties" | "rules" | "repair:..."
    system: str                # stable text first (cached prefix)
    user: str
    schema: dict | None = None  # JSON Schema the answer must satisfy


@dataclass
class Exchange:
    request: Request
    response: str
    usage: dict[str, Any] = field(default_factory=dict)


class Client(Protocol):
    def complete(self, req: Request) -> str: ...


class ScriptedClient:
    """Answers from a script: each entry is a dict/list (JSON-encoded), a string, or a callable
    taking the Request and returning one of those.  Records every exchange."""

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.exchanges: list[Exchange] = []

    def complete(self, req: Request) -> str:
        if not self.responses:
            raise LlmError(f"script exhausted at request {req.name!r}")
        r = self.responses.pop(0)
        if callable(r):
            r = r(req)
        text = r if isinstance(r, str) else json.dumps(r)
        self.exchanges.append(Exchange(req, text))
        return text

    @classmethod
    def from_file(cls, path) -> "ScriptedClient":
        return cls(json.loads(open(path).read()))


class AnthropicClient:
    """Claude via the official SDK.  The system prompt is cached (prefix); server-side refusal
    fallbacks are enabled (`fallbacks: "default"`).  Decoding is *not* constrained by default
    (D37): the API's structured outputs reject recursive schemas, and the predicate grammar is
    recursive; a free-form envelope is worse still (nested values are forced to scalars).  The
    schema is in the system prompt and enforced locally.  `use_schema=True` sends it as
    `output_config.format` anyway (for an API that accepts it); if rejected, the request is retried
    without it and `schema_rejected` keeps the reason."""

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "high", max_tokens: int = 16000,
                 use_schema: bool = False):
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.use_schema = use_schema
        self.schema_rejected: str | None = None
        self.exchanges: list[Exchange] = []

    def complete(self, req: Request) -> str:
        constrained = self.use_schema and req.schema is not None
        try:
            resp = self._create(req, constrained)
        except self._anthropic.BadRequestError as e:
            if constrained and "schema" in str(e).lower():
                self.use_schema = constrained = False  # prompt-only JSON for the rest of the session
                self.schema_rejected = e.message
                resp = self._create(req, False)
            else:
                raise LlmError(f"bad request: {e.message}") from e
        if resp.stop_reason == "refusal":
            raise LlmError(f"the model declined the request ({getattr(resp.stop_details, 'category', None)})")
        if resp.stop_reason == "max_tokens":
            raise LlmError("the answer was cut off at max_tokens")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens,
                 "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", None),
                 "constrained": constrained}
        self.exchanges.append(Exchange(req, text, usage))
        return text

    def _create(self, req: Request, with_schema: bool):
        output_config: dict[str, Any] = {"effort": self.effort}
        if with_schema:
            output_config["format"] = {"type": "json_schema", "schema": req.schema}
        return self._client.beta.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=[{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": req.user}],
            output_config=output_config,
        )


def client_from_env(model: str = DEFAULT_MODEL, effort: str = "high") -> Client:
    """`MLS_LLM_SCRIPT=<file>` selects a scripted client (tests, offline runs)."""
    import os

    script = os.environ.get("MLS_LLM_SCRIPT")
    if script:
        return ScriptedClient.from_file(script)
    return AnthropicClient(model=model, effort=effort)
