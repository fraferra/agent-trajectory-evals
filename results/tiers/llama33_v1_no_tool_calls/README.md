# llama-3.3-70b, run 1 — unusable, kept as the before half

Fifteen episodes from the sweep-6 run. **Do not cite these as model behaviour.**

Thirteen of fifteen ended at zero steps: the model never emitted a tool call,
spending roughly 715 input and 134 output tokens before stopping. Six tool calls
were made across the entire cell and none were malformed, so this is not the
argument-formatting failure seen in `llama-3.1-8b` — the calls simply never
arrived in the shape the adapter reads.

**Why it cannot be diagnosed from these files.** They predate `Trace.final_text`.
A turn that calls no tool produces no `Step`, so at the time these were recorded
everything the model said was discarded. What survives is a token count. Refusal,
clarifying question, prose-instead-of-action and function-call syntax emitted as
plain text are indistinguishable here, and they are different failures.

**Working hypothesis, untested:** Llama models often emit Python-style function
call syntax in `content` rather than in structured `tool_calls`, and the gateway
does not always lift it. If so the fix belongs in the adapter or in the provider
routing, not in the model's score.

Run 2 lives in `results/tiers/` alongside the other models and was recorded with
terminating-turn text, so it can answer this. Same before/after pattern as
`results/contaminated_v1/`.
