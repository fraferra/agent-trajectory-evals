"""OpenAI-compatible adapter — one implementation, many model families.

The Chat Completions shape is the de-facto interoperability layer: OpenAI,
OpenRouter, Together, Fireworks, DeepSeek, Groq, and a local vLLM server all
speak it. Pointing `base_url` at a different host is the whole difference, so
this single adapter is what opens the benchmark to non-Anthropic models —
including the weaker and open-weight ones, which is the population the shortcut
hypothesis was always about.

Two shape differences from `anthropic_adapter` are worth knowing:

* **Tool schemas.** Anthropic sends `{name, description, input_schema}`.
  OpenAI wraps it: `{"type": "function", "function": {..., "parameters": ...}}`.
  `to_openai_tools` converts, so environments keep emitting one shape.
* **Tool arguments arrive as a JSON *string*.** Anthropic hands back a parsed
  object; here it is text the model generated, and weak models generate invalid
  JSON. See `_parse_args`.

ROBUSTNESS
----------
Every defensive branch below exists because the models this adapter was written
to reach are less reliable than Claude, not because the code is timid. A sweep
that dies on episode 34 of 60 because a 7B model emitted a trailing comma has
measured nothing, and the failure is the harness's, not the model's. Where a
response cannot be interpreted, the adapter records the fact in the trace and
lets the episode end — it never silently substitutes a well-formed action for a
malformed one, because that would launder a model's failure into the harness's
own competence.
"""

from __future__ import annotations

import json
from typing import Any

from atrace.schema import Action
from harness.adapters.base import ToolCall, ToolOutcome, Turn, Usage

MAX_TOKENS = 16000

EFFORT_TO_REASONING: dict[str, str] = {"low": "low", "medium": "medium", "high": "high"}
"""`effort` maps onto `reasoning_effort` for models that accept it.

Sent only when the caller declares the model supports it — see `ModelSpec` in
`harness/models.py`. Providers differ on whether an unknown parameter is ignored
or rejected with a 400, and discovering that mid-sweep costs episodes.
"""


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert environment tool schemas from Anthropic shape to OpenAI shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in tools
    ]


def _parse_args(raw: str | None) -> tuple[dict[str, Any], str | None]:
    """Parse tool-call arguments, reporting failure rather than guessing.

    Returns `(args, error)`. On malformed JSON the args are empty and the error
    is a string — the runner passes the empty args to the environment, which
    rejects them, and the failed step is recorded like any other tool error.
    That is the honest outcome: the model emitted something unusable and the
    trace should say so.

    An earlier version attempted repair (stripping trailing commas, closing
    braces). It was removed: a repaired call is the harness's action, not the
    model's, and it would silently improve the score of exactly the weak models
    the benchmark is trying to characterize.
    """
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        return {}, f"unparseable tool arguments: {exc}"
    if not isinstance(parsed, dict):
        return {}, f"tool arguments were {type(parsed).__name__}, expected object"
    return parsed, None


REQUIRE_TOOL_SUPPORT: dict[str, Any] = {"provider": {"require_parameters": True}}
"""OpenRouter routing: only use hosts that accept every parameter sent.

A model slug on OpenRouter is served by several hosts. When one of them ignores
or mishandles `tools`, requests routed there come back with no content and no
tool calls, having billed a handful of output tokens — and the next request for
the same slug may land somewhere that works, so the failure is intermittent and
looks like the model rather than the routing. This is the documented control for
it, and it is off by default because it narrows the host pool and can raise
latency and price.
"""


def _describe_empty(choice: Any, message: Any) -> str:
    """Compact record of a response that carried neither text nor tool calls.

    Deliberately a string rather than structured data: it is read by a human
    debugging one episode, not by an analysis pass, and the useful content is
    whatever non-standard field the provider decided to use — which cannot be
    known in advance.
    """
    parts = [f"finish_reason={getattr(choice, 'finish_reason', None)!r}"]
    for attr in ("content", "reasoning", "reasoning_content", "refusal",
                 "function_call", "tool_calls"):
        value = getattr(message, attr, None)
        if value:
            parts.append(f"{attr}={str(value)[:400]!r}")
    extra = getattr(message, "model_extra", None) or {}
    for key, value in list(extra.items())[:6]:
        if value:
            parts.append(f"extra.{key}={str(value)[:400]!r}")
    if len(parts) == 1:
        parts.append("message carried no recognisable field")
    return " | ".join(parts)


