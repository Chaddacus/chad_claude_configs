#!/usr/bin/env python3
"""Bandit data query tool — join route decisions + reward records.

Reads:
  ~/.claude/state/route_decisions.jsonl  (written by classify_prompt.py hook)
  ~/.claude/state/route_rewards.jsonl    (written by stop_reason_telemetry.py)

Outputs per-route reward stats for multi-armed bandit training analysis.

Usage:
  python3 bandit_data.py [--format json|table] [--window DAYS]

This tool is READ-ONLY. It never writes to route_manifest.json or any
decision/reward log. It is the query surface for the bandit instrumentation
added in Slice 5 of the control-plane research program.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

HOME = Path(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")))
STATE_DIR = HOME / "state"
DECISIONS_LOG = STATE_DIR / "route_decisions.jsonl"
REWARDS_LOG = STATE_DIR / "route_rewards.jsonl"


def load_jsonl(path: Path, window_days: int | None = None) -> list[dict]:
    """Load JSONL records, optionally filtering to last N days."""
    if not path.exists():
        return []
    cutoff = None
    if window_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()

    records = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if cutoff and isinstance(rec.get("ts"), str) and rec["ts"] < cutoff:
                    continue
                records.append(rec)
            except json.JSONDecodeError:
                pass
    except OSError:
        pass
    return records


def join_decisions_rewards(decisions: list[dict], rewards: list[dict]) -> list[dict]:
    """Left join decisions → rewards by decision_id."""
    reward_by_id: dict[str, float] = {}
    for r in rewards:
        did = r.get("decision_id")
        if did is not None:
            reward_by_id[did] = r.get("reward", 0.0)

    joined = []
    for d in decisions:
        did = d.get("decision_id")
        joined.append({
            **d,
            "reward": reward_by_id.get(did) if did is not None else None,
        })
    return joined


def per_route_stats(joined: list[dict]) -> dict[str, dict]:
    """Compute per-route reward stats."""
    buckets: dict[str, dict] = {}
    for rec in joined:
        route = rec.get("route_hint", "unknown")
        if route not in buckets:
            buckets[route] = {"n_decisions": 0, "n_with_reward": 0,
                              "reward_sum": 0.0, "reward_dist": {}}
        b = buckets[route]
        b["n_decisions"] += 1
        r = rec.get("reward")
        if r is not None:
            b["n_with_reward"] += 1
            b["reward_sum"] += r
            key = str(r)
            b["reward_dist"][key] = b["reward_dist"].get(key, 0) + 1

    result = {}
    for route, b in sorted(buckets.items()):
        n = b["n_with_reward"]
        result[route] = {
            "n_decisions": b["n_decisions"],
            "n_with_reward": n,
            "mean_reward": round(b["reward_sum"] / n, 4) if n > 0 else None,
            "reward_dist": b["reward_dist"],
        }
    return result


def print_table(stats: dict[str, dict], total_decisions: int, total_rewards: int) -> None:
    print(f"\nBandit data: {total_decisions} decisions, {total_rewards} rewards joined")
    print(f"{'Route':<8} {'N_dec':>6} {'N_rew':>6} {'Mean_R':>8}  Reward distribution")
    print("-" * 60)
    for route, s in stats.items():
        mean = f"{s['mean_reward']:.3f}" if s["mean_reward"] is not None else "  N/A"
        dist_str = "  ".join(f"{v}×{k}" for k, v in sorted(s["reward_dist"].items()))
        print(f"{route:<8} {s['n_decisions']:>6} {s['n_with_reward']:>6} {mean:>8}  {dist_str}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=["json", "table"], default="table")
    parser.add_argument("--window", type=int, metavar="DAYS",
                        help="Only include records from the last N days")
    args = parser.parse_args()

    decisions = load_jsonl(DECISIONS_LOG, args.window)
    rewards = load_jsonl(REWARDS_LOG, args.window)
    joined = join_decisions_rewards(decisions, rewards)
    stats = per_route_stats(joined)

    n_with_reward = sum(1 for j in joined if j.get("reward") is not None)

    if args.format == "json":
        print(json.dumps({
            "total_decisions": len(decisions),
            "total_rewards_joined": n_with_reward,
            "per_route": stats,
        }, indent=2))
    else:
        print_table(stats, len(decisions), n_with_reward)

    return 0


if __name__ == "__main__":
    sys.exit(main())
