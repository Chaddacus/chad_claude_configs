#!/usr/bin/env python3
"""Print a fitness dashboard summarizing the evolve history.

Usage:
    evolve_fitness.py [--window 10]
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
HISTORY = HOME / ".claude" / "evolve" / "history.jsonl"


def load() -> list[dict]:
    if not HISTORY.exists():
        return []
    return [json.loads(l) for l in HISTORY.read_text().splitlines() if l.strip()]


def summary(window: list[dict]) -> dict:
    if not window:
        return {}
    n = len(window)
    cats: Counter[str] = Counter()
    for r in window:
        for k, v in r.get("failure_categories", {}).items():
            cats[k] += v
    return {
        "runs": n,
        "first_try_pass_rate": sum(r["metrics"].get("first_try_pass_rate", 0) for r in window) / n,
        "supervisor_takeovers": sum(r["metrics"].get("supervisor_takeovers", 0) for r in window) / n,
        "cheat_count": sum(r["metrics"].get("cheat_count", 0) for r in window) / n,
        "infra_down": sum(r["metrics"].get("infra_down_count", 0) for r in window) / n,
        "sandbox_violations": sum(r["metrics"].get("sandbox_violation_count", 0) for r in window) / n,
        "avg_attempts": sum(r["metrics"].get("avg_attempts_per_slice", 0) for r in window) / n,
        "avg_wall_time_s": sum(r.get("wall_time_seconds", 0) for r in window) / n,
        "top_failure_categories": cats.most_common(3),
    }


def fmt(s: dict) -> str:
    if not s:
        return "(no runs yet)"
    lines = [
        f"  Runs:                  {s['runs']}",
        f"  First-try pass rate:   {s['first_try_pass_rate']:.0%}",
        f"  Supervisor takeovers:  {s['supervisor_takeovers']:.1f}/run",
        f"  Cheat flags:           {s['cheat_count']:.1f}/run",
        f"  Infra-down events:     {s['infra_down']:.1f}/run",
        f"  Sandbox violations:    {s['sandbox_violations']:.1f}/run",
        f"  Avg attempts/slice:    {s['avg_attempts']:.2f}",
        f"  Avg wall time:         {s['avg_wall_time_s']:.0f}s",
    ]
    if s["top_failure_categories"]:
        lines.append(f"  Top failure categories: {s['top_failure_categories']}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--window", type=int, default=10)
    args = p.parse_args()

    h = load()
    if not h:
        print("No history yet. Run a task through /evolve first.")
        return 0

    recent = h[-args.window:]
    older = h[-2 * args.window:-args.window] if len(h) >= 2 * args.window else []

    recent_s = summary(recent)
    older_s = summary(older)

    print(f"Evolve Fitness Report (last {len(recent)} runs):\n")
    print(fmt(recent_s))

    if older_s:
        print(f"\nvs prior {len(older)} runs:")
        for key in ("first_try_pass_rate", "supervisor_takeovers", "cheat_count", "avg_attempts"):
            a = older_s[key]
            b = recent_s[key]
            arrow = "↑" if b > a else ("↓" if b < a else "→")
            print(f"  {key}: {a:.2f} {arrow} {b:.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
