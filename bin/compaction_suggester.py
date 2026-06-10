#!/usr/bin/env python3
"""PostCompact + PostToolUse hook — suggest /compact at logical breakpoints.

Fires on PostCompact to log that a compaction occurred. Fires on PostToolUse
to detect slice-accepted / track-closed events in auto_runtime logs and
surface a suggestion line once per event.

Suggestion-only. Never auto-invokes /compact. Never blocks. Keyed off event
type, not context-window percentage, to avoid double-firing with PreCompact.
"""

from __future__ import annotations

import json
import os
import sys
import time

STATE_PATH = f"/tmp/claude-compaction-suggest-{os.environ.get('CLAUDE_CODE_SESSION_ID') or os.environ.get('CLAUDE_SESSION_ID') or 'default'}.json"
AUTONOMY_DIR = os.path.expanduser("~/.claude/state/autonomy")
COOLDOWN_SECONDS = 600  # don't re-suggest within 10 min


def load_state() -> dict:
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return {"last_suggested_at": 0, "seen_events": []}


def save_state(state: dict) -> None:
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except IOError:
        pass


def latest_slice_event() -> tuple[str, float] | None:
    """Return (event_kind, timestamp) of latest slice-accepted or objective-complete event."""
    if not os.path.isdir(AUTONOMY_DIR):
        return None
    latest: tuple[str, float] | None = None
    try:
        for track_id in os.listdir(AUTONOMY_DIR):
            event_log = os.path.join(AUTONOMY_DIR, track_id, "events.jsonl")
            if not os.path.isfile(event_log):
                continue
            try:
                with open(event_log, "r") as f:
                    for line in f:
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        kind = ev.get("kind", "")
                        ts = ev.get("ts", 0)
                        if kind in ("node_accepted", "objective_complete") and (
                            latest is None or ts > latest[1]
                        ):
                            latest = (f"{track_id}:{kind}", ts)
            except IOError:
                continue
    except OSError:
        return None
    return latest


def emit_suggestion(reason: str) -> None:
    envelope = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"[context: consider /compact — {reason}]",
        }
    }
    print(json.dumps(envelope))


def main() -> None:
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, IOError):
        sys.exit(0)

    event_name = hook_input.get("hook_event_name", "")

    if event_name == "PostCompact":
        # Just log; nothing to suggest since compaction already happened.
        sys.exit(0)

    state = load_state()
    now = time.time()
    if now - state.get("last_suggested_at", 0) < COOLDOWN_SECONDS:
        sys.exit(0)

    latest = latest_slice_event()
    if latest is None:
        sys.exit(0)

    event_key, event_ts = latest
    # Only suggest for events in the last 2 minutes and not already seen.
    if now - event_ts > 120:
        sys.exit(0)
    if event_key in state.get("seen_events", []):
        sys.exit(0)

    reason = "slice accepted" if "node_accepted" in event_key else "objective complete"
    emit_suggestion(reason)
    state["last_suggested_at"] = now
    state.setdefault("seen_events", []).append(event_key)
    # Keep the last 50 to bound the file.
    state["seen_events"] = state["seen_events"][-50:]
    save_state(state)
    sys.exit(0)


if __name__ == "__main__":
    main()
