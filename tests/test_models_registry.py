"""Registry resolution, key gating, and the effort-collapse rule."""

import pytest

from harness.models import (
    REGISTRY,
    TIERS,
    MissingKeyError,
    build_adapter,
    describe,
    has_key,
    resolve,
    supports_effort,
)


def test_registered_models_resolve_to_themselves() -> None:
    spec = resolve("claude-haiku-4-5")
    assert spec.provider == "anthropic"
    assert spec.tier == "small"
    assert not spec.supports_effort


def test_unregistered_slug_resolves_as_openai_compatible() -> None:
    """Hosted slugs churn; chasing one must not need a code change."""
    spec = resolve("some-lab/model-v9")
    assert spec.provider == "openai"
    assert spec.api_model == "some-lab/model-v9"
    assert spec.base_url is not None


def test_unregistered_bare_name_without_base_url_is_an_error() -> None:
    with pytest.raises(KeyError):
        resolve("gpt-9")


def test_base_url_override_wins_for_a_registered_model() -> None:
    spec = resolve("llama-3.3-70b", base_url="http://localhost:8000/v1")
    assert spec.base_url == "http://localhost:8000/v1"
    assert spec.api_model == "meta-llama/llama-3.3-70b-instruct"


def test_missing_key_fails_before_any_episode_runs(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(MissingKeyError) as exc:
        build_adapter("llama-3.3-70b")
    assert "OPENROUTER_API_KEY" in str(exc.value)


def test_every_registered_tier_is_known() -> None:
    for spec in REGISTRY.values():
        assert spec.tier in TIERS, spec.model_id


def test_unverified_pricing_is_reported_as_zero_not_guessed(monkeypatch) -> None:
    """A fabricated price in a results table is worse than a missing one."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    adapter = build_adapter("llama-3.3-70b")
    assert adapter.pricing == (0.0, 0.0)
    assert not resolve("llama-3.3-70b").pricing_verified


def test_anthropic_pricing_is_verified() -> None:
    for model_id in ("claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"):
        assert resolve(model_id).pricing_verified, model_id


def test_effort_support_is_declared_per_model() -> None:
    assert supports_effort("claude-opus-4-8")
    assert not supports_effort("claude-haiku-4-5")


def test_describe_lists_every_model_and_its_key_state() -> None:
    text = describe()
    for model_id in REGISTRY:
        assert model_id in text
    assert has_key(resolve("claude-opus-4-8")) == ("ANTHROPIC_API_KEY" in __import__("os").environ)
