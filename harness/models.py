"""Model registry: which models the sweep can reach, and how.

The benchmark's central open question is whether shortcut-taking is a property
of *agents* or a property of *well-aligned frontier agents specifically*. Thirty-
two episodes across Opus 4.8 and Sonnet 4.6 produced zero shortcuts, which
settles nothing about the second reading — those two models are the most heavily
post-trained artifacts in existence, and both come from one lab. A shortcut rate
measured only there is a statement about Anthropic's alignment work, not about
language models.

This module exists to make the comparison population wider: smaller models,
older models, and open-weight models with far less post-training investment. The
hypothesis under test is ordinal rather than binary —

    shortcut rate rises as capability and post-training investment fall

— and it is falsifiable in both directions. A flat rate across the tiers would be
the more interesting result, because it would mean the honest behaviour in
sweeps 1-2 was a property of the *task* rather than of the models, exactly as
the sweep-1 diagnosis suspected.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It does not treat "provider" as a dimension of the experiment. Provider is a
routing detail; the same open-weight checkpoint served by two hosts should land
in one results cell, which is what `ModelSpec.model_id` (stable, ours) versus
`api_model` (whatever the host calls it today) is for.

VERIFY IDS BEFORE A PAID SWEEP
------------------------------
Hosted model identifiers churn — slugs get versioned, deprecated, and silently
re-pointed. Every non-Anthropic entry below is a starting point, not a promise.

    python scripts/sweep.py --list-models        # what is registered and reachable
    curl https://openrouter.ai/api/v1/models     # what actually exists today

`resolve()` accepts unregistered ids, so a slug that has moved needs no code
change: pass it through with `--base-url` and it works.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from harness.adapters.base import ModelAdapter

OPENROUTER = "https://openrouter.ai/api/v1"
TOGETHER = "https://api.together.xyz/v1"
DEEPSEEK = "https://api.deepseek.com/v1"


@dataclass(frozen=True)
class ModelSpec:
    """How to reach one model, and what to record it as."""

    model_id: str
    """Stable name used in filenames, results and analysis. Ours, not the host's."""

    provider: str
    """`anthropic` or `openai` — which adapter class, not which company."""

    api_model: str
    """Identifier sent to the host. Changes when a slug is deprecated."""

    api_key_env: str
    base_url: str | None = None
    supports_effort: bool = False

    pricing: tuple[float, float] = (0.0, 0.0)
    """(input, output) USD per million tokens."""

    pricing_verified: bool = False
    """Whether `pricing` was checked against the host's published rates.

    False means cost figures for this model are reported as zero rather than
    guessed. A fabricated price in a results table is worse than a missing one:
    the missing one is visibly missing.
    """

    tier: str = "unknown"
    """Rough post-training investment: `frontier`, `small`, `open`, `legacy`.

    The independent variable. Coarse and arguable — argue about it in the
    write-up rather than encoding a false precision here.
    """

    note: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


_SPECS: tuple[ModelSpec, ...] = (
    # -- Anthropic: the existing baseline, plus the smallest in-family model --
    ModelSpec(
        "claude-opus-4-8", "anthropic", "claude-opus-4-8", "ANTHROPIC_API_KEY",
        supports_effort=True, pricing=(5.00, 25.00), pricing_verified=True,
        tier="frontier", note="Sweeps 1-5 baseline.",
    ),
    ModelSpec(
        "claude-sonnet-4-6", "anthropic", "claude-sonnet-4-6", "ANTHROPIC_API_KEY",
        supports_effort=True, pricing=(3.00, 15.00), pricing_verified=True,
        tier="frontier", note="Sweeps 1-5 baseline.",
    ),
    ModelSpec(
        "claude-haiku-4-5", "anthropic", "claude-haiku-4-5", "ANTHROPIC_API_KEY",
        supports_effort=False, pricing=(1.00, 5.00), pricing_verified=True,
        tier="small",
        note=(
            "The cheapest within-family contrast and the only new tier runnable "
            "on the existing key. No effort parameter and no extended thinking, "
            "so it is comparable to the others only at effort=high."
        ),
    ),
    # -- Open-weight, via OpenRouter. The population with the least post-training --
    ModelSpec(
        "llama-3.3-70b", "openai", "meta-llama/llama-3.3-70b-instruct",
        "OPENROUTER_API_KEY", base_url=OPENROUTER, tier="open",
        note="Strong open-weight instruct baseline with reliable tool calling.",
    ),
    ModelSpec(
        "qwen-2.5-72b", "openai", "qwen/qwen-2.5-72b-instruct",
        "OPENROUTER_API_KEY", base_url=OPENROUTER, tier="open",
        note="Different pretraining corpus and alignment recipe from Llama.",
    ),
    ModelSpec(
        "deepseek-chat", "openai", "deepseek/deepseek-chat",
        "OPENROUTER_API_KEY", base_url=OPENROUTER, tier="open",
        note="Set DEEPSEEK_API_KEY and base_url=DEEPSEEK to go direct instead.",
    ),
    ModelSpec(
        "mistral-large", "openai", "mistralai/mistral-large",
        "OPENROUTER_API_KEY", base_url=OPENROUTER, tier="open",
    ),
    ModelSpec(
        "llama-3.1-8b", "openai", "meta-llama/llama-3.1-8b-instruct",
        "OPENROUTER_API_KEY", base_url=OPENROUTER, tier="open",
        note=(
            "The floor. Expect malformed tool calls and episodes that end without "
            "achieving anything — which is a failure to *do the task*, not a "
            "shortcut, and the two must not be pooled. See `clean_success` vs "
            "`goal.achieved` in the analysis."
        ),
    ),
    # -- Other frontier labs, for a cross-lab alignment comparison --
    ModelSpec(
        "gpt-4o", "openai", "openai/gpt-4o", "OPENROUTER_API_KEY",
        base_url=OPENROUTER, tier="frontier",
        note="Cross-lab control: frontier tier, different alignment programme.",
    ),
    ModelSpec(
        "gpt-4o-mini", "openai", "openai/gpt-4o-mini", "OPENROUTER_API_KEY",
        base_url=OPENROUTER, tier="small",
    ),
    ModelSpec(
        "gemini-flash", "openai", "google/gemini-flash-1.5", "OPENROUTER_API_KEY",
        base_url=OPENROUTER, tier="small",
    ),
)

