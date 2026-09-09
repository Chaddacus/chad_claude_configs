#!/usr/bin/env python3
"""UserPromptSubmit hook — rotate the session case file at turn boundaries.

The case file at ~/.claude/state/cases/${session_id}/ accumulates
events.jsonl across the entire Claude session. The L2 stop gate needs
per-turn scope so claims are checked against this-turn activity, not
session-wide.

On each user prompt: archive {events.jsonl, summary.json, completion.json}
under turns/{N}/ and start fresh. The "live" view at the top level always
reflects only the current turn.

Archived turns remain queryable for postmortems and omni-mem ingest.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"))
from case_file import case_dir, events_path, summary_path, completion_path


def next_turn_number(case_path: Path) -> int:
    turns_dir = case_path / "turns"
    if not turns_dir.exists():
        return 1
    nums = []
    for d in turns_dir.iterdir():
        if d.is_dir():
            try:
                nums.append(int(d.name))
            except ValueError:
                continue
    return (max(nums) + 1) if nums else 1


def rotate() -> dict:
    case_path = case_dir()
    events = events_path()
    summary = summary_path()
    completion = completion_path()

    # If there's nothing to rotate, no-op
    if not events.exists() and not completion.exists():
        return {"rotated": False, "reason": "no live state"}

    n = next_turn_number(case_path)
    turn_dir = case_path / "turns" / str(n)
    turn_dir.mkdir(parents=True, exist_ok=True)

    moved = []
    for src in (events, summary, completion):
        if src.exists():
            dst = turn_dir / src.name
            try:
                shutil.move(str(src), str(dst))
                moved.append(src.name)
            except (IOError, OSError):
                pass

    # Write a small turn-boundary marker
    try:
        (turn_dir / "boundary.json").write_text(json.dumps({
            "turn_number": n,
            "rotated_at": time.time(),
            "files": moved,
        }, indent=2))
    except IOError:
        pass

    return {"rotated": True, "turn_number": n, "files": moved}


def main() -> int:
    # Read but ignore UserPromptSubmit input — we don't need its contents.
    try:
        sys.stdin.read()
    except Exception:
        pass

    try:
        rotate()
    except Exception:
        pass

    # UserPromptSubmit hooks emit nothing (or additionalContext if desired);
    # we emit nothing — pure observer/side effect.
    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
