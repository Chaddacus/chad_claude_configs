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
    (r"rm\s+(-[rf]+\s+)*(/etc|/usr|/var|/System|/Library|/bin|/sbin)\b", "rm -rf of system directory"),
    (r"find\s.*-delete", "find with -delete (mass file deletion)"),
    (r">\s*/dev/sd", "write to block device"),
    (r"mkfs\.", "format filesystem"),
    (r"dd\s+.*of=/dev/", "raw disk write via dd"),
    (r"git\s+push\s+.*--force", "git force push"),
    (r"git\s+push\s+-f\b", "git force push (-f)"),
    (r"git\s+reset\s+--hard", "git reset --hard (discards local changes; policy: only on explicit user request)"),
    (r"git\s+checkout\s+--\s", "git checkout -- (discards local changes; policy: only on explicit user request)"),
    (r"(curl|wget)\s[^|;&]*\|\s*(ba|z|da)?sh\b", "remote script piped to shell (curl|sh)"),
    (r":\s*>\s*\S", "file truncation via colon redirect"),
    (r"truncate\s+(?!--help|--version)", "file truncation"),
    (r"rm\s+(-[rf]+\s+)*\*\s*$", "rm -rf * (wildcard deletion in current dir)"),
]

# SQL mass-mutation guard: DELETE/UPDATE without WHERE in a CLI invocation.
SQL_CLI = re.compile(r"\b(psql|mysql|sqlite3|mariadb)\b", re.IGNORECASE)
SQL_MUTATION = re.compile(r"\b(delete\s+from\s+\S+|update\s+\S+\s+set\s+)", re.IGNORECASE)
SQL_WHERE = re.compile(r"\bwhere\b", re.IGNORECASE)

# Policy-path write guard: Bash writes to gated policy files bypass
# policy_edit_gate (PreToolUse on Edit|Write only). Force those through the
# Edit tool so the gate can score them. Read access stays unrestricted.
POLICY_PATH = re.compile(
    r"(\.claude/CLAUDE\.md|\.claude/state/route_manifest\.json"
    r"|\.claude/state/control_plane\.json|\.claude/agents/[\w.-]+\.md)"
)
WRITE_INDICATOR = re.compile(
    r"(\bsed\s+-i\b|\btee\b|>{1,2}\s*\S*\.claude/|\bopen\([^)]*['\"](w|a)"
    r"|write_text\(|json\.dump\b|\bmv\s|\bcp\s|\btruncate\b|\bshutil\.)"
)


# Shell/interpreter -c invocations execute their quoted argument — those
# quotes must NOT be stripped before matching.
INLINE_EXEC = re.compile(r"\b((ba|z|da)?sh|python3?|perl|ruby|node)\s+(-[A-Za-z]*\s+)*-[ce]\b")
_DQUOTED = re.compile(r'"[^"]*"')
_SQUOTED = re.compile(r"'[^']*'")


def strippable_text(command: str) -> str:
    """Text used for BLOCK_PATTERNS matching. Quoted spans are data, not
    commands (evidence strings, commit messages, log text constantly NAME
    destructive patterns) — strip them, unless the command is an inline
    shell/interpreter exec whose quoted argument IS the command."""
    if INLINE_EXEC.search(command):
        return command
    return _SQUOTED.sub(" ", _DQUOTED.sub(" ", command))


def sql_mass_mutation(command: str) -> bool:
    if not SQL_CLI.search(command):
        return False
    m = SQL_MUTATION.search(command)
    if not m:
        return False
    # WHERE anywhere after the mutation keyword counts as scoped.
    return not SQL_WHERE.search(command[m.start():])


def policy_write_bypass(command: str) -> bool:
    return bool(POLICY_PATH.search(command)) and bool(WRITE_INDICATOR.search(command))


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

    # SQL + policy-write checks intentionally see the full command (their
    # signal lives inside quotes: psql -c '...', quoted paths).
    match_text = strippable_text(command)

    for pattern, description in BLOCK_PATTERNS:
        if re.search(pattern, match_text, re.IGNORECASE):
            reason = f"🛑 Blocked by pre-tool guard: {description}\nCommand: {command}\n\nThis command was blocked because it matches a catastrophic operation pattern. If this is intentional, ask the user to run it manually."
            print(reason, file=sys.stderr)
            sys.exit(2)

    if sql_mass_mutation(command):
        print(
            "🛑 Blocked by pre-tool guard: SQL DELETE/UPDATE without WHERE.\n"
            f"Command: {command}\n\nScope the mutation with a WHERE clause, "
            "or ask the user to run it manually if a full-table mutation is intended.",
            file=sys.stderr,
        )
        sys.exit(2)

    if policy_write_bypass(command):
        print(
            "🛑 Blocked by pre-tool guard: Bash write to a gated policy file.\n"
            f"Command: {command}\n\nPolicy files (CLAUDE.md, route_manifest.json, "
            "control_plane.json, agents/*.md) must be edited via the Edit/Write "
            "tools so policy_edit_gate can score the change. Reads are unrestricted.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Allow
    sys.exit(0)


if __name__ == "__main__":
    main()
