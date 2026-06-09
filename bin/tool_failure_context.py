#!/usr/bin/env python3
"""PostToolUseFailure hook — structured recovery context for Bash failures.

Reads hook input from stdin, parses the failed command and stderr,
classifies the failure type, and returns structured recovery context.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"))
from hook_profile import should_run
if not should_run("tool_failure_context"):
    sys.exit(0)

from case_file import resolve_session_id, verify_ledger_path

# Set in main() from the hook's stdin session_id. None → no edited-files
# context (failure classification still works).
LEDGER_PATH = None

# Patterns for classifying failure types
TEST_PATTERNS = [
    r"(npm\s+test|npx\s+jest|npx\s+vitest|pytest|python\s+-m\s+pytest|cargo\s+test|go\s+test)",
]
TYPE_PATTERNS = [
    r"(npx\s+tsc|tsc\s+--noEmit|mypy|cargo\s+check|go\s+vet)",
]
BUILD_PATTERNS = [
    r"(npm\s+run\s+build|cargo\s+build|go\s+build|make\b)",
]

# Error extraction patterns
TS_ERROR_RE = re.compile(r"([\w/.]+\.tsx?)\((\d+),(\d+)\):\s*error\s+TS\d+:\s*(.+)")
PYTEST_ERROR_RE = re.compile(r"FAILED\s+([\w/.]+::[\w]+)(?:\s*-\s*(.+))?")
JEST_ERROR_RE = re.compile(r"●\s+(.+)")
RUST_ERROR_RE = re.compile(r"error(?:\[E\d+\])?: (.+)\n\s*-->\s*([\w/.]+):(\d+):(\d+)")
GO_ERROR_RE = re.compile(r"([\w/.]+\.go):(\d+):(\d+):\s*(.+)")
GENERIC_ERROR_RE = re.compile(r"(?:error|Error|ERROR)[:]\s*(.+)")


def load_edited_files() -> set:
    """Load list of recently edited files from the ledger."""
    if not LEDGER_PATH or not os.path.exists(LEDGER_PATH):
        return set()
    try:
        with open(LEDGER_PATH, "r") as f:
            ledger = json.load(f)
        return {edit.get("file", "") for edit in ledger.get("edits", [])}
    except (json.JSONDecodeError, IOError):
        return set()


def classify_failure(command: str) -> str:
    """Classify the type of failure based on the command."""
    cmd_lower = command.lower()
    for pattern in TEST_PATTERNS:
        if re.search(pattern, cmd_lower):
            return "test"
    for pattern in TYPE_PATTERNS:
        if re.search(pattern, cmd_lower):
            return "type"
    for pattern in BUILD_PATTERNS:
        if re.search(pattern, cmd_lower):
            return "build"
    if "permission denied" in cmd_lower or "not found" in cmd_lower:
        return "permission"
    return "unknown"


def extract_errors(output: str, failure_type: str) -> list[dict]:
    """Extract structured errors from command output."""
    errors = []

    if failure_type == "test":
        # Try Jest/Vitest
        for match in JEST_ERROR_RE.finditer(output):
            errors.append({"message": match.group(1).strip(), "file": None, "line": None})
        # Try pytest
        for match in PYTEST_ERROR_RE.finditer(output):
            errors.append({"message": match.group(2) or match.group(1), "file": match.group(1), "line": None})

    elif failure_type == "type":
        # TypeScript errors
        for match in TS_ERROR_RE.finditer(output):
            errors.append({"file": match.group(1), "line": match.group(2), "message": match.group(4)})
        # Go errors
        for match in GO_ERROR_RE.finditer(output):
            errors.append({"file": match.group(1), "line": match.group(2), "message": match.group(4)})

    elif failure_type == "build":
        # Rust errors
        for match in RUST_ERROR_RE.finditer(output):
            errors.append({"file": match.group(2), "line": match.group(3), "message": match.group(1)})

    # Generic fallback
    if not errors:
        for match in GENERIC_ERROR_RE.finditer(output):
            errors.append({"message": match.group(1).strip(), "file": None, "line": None})

    return errors[:5]  # Top 5 errors


def classify_introduced(errors: list[dict], edited_files: set) -> tuple[int, int]:
    """Classify how many errors are in edited files vs pre-existing."""
    introduced = 0
    preexisting = 0
    for err in errors:
        err_file = err.get("file")
        if err_file and any(err_file.endswith(ef) or ef.endswith(err_file) for ef in edited_files if ef):
            introduced += 1
            err["introduced"] = True
        else:
            preexisting += 1
            err["introduced"] = False
    return introduced, preexisting


def format_error(idx: int, err: dict) -> str:
    """Format a single error for display."""
    parts = [f"{idx}."]
    if err.get("file"):
        loc = err["file"]
        if err.get("line"):
            loc += f":{err['line']}"
        parts.append(loc)
        parts.append("—")
    parts.append(err.get("message", "unknown error"))
    if not err.get("introduced", True):
        parts.append("(pre-existing)")
    return " ".join(parts)


def main():
    # Read hook input from stdin
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, IOError):
        sys.exit(0)

    sid = resolve_session_id(hook_input)
    if sid:
        global LEDGER_PATH
        LEDGER_PATH = str(verify_ledger_path(sid))

    tool_name = hook_input.get("tool_name", "")

    # Only act on Bash tool failures
    if tool_name != "Bash":
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    tool_response = hook_input.get("tool_response", "")
    command = tool_input.get("command", "")

    if not command:
        sys.exit(0)

    # Get the output (stderr + stdout)
    output = ""
    if isinstance(tool_response, dict):
        output = tool_response.get("stderr", "") + "\n" + tool_response.get("stdout", "")
    elif isinstance(tool_response, str):
        output = tool_response

    failure_type = classify_failure(command)
    errors = extract_errors(output, failure_type)
    edited_files = load_edited_files()

    if not errors:
        # Couldn't extract structured errors, provide minimal context
        sys.exit(0)

    introduced, preexisting = classify_introduced(errors, edited_files)

    # Build structured context
    type_label = {
        "test": "test failure",
        "type": "type error",
        "build": "build failure",
        "permission": "permission/not-found",
        "unknown": "command failure",
    }.get(failure_type, "failure")

    lines = [
        f"⚠️ Command failed: {command}",
        f"Type: {type_label} ({len(errors)} error{'s' if len(errors) != 1 else ''})",
    ]

    if edited_files:
        likely = "yes" if introduced > 0 else "no"
        lines.append(f"Likely introduced: {likely} ({introduced}/{len(errors)} in recently edited files)")

    lines.append("")
    lines.append("Top errors:")
    for i, err in enumerate(errors, 1):
        lines.append(format_error(i, err))

    if introduced > 0 and preexisting > 0:
        lines.append("")
        introduced_nums = [str(i + 1) for i, e in enumerate(errors) if e.get("introduced")]
        preexisting_nums = [str(i + 1) for i, e in enumerate(errors) if not e.get("introduced")]
        lines.append(f"Suggested: Fix #{', #'.join(introduced_nums)} (your edits). #{', #'.join(preexisting_nums)} appear{'s' if len(preexisting_nums) == 1 else ''} pre-existing.")

    context = "\n".join(lines)
    envelope = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUseFailure",
            "additionalContext": context,
        }
    }
    print(json.dumps(envelope))


if __name__ == "__main__":
    main()
