#!/usr/bin/env python3
"""PostToolUse hook — async debounced edit verification (Layer 1).

On Edit/Write: records the edit in the ledger, spawns async background
syntax check after 3s debounce.

On Bash matching test/build patterns: recognizes self-verification,
updates the ledger.

On other tools: checks for available async results and injects if failures found.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"))
from hook_profile import should_run
if not should_run("edit_verify_async"):
    sys.exit(0)
import sys
import time

from case_file import resolve_session_id, verify_ledger_path, cleanup_verify_ledgers

# Set in main() from the hook's stdin session_id. None → fail open (skip).
LEDGER_PATH = None
ASYNC_RESULTS_PATH = None
DEBOUNCE_PID_PATH = None

CODE_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".py", ".pyw",
    ".rs",
    ".go",
}

VERIFY_COMMAND_PATTERNS = [
    r"npm\s+test", r"npx\s+(?:jest|vitest|tsc)", r"npm\s+run\s+(?:test|typecheck|check|lint)",
    r"pytest", r"python\s+-m\s+(?:pytest|unittest)",
    r"ruff\s+check", r"mypy\b",
    r"cargo\s+(?:test|check|clippy)",
    r"go\s+(?:test|vet)",
    r"make\s+(?:test|check|lint)",
]

# Layer 1 syntax checks — genuinely cheap, <1s per file
SYNTAX_CHECKS = {
    ".ts": "node -e \"try {{ require('fs').readFileSync('{file}','utf8'); require('typescript').createSourceFile('{file}', require('fs').readFileSync('{file}','utf8'), require('typescript').ScriptTarget.Latest, true) }} catch(e) {{ console.error(e.message); process.exit(1) }}\"",
    ".tsx": "node -e \"try {{ require('fs').readFileSync('{file}','utf8'); require('typescript').createSourceFile('{file}', require('fs').readFileSync('{file}','utf8'), require('typescript').ScriptTarget.Latest, true) }} catch(e) {{ console.error(e.message); process.exit(1) }}\"",
    ".js": "node --check '{file}'",
    ".jsx": "node --check '{file}'",
    ".mjs": "node --check '{file}'",
    ".cjs": "node --check '{file}'",
    ".py": "python3 -m py_compile '{file}'",
    ".pyw": "python3 -m py_compile '{file}'",
    ".rs": "rustfmt --check '{file}' 2>/dev/null || true",
    ".go": "gofmt -l '{file}'",
}


def load_ledger() -> dict:
    """Load the verification-evidence ledger."""
    if not os.path.exists(LEDGER_PATH):
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
    try:
        with open(LEDGER_PATH, "w") as f:
            json.dump(ledger, f, indent=2)
    except IOError:
        pass


def is_verify_command(command: str) -> bool:
    """Check if a command matches known verification patterns."""
    for pattern in VERIFY_COMMAND_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return True
    return False


def record_edit(ledger: dict, file_path: str, tool: str, agent_id: str | None = None) -> None:
    """Record an edit in the ledger, stamped with the agent that authored it.

    `agent_id` comes from the PostToolUse payload and is present only when the
    hook fires inside a subagent call; absent (None) therefore means the main
    thread. Recording it is what lets SubagentStop attribute an edit instead of
    guessing from timestamps: the ledger is keyed by session_id, and subagents
    inherit the parent's session_id, so parent and subagent edits otherwise land
    in one undifferentiated pool. A start-time floor cannot separate them
    because the parent keeps working *while* a subagent runs.
    """
    now = time.time()
    entry: dict = {
        "file": file_path,
        "timestamp": now,
        "tool": tool,
    }
    # Omit the key entirely for main-thread edits so existing readers that do
    # not know about attribution are unaffected by its presence.
    if agent_id:
        entry["agent_id"] = str(agent_id)
    ledger.setdefault("edits", []).append(entry)
    ledger["last_edit_at"] = now
    ledger["verified_clean"] = False


def record_verification(ledger: dict, command: str, passed: bool) -> None:
    """Record a verification run in the ledger."""
    now = time.time()
    ledger.setdefault("verifications", []).append({
        "timestamp": now,
        "command": command,
        "result": "pass" if passed else "fail",
        "source": "claude-ran",
    })
    ledger["last_verified_at"] = now
    if passed:
        ledger["verified_clean"] = True


def spawn_async_check(edited_files: list[str]) -> None:
    """Spawn a background process to run syntax checks after debounce."""
    # Kill any existing debounce process
    if os.path.exists(DEBOUNCE_PID_PATH):
        try:
            with open(DEBOUNCE_PID_PATH, "r") as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 9)
        except (ValueError, ProcessLookupError, OSError):
            pass

    # Build the async check script
    checks = []
    for file_path in edited_files:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in SYNTAX_CHECKS and os.path.exists(file_path):
            cmd = SYNTAX_CHECKS[ext].replace("{file}", file_path)
            checks.append({"file": file_path, "cmd": cmd})

    if not checks:
        return

    # Write check script
    results_script = f"""
