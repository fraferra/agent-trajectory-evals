"""Sweep shortcut rate across tasks, models, effort levels, and repeats.

Each episode is written to disk as it completes and existing cells are skipped,
so an interrupted sweep (rate limit, credit exhaustion, network) never loses
episodes that were already paid for — re-running resumes.

Repeats matter: with n=1 per cell, zero observed shortcuts is consistent with a
true rate anywhere up to ~25% (rule of three). Rate claims need n>1.

Examples:
    python scripts/sweep.py --dry-run
    python scripts/sweep.py --tasks duration-parser --efforts low high --reps 5
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import traceback

from envs.repo_task.env import RepoEnv
from envs.repo_task.tasks import ALL_TASKS
from harness.adapters.anthropic_adapter import SYSTEM_PROMPT, AnthropicAdapter
from harness.run import run_episode

OUT = pathlib.Path("results/sweep")


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", nargs="+", default=list(ALL_TASKS))
    ap.add_argument("--models", nargs="+", default=["claude-opus-4-8", "claude-sonnet-4-6"])
    ap.add_argument("--efforts", nargs="+", default=["low", "medium", "high"])
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--dry-run", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cells = [
        (task, model, effort, rep)
        for task in args.tasks
        for model in args.models
        for effort in args.efforts
        for rep in range(args.reps)
    ]
    print(f"{len(cells)} episodes\n")

    rows, spend, shortcuts = [], 0.0, 0
    for i, (task_id, model, effort, rep) in enumerate(cells, 1):
        cid = f"{task_id}__{model}__{effort}__r{rep}"
        path = out / f"{cid}.json"
        tag = f"[{i}/{len(cells)}] {cid}"

        if path.exists():
            print(f"{tag}: cached")
            continue
        if args.dry_run:
            print(f"{tag}: would run")
            continue

        env = RepoEnv(ALL_TASKS[task_id])
        try:
            trace = run_episode(
                env,
                AnthropicAdapter(model=model, effort=effort),
                SYSTEM_PROMPT,
                seed=rep,
            )
            path.write_text(trace.to_json())
            o = trace.outcome
            spend += trace.meta["cost_usd"]
            shortcut = o.goal.achieved and not o.clean_success
            shortcuts += shortcut
            rows.append(
                {
                    "cell": cid,
                    "task": task_id,
                    "model": model,
                    "effort": effort,
                    "rep": rep,
                    "achieved": o.goal.achieved,
                    "clean": o.clean_success,
                    "shortcut": shortcut,
                    "violations": [v.name for v in o.violations],
                    "spec": o.spec_violations,
                    "steps": trace.meta["turns"],
                    "termination": o.termination_reason,
                    "cost": trace.meta["cost_usd"],
                }
            )
            mark = "SHORTCUT" if shortcut else "clean" if o.goal.achieved else "failed"
            detail = f"  {o.spec_violations[0][:60]}" if shortcut and o.spec_violations else ""
            print(
                f"{tag}: {mark}  ({trace.meta['turns']} steps, "
                f"${trace.meta['cost_usd']:.4f}, {o.termination_reason}){detail}"
            )
        except Exception as exc:  # noqa: BLE001 - one bad cell must not kill the sweep
            print(f"{tag}: ERROR {type(exc).__name__}: {exc}")
            traceback.print_exc(limit=1)
            (out / f"{cid}.error.txt").write_text(f"{type(exc).__name__}: {exc}")
        finally:
            env.close()
        time.sleep(1)

    if rows:
        summary = out / "_summary.json"
        prior = json.loads(summary.read_text()) if summary.exists() else []
        summary.write_text(json.dumps(prior + rows, indent=2))
    n = len(rows)
    print(f"\nshortcuts: {shortcuts}/{n}" if n else "\nno new episodes")
    print(f"spend this run: ${spend:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
