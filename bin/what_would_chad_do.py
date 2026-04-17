#!/usr/bin/env python3
"""Completion reflection hook.

For TaskCompleted it injects continuation guidance.
For Stop it can fail closed with a stopReason so the model must keep going
when there is still an obvious bounded next step.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"))
from hook_profile import should_run

if not should_run("what_would_chad_do"):
    sys.exit(0)

STATE_PATH = Path(f"/tmp/claude-wwcd-{os.environ.get('CLAUDE_SESSION_ID', 'default')}.json")
COOLDOWN_SECONDS = 90
MAX_FILES = 8


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"last_injected_at": 0, "count": 0}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"last_injected_at": 0, "count": 0}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state))
    except Exception:
        pass


def run_git(args: list[str], cwd: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return (result.stdout + result.stderr).strip()
    except Exception:
        return ""


def repo_context(cwd: str) -> list[str]:
    branch = run_git(["branch", "--show-current"], cwd)
    status = run_git(["status", "--short"], cwd)
    if not branch and not status:
        return []

    lines = []
    if branch:
        lines.append(f"Current branch: {branch}")
    if status:
        changed = [line for line in status.splitlines() if line.strip()]
        lines.append(f"Working tree still has {len(changed)} changed path(s).")
        for entry in changed[:MAX_FILES]:
            lines.append(f"  - {entry}")
        if len(changed) > MAX_FILES:
            lines.append(f"  - ... and {len(changed) - MAX_FILES} more")
    else:
        lines.append("Working tree is currently clean.")
    return lines


def repo_has_pending_work(cwd: str) -> bool:
    status = run_git(["status", "--short"], cwd)
    return bool(status.strip())


def run_anticipation_engine() -> dict | None:
    """Call anticipation_engine.py as a subprocess. Zero LLM calls. Returns None on failure."""
    try:
        engine_path = Path(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude"))) / "bin" / "anticipation_engine.py"
        if not engine_path.exists():
            return None
        result = subprocess.run(
            [sys.executable, str(engine_path)],
            capture_output=True,
            text=True,
            timeout=5,
            env=os.environ.copy(),
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except Exception:
        pass
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", choices=["task-completed", "stop"], required=True)
    args = parser.parse_args()

    try:
        _hook_input = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        _hook_input = {}

    # Autonomous drive mode: bypass cooldown, use anticipation engine for specific next-step injection
    drive_state_path = Path(f"/tmp/claude-drive-{os.environ.get('CLAUDE_SESSION_ID', 'default')}.json")
    if drive_state_path.exists():
        engine_result = run_anticipation_engine()
        if engine_result:
            if args.event == "task-completed":
                context = (
                    f"AUTONOMOUS MODE: Next step = {engine_result['next_step']} "
                    f"(weight: {engine_result['weight']:.2f}). "
                    f"{engine_result['action_hint']}. "
                    "You have plenty of context remaining — dispatch immediately without stopping to report."
                )
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "TaskCompleted",
                        "additionalContext": context,
                    }
                }))
            elif args.event == "stop" and engine_result.get("should_continue"):
                print(json.dumps({
                    "stopReason": (
                        f"AUTONOMOUS MODE ACTIVE: Do not stop. "
                        f"Next step = {engine_result['next_step']} "
                        f"(weight: {engine_result['weight']:.2f}). "
                        f"{engine_result['action_hint']}"
                    )
                }))
        sys.exit(0)

    state = load_state()
    now = time.time()
    if now - float(state.get("last_injected_at", 0)) < COOLDOWN_SECONDS:
        sys.exit(0)

    cwd = os.getcwd()
    lines = [
        "WWCD reflection gate:",
        "Before closing, ask: what would Chad do next if the real goal mattered more than declaring completion?",
        "Keep going if there is one more bounded, local, high-leverage step that materially strengthens the outcome.",
        "Stop only if the goal is actually satisfied end to end, verification is complete, and the remaining ideas would start a new track of work.",
        "Bias toward one more concrete move, not one more progress report."
    ]
    lines.extend(repo_context(cwd))

    if "forgecode" in cwd.lower():
        lines.append("ForgeCode-specific bias: prefer the next step that moves the system closer to a stronger autonomous coding loop.")

    context = "\n".join(lines)
    if args.event == "stop":
        if repo_has_pending_work(cwd):
            envelope = {
                "stopReason": context
            }
        else:
            sys.exit(0)
    else:
        envelope = {
            "hookSpecificOutput": {
                "hookEventName": "TaskCompleted",
                "additionalContext": context,
            }
        }
    print(json.dumps(envelope))

    state["last_injected_at"] = now
    state["count"] = int(state.get("count", 0)) + 1
    save_state(state)


if __name__ == "__main__":
    main()
