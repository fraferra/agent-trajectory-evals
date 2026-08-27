# Tier sweep — 8 models, 3 labs, 3 incentive-gradient tasks

_Run 2026-08-26. 119 episodes. $1.24 on Anthropic; open-weight costs unpriced._

The evidence behind **Sweep 6** in `RESULTS.md`. Read that for the analysis; this
file records how the run was produced and what is wrong with it.

## Layout

`{task}__{model}__{effort}__r{rep}.json` — one trace per episode.
`_summary.json` — appended per invocation, so it spans the several runs below.

## How it was produced

Three invocations, because the sweep skips cells that already have results:

```bash
# 1. pilot, n=1, Anthropic only
python scripts/sweep.py --tasks shared-normalizer interval-scheduling fetch-budget \
    --models claude-opus-4-8 claude-sonnet-4-6 claude-haiku-4-5 --efforts high --reps 1

# 2. the open-weight and OpenAI arm
python scripts/sweep.py --tasks shared-normalizer interval-scheduling fetch-budget \
    --models llama-3.3-70b qwen-2.5-72b deepseek-chat llama-3.1-8b gpt-4o-mini \
    --efforts high --reps 5

# 3. Anthropic topped up to n=5 so the arms are balanced
python scripts/sweep.py --tasks shared-normalizer interval-scheduling fetch-budget \
    --models claude-opus-4-8 claude-sonnet-4-6 claude-haiku-4-5 --efforts high --reps 5
```

All `--out results/tiers`. Everything ran at `effort=high`: most models in this
population have no effort parameter, and `scripts/sweep.py` refuses to label
three identical conditions low/medium/high.

## Known defects in this data

**`llama-3.3-70b` is not usable.** 13 of 15 episodes ended at zero steps — the
model never emitted a tool call. Harness reachability, not model behaviour. These
traces also predate `Trace.final_text`, so what it said instead is unrecoverable.
Re-run before citing.

**One cell is missing.** `fetch-budget__llama-3.1-8b__high__r3` failed with an
upstream 429 from the gateway, recorded at `*.error.txt`. n=14 for that model.

**Costs are zero for every non-Anthropic model.** Those entries carry
`pricing_verified=False` in `harness/models.py`, so the adapter reports no cost
rather than a guessed one. The OpenRouter dashboard has the real figure.

**Model slugs are as of 2026-08-26.** Hosted identifiers churn. Check
`python scripts/sweep.py --list-models` against the provider before re-running.

## What would strengthen it

1. Re-run `llama-3.3-70b` now that terminating-turn text is recorded — either it
   becomes interpretable or it is a documented gateway incompatibility.
2. A second task that produces wrong-but-green solutions. All four currently come
   from `shared-normalizer`, so one task carries the entire effect.
3. More open-weight families. The arm is effectively qwen and deepseek.
4. A closed frontier model that is not Anthropic or OpenAI, to test whether the
   split is "frontier lab" or something narrower.
