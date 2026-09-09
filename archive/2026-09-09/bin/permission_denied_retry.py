#!/usr/bin/env python3
"""PermissionDenied hook — single-retry observability signal.

Reads the hook event JSON from stdin, journals it, and emits
`hookSpecificOutput.retry: true` UNLESS the same (tool_name, tool_input)
fingerprint was already retried within the last 60 seconds — that breaks
runaway retry loops while still letting the model re-attempt one-off
classifier denials.

Logs to: ~/.claude/state/permission-denials.jsonl
Stdout:  {"hookSpecificOutput": {"retry": <bool>}}
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

STATE = Path.home() / ".claude" / "state" / "permission-denials.jsonl"
WINDOW_SECONDS = 60.0
TAIL_LINES = 200


def main() -> int:
    try:
        evt = json.load(sys.stdin)
    except Exception:
        # Malformed event — don't emit retry, don't crash the loop.
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"retry": False}}))
        return 0

    tool_name = evt.get("tool_name", "")
    tool_input = evt.get("tool_input", {})
    fingerprint = hashlib.sha256(
        (tool_name + "|" + json.dumps(tool_input, sort_keys=True, default=str)).encode()
    ).hexdigest()[:12]

    STATE.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()

    seen_recently = False
    if STATE.exists():
        try:
            tail = STATE.read_text(encoding="utf-8").splitlines()[-TAIL_LINES:]
            for line in reversed(tail):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("fp") == fingerprint and (now - rec.get("ts", 0)) < WINDOW_SECONDS:
                    seen_recently = True
                    break
        except Exception:
            seen_recently = False

    retry = not seen_recently
    rec = {
        "ts": now,
        "fp": fingerprint,
        "tool": tool_name,
        "retry_emitted": retry,
        "tool_use_id": evt.get("tool_use_id", ""),
    }
    try:
        with STATE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass

    sys.stdout.write(json.dumps({"hookSpecificOutput": {"retry": retry}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
