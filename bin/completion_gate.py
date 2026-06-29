#!/usr/bin/env python3
"""Completion verification gate for TaskCompleted and Stop hooks.

Reads the verification-evidence ledger and runs project-level validation
if there are unverified code edits. Returns structured context via
hookSpecificOutput.

Usage:
    python3 completion_gate.py --event task-completed
    python3 completion_gate.py --event stop
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"))
from hook_profile import should_run
# Determine hook_id from --event arg
_event = "stop" if "--event" in sys.argv and "stop" in sys.argv else "task"
if not should_run(f"completion_gate_{_event}"):
    sys.exit(0)

from case_file import resolve_session_id, verify_ledger_path

# Set in main() from the hook's stdin session_id. None → fail open (skip).
LEDGER_PATH = None
CODE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".py", ".pyw",
    ".rs",
    ".go",
    ".java", ".kt", ".kts",
    ".rb",
    ".c", ".cpp", ".cc", ".h", ".hpp",
    ".cs",
    ".swift",
    ".sh", ".bash", ".zsh",
}
MAX_OUTPUT_LINES = 30


def load_ledger() -> dict:
    """Load the verification-evidence ledger."""
    if not LEDGER_PATH or not os.path.exists(LEDGER_PATH):
        return {
            "edits": [],
            "verifications": [],
            "last_edit_at": 0,
            "last_verified_at": 0,
            "verified_clean": True,
        }
    try:
        with open(LEDGER_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {
            "edits": [],
            "verifications": [],
            "last_edit_at": 0,
            "last_verified_at": 0,
            "verified_clean": True,
        }


def save_ledger(ledger: dict) -> None:
    """Save the verification-evidence ledger."""
    if not LEDGER_PATH:
        return
    try:
        with open(LEDGER_PATH, "w") as f:
            json.dump(ledger, f, indent=2)
    except IOError:
        pass


def has_code_edits(ledger: dict) -> bool:
    """Check if there are any code file edits in the ledger."""
    for edit in ledger.get("edits", []):
        ext = os.path.splitext(edit.get("file", ""))[1].lower()
        if ext in CODE_EXTENSIONS:
            return True
    return False


def find_project_root() -> str:
    """Find the project root by looking for common project files."""
    cwd = os.getcwd()
    markers = [
        "package.json", "Cargo.toml", "go.mod", "pyproject.toml",
        "setup.py", "Makefile", ".git",
    ]
    path = cwd
    while path != "/":
        for marker in markers:
            if os.path.exists(os.path.join(path, marker)):
                return path
        path = os.path.dirname(path)
    return cwd


def resolve_commands(project_root: str) -> list[dict]:
    """Resolve verification commands based on project type.

    Returns list of {"cmd": str, "label": str} dicts.
    """
    commands = []

    # Node.js
    pkg_json_path = os.path.join(project_root, "package.json")
    if os.path.exists(pkg_json_path):
        try:
            with open(pkg_json_path, "r") as f:
                pkg = json.load(f)
            scripts = pkg.get("scripts", {})
            if "test" in scripts:
                commands.append({"cmd": "npm test", "label": "tests"})
            if "typecheck" in scripts:
                commands.append({"cmd": "npm run typecheck", "label": "typecheck"})
            elif "check" in scripts:
                commands.append({"cmd": "npm run check", "label": "check"})
            elif os.path.exists(os.path.join(project_root, "tsconfig.json")):
                commands.append({"cmd": "npx tsc --noEmit", "label": "typecheck"})
        except (json.JSONDecodeError, IOError):
            pass

    # Python
    pyproject_path = os.path.join(project_root, "pyproject.toml")
    setup_py_path = os.path.join(project_root, "setup.py")
    if os.path.exists(pyproject_path) or os.path.exists(setup_py_path):
        if os.path.exists(pyproject_path):
            try:
                with open(pyproject_path, "r") as f:
                    content = f.read()
                if "[tool.pytest" in content or "[tool.pytest.ini_options]" in content:
                    commands.append({"cmd": "python -m pytest", "label": "tests"})
                if "ruff" in content:
                    commands.append({"cmd": "ruff check .", "label": "lint"})
            except IOError:
                pass

    # Rust
    cargo_path = os.path.join(project_root, "Cargo.toml")
    if os.path.exists(cargo_path):
        commands.append({"cmd": "cargo test", "label": "tests"})
        commands.append({"cmd": "cargo clippy", "label": "lint"})

    # Go
    go_mod_path = os.path.join(project_root, "go.mod")
    if os.path.exists(go_mod_path):
        commands.append({"cmd": "go test ./...", "label": "tests"})
        commands.append({"cmd": "go vet ./...", "label": "lint"})

    # Generic Makefile fallback
    if not commands:
        makefile_path = os.path.join(project_root, "Makefile")
        if os.path.exists(makefile_path):
            try:
                with open(makefile_path, "r") as f:
                    content = f.read()
                if "\ntest:" in content or "\ntest " in content:
                    commands.append({"cmd": "make test", "label": "tests"})
                if "\ncheck:" in content or "\ncheck " in content:
                    commands.append({"cmd": "make check", "label": "check"})
            except IOError:
                pass

    return commands


def run_command(cmd: str, project_root: str, timeout: int = 25) -> dict:
    """Run a verification command and return structured results."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=project_root,
        )
        output = (result.stdout + result.stderr).strip()
        lines = output.split("\n")
        if len(lines) > MAX_OUTPUT_LINES:
            lines = lines[:MAX_OUTPUT_LINES] + [f"... ({len(lines) - MAX_OUTPUT_LINES} more lines)"]
        return {
            "command": cmd,
            "exit_code": result.returncode,
            "output": "\n".join(lines),
            "passed": result.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {
            "command": cmd,
            "exit_code": -1,
            "output": f"Command timed out after {timeout}s",
            "passed": False,
        }
    except Exception as e:
        return {
            "command": cmd,
            "exit_code": -1,
            "output": f"Error running command: {e}",
            "passed": False,
        }


def _subagent_start_floor(hook_input: dict) -> float | None:
    """Epoch time this subagent began, from its OWN transcript's first entry.

    A subagent inherits the parent's session_id, so the ledger it resolves to is
    the PARENT's — full of the parent session's unverified edits. Without a floor,
    the task-completed gate attributes the parent's code edits to EVERY subagent
    and injects a verification reminder it cannot act on, which re-prompts the
    subagent into a stop loop (the 2026-06-10 fleet-audit incident; the same guard
    already lives in subagent_verify.py). Returns None when unresolvable — the
    caller must then suppress (fail open: never nag a subagent about edits it
    cannot be shown to have made)."""
    tp = hook_input.get("agent_transcript_path") or hook_input.get("transcript_path")
    if not tp:
        return None
    p = os.path.expanduser(str(tp))
    try:
        with open(p, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = json.loads(line).get("timestamp")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if ts:
                    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        return os.path.getctime(p)  # transcript exists but no parseable ts
    except (OSError, ValueError):
        return None


def _has_own_code_edits(ledger: dict, floor: float) -> bool:
    """True iff the ledger has a code edit at/after the subagent's start floor."""
    for edit in ledger.get("edits", []):
        if edit.get("timestamp", 0) >= floor:
            ext = os.path.splitext(edit.get("file", ""))[1].lower()
            if ext in CODE_EXTENSIONS:
                return True
    return False


def _task_mark_path(sid: str, agent_key: str) -> str:
    """Per-(session, subagent-task) idempotency marker. Once the task-completed
    gate has surfaced its verdict for a task it must not surface it again, or the
    injected context re-prompts the subagent forever."""
    base = os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude"))
    d = os.path.join(base, "state", "completion-gate-task-marks")
    os.makedirs(d, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in f"{sid}-{agent_key}")[:180]
    return os.path.join(d, safe + ".done")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", choices=["task-completed", "stop"], required=True)
    args = parser.parse_args()

    # Hook input arrives on stdin; session_id scopes the ledger. Without a
    # session id, skip rather than touch a shared key (2026-06-09 incident).
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, IOError):
        hook_input = {}
    sid = resolve_session_id(hook_input)
    if not sid:
        sys.exit(0)
    global LEDGER_PATH
    LEDGER_PATH = str(verify_ledger_path(sid))

    ledger = load_ledger()

    # Subagent task completion: scope to THIS subagent's OWN edits and fire at
    # most once. A subagent inherits the parent's session_id, so `ledger` is the
    # parent's — full of the parent's unverified edits. Without these two guards
    # the gate runs project verification against the parent's edits and injects
    # the result into every read-only subagent (auditor/reviewer/explorer),
    # re-prompting it into the "Same list / Holding / No change" stop loop. The
    # parent's own `--event stop` pass (below, unchanged) remains the real gate.
    if args.event == "task-completed":
        floor = _subagent_start_floor(hook_input)
        if floor is None:
            sys.exit(0)  # cannot attribute edits to this subagent -> never nag
        agent_key = str(
            hook_input.get("agent_id")
            or os.path.basename(str(hook_input.get("agent_transcript_path")
                                    or hook_input.get("transcript_path") or "task"))
        )
        mark = _task_mark_path(sid, agent_key)
        if os.path.exists(mark):
            sys.exit(0)  # already gated this task -> idempotent, cannot loop
        try:
            with open(mark, "w"):
                pass  # claim the mark up front so a slow/failed verify can't re-fire
        except OSError:
            pass
        if not _has_own_code_edits(ledger, floor):
            sys.exit(0)  # read-only subagent / no code edits of its own -> nothing to verify

    # No code edits at all — nothing to verify
    if not has_code_edits(ledger):
        sys.exit(0)

    # Already verified clean after last edit
    last_edit = ledger.get("last_edit_at", 0)
    last_verified = ledger.get("last_verified_at", 0)
    if last_verified > last_edit and ledger.get("verified_clean", False):
        sys.exit(0)

    # Need to run verification
    project_root = find_project_root()
    commands = resolve_commands(project_root)

    if not commands:
        # No verifiable project detected
        sys.exit(0)

    results = []
    all_passed = True
    for cmd_info in commands:
        result = run_command(cmd_info["cmd"], project_root)
        result["label"] = cmd_info["label"]
        results.append(result)
        if not result["passed"]:
            all_passed = False

    # Update ledger
    now = time.time()
    for result in results:
        ledger.setdefault("verifications", []).append({
            "timestamp": now,
            "command": result["command"],
            "result": "pass" if result["passed"] else "fail",
            "source": "hook",
        })
    ledger["last_verified_at"] = now
    ledger["verified_clean"] = all_passed
    save_ledger(ledger)

    # Build output
    if all_passed:
        context = "✅ Completion gate: all checks passed"
        for r in results:
            context += f"\n  {r['label']}: ✅ {r['command']}"
    else:
        context = "❌ Completion gate: verification failed\n"
        for r in results:
            status = "✅" if r["passed"] else "❌"
            context += f"\n  {r['label']}: {status} {r['command']}"
            if not r["passed"]:
                context += f"\n{r['output']}"
        context += "\n\nFix before claiming done."

    # Stop hooks don't support hookSpecificOutput — use top-level stopReason.
    # PostToolUse/PreToolUse hooks use hookSpecificOutput with additionalContext.
    if args.event == "stop":
        envelope = {"stopReason": context}
    else:
        envelope = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": context,
            }
        }
    print(json.dumps(envelope))


if __name__ == "__main__":
    main()
