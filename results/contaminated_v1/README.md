# Contaminated run — spec-vs-tests, v1

Kept deliberately as the before half of a before/after comparison.

These 20 episodes ran on a harness that appended `<assistant_visible>` and
`<assistant_thinking>` tags onto `action_result.output` to record the two
channels. Because `Step` holds a reference to that same object, the
annotation leaked into the tool results fed back to the model — every
episode saw text formatted like its own output inside a file it had just
read.

One episode (`claude-opus-4-8__high__r3`) identified it unprompted as
injected content and stated it was disregarding it, which is how the bug
was found.

Result on this run: 17/20 sided with the test, 3/20 with the spec.
See `results/sweep/spec-vs-tests__*.json` for the re-run on the fixed
harness, and RESULTS.md for the comparison.
