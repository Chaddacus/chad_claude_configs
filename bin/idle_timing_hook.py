#!/usr/bin/env python3
"""UserPromptSubmit hook — inject elapsed-time context since last turn.

Closes a blind spot in long-running sessions: without this, the model has no
signal that a 45-minute gap occurred between turns and can't reason about
time-of-pause decisions ("that build should be done by now", "the deploy
window has passed").

Read-only, additive context. Silent no-op under 5 minutes to avoid noise.
"""

from __future__ import annotations

import json
import os
import sys
import time

MARKER_PATH = f"/tmp/claude-last-turn-{os.environ.get('CLAUDE_CODE_SESSION_ID') or os.environ.get('CLAUDE_SESSION_ID') or 'default'}"
MIN_IDLE_SECONDS = 300  # below this, don't inject — noise


def read_last_turn() -> float | None:
    try:
        with open(MARKER_PATH, "r") as f:
            return float(f.read().strip())
    except (IOError, ValueError):
        return None


def write_last_turn(ts: float) -> None:
    try:
        with open(MARKER_PATH, "w") as f:
            f.write(str(ts))
    except IOError:
        pass


def format_elapsed(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        hours = seconds / 3600
        return f"{hours:.1f}h"
    days = seconds / 86400
    return f"{days:.1f}d"


def main() -> None:
    now = time.time()
    last = read_last_turn()
    write_last_turn(now)

    if last is None:
        sys.exit(0)

    elapsed = now - last
    if elapsed < MIN_IDLE_SECONDS:
        sys.exit(0)

    envelope = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": f"[idle: {format_elapsed(elapsed)} since last turn]",
        }
    }
    print(json.dumps(envelope))
    sys.exit(0)


if __name__ == "__main__":
    main()
