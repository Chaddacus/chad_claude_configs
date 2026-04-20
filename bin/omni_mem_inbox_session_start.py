#!/usr/bin/env python3
"""SessionStart hook — inject a compact dev-inbox of a2a tasks awaiting action.

Shells out to the management-mcp CLI (`omni-mem-manage inbox --json`) to read
the caller's inbox (dispatched tasks targeting their identity from the cloud
omni-mem), filters to non-terminal states, and emits
hookSpecificOutput.additionalContext.

Fails silently on any error — the session must never fail to start because of
this hook.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

TIMEOUT_SECONDS = 10
DEFAULT_REPO_ROOT = "/Users/chadsimon/code/omni-mem"
MAX_ENTRIES = 8
TERMINAL_STATES = {"completed", "failed", "rejected"}


def render_inbox(report: Any) -> str | None:
    """Pure function: turn an inbox report dict into a compact summary string.

    Returns None if the report is unusable or contains no actionable tasks.
    """
    if not isinstance(report, dict):
        return None
    assignee = report.get("assignee")
    tasks = report.get("tasks")
    if not isinstance(assignee, str) or not isinstance(tasks, list):
        return None

    open_tasks = [
        t for t in tasks
        if isinstance(t, dict) and t.get("status") not in TERMINAL_STATES
    ]
    if not open_tasks:
        return None

    open_tasks.sort(key=lambda t: str(t.get("lastUpdateAt") or ""), reverse=True)
    lines = [
        f"## a2a inbox — {len(open_tasks)} open task(s) for `{assignee}`",
        "",
    ]
    for t in open_tasks[:MAX_ENTRIES]:
        status = t.get("status") or "?"
        title = t.get("title") or "(untitled)"
        creator = t.get("creator") or "?"
        task_id = t.get("taskId") or "?"
        updated = t.get("lastUpdateAt") or ""
        note = t.get("latestNote") or ""
        lines.append(f"- **[{status}]** {title}")
        lines.append(f"  - from `{creator}` · id `{task_id}` · updated {updated}")
        if note:
            lines.append(f"  - note: {note}")
    if len(open_tasks) > MAX_ENTRIES:
        lines.append("")
        lines.append(f"…and {len(open_tasks) - MAX_ENTRIES} more. Run `omni-mem-manage inbox` to see all.")
    lines.append("")
    lines.append("To act: `mcp__omni-mem-manage__list_my_tasks`, then `mcp__omni-mem-manage__record_task_transition`.")
    return "\n".join(lines)


def _load_mcp_env_fallback(path: str = "/Users/chadsimon/.mcp.json") -> dict[str, str]:
    """Read OMNI_MEM_* env vars from the omni-mem-manage MCP server config.

    Hooks don't inherit Chad's shell rc exports, but `~/.mcp.json` already
    carries the canonical config for the MCP server. Pull the env block from
    there as a fallback so the hook and the MCP server stay in sync.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    servers = data.get("mcpServers") or data.get("servers") or {}
    entry = servers.get("omni-mem-manage") or {}
    env = entry.get("env") or {}
    return {k: str(v) for k, v in env.items() if isinstance(k, str)}


def _fetch_inbox() -> dict[str, Any] | None:
    """Shell out to the management-mcp CLI and return the parsed inbox report."""
    repo_root = os.environ.get("OMNI_MEM_REPO_ROOT", DEFAULT_REPO_ROOT)
    script = os.path.join(repo_root, "scripts", "omni-mem-manage.ts")
    if not os.path.exists(script):
        return None
    env = {**os.environ}
    if "OMNI_MEM_CLOUD_URL" not in env or "OMNI_MEM_MANAGER_USER_ID" not in env:
        # Hooks don't inherit shell rc exports; fall back to the config shared
        # with the MCP server.
        fallback = _load_mcp_env_fallback()
        for key, value in fallback.items():
            env.setdefault(key, value)
        if "OMNI_MEM_CLOUD_URL" not in env or "OMNI_MEM_MANAGER_USER_ID" not in env:
            return None
    try:
        proc = subprocess.run(
            ["npx", "tsx", script, "inbox", "--json"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def main() -> int:
    try:
        _ = sys.stdin.read()  # consume hook input; we don't use it
    except Exception:
        pass

    report = _fetch_inbox()
    if report is None:
        return 0

    rendered = render_inbox(report)
    if not rendered:
        return 0

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": rendered,
        }
    }
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
