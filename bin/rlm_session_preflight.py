#!/usr/bin/env python3
"""
SessionStart hook: load cached RLM scan summary into Claude's context.
Outputs {"systemMessage": "..."} if a scan cache exists for the current cwd.
Exits silently (no output, code 0) when no cache is found.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")))
MAX_CACHE_BYTES = 2 * 1024 * 1024  # 2MB guard


def encode_path(p: str) -> str:
    return p.lstrip("/").replace("/", "-")


def main():
    hook_input = {}
    try:
        raw = sys.stdin.read()
        if raw.strip():
            hook_input = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        pass

    cwd = hook_input.get("cwd") or os.environ.get("PWD", os.getcwd())
    encoded = encode_path(cwd)
    mem_dir = CLAUDE_HOME / "projects" / encoded / "memory"

    for scan_type in ("security", "general", "architecture"):
        cache_file = mem_dir / f"rlm_scan_{scan_type}.json"
        if not cache_file.exists():
            continue

        # Size guard
        if cache_file.stat().st_size > MAX_CACHE_BYTES:
            continue

        try:
            data = json.loads(cache_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        scanned_at = data.get("scanned_at", "")
        try:
            scanned_dt = datetime.fromisoformat(scanned_at.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - scanned_dt).days
            age_str = f"{age_days}d ago"
        except (ValueError, AttributeError):
            age_str = "unknown age"

        project_summary = data.get("project_summary", "")
        key_findings = data.get("key_findings", [])[:5]
        file_count = len(data.get("file_hashes", {}))
        module_count = len(data.get("modules", []))

        msg = f"[RLM Scan — {scan_type}, {age_str}]\n"
        msg += f"{project_summary}\n"
        if key_findings:
            msg += "\nKey findings:\n"
            for f in key_findings:
                msg += f"  • {f}\n"
        msg += f"\n({module_count} modules, {file_count} files indexed)\n"
        msg += "Run /rlm-scan to refresh or scan a new path."

        print(json.dumps({"systemMessage": msg}))
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