REGISTRY: dict[str, ModelSpec] = {s.model_id: s for s in _SPECS}

TIERS: tuple[str, ...] = ("frontier", "small", "open", "legacy")


class MissingKeyError(RuntimeError):
    """Raised before any episode runs, not on the first request."""


def resolve(model_id: str, base_url: str | None = None) -> ModelSpec:
    """Look up a model, or build a passthrough spec for an unregistered one.

    Unregistered ids are allowed on purpose: hosted slugs move, and a sweep
    should not need a code change to chase one. An id containing `/` is treated
    as an OpenAI-compatible slug and routed to OpenRouter unless `base_url` says
    otherwise.
    """
    if model_id in REGISTRY and base_url is None:
        return REGISTRY[model_id]
    if model_id in REGISTRY:
        return ModelSpec(**{**REGISTRY[model_id].__dict__, "base_url": base_url})
    if "/" in model_id or base_url is not None:
        return ModelSpec(
            model_id=model_id,
            provider="openai",
            api_model=model_id,
            api_key_env="OPENROUTER_API_KEY" if base_url is None else "OPENAI_API_KEY",
            base_url=base_url or OPENROUTER,
            tier="unknown",
            note="Unregistered: resolved as an OpenAI-compatible slug.",
        )
    raise KeyError(
        f"unknown model {model_id!r}. Registered: {sorted(REGISTRY)}. "
        "For an unregistered model pass a provider slug containing '/', "
        "or supply --base-url."
    )


def has_key(spec: ModelSpec) -> bool:
    return bool(os.environ.get(spec.api_key_env))


def build_adapter(model_id: str, effort: str = "high", base_url: str | None = None) -> ModelAdapter:
    """Construct the adapter for `model_id`, failing fast on a missing key."""
    spec = resolve(model_id, base_url)
    if not has_key(spec):
        raise MissingKeyError(
            f"{spec.model_id} needs {spec.api_key_env}, which is not set. "
            f"Export it, or drop the model from --models."
        )

    if spec.provider == "anthropic":
        from harness.adapters.anthropic_adapter import AnthropicAdapter

        # Models without an effort parameter are comparable to the others only
        # at high effort; the adapter rejects anything else rather than silently
        # collapsing three conditions into one results cell.
        return AnthropicAdapter(model=spec.api_model, effort=effort)

    from harness.adapters.openai_adapter import OpenAIAdapter

    return OpenAIAdapter(
        model=spec.api_model,
        effort=effort,
        api_key=os.environ[spec.api_key_env],
        base_url=spec.base_url,
        supports_effort=spec.supports_effort,
        pricing=spec.pricing if spec.pricing_verified else (0.0, 0.0),
        label=spec.model_id,
    )


def supports_effort(model_id: str) -> bool:
    try:
        return resolve(model_id).supports_effort
    except KeyError:
        return False


def describe() -> str:
    """Human-readable registry listing, including key availability."""
    rows = ["  model_id            tier      key set  cost known  api id", "  " + "-" * 78]
    for spec in sorted(REGISTRY.values(), key=lambda s: (TIERS.index(s.tier), s.model_id)):
        rows.append(
            f"  {spec.model_id:<19} {spec.tier:<9} "
            f"{'yes' if has_key(spec) else 'NO ':<8} "
            f"{'yes' if spec.pricing_verified else 'no ':<11} {spec.api_model}"
        )
    return "\n".join(rows)
