"""Episode runner: drive a model against an environment and record the trace.

The loop lives here rather than in the adapter because environment state must be
snapshotted immediately before and after *each* tool call. That is the whole
premise of the project — a trajectory-level eval cannot delegate tool execution
to something that hides the per-call boundary.
"""

from __future__ import annotations

from atrace.schema import (
    ActionResult,
    Outcome,
    RewardVector,
    Step,
    Trace,
)
from envs.base import BaseEnv
from harness.adapters.base import ModelAdapter, ToolOutcome, Usage

MAX_TURNS = 40
"""Hard stop on adapter turns, independent of the env's own step budget.

Guards against a model that emits zero tool calls forever without ending, which
would otherwise spin without incrementing the env step count.
"""


def run_episode(
    env: BaseEnv,
    adapter: ModelAdapter,
    system: str,
    seed: int = 0,
    grade_every_step: bool = False,
) -> Trace:
    """Run one episode and return its recorded trace.

    `grade_every_step` recomputes goal progress after every tool call. It gives
    a dense progress signal for the policy layer, but in the repo environment
    each call runs the test suite in a subprocess — so it is off by default and
    goal progress is measured once, at the end.
    """
    observation = env.reset(seed=seed)
    trace = Trace(
        task_id=env.spec.task_id,
        env_id=type(env).__name__,
        model=adapter.model,
        seed=seed,
    )

    turn = adapter.start(system, env.tool_schemas(), observation.text)
    t = 0
    termination = "end_turn"

    for _ in range(MAX_TURNS):
        if turn.stop_reason == "refusal":
            termination = "refusal"
            break
        if turn.done:
            termination = "model_stopped"
            break

        outcomes: list[ToolOutcome] = []
        for i, call in enumerate(turn.calls):
            before = env.snapshot_state()
            obs, result = env.step(call.action)
            after = env.snapshot_state()

            trace.steps.append(
                Step(
                    t=t,
                    state=before,
                    observation=obs.text,
                    action=call.action,
                    action_result=result,
                    next_state=after,
                    context=env.context(
                        model=adapter.model,
                        tokens_used=turn.usage.input_tokens + turn.usage.output_tokens,
                        prior_failures=sum(
                            1 for s in trace.steps if not s.action_result.ok
                        ),
                    ),
                    reward=_reward(env, turn.usage if i == 0 else Usage(), grade_every_step),
                )
            )
            # The turn's assistant text belongs to the turn, not to any one call.
            # Attach it to the first step so leakage detection has a target
            # without double-counting it across parallel calls.
            if i == 0 and (turn.text or turn.thinking):
                trace.steps[-1].action_result.output = _tag_channels(
                    result, turn.text, turn.thinking
                )

            outcomes.append(
                ToolOutcome(
                    call_id=call.call_id,
                    output=result.output or obs.text,
                    is_error=not result.ok,
                )
            )
            t += 1

        if env.exhausted:
            termination = "step_budget_exhausted"
            break

        turn = adapter.send_results(outcomes)
    else:
        termination = "turn_budget_exhausted"

    trace.outcome = Outcome(
        goal=env.check_goal(),
        violations=env.check_invariants(),
        spec_violations=env.check_spec(),
        terminated=True,
        termination_reason=termination,
    )

    total = adapter.total_usage()
    trace.meta = {
        "input_tokens": total.input_tokens,
        "output_tokens": total.output_tokens,
        "cache_read_tokens": total.cache_read_tokens,
        "cost_usd": round(total.cost_usd, 4),
        "turns": t,
    }
    return trace


def _reward(env: BaseEnv, usage: Usage, grade_every_step: bool) -> RewardVector:
    """Per-step reward. Axes stay separate — see RewardVector's docstring."""
    return RewardVector(
        goal_progress=env.check_goal().progress if grade_every_step else 0.0,
        invariant_penalty=-float(len(env.check_invariants())),
        cost_penalty=-usage.cost_usd,
    )


def _tag_channels(result: ActionResult, text: str, thinking: str) -> str:
    """Record the user-visible and reasoning channels separately.

    Kept distinct because REASONING_LEAKAGE is defined as reasoning appearing in
    the user-visible channel; merging them would make the detector unable to
    tell the two apart.
    """
    parts = [result.output] if result.output else []
    if text:
        parts.append(f"<assistant_visible>{text}</assistant_visible>")
    if thinking:
        parts.append(f"<assistant_thinking>{thinking}</assistant_thinking>")
    return "\n".join(parts)
