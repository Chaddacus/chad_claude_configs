#!/usr/bin/env python3
"""Flip-readiness dashboard for the dandori streaming gate.

Reports graduation status from graduation.py's deterministic predicates
(the same predicates that auto-flip shadow -> on and auto-demote on
FALSE_GREEN). v1 records (pre-2026-06-09 real_verdict fix) are shown for
history but carry no evidence weight.
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, "/Users/chadsimon/.claude/dandori/bin")
import graduation

LOG = Path("/Users/chadsimon/.claude/state/dandori/shadow_log.jsonl")


def main():
    shadow_rows = graduation._read_jsonl(LOG)
    grad_rows = graduation._read_jsonl(graduation.GRAD_LOG)
    if not shadow_rows:
        print("no shadow log yet — accumulate real sessions first")
        return 0

    v1 = [r for r in shadow_rows if r.get("v") != 2]
    v2 = [r for r in shadow_rows if r.get("v") == 2]
    print(f"shadow samples: {len(shadow_rows)} "
          f"(v1 legacy, no evidence weight: {len(v1)}; v2: {len(v2)})")
    agree = Counter(r.get("agreement", "?") for r in v2)
    for k in ("agree", "FALSE_GREEN", "false_alarm", "inconclusive", "no_real_signal"):
        print(f"  {k:14} {agree.get(k, 0)}")

    status = graduation.evaluate(shadow_rows, grad_rows)
    print(f"\nmode: {status['mode']}   baseline_ts: {status['baseline_ts']}")
    print("graduation readiness (auto-flips when all PASS):")
    labels = {
        "no_false_greens": "no false-greens (streamed PASS while real FAIL)",
        "enough_decisive": f"decisive samples >= {graduation.MIN_DECISIVE} "
                           f"(have {status['decisive']})",
        "enough_sessions": f"distinct decisive sessions >= {graduation.MIN_SESSIONS} "
                           f"(have {status['decisive_sessions']})",
        "inconclusive_rate_ok": f"inconclusive rate < {graduation.MAX_INCONCLUSIVE_RATE} "
                                f"(at {status['inconclusive_rate']})",
    }
    for key, ok in status["checks"].items():
        print(f"  [{'PASS' if ok else 'WAIT'}] {labels.get(key, key)}")
    print(f"\n  --> {'READY' if status['ready'] else 'NOT READY'} "
          f"(transitions are automatic; see graduation_log.jsonl)")

    if grad_rows:
        print("\ntransition history:")
        for r in grad_rows[-5:]:
            print(f"  {r.get('ts')}: {r.get('event')} "
                  f"{r.get('reason', '')}".rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
