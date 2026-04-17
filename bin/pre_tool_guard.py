#!/usr/bin/env python3
"""PreToolUse hook — catastrophic command guard.

Blocks high-confidence destructive commands that would cause irreversible
data loss. This is a safety net, not governance policy.

Exit codes:
  0 — allow (no output needed)
  2 — block (output is shown to user as rejection reason)
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"))
from hook_profile import should_run
if not should_run("pre_tool_guard"):
    sys.exit(0)

# High-confidence catastrophic patterns only
BLOCK_PATTERNS = [
    (r"rm\s+(-[rf]+\s+)*/\s*$", "rm -rf / (root filesystem deletion)"),
    (r"rm\s+(-[rf]+\s+)*/\*", "rm -rf /* (root wildcard deletion)"),
    (r"find\s.*-delete", "find with -delete (mass file deletion)"),
    (r">\s*/dev/sd", "write to block device"),
    (r"mkfs\.", "format filesystem"),
    (r"dd\s+.*of=/dev/", "raw disk write via dd"),
    (r"git\s+push\s+.*--force", "git force push"),
    (r"git\s+push\s+-f\b", "git force push (-f)"),
    (r":\s*>\s*\S", "file truncation via colon redirect"),
    (r"truncate\s+(?!--help|--version)", "file truncation"),
    (r"rm\s+(-[rf]+\s+)*\*\s*$", "rm -rf * (wildcard deletion in current dir)"),
]


def main():
    # Read hook input from stdin
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, IOError):
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "")

    # Only guard Bash commands
    if tool_name != "Bash":
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    command = tool_input.get("command", "")

    if not command:
        sys.exit(0)

    for pattern, description in BLOCK_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            reason = f"🛑 Blocked by pre-tool guard: {description}\nCommand: {command}\n\nThis command was blocked because it matches a catastrophic operation pattern. If this is intentional, ask the user to run it manually."
            print(reason, file=sys.stderr)
            sys.exit(2)

    # Allow
    sys.exit(0)


if __name__ == "__main__":
    main()
