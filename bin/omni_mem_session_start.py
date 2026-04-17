#!/usr/bin/env python3
"""SessionStart hook — inject omni-mem briefing for the current workspace.

Runs on session startup (and optionally resume). Derives the workspace ID
from $PWD basename, calls omni-mem's build_briefing tool via MCP stdio, and
emits hookSpecificOutput.additionalContext for Claude Code to inject into
the session.

Fails silently on any error — never blocks session start.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

TIMEOUT_SECONDS = 5
CONTAINER = "omni-mem"


def _workspace_id() -> str:
    """Derive workspace ID from CWD basename, falling back to 'default'."""
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    name = Path(cwd).name
    return name or "default"


def _mcp_call(workspace_id: str) -> str | None:
    """One-shot MCP call to omni-mem's build_briefing. Returns text or None."""
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "session-start-hook", "version": "0"},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    call = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {
            "name": "build_briefing",
            "arguments": {"workspaceId": workspace_id},
        },
    }
    payload = "\n".join(json.dumps(m) for m in (init, initialized, call)) + "\n"

    try:
        proc = subprocess.run(
            ["docker", "exec", "-i", CONTAINER, "omni-mem", "mcp-server"],
            input=payload,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    for line in proc.stdout.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") != 2:
            continue
        result = msg.get("result") or {}
        content = result.get("content") or []
        texts = [c.get("text") for c in content if c.get("type") == "text" and c.get("text")]
        if texts:
            return "\n".join(texts)
        structured = result.get("structuredContent")
        if structured:
            return json.dumps(structured, indent=2)
    return None


def main() -> int:
    try:
        _ = sys.stdin.read()  # consume hook input; we don't need its fields
    except Exception:
        pass

    workspace_id = _workspace_id()
    briefing = _mcp_call(workspace_id)
    if not briefing:
        return 0

    # Cap injected context so very long briefings don't blow session budget.
    max_chars = 4000
    if len(briefing) > max_chars:
        briefing = briefing[:max_chars] + "\n\n[briefing truncated]"

    header = f"## omni-mem briefing — workspace `{workspace_id}`\n\n"
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": header + briefing,
        }
    }
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
