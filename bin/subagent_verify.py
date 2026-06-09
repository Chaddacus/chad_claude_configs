#!/usr/bin/env python3
"""SubagentStop hook — verification reminder for subagents with code changes.

When a subagent completes and had write permissions, checks the ledger
for unverified edits and injects a reminder if found.
"""

import json
import os
import sys

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

    # Check for unverified code edits
    unverified = []
    for edit in ledger.get("edits", []):
        if edit.get("timestamp", 0) > last_verified:
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
