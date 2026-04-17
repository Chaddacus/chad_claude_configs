#!/usr/bin/env python3
"""
PermissionRequest hook — logs permission requests for audit trail.
Pass-through: exits 0 so Claude proceeds normally.
Audit log: ~/.claude/state/permission_audit.log
"""
import json
import sys
import os
from datetime import datetime, timezone

LOG_PATH = os.path.expanduser("~/.claude/state/permission_audit.log")

def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": data.get("tool_name", "unknown"),
        "input": data.get("tool_input", {}),
        "session": data.get("session_id", ""),
    }

    try:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

    sys.exit(0)

if __name__ == "__main__":
    main()