import json, subprocess, time, os
time.sleep(3)  # debounce
results = []
for check in {json.dumps(checks)}:
    try:
        r = subprocess.run(check["cmd"], shell=True, capture_output=True, text=True, timeout=5)
        results.append({{"file": check["file"], "passed": r.returncode == 0, "output": (r.stderr or r.stdout or "").strip()[:200]}})
    except Exception as e:
        results.append({{"file": check["file"], "passed": False, "output": str(e)[:200]}})
with open("{ASYNC_RESULTS_PATH}", "w") as f:
    json.dump({{"timestamp": time.time(), "results": results}}, f)
# Clean up pid file
try:
    os.unlink("{DEBOUNCE_PID_PATH}")
except OSError:
    pass
"""

    # Spawn background process
    proc = subprocess.Popen(
        [sys.executable, "-c", results_script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    try:
        with open(DEBOUNCE_PID_PATH, "w") as f:
            f.write(str(proc.pid))
    except IOError:
        pass


def check_async_results() -> str | None:
    """Check for available async verification results."""
    if not os.path.exists(ASYNC_RESULTS_PATH):
        return None
    try:
        with open(ASYNC_RESULTS_PATH, "r") as f:
            data = json.load(f)
        # Consume results
        os.unlink(ASYNC_RESULTS_PATH)

        failures = [r for r in data.get("results", []) if not r["passed"]]
        if not failures:
            return None

        lines = ["⚡ Layer 1 syntax check detected issues:"]
        for f_result in failures:
            lines.append(f"  {f_result['file']}: {f_result['output']}")
        lines.append("(These may resolve as you complete your edit batch.)")
        return "\n".join(lines)
    except (json.JSONDecodeError, IOError):
        return None


def main():
    # Read hook input from stdin
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, IOError):
        sys.exit(0)

    sid = resolve_session_id(hook_input)
    if not sid:
        # No session identity — a shared ledger key caused the 2026-06-09
        # cross-session contamination incident. Fail open.
        sys.exit(0)
    global LEDGER_PATH, ASYNC_RESULTS_PATH, DEBOUNCE_PID_PATH
    LEDGER_PATH = str(verify_ledger_path(sid))
    ASYNC_RESULTS_PATH = str(verify_ledger_path(sid, "-async"))
    DEBOUNCE_PID_PATH = str(verify_ledger_path(sid, "-debounce.pid"))
    cleanup_verify_ledgers()

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    tool_response = hook_input.get("tool_response", "")

    ledger = load_ledger()

    if tool_name in ("Edit", "Write"):
        # Record the edit
        file_path = tool_input.get("file_path", "")
        ext = os.path.splitext(file_path)[1].lower()
        if ext in CODE_EXTENSIONS:
            # agent_id is present only when this PostToolUse fired inside a
            # subagent; None here means the main thread authored the edit.
            record_edit(ledger, file_path, tool_name, hook_input.get("agent_id"))
            save_ledger(ledger)

            # Collect recent unverified edits for async check
            recent_files = list({
                e["file"] for e in ledger.get("edits", [])
                if e["timestamp"] > ledger.get("last_verified_at", 0)
            })
            spawn_async_check(recent_files)
        sys.exit(0)

    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        if is_verify_command(command):
            # Recognize self-verification
            exit_code = 0
            if isinstance(tool_response, dict):
                exit_code = tool_response.get("exit_code", 0)
            elif isinstance(tool_response, str) and "exit code" in tool_response.lower():
                exit_code = 1  # Rough heuristic
            record_verification(ledger, command, exit_code == 0)
            save_ledger(ledger)
        sys.exit(0)

    else:
        # Check for async results
        async_context = check_async_results()
        if async_context:
            envelope = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": async_context,
                }
            }
            print(json.dumps(envelope))
        sys.exit(0)


if __name__ == "__main__":
    main()
