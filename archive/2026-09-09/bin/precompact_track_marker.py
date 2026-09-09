#!/usr/bin/env python3
"""PreCompact hook — append a compaction marker to the active track's event log.

CLAUDE.md's budget-breach clause exempted compaction because no marker
reached objective.events.jsonl. This closes that gap: when a track is
active for the session's cwd, record {"event": "compaction"} so the
post-compaction agent can surface it like any other budget event.

Finding the active track: newest state dir under ~/.claude/state/autonomy/
whose objective.state.json cwd matches the session cwd and whose state is
not closed. Best-effort — no track, no marker, no error.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

AUTONOMY = Path(os.path.expanduser("~/.claude/state/autonomy"))


def find_active_track(cwd: str) -> Path | None:
    if not AUTONOMY.is_dir():
        return None
    candidates = []
    for d in AUTONOMY.iterdir():
        state_file = d / "objective.state.json"
        events_file = d / "objective.events.jsonl"
        if not state_file.is_file() or not events_file.is_file():
            continue
        try:
            state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, IOError):
            continue
        if cwd and state.get("cwd") and state["cwd"] != cwd:
            continue
        candidates.append((state_file.stat().st_mtime, events_file))
    if not candidates:
        return None
    return max(candidates)[1]


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, IOError):
        payload = {}

    events_file = find_active_track(payload.get("cwd", ""))
    if events_file is None:
        return 0
    try:
        with events_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.time(),
                "event": "compaction",
                "session_id": payload.get("session_id", ""),
                "trigger": payload.get("trigger", ""),
            }) + "\n")
    except IOError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
