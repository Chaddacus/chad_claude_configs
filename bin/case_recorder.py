#!/usr/bin/env python3
"""PostToolUse / PostToolUseFailure recorder — appends to the session case file.

Wire under BOTH hook events. The invocation flag determines exit code:

    PostToolUse  → case_recorder.py             → exit=0 on Bash
    PostToolUseFailure → case_recorder.py --failure → exit=1 on Bash

Without dual-wiring, every failing Bash call goes silently unrecorded.

Appends to ~/.claude/state/cases/${session_id}/events.jsonl and rebuilds
summary.json. Cheap (one append + one small JSON dump per tool call).
Pure observer — does not interfere with other PostToolUse hooks.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"))
from case_file import (
    append_event,
    rebuild_summary,
    classify_command,
    resolve_session_id,
    summarize_bash_output,
)


def main() -> int:
    # Invocation channel: --failure flag → exit code = 1, else 0.
    failure_channel = "--failure" in sys.argv

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw else {}
    except Exception:
        return 0

    tool_name = data.get("tool_name") or data.get("tool", "")
    tool_input = data.get("tool_input", {}) or {}
    tool_response = data.get("tool_response", {}) or {}
    # Some payloads include a top-level tool_error field on failure
    tool_error = data.get("tool_error")

    if not tool_name:
        return 0

    event: dict = {"tool": tool_name}

    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        fp = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if fp:
            event["file"] = fp
        # Edit/Write failures still get recorded with exit=1 so we can detect
        # claimed implementations whose underlying write actually failed.
        event["exit"] = 1 if (failure_channel or tool_error) else 0
    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")
        event["command"] = cmd
        event["kind"] = classify_command(cmd)
        event["exit"] = 1 if (failure_channel or tool_error) else 0

        # Capture stdout/stderr summary regardless of channel
        if isinstance(tool_response, dict):
            stdout = tool_response.get("stdout", "") or tool_response.get("output", "") or ""
            stderr = tool_response.get("stderr", "") or ""
            combined = (str(stdout) + "\n" + str(stderr)).strip()
        elif isinstance(tool_response, str):
            combined = tool_response
        else:
            combined = ""
        event["summary"] = summarize_bash_output(combined)
    else:
        # Other tools (Read, Grep, etc.) — record name only, no payload
        # (keeps events.jsonl small; we don't need them for completion verify)
        return 0

    sid = resolve_session_id(data)
    if not sid:
        # No session identity — recording under a shared key would let one
        # session's activity masquerade as another's. Skip.
        return 0
    append_event(event, session_id=sid)
    # Rebuild summary on every relevant event — small file, cheap.
    rebuild_summary(session_id=sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
