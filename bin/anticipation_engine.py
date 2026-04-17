#!/usr/bin/env python3
"""Anticipation engine for /drive autonomous mode.

Zero LLM calls. Reads the completion_gate verify ledger and git state,
applies pattern matching, and outputs the highest-weight next step as JSON.

Output: {"next_step": str, "weight": float, "should_continue": bool, "action_hint": str}
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


CONTINUE_THRESHOLD = 0.60


def load_verify_ledger(session_id: str) -> dict:
    path = Path(f"/tmp/claude-verify-{session_id}.json")
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"verified_clean": True, "last_edit_at": 0, "last_verified_at": 0, "edits": [], "verifications": []}


def has_code_edits(ledger: dict) -> bool:
    code_exts = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rs", ".java", ".kt", ".rb", ".cs"}
    return any(
        Path(e.get("file", "")).suffix.lower() in code_exts
        for e in ledger.get("edits", [])
    )


def has_pending_git_changes(cwd: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def classify_test_failure(ledger: dict) -> str:
    """Returns 'clear', 'ambiguous', or 'none'."""
    if ledger.get("verified_clean", True):
        return "none"
    for v in reversed(ledger.get("verifications", [])):
        if v.get("result") == "fail":
            output = v.get("output", "") or ""
            clear_signals = ["Error:", "error:", "FAIL", "assert", "TypeError",
                             "SyntaxError", "line ", "File \"", "Exception", "failed"]
            if any(sig in output for sig in clear_signals):
                return "clear"
            return "ambiguous"
    return "none"


def compute_next_step(session_id: str, cwd: str) -> dict:
    ledger = load_verify_ledger(session_id)
    last_edit = float(ledger.get("last_edit_at", 0))
    last_verified = float(ledger.get("last_verified_at", 0))
    verified_clean = ledger.get("verified_clean", True)
    has_edits = has_code_edits(ledger)
    failure_type = classify_test_failure(ledger)

    # Pattern: unverified code edits → run tests
    if has_edits and last_edit > last_verified:
        return {
            "next_step": "run_tests",
            "weight": 0.90,
            "should_continue": True,
            "action_hint": "Code was edited since last verification — run the test suite now",
        }

    # Pattern: tests failing with clear error → fix
    if failure_type == "clear":
        return {
            "next_step": "fix_error",
            "weight": 0.85,
            "should_continue": True,
            "action_hint": "Tests are failing with a clear error — fix it before continuing",
        }

    # Pattern: tests failing ambiguously → investigate
    if failure_type == "ambiguous":
        return {
            "next_step": "investigate_error",
            "weight": 0.65,
            "should_continue": True,
            "action_hint": "Tests are failing but the error is unclear — investigate before fixing",
        }

    # Pattern: tests passing, code changed → check typecheck and sprint criteria
    if verified_clean and has_edits:
        return {
            "next_step": "check_sprint_criteria",
            "weight": 0.85,
            "should_continue": True,
            "action_hint": "Tests passing — run typecheck/lint and evaluate sprint acceptance criteria",
        }

    # Pattern: pending git changes (uncommitted work) → continue
    if has_pending_git_changes(cwd):
        return {
            "next_step": "continue_work",
            "weight": 0.75,
            "should_continue": True,
            "action_hint": "There are uncommitted changes — continue driving toward the goal",
        }

    # No strong signal — soft prompt to check criteria
    return {
        "next_step": "check_sprint_criteria",
        "weight": 0.50,
        "should_continue": False,
        "action_hint": "Check whether all sprint acceptance criteria are satisfied",
    }


def main() -> None:
    session_id = os.environ.get("CLAUDE_SESSION_ID", "default")
    cwd = os.getcwd()
    result = compute_next_step(session_id, cwd)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
