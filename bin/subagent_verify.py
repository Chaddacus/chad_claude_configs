#!/usr/bin/env python3
"""SubagentStop hook — verification reminder for subagents with code changes.

When a subagent completes and had write permissions, checks the ledger
for unverified edits and injects a reminder if found.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"))
from hook_profile import should_run
if not should_run("subagent_verify"):
    sys.exit(0)

from case_file import resolve_session_id, verify_ledger_path

CODE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".py", ".pyw",
    ".rs",
    ".go",
    ".java", ".kt",
    ".rb",
    ".c", ".cpp", ".cc", ".h", ".hpp",
    ".cs",
    ".swift",
}


def _subagent_start_floor(hook_input: dict) -> float | None:
    """Epoch time this subagent began, from its OWN transcript's first entry.

    Subagents inherit the parent's session_id (case_file.py:resolve_session_id),
    so the verify ledger they resolve to is the PARENT's — full of the parent
    session's unverified edits. Without a floor, this hook reports the parent's
    code edits against every subagent stop (the 2026-06-10 fleet-audit incident:
    25 read-only auditors each nagged about the parent's ~/.claude/*.py edits).

    The subagent's own transcript_path lets us bound "edits made by THIS
    subagent" to those after it started. Returns None when unresolvable —
    callers must then suppress (a subagent must not be nagged about edits it
    cannot be shown to have made).

    NOTE (2026-07-01 fix): on a SubagentStop event the subagent's OWN transcript
    is at `agent_transcript_path`; plain `transcript_path` is the PARENT session's.
    Reading the parent transcript put the floor at the parent's start, so EVERY
    parent edit counted as "after the subagent started" — the read-only subagent
    got nagged about the parent's edits and looped (the 2026-06-10 incident this
    guard was meant to prevent, reintroduced by reading the wrong path). Prefer
    the agent-specific path, matching completion_gate.py."""
    tp = hook_input.get("agent_transcript_path") or hook_input.get("transcript_path")
    if not tp:
        return None
    p = Path(os.path.expanduser(tp))
    try:
        with open(p, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = json.loads(line).get("timestamp")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if ts:
                    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        return p.stat().st_ctime  # transcript exists but no parseable ts
    except (OSError, ValueError):
        return None


def _verify_mark_path(sid: str, agent_key: str) -> str:
    """Per-(session, subagent) idempotency marker. A SubagentStop reminder itself
    triggers another SubagentStop, so without this the reminder re-prompts the
    subagent forever (2026-07-01: a read-only probe was handed the parent's edit
    list 8x, and its defensive reply — not its deliverable — surfaced to the parent)."""
    base = os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude"))
    d = os.path.join(base, "state", "subagent-verify-marks")
    safe = "".join(c if c.isalnum() else "_" for c in f"{sid}-{agent_key}")[:180]
    return os.path.join(d, safe + ".done")


def main():
    # Read hook input from stdin
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, IOError):
        sys.exit(0)

    # Session-scoped ledger only. A shared fallback key let this hook block
    # subagents with OTHER sessions' unverified edits (2026-06-09 incident,
    # which trained an agent to game the gate). No session id → fail open.
    sid = resolve_session_id(hook_input)
    if not sid:
        sys.exit(0)

    # At-most-once per subagent task: claim an idempotency mark up front so the
    # reminder cannot re-fire into a stop loop even if attribution is imperfect.
    agent_key = str(hook_input.get("agent_id")
                    or os.path.basename(str(hook_input.get("agent_transcript_path") or "sub")))
    mark = _verify_mark_path(sid, agent_key)
    if os.path.exists(mark):
        sys.exit(0)
    try:
        os.makedirs(os.path.dirname(mark), exist_ok=True)
        with open(mark, "w"):
            pass
    except OSError:
        pass

    ledger_path = verify_ledger_path(sid)

    # Check if ledger has unverified edits
    if not ledger_path.exists():
        sys.exit(0)

    try:
        with open(ledger_path, "r") as f:
            ledger = json.load(f)
    except (json.JSONDecodeError, IOError):
        sys.exit(0)

    last_edit = ledger.get("last_edit_at", 0)
    last_verified = ledger.get("last_verified_at", 0)

    if last_edit <= 0:
        sys.exit(0)

    # Scope to edits THIS subagent made: only those after it started. The
    # ledger is parent-session-scoped, so without this floor the parent's
    # edits get misattributed to the subagent. No floor → suppress (fail open):
    # a subagent must not be nagged about edits it cannot be shown to own.
    start_floor = _subagent_start_floor(hook_input)
    if start_floor is None:
        sys.exit(0)

    # Check for unverified code edits made during this subagent's run.
    unverified = []
    for edit in ledger.get("edits", []):
        ts = edit.get("timestamp", 0)
        if ts > last_verified and ts >= start_floor:
            ext = os.path.splitext(edit.get("file", ""))[1].lower()
            if ext in CODE_EXTENSIONS:
                unverified.append(edit["file"])

    if not unverified:
        sys.exit(0)

    unique_files = sorted(set(unverified))
    lines = [
        "⚠️ Subagent completed with unverified code changes.",
        f"Files changed ({len(unique_files)}):",
    ]
    for f in unique_files[:10]:
        lines.append(f"  - {f}")
    if len(unique_files) > 10:
        lines.append(f"  ... and {len(unique_files) - 10} more")
    lines.append("Run verification before accepting these changes.")

    context = "\n".join(lines)
    envelope = {
        "hookSpecificOutput": {
            "hookEventName": "SubagentStop",
            "additionalContext": context,
        }
    }
    print(json.dumps(envelope))


if __name__ == "__main__":
    main()
