#!/usr/bin/env python3
"""PostToolUseFailure FAL adapter.

Detects whether the failed Bash command looks like a test/build/lint
invocation; if so, parses the tool_error through fal_parse.py and appends
the FAL record to ~/.claude/state/fal-records.jsonl.

Always exits 0 — observability only, never blocks. Pass-through for
non-test failures (just no FAL record emitted).

Wired as an additive entry under hooks.PostToolUseFailure in settings.json.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

STATE = Path.home() / ".claude" / "state" / "fal-records.jsonl"
PARSER = Path.home() / ".claude" / "bin" / "fal_parse.py"

# Commands whose failures should produce a FAL record. Conservative — we'd
# rather miss a few than emit garbage records for unrelated failures.
TEST_BUILD_PATTERNS = re.compile(
    r"\b("
    r"pytest|py\.test|"
    r"npm\s+(?:test|run\s+test)|"
    r"yarn\s+test|"
    r"pnpm\s+(?:test|run\s+test)|"
    r"jest|vitest|mocha|"
    r"cargo\s+test|cargo\s+build|cargo\s+check|"
    r"go\s+test|go\s+build|go\s+vet|"
    r"make\s+(?:test|check|build)|"
    r"tsc|"
    r"mypy|"
    r"ruff|"
    r"eslint|"
    r"rspec|rake\s+test|"
    r"bundle\s+exec\s+rspec|"
    r"phpunit|"
    r"ctest"
    r")\b",
    re.IGNORECASE,
)


def _looks_like_test_or_build(cmd: str) -> bool:
    return bool(TEST_BUILD_PATTERNS.search(cmd))


def main() -> int:
    try:
        evt = json.load(sys.stdin)
    except Exception:
        return 0  # malformed event — never block

    tool_name = evt.get("tool_name", "")
    tool_input = evt.get("tool_input", {}) or {}
    tool_error = evt.get("tool_error", "") or ""
    tool_use_id = evt.get("tool_use_id", "")

    if tool_name != "Bash":
        return 0
    cmd = (tool_input.get("command") or "")
    if not _looks_like_test_or_build(cmd):
        return 0
    if not tool_error:
        return 0

    # Run fal_parse.py in a subprocess so a parser bug can't crash this hook.
    try:
        proc = subprocess.run(
            ["python3", str(PARSER), "--tool-use-id", tool_use_id],
            input=tool_error,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return 0
        rec = json.loads(proc.stdout)
    except Exception:
        return 0

    # Augment with the originating command for cross-reference.
    rec["originating_command"] = cmd[:500]
    rec["recorded_at"] = time.time()

    STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with STATE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
