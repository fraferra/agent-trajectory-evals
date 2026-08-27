"""OpenAI-compatible adapter, exercised offline against a fake client.

The adapter is the thinnest piece of the system and the only one that talks to
a network, so the parts worth testing are the two shape translations and the
handling of output that weaker models actually produce: malformed tool
arguments, missing content, empty responses.

The malformed-arguments tests are the important ones. Reaching non-frontier
models is the whole purpose of this adapter, and those models emit invalid JSON
often enough that how the harness responds is a methodological choice, not an
implementation detail — see `_parse_args`.
"""

from types import SimpleNamespace

import pytest

from envs.repo_task.env import RepoEnv
from envs.repo_task.tasks_hard import INTERVAL_SCHEDULING
from harness.adapters.openai_adapter import OpenAIAdapter, _parse_args, to_openai_tools
from harness.run import run_episode

SYSTEM = "You are a software engineering agent."


def message(content=None, tool_calls=None, reasoning=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [], reasoning=reasoning)


def tool_call(cid, name, arguments):
    return SimpleNamespace(
        id=cid, function=SimpleNamespace(name=name, arguments=arguments)
    )


def response(msg, finish_reason="stop", prompt_tokens=100, completion_tokens=50):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason=finish_reason)],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_tokens_details=None,
        ),
    )


class FakeClient:
    """Replays canned responses and records what it was sent."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        if not self._responses:
            return response(message(content="done"))
        return self._responses.pop(0)


def adapter(responses, **kw):
    return OpenAIAdapter("fake-model", client=FakeClient(responses), **kw)


# --------------------------------------------------------------------------- #
# Schema translation
# --------------------------------------------------------------------------- #


def test_tool_schemas_are_converted_to_function_shape() -> None:
    env = RepoEnv(INTERVAL_SCHEDULING)
    try:
        converted = to_openai_tools(env.tool_schemas())
    finally:
        env.close()

    assert {t["function"]["name"] for t in converted} == {
        "list_files", "read_file", "write_file", "delete_file", "run_tests"
    }
    assert all(t["type"] == "function" for t in converted)
    write = next(t for t in converted if t["function"]["name"] == "write_file")
    # input_schema -> parameters, contents preserved
    assert set(write["function"]["parameters"]["properties"]) == {"path", "content"}
    assert write["function"]["parameters"]["required"] == ["path", "content"]


# --------------------------------------------------------------------------- #
# Argument parsing — the weak-model surface
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"path": "a.py"}', {"path": "a.py"}),
        ("{}", {}),
        ("", {}),
        (None, {}),
    ],
)
def test_well_formed_arguments_parse(raw, expected) -> None:
    args, error = _parse_args(raw)
    assert args == expected
    assert error is None


@pytest.mark.parametrize(
    "raw",
    ['{"path": "a.py",}', "{path: 'a.py'}", "not json at all", "[1, 2]", '"a string"'],
)
def test_malformed_arguments_report_rather_than_repair(raw) -> None:
    args, error = _parse_args(raw)
    assert args == {}
    assert error is not None


def test_malformed_call_keeps_the_raw_text_and_becomes_a_failed_step() -> None:
    """A model that emits unusable JSON must be recorded doing so.

    Repairing it here would credit the harness's competence to the model, which
    matters precisely for the models this adapter exists to reach.
    """
    env = RepoEnv(INTERVAL_SCHEDULING)
    a = adapter(
        [
            response(message(tool_calls=[tool_call("c1", "read_file", '{"path": }')]),
                     finish_reason="tool_calls"),
            response(message(content="giving up")),
        ]
    )
    try:
        trace = run_episode(env, a, SYSTEM)
    finally:
        env.close()

    assert len(trace.steps) == 1
    step = trace.steps[0]
    assert step.action.tool == "read_file"
    assert step.action.args == {}
    assert "unparseable" in (step.action.raw or "")
    assert not step.action_result.ok


# --------------------------------------------------------------------------- #
# Conversation mechanics
# --------------------------------------------------------------------------- #


def test_tool_results_go_back_as_tool_messages_with_matching_ids() -> None:
    env = RepoEnv(INTERVAL_SCHEDULING)
    a = adapter(
        [
            response(message(content="looking", tool_calls=[tool_call("c1", "list_files", "{}")]),
                     finish_reason="tool_calls"),
            response(message(content="done")),
        ]
    )
    try:
        run_episode(env, a, SYSTEM)
    finally:
        env.close()

    second = a.client.requests[1]["messages"]
    assistant = next(m for m in second if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["id"] == "c1"
    tool_msg = next(m for m in second if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "c1"
    assert tool_msg["content"]


def test_system_prompt_is_a_system_message() -> None:
    a = adapter([response(message(content="hi"))])
    a.start(SYSTEM, [], "observation")
    messages = a.client.requests[0]["messages"]
    assert messages[0] == {"role": "system", "content": SYSTEM}
    assert messages[1]["role"] == "user"


def test_reasoning_effort_is_sent_only_when_declared_supported() -> None:
    off = adapter([response(message(content="hi"))], effort="low")
    off.start(SYSTEM, [], "obs")
    assert "reasoning_effort" not in off.client.requests[0]

    on = adapter([response(message(content="hi"))], effort="low", supports_effort=True)
    on.start(SYSTEM, [], "obs")
    assert on.client.requests[0]["reasoning_effort"] == "low"


def test_empty_choices_terminate_instead_of_raising() -> None:
    empty = SimpleNamespace(choices=[], usage=None)
    a = adapter([empty])
    turn = a.start(SYSTEM, [], "obs")
    assert turn.done
    assert turn.stop_reason == "empty_response"


def test_missing_content_is_not_a_crash() -> None:
    a = adapter([response(message(content=None))])
    turn = a.start(SYSTEM, [], "obs")
    assert turn.text == ""
    assert turn.done


def test_label_overrides_the_traced_model_name() -> None:
    """One checkpoint served by two hosts belongs in one results cell."""
    a = OpenAIAdapter(
        "meta-llama/llama-3.3-70b-instruct",
        client=FakeClient([]),
        label="llama-3.3-70b",
    )
    assert a.model == "llama-3.3-70b"
    assert a.api_model == "meta-llama/llama-3.3-70b-instruct"


def test_cost_is_zero_when_pricing_is_unknown() -> None:
    a = adapter([response(message(content="hi"))])
    a.start(SYSTEM, [], "obs")
    usage = a.total_usage()
    assert usage.input_tokens == 100
    assert usage.cost_usd == 0.0


def test_cost_uses_supplied_pricing() -> None:
    a = adapter([response(message(content="hi"))], pricing=(10.0, 20.0))
    a.start(SYSTEM, [], "obs")
    # 100 input @ $10/Mtok + 50 output @ $20/Mtok
    assert a.total_usage().cost_usd == pytest.approx((100 * 10.0 + 50 * 20.0) / 1e6)
