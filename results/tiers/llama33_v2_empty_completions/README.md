# llama-3.3-70b, run 2 — empty completions. Still unusable, but now diagnosed.

Twenty episodes, 2026-08-28, on the harness that records terminating-turn text.
**Do not cite as model behaviour.** Kept because it rules out the hypothesis run 1
left open, and because it is what motivated the empty-turn diagnostic.

## What it settled

Run 1's guess was that the model emitted Llama-style function syntax as plain
text that the gateway never lifted into `tool_calls`. **Wrong.** With
`Trace.final_text` recording, 18 of 20 episodes came back with:

* `final_text` empty,
* `final_thinking` empty,
* no tool calls,
* and **13-31 output tokens billed**.

Something was generated and none of it arrived as content. Had the model been
writing prose or function syntax, `final_text` would hold it.

## What separates the two that worked

| | episodes | output tokens |
|---|---|---|
| empty | 18 | 13-31 |
| ran normally | 2 | 168, 253 |

`allowlist-guard__r4` produced a **clean success** in four steps and
`shared-normalizer__r4` took one step. Same model, same prompt, same harness,
same minute. A model that could not use these tools would fail consistently;
this is intermittent, at roughly one in ten.

Intermittency at a fixed configuration points at the layer that varies between
otherwise identical requests, which on OpenRouter is **which host serves them**.
One slug is load-balanced across several providers and they do not implement
tool calling identically. The `llama-3.1-8b` error in the sweep-6 run named two
hosts for one slug in a single retry chain, so the routing is observable in this
data.

**That remains a hypothesis.** It fits, it is not confirmed, and the last
hypothesis about this cell was also plausible and also wrong.

## What run 3 needs

Both changes exist now:

* `Turn.diagnostic` / `trace.meta["empty_turn"]` — when a turn carries neither
  text nor tool calls, the adapter records `finish_reason` and every non-standard
  field the message did carry. If something is arriving in a field nothing reads,
  this prints it.
* `--require-tool-support` — OpenRouter routing restricted to hosts that accept
  every parameter sent, `tools` included. Off by default; it narrows the host
  pool, and enabling it silently would mean two runs of one model were served by
  different host populations without the results saying so. Recorded per episode
  when used.

```bash
python scripts/sweep.py --tasks shared-normalizer interval-scheduling \
    fetch-budget allowlist-guard --models llama-3.3-70b --efforts high \
    --reps 5 --require-tool-support --out results/tiers
```

Move these twenty traces aside first, or the sweep will report them as cached.

Read the outcome as: **empty turns disappear** → routing, and the model finally
has a score; **empty turns persist and `empty_turn` names a field** → adapter
bug, fix it there; **empty turns persist with nothing in the message** → the
provider is returning empty completions for this slug, which is a finding about
the gateway and the end of the line for this model.