class OpenAIAdapter:
    """Turn-taking wrapper around any OpenAI-compatible Chat Completions API."""

    def __init__(
        self,
        model: str,
        effort: str = "high",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        supports_effort: bool = False,
        pricing: tuple[float, float] = (0.0, 0.0),
        client: Any | None = None,
        label: str | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.api_model = model
        self.model = label or model
        """Trace-facing name. Distinct from `api_model` so an OpenRouter slug and
        a direct-provider id for the same weights land in the same results cell."""

        self.effort = effort
        self.supports_effort = supports_effort
        self.extra_body = extra_body or {}
        """Passed through to the provider verbatim.

        Exists for OpenRouter's routing controls. OpenRouter load-balances one
        model slug across several hosts, and they do not all implement tool
        calling identically, so which host serves a request can decide whether
        the episode runs at all. `{"provider": {"require_parameters": True}}`
        restricts routing to hosts that accept every parameter sent — `tools`
        included. See ROUTING in this module."""
        self.pricing = pricing
        self._messages: list[dict[str, Any]] = []
        self._tools: list[dict[str, Any]] = []
        self._usage = Usage()

        if client is not None:
            self.client = client
        else:
            import openai  # imported lazily: the package is an optional extra

            self.client = openai.OpenAI(api_key=api_key, base_url=base_url)

    # -- protocol ---------------------------------------------------------- #

    def start(self, system: str, tools: list[dict], observation: str) -> Turn:
        self._tools = to_openai_tools(tools)
        self._messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": observation},
        ]
        self._usage = Usage()
        return self._advance()

    def send_results(self, outcomes: list[ToolOutcome]) -> Turn:
        """One `tool` message per tool_call_id, or the next request is rejected."""
        for o in outcomes:
            self._messages.append(
                {
                    "role": "tool",
                    "tool_call_id": o.call_id,
                    "content": o.output or "(no output)",
                }
            )
        return self._advance()

    def total_usage(self) -> Usage:
        return self._usage

    # -- internals --------------------------------------------------------- #

    def _advance(self) -> Turn:
        kwargs: dict[str, Any] = {
            "model": self.api_model,
            "max_tokens": MAX_TOKENS,
            "messages": self._messages,
            "tools": self._tools,
        }
        if self.supports_effort and self.effort in EFFORT_TO_REASONING:
            kwargs["reasoning_effort"] = EFFORT_TO_REASONING[self.effort]
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body

        response = self.client.chat.completions.create(**kwargs)

        turn = Turn(usage=self._account(response))
        self._usage = self._usage + turn.usage

        if not getattr(response, "choices", None):
            # Some gateways return an empty choices list on a content filter.
            turn.stop_reason = "empty_response"
            turn.text = "[no choices returned]"
            return turn

        choice = response.choices[0]
        message = choice.message
        turn.stop_reason = choice.finish_reason or ""

        turn.text = (message.content or "").strip()
        turn.thinking = (getattr(message, "reasoning", None) or "").strip()

        # The assistant message must go back verbatim, tool calls included, or
        # the provider rejects the follow-up for having tool results with no
        # matching call.
        assistant: dict[str, Any] = {"role": "assistant", "content": message.content}
        raw_calls = getattr(message, "tool_calls", None) or []
        if raw_calls:
            assistant["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.function.name, "arguments": c.function.arguments},
                }
                for c in raw_calls
            ]
        self._messages.append(assistant)

        for c in raw_calls:
            args, error = _parse_args(c.function.arguments)
            turn.calls.append(
                ToolCall(
                    call_id=c.id,
                    action=Action(
                        tool=c.function.name,
                        args=args,
                        # `raw` is what the model actually emitted. On a parse
                        # failure it is the only record of what it tried to do.
                        raw=error or c.function.arguments,
                    ),
                )
            )

        if not turn.calls and not turn.text:
            turn.diagnostic = _describe_empty(choice, message)
        return turn

    def _account(self, response: Any) -> Usage:
        u = getattr(response, "usage", None)
        if u is None:
            return Usage()
        in_tok = getattr(u, "prompt_tokens", 0) or 0
        out_tok = getattr(u, "completion_tokens", 0) or 0
        details = getattr(u, "prompt_tokens_details", None)
        cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        in_rate, out_rate = self.pricing
        return Usage(
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read_tokens=cached,
            cost_usd=(in_tok * in_rate + out_tok * out_rate) / 1_000_000,
        )
