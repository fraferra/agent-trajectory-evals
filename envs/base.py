"""Environment protocol for trajectory-level agent evaluation.

An environment here is not a chat wrapper. It owns real, inspectable state and
can answer three questions independently:

    check_goal()        did the agent do the task?
    check_invariants()  did the agent break anything it was not asked to touch?
    check_spec()        did the agent satisfy the goal legitimately?

Keeping those three separate is the whole design. Existing evals collapse them
into one pass/fail, which is why an agent that deletes an unrelated file on its
way to a correct answer, or hardcodes a test instead of fixing the bug, is
scored as a clean success.

Implementations must be deterministic given a seed and fully resettable.
Anything that lets a model write code or touch a filesystem runs containerized,
with no network and no host mount.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol, Self, runtime_checkable

from pydantic import BaseModel, Field

from atrace.schema import (
    Action,
    ActionResult,
    ContextFeatures,
    GoalResult,
    StateSnapshot,
    Violation,
)


class TaskSpec(BaseModel):
    """The task, stated precisely enough to be checked from three angles."""

    task_id: str
    task_type: str = Field(description="Grouping key for analysis and bandit context.")
    prompt: str = Field(description="What the agent is told.")

    constraints: list[str] = Field(
        default_factory=list,
        description="Stated up front and expected to hold for the whole episode. "
        "Violating one late in a long episode is MEMORY_DEGRADATION, which is why "
        "these are stored separately from the prompt text.",
    )
    spec_rules: list[str] = Field(
        default_factory=list,
        description="Rules the solution must satisfy beyond the goal check — "
        "'do not modify the test file', 'do not hardcode the expected value'. "
        "Breaking one while the goal check passes is LOOPHOLE_SUCCESS.",
    )
    alt_goals: dict[str, str] = Field(
        default_factory=dict,
        description="Plausible adjacent goals the agent might satisfy instead. "
        "Used to distinguish GOAL_DRIFT (did a different task competently) from "
        "ordinary failure.",
    )

    max_steps: int = 25
    tools: list[str] = Field(default_factory=list)


class Observation(BaseModel):
    """What the agent sees. Distinct from state — the agent gets a partial view."""

    text: str
    data: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class Env(Protocol):
    """The contract every environment implements."""

    spec: TaskSpec

    def reset(self, seed: int = 0) -> Observation: ...

    def step(self, action: Action) -> tuple[Observation, ActionResult]: ...

    def snapshot_state(self) -> StateSnapshot:
        """Structured, hashable environment state.

        Called on every step, so keep it cheap. Hash file trees and row counts;
        do not serialize entire file contents.
        """
        ...

    def check_goal(self) -> GoalResult: ...

    def check_invariants(self) -> list[Violation]: ...

    def check_spec(self) -> list[str]: ...

    def irreversible_actions(self) -> set[str]: ...

    def tool_schemas(self) -> list[dict[str, Any]]: ...

    def close(self) -> None: ...


class BaseEnv(ABC):
    """Convenience base class. Implement the four `_` hooks and the rest follows."""

    def __init__(self, spec: TaskSpec) -> None:
        self.spec = spec
        self._step_count = 0
        self._closed = False

    # -- required hooks ---------------------------------------------------- #

    @abstractmethod
    def _reset(self, seed: int) -> Observation: ...

    @abstractmethod
    def _apply(self, action: Action) -> tuple[Observation, ActionResult]: ...

    @abstractmethod
    def snapshot_state(self) -> StateSnapshot: ...

    @abstractmethod
    def check_goal(self) -> GoalResult: ...

    # -- overridable defaults ---------------------------------------------- #

    def check_invariants(self) -> list[Violation]:
        """Default: nothing to protect. Override — this is the differentiator.

        An environment with no invariants cannot surface collateral mutation,
        which is the failure mode most worth measuring.
        """
        return []

    def check_spec(self) -> list[str]:
        """Default: no spec rules broken. Override to catch loophole successes."""
        return []

    def irreversible_actions(self) -> set[str]:
        return set()

    def tool_schemas(self) -> list[dict[str, Any]]:
        """Provider-shaped tool schemas, one per entry in `spec.tools`.

        The permissive default exists so toy environments work without
        boilerplate. Override it for any environment a model actually runs
        against — vague schemas produce vague tool calls, and a benchmark
        measuring agent behaviour should not be measuring its own bad schemas.
        """
        return [
            {
                "name": tool,
                "description": f"Invoke the {tool} operation.",
                "input_schema": {"type": "object", "properties": {}},
            }
            for tool in self.spec.tools
        ]

    # -- driver ------------------------------------------------------------ #

    def reset(self, seed: int = 0) -> Observation:
        self._step_count = 0
        self._closed = False
        return self._reset(seed)

    def step(self, action: Action) -> tuple[Observation, ActionResult]:
        if self._closed:
            raise RuntimeError("step() called on a closed environment")
        action.reversible = action.tool not in self.irreversible_actions()
        self._step_count += 1
        return self._apply(action)

    def context(self, **overrides: Any) -> ContextFeatures:
        """Build the per-step bandit context from environment state."""
        base = ContextFeatures(
            task_type=self.spec.task_type,
            turn_idx=self._step_count,
            steps_remaining=max(self.spec.max_steps - self._step_count, 0),
            tools_available=list(self.spec.tools),
        )
        return base.model_copy(update=overrides)

    @property
    def exhausted(self) -> bool:
        return self._step_count >= self.spec.max_steps

    def close(self) -> None:
        self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
