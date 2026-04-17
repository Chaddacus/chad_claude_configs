#!/usr/bin/env python3
"""PostToolUse observation capture hook.

Logs tool calls to a JSONL file for later pattern analysis.
Only active when CLAUDE_HOOK_PROFILE=strict.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Only run in strict profile
profile = os.environ.get("CLAUDE_HOOK_PROFILE", "standard")
if profile != "strict":
    sys.exit(0)

# Read hook input
try:
    hook_input = json.loads(sys.stdin.read())
except (json.JSONDecodeError, IOError):
    sys.exit(0)

tool_name = hook_input.get("tool_name", "")
tool_input = hook_input.get("tool_input", {})
session_id = os.environ.get("CLAUDE_SESSION_ID", "unknown")
cwd = os.environ.get("PWD", os.getcwd())

# Determine project scope from git remote
project_id = "unknown"
try:
    import subprocess
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, timeout=2, cwd=cwd
    )
    if result.returncode == 0:
        import hashlib
        project_id = hashlib.sha256(result.stdout.strip().encode()).hexdigest()[:12]
except Exception:
    pass

# Write observation
obs_dir = Path(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude"))) / "observations"
obs_dir.mkdir(exist_ok=True)
obs_file = obs_dir / f"{project_id}.jsonl"

observation = {
    "ts": datetime.now(timezone.utc).isoformat(),
    "session": session_id,
    "tool": tool_name,
    "input_summary": str(tool_input)[:200],
    "success": not hook_input.get("tool_error"),
    "cwd": cwd,
}

with open(obs_file, "a") as f:
    f.write(json.dumps(observation) + "\n")

sys.exit(0)
