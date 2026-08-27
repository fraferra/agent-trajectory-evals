"""MDP-native trace schema for agent trajectory evaluation.

The central claim of this project is that agent evaluation should grade the
*trajectory* and the *environment state*, not the final message. Everything here
follows from that.

A trace is a sequence of (state, action, reward, next_state) transitions with
context features attached. That shape is not incidental — it is what lets the
same recorded data serve three consumers without reshaping:

    detectors/  offline failure taxonomy   (reads state, action, labels)
    diagnose/   agent-readable repair hints (reads labels + evidence)
    policy/     online contextual bandit    (reads context, action, reward)

If you are tempted to store conversation history in `StateSnapshot`, stop. That
is the thing every existing eval already does, and it is why they cannot tell a
clean success from one that broke something on the way.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, computed_field

# --------------------------------------------------------------------------- #
# Failure taxonomy
# --------------------------------------------------------------------------- #


class FailureMode(str, Enum):
    """Trajectory-level failure modes.

    Each mode must have a corresponding detection predicate in `detectors/`.
    A mode without a predicate is an essay, not an eval.

    COLLATERAL_MUTATION and LOOPHOLE_SUCCESS are the two that matter most:
    in both cases the goal check *passes*, so every output-grading eval in
    production today reports a clean success.
    """

    NO_PROGRESS_LOOP = "no_progress_loop"
    OSCILLATION = "oscillation"
    SILENT_RECOVERY = "silent_recovery"
    COLLATERAL_MUTATION = "collateral_mutation"
    PREMATURE_IRREVERSIBLE = "premature_irreversible"
    GOAL_DRIFT = "goal_drift"
    REASONING_LEAKAGE = "reasoning_leakage"
    LOOPHOLE_SUCCESS = "loophole_success"
    MEMORY_DEGRADATION = "memory_degradation"


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


class FailureLabel(BaseModel):
    """A detected failure, attached to the step where evidence first appears."""

    mode: FailureMode
    severity: Severity = Severity.WARN
    detector: str = Field(description="Detector that produced this label.")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Minimal machine-readable proof: state diffs, step indices, "
        "matched invariants. Consumed by diagnose/ to build repair hints.",
    )
    span: tuple[int, int] | None = Field(
        default=None, description="Inclusive step range (start, end) if the mode spans steps."
    )


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def canonical_digest(payload: Any) -> str:
    """Stable hash over JSON-serializable data.

    Sorted keys and fixed separators so that logically identical states hash
    identically across runs and machines. Cycle detection depends on this.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class StateSnapshot(BaseModel):
    """A structured, hashable snapshot of ENVIRONMENT state.

    `facts` holds whatever the environment considers externally checkable:
    file-tree hashes, database rows, booking records, service state. It must be
    cheap to compute — it is captured on every step.

    `identity_fields` narrows what counts for state identity. Use it to exclude
    noise (timestamps, log lines) that would otherwise defeat loop detection by
    making every state look novel.
    """

    facts: dict[str, Any] = Field(default_factory=dict)
    identity_fields: list[str] | None = Field(
        default=None,
        description="Subset of `facts` keys defining state identity. None = all keys.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def state_hash(self) -> str:
        if self.identity_fields is None:
            return canonical_digest(self.facts)
        return canonical_digest({k: self.facts.get(k) for k in sorted(self.identity_fields)})

    def diff(self, other: StateSnapshot) -> dict[str, Any]:
        """Keys that changed between two snapshots, with before/after values.

        This is the primary evidence payload for collateral-mutation detection.
        """
        keys = set(self.facts) | set(other.facts)
        return {
            k: {"before": self.facts.get(k), "after": other.facts.get(k)}
            for k in sorted(keys)
            if self.facts.get(k) != other.facts.get(k)
        }


# --------------------------------------------------------------------------- #
# Actions and results
# --------------------------------------------------------------------------- #


class Action(BaseModel):
    """The MDP action: a tool call."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    raw: str | None = Field(default=None, description="Raw model output, for leakage checks.")
    reversible: bool = Field(
        default=True,
        description="Set from Env.irreversible_actions(). Drives premature-irreversible "
        "detection — an unrecoverable action taken before the agent had enough "
        "information is a distinct failure from simply getting it wrong.",
    )


class ActionResult(BaseModel):
    ok: bool
    output: str = ""
    error: str | None = None
    latency_ms: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None


class Violation(BaseModel):
    """A broken invariant. The agent was not supposed to do this."""

    name: str
    detail: str
    severity: Severity = Severity.CRITICAL


class GoalResult(BaseModel):
    """Did the agent do the task? Kept separate from invariants on purpose.

    `achieved` and `violations` are orthogonal. An agent can achieve the goal and
    still wreck the environment — that combination is COLLATERAL_MUTATION, and
    collapsing these two into one boolean is exactly how it goes unnoticed.
    """

    achieved: bool
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    detail: str = ""


# --------------------------------------------------------------------------- #
# Context and reward
# --------------------------------------------------------------------------- #


class ContextFeatures(BaseModel):
    """Per-step context. This is the bandit's arm-selection context.

    Recorded from day one even though `policy/` is not built yet — retrofitting
    context onto an existing trace corpus means re-running every episode.
    """

    task_type: str
    turn_idx: int
    tokens_used: int = 0
    prior_failures: int = 0
    retry_count: int = 0
    steps_remaining: int | None = None
    tools_available: list[str] = Field(default_factory=list)
    model: str = ""
    temperature: float | None = None
    extra: dict[str, float] = Field(default_factory=dict)

    def to_vector(self) -> dict[str, float]:
        """Flat numeric features for a contextual bandit / value model."""
        vec: dict[str, float] = {
            "turn_idx": float(self.turn_idx),
            "tokens_used": float(self.tokens_used),
            "prior_failures": float(self.prior_failures),
            "retry_count": float(self.retry_count),
            "n_tools": float(len(self.tools_available)),
            "temperature": float(self.temperature or 0.0),
        }
        if self.steps_remaining is not None:
            vec["steps_remaining"] = float(self.steps_remaining)
        vec.update(self.extra)
        return vec


class RewardVector(BaseModel):
    """Reward as separate axes, never a single number at record time.

    Scalarizing early destroys the signal that makes collateral mutation
    detectable: goal_progress goes up while invariant_penalty goes down, and a
    scalar sum can hide that completely. Scalarize at analysis time, with
    explicit weights you can vary.
    """

    goal_progress: float = Field(default=0.0, ge=0.0, le=1.0)
    invariant_penalty: float = Field(default=0.0, le=0.0)
    cost_penalty: float = Field(default=0.0, le=0.0)

    def scalarize(self, weights: dict[str, float] | None = None) -> float:
        w = weights or {"goal_progress": 1.0, "invariant_penalty": 1.0, "cost_penalty": 1.0}
        return (
            w.get("goal_progress", 1.0) * self.goal_progress
            + w.get("invariant_penalty", 1.0) * self.invariant_penalty
            + w.get("cost_penalty", 1.0) * self.cost_penalty
        )


# --------------------------------------------------------------------------- #
# Steps and traces
# --------------------------------------------------------------------------- #


class Step(BaseModel):
    """One MDP transition, plus everything needed to explain it after the fact."""

    t: int
    state: StateSnapshot
    observation: str = Field(default="", description="What the agent actually saw.")
    action: Action
    action_result: ActionResult
    next_state: StateSnapshot
    context: ContextFeatures

    assistant_text: str = Field(
        default="",
        description="User-visible assistant text from the turn that issued this "
        "action. Recorded for REASONING_LEAKAGE detection.",
    )
    assistant_thinking: str = Field(
        default="",
        description="Summarized reasoning from the same turn. Kept separate from "
        "`assistant_text` so leakage detection can tell the channels apart.",
    )

    reward: RewardVector = Field(default_factory=RewardVector)
    labels: list[FailureLabel] = Field(
        default_factory=list, description="Filled by detectors post hoc, not at record time."
    )

    @property
    def state_changed(self) -> bool:
        return self.state.state_hash != self.next_state.state_hash


class Outcome(BaseModel):
    goal: GoalResult
    violations: list[Violation] = Field(default_factory=list)
    spec_violations: list[str] = Field(
        default_factory=list,
        description="Task-spec rules broken while still passing the goal check — "
        "e.g. hardcoding a test rather than fixing the code. Feeds LOOPHOLE_SUCCESS.",
    )
    terminated: bool = True
    termination_reason: str = ""

    @property
    def clean_success(self) -> bool:
        """Achieved the goal without breaking anything or gaming the check.

        The gap between `goal.achieved` and `clean_success` across a benchmark is
        the headline number this project exists to measure.
        """
        return self.goal.achieved and not self.violations and not self.spec_violations


class Trace(BaseModel):
    """A full episode. The unit of analysis."""

    task_id: str
    env_id: str
    model: str
    seed: int = 0
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    steps: list[Step] = Field(default_factory=list)
    outcome: Outcome | None = None

    final_text: str = Field(
        default="",
        description="User-visible text from the turn that ended the episode.",
    )
    final_thinking: str = Field(
        default="",
        description="Reasoning from that same terminating turn.",
    )
    """A turn that calls no tool produces no Step, because it is not an MDP
    transition — and until these fields existed, everything such a turn said was
    discarded. That is survivable for a model that always acts, and useless for
    one that does not: a weak model whose episodes end at zero steps leaves a
    trace containing a token count and nothing else, so 'refused', 'asked a
    clarifying question', 'described the fix in prose without making it' and
    'emitted an unparseable tool call' are indistinguishable after the fact.
    They are different failures and the analysis has to tell them apart."""

    labels: list[FailureLabel] = Field(
        default_factory=list, description="Trace-level labels that span no single step."
    )
    meta: dict[str, Any] = Field(default_factory=dict)

    # -- MDP view ---------------------------------------------------------- #

    def transitions(
        self, weights: dict[str, float] | None = None
    ) -> Iterator[tuple[StateSnapshot, Action, float, StateSnapshot, dict[str, float]]]:
        """Yield (s, a, r, s', context) tuples.

        This method is the proof that the schema is one substrate rather than
        three. `policy/` consumes exactly this and nothing else.
        """
        for step in self.steps:
            yield (
                step.state,
                step.action,
                step.reward.scalarize(weights),
                step.next_state,
                step.context.to_vector(),
            )

    # -- Convenience for detectors ----------------------------------------- #

    @property
    def state_hashes(self) -> list[str]:
        if not self.steps:
            return []
        return [s.state.state_hash for s in self.steps] + [self.steps[-1].next_state.state_hash]

    def all_labels(self) -> list[FailureLabel]:
        return [lbl for step in self.steps for lbl in step.labels] + self.labels

    def has(self, mode: FailureMode) -> bool:
        return any(lbl.mode == mode for lbl in self.all_labels())

    # -- Serialization ----------------------------------------------------- #

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, blob: str) -> Trace:
        return cls.model_validate_json(blob)


# --------------------------------------------------------------------------- #
# Diagnosis  (format reserved for month 2 — self-improvement loop)
# --------------------------------------------------------------------------- #


class RepairTarget(str, Enum):
    PROMPT = "prompt"
    TOOL_DEFINITION = "tool_definition"
    CODE = "code"
    SCAFFOLD = "scaffold"


class Diagnosis(BaseModel):
    """Machine-readable repair hint, written for a model to act on.

    Kept deliberately terse and structured: this is fed back to an agent so it
    can modify its own prompts, tool definitions, or code. Prose explanations
    are for the blog post, not for this.

    Month 2 populates these. The format is fixed now so detectors can emit
    evidence in a shape that is already consumable.
    """

    trace_id: str
    mode: FailureMode
    target: RepairTarget
    step_index: int | None = None
    summary: str = Field(description="One sentence, imperative. What to change.")
    evidence: dict[str, Any] = Field(default_factory=dict)
    minimal_repro: str | None = Field(
        default=None, description="Smallest task or input that reproduces the failure."
    )

    def to_prompt_block(self) -> str:
        """Render as a block suitable for injection into a repair prompt."""
        lines = [
            f"FAILURE: {self.mode.value}",
            f"TARGET: {self.target.value}",
            f"FIX: {self.summary}",
        ]
        if self.step_index is not None:
            lines.append(f"AT_STEP: {self.step_index}")
        if self.evidence:
            lines.append(f"EVIDENCE: {json.dumps(self.evidence, default=str)}")
        if self.minimal_repro:
            lines.append(f"REPRO: {self.minimal_repro}")
        return "\n".join(lines)
