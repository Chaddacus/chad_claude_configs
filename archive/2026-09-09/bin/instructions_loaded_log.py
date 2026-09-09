#!/usr/bin/env python3
"""InstructionsLoaded hook — audit log of policy/rules-file loads.

Captures every CLAUDE.md / .claude/rules/*.md load (session_start,
nested_traversal, path_glob_match, include, compact) with the file's
SHA256 so post-run forensics can detect policy drift.

Logs to: ~/.claude/state/instructions-loaded.jsonl
Stdout:  (empty — this hook cannot block)
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

STATE = Path.home() / ".claude" / "state" / "instructions-loaded.jsonl"


def main() -> int:
    try:
        evt = json.load(sys.stdin)
    except Exception:
        return 0

    fp = evt.get("file_path", "")
    sha = ""
    size = 0
    if fp:
        try:
            data = Path(fp).read_bytes()
            sha = hashlib.sha256(data).hexdigest()[:16]
            size = len(data)
        except Exception:
            sha = "unreadable"

    rec = {
        "ts": time.time(),
        "file_path": fp,
        "memory_type": evt.get("memory_type", ""),
        "load_reason": evt.get("load_reason", ""),
        "sha256_16": sha,
        "size_bytes": size,
        "trigger_file_path": evt.get("trigger_file_path", ""),
        "parent_file_path": evt.get("parent_file_path", ""),
    }

    STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with STATE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
