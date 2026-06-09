#!/usr/bin/env python3
"""agent_def_edit_warn.py — PostToolUse hook on Edit|Write.

When an agent definition under ~/.claude/agents/*.md is edited mid-session,
agent dispatch can still hit the session-cached version of the file. Emit a
stderr warning so the user knows to restart Claude Code (or dispatch via a
non-cached agent type) to pick up the change.

Hook contract: exit 0 always (advisory only). stderr surfaces to the user.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
AGENTS_DIR = HOME / ".claude" / "agents"


def _extract_target(payload: dict) -> str:
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    return (
        tool_input.get("file_path")
        or tool_input.get("filePath")
        or tool_input.get("path")
        or payload.get("file_path")
        or ""
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    target = _extract_target(payload)
    if not target:
        return 0
    target_abs = str(Path(target).expanduser().resolve())
    pattern = str(AGENTS_DIR / "*.md")
    if not fnmatch.fnmatch(target_abs, pattern):
        return 0

    name = Path(target_abs).stem
    msg = (
        f"\n[agent_def_edit_warn] {name}.md was edited. The Agent dispatcher caches "
        f"agent definitions at session start — your edit will NOT take effect for "
        f"`subagent_type: {name}` calls until Claude Code is restarted, OR you "
        f"dispatch via subagent_type: general-purpose with the new prompt embedded.\n"
    )
    print(msg, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
