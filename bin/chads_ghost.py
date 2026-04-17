#!/usr/bin/env python3
"""Chad's Ghost — Stop hook that injects a self-audit when work looks incomplete.

Instead of calling an external LLM, this fires a structured self-audit prompt
via stopReason, which gets fed back into the *current* Claude session. Claude
already has the full conversation context — it's the best evaluator of its own
completion state. We just force it to answer the right questions.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

SESSION_ID = os.environ.get("CLAUDE_SESSION_ID", "default")
STATE_PATH = Path(f"/tmp/chads-ghost-{SESSION_ID}.json")
VERIFY_LEDGER_PATH = Path(f"/tmp/claude-verify-{SESSION_ID}.json")
COOLDOWN_SECONDS = 180  # 3 minutes — prevent audit loops

HEDGING_PATTERNS = [
    "should work",
    "should be working",
    "should be fine",
    "should be good",
    "probably",
    "i believe",
    "i think this",
    "seems correct",
    "seems like it",
    "appears to",
    "ought to",
    "might work",
    "let me know if you",
    "let me know if you'd like",
    "if you want me to",
    "if you'd like me to",
]

CHAD_SELF_AUDIT = """STOP BLOCKED — complete this self-audit before stopping:

1. ORIGINAL TASK: State exactly what was asked. Is it fully complete end-to-end — not just implemented, but wired up and reachable?

2. VERIFICATION: List the exact commands you ran (tests, typecheck, lint) and their actual output. If you didn't run them, run them now before responding.

3. INTEGRATION: Is the change actually connected? If it's a function, is it called? If it's a feature, is it reachable? If it's a fix, is the bug path closed?

4. HEDGING CHECK: Your last message contained language suggesting uncertainty ("should work", "probably", "I believe", "seems correct", or similar). That means you're guessing. Stop guessing — verify and report what actually happened.

If all four are satisfied with real evidence: state the evidence concisely, then stop.
If any are unclear: identify the specific gap and close it. Do not stop until it's clean."""


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {"last_fired_at": 0, "fire_count": 0}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state))
    except Exception:
        pass


def git_dirty(cwd: str) -> bool:
    try:
        r = subprocess.run(
            ["git", "status", "--short"],
            cwd=cwd, capture_output=True, text=True, timeout=3
        )
        return bool(r.stdout.strip())
    except Exception:
        return False


def has_meaningful_work(cwd: str) -> bool:
    """True if there's evidence of real coding work this session."""
    if VERIFY_LEDGER_PATH.exists():
        try:
            if json.loads(VERIFY_LEDGER_PATH.read_text()):
                return True
        except Exception:
            pass
    if git_dirty(cwd):
        return True
    try:
        r = subprocess.run(
            ["git", "log", "--since=2 hours ago", "--oneline"],
            cwd=cwd, capture_output=True, text=True, timeout=3
        )
        if r.stdout.strip():
            return True
    except Exception:
        pass
    return False


def hedging_detected(message: str) -> bool:
    lower = message.lower()
    return any(p in lower for p in HEDGING_PATTERNS)


def ledger_empty_after_edits(cwd: str) -> bool:
    """True if edits happened but no verification was run."""
    # If ledger exists and has data, verification happened — no trigger
    if VERIFY_LEDGER_PATH.exists():
        try:
            data = json.loads(VERIFY_LEDGER_PATH.read_text())
            if data:
                return False
        except Exception:
            pass
    # Ledger is empty/missing — check if git has recent changes suggesting edits happened
    try:
        r = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=cwd, capture_output=True, text=True, timeout=3
        )
        if r.stdout.strip():
            return True  # edits exist, no verification recorded
    except Exception:
        pass
    return False


def build_stop_reason(triggers: list[str]) -> str:
    audit = CHAD_SELF_AUDIT
    if "hedging" not in triggers:
        # Remove the hedging-specific line if it wasn't the trigger
        lines = audit.splitlines()
        lines = [l for l in lines if "HEDGING CHECK" not in l and "uncertain" not in l]
        audit = "\n".join(lines)
    return audit


def main() -> None:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except Exception:
        hook_input = {}

    # Recursion guard
    if hook_input.get("stop_hook_active"):
        sys.exit(0)

    # Cooldown guard
    state = load_state()
    now = time.time()
    if now - float(state.get("last_fired_at", 0)) < COOLDOWN_SECONDS:
        sys.exit(0)

    cwd = os.getcwd()

    # Skip pure chat/research sessions
    if not has_meaningful_work(cwd):
        sys.exit(0)

    last_message = hook_input.get("last_assistant_message", "")

    # Evaluate triggers
    triggers = []
    if git_dirty(cwd):
        triggers.append("git_dirty")
    if hedging_detected(last_message):
        triggers.append("hedging")
    if ledger_empty_after_edits(cwd):
        triggers.append("no_verification")

    if not triggers:
        sys.exit(0)

    # Update state before firing
    state["last_fired_at"] = now
    state["fire_count"] = int(state.get("fire_count", 0)) + 1
    state["last_triggers"] = triggers
    save_state(state)

    print(json.dumps({"stopReason": build_stop_reason(triggers)}))


if __name__ == "__main__":
    main()
