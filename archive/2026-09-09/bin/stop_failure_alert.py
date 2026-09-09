#!/usr/bin/env python3
"""StopFailure hook — observe crashed Stop hooks.

This config leans on Stop-gate enforcement (stop_gate, completion_gate,
omni-mem save). Before this hook, a Stop hook that crashed failed silent —
the gate chain quietly stopped gating. Now: append a structured record to
~/.claude/state/stop-failures.jsonl and fire a desktop notification.

Justification for a new script (anti-overengineering gate): no existing
primitive observes the StopFailure event.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

LOG = os.path.expanduser("~/.claude/state/stop-failures.jsonl")
NOTIFY = os.path.expanduser("~/.claude/bin/notify_done.sh")


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, IOError):
        payload = {}

    # Field contract verified against the shipped v2.1.211 dispatch source
    # (2026-07-16 audit M10): StopFailure sends {error, error_details,
    # last_assistant_message, ...base}. There is no hook_command/command
    # field — the old lookups left failed_hook empty and error "unknown" on
    # all 67 records observed at audit time. error_details carries the
    # attribution; last_assistant_message gives postmortem context.
    record = {
        "ts": time.time(),
        "session_id": payload.get("session_id", ""),
        "hook_event_name": payload.get("hook_event_name", "StopFailure"),
        "error": str(payload.get("error") or "")[:500],
        "error_details": str(payload.get("error_details") or "")[:2000],
        "last_assistant_message": str(payload.get("last_assistant_message") or "")[:400],
        "cwd": payload.get("cwd", ""),
    }
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except IOError:
        pass

    if os.path.exists(NOTIFY):
        try:
            subprocess.run(
                ["bash", NOTIFY, "--status", "failure", "--task", "stop-hook-crashed", "--channel", "desktop"],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
