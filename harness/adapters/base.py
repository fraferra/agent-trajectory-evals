"""Model adapter protocol.

The adapter owns the conversation; the **runner** owns the environment and the
loop. That split is deliberate: trace recording has to happen at the boundary of
every individual tool call, so the harness cannot delegate the loop to an SDK
tool runner that executes tools internally.

An adapter is a turn-taking interface:

    start(system, tools, observation)  -> Turn
    send_results(outcomes)             -> Turn

Each `Turn` reports the assistant's user-visible text, the tool calls it wants
executed, and token usage. The runner executes the calls against the
environment, snapshots state around each one, and feeds results back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from atrace.schema import Action


@dataclass
class ToolCall:
    """A tool call the model wants executed."""

    call_id: str
    """Provider-side ID. Must be echoed back so the result pairs correctly."""
    action: Action


@dataclass
class ToolOutcome:
    """The result of executing one `ToolCall`, on its way back to the model."""

    call_id: str
    output: str
    is_error: bool = False


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


@dataclass
class Turn:
    """One assistant turn."""

    text: str = ""
    """User-visible text only.

    Reasoning/thinking is deliberately NOT included here — REASONING_LEAKAGE is
    defined as internal reasoning appearing in the *user-visible* channel, so
    mixing the two would make the detector vacuous.
    """

    thinking: str = ""
    """Summarized reasoning, when the provider returns it. Recorded, not graded."""

    calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    usage: Usage = field(default_factory=Usage)

    diagnostic: str = ""
    """Set when a turn yields neither text nor tool calls.

    An empty turn ends the episode, and without this there is nothing to debug
    from afterwards: the trace shows zero steps and a token count. Populated by
    the adapter with the provider's own view of what it sent, because the useful
    question at that point is not what the model meant but what actually
    arrived."""

    @property
    def done(self) -> bool:
        return not self.calls


class ModelAdapter(Protocol):
    """What the runner needs from a model provider."""

    model: str

    def start(self, system: str, tools: list[dict], observation: str) -> Turn:
        """Begin an episode. `tools` are provider-shaped JSON schemas."""
        ...

    def send_results(self, outcomes: list[ToolOutcome]) -> Turn:
        """Return tool results and get the next turn."""
        ...

    def total_usage(self) -> Usage: ...
