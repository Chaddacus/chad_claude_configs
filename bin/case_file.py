"""Case-file library for L2 stop-gate.

A "case" is the durable record of what a session actually did. Lives at
`~/.claude/state/cases/${session_id}/` with three files:

  events.jsonl  — append-only log of every tool call
                  {ts, tool, file, command, exit, summary}
  summary.json  — materialized rollup of events
                  {files_touched, commands_run, verifications, last_edit_at, last_verify_at}
  completion.json — structured completion report filed by claim_complete.py

The case file is the source of truth for "what got done". The stop gate
reads it to validate completion claims against actual tool activity.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

# Verification tool patterns — used to classify Bash commands.
VERIFY_PATTERNS = [
    (re.compile(r"\b(vitest|jest|pytest|cargo\s+test|go\s+test|rspec|mocha)\b"), "test"),
    (re.compile(r"\b(tsc|mypy|pyright|flow\s+check)\b"), "typecheck"),
    (re.compile(r"\b(eslint|ruff|flake8|rubocop|golangci-lint)\b"), "lint"),
    (re.compile(r"\b(npm\s+run\s+build|cargo\s+build|go\s+build|make\b)\b"), "build"),
]

# State-mutation tool patterns.
STATE_PATTERNS = [
    (re.compile(r"\bgit\s+commit\b"), "git_commit"),
    (re.compile(r"\bgit\s+push\b"), "git_push"),
    (re.compile(r"\bgh\s+pr\s+merge\b"), "pr_merge"),
    (re.compile(r"\bgh\s+pr\s+create\b"), "pr_create"),
]

# Zero-tests indicators — a "passing" test run that didn't actually test anything.
ZERO_TEST_INDICATORS = [
    "no tests ran",
    "no tests collected",
    "0 tests",
    "0 passing",
    "0 passed",
    "no tests found",
    "ran 0 tests",
]


def resolve_session_id(hook_input: dict | None = None) -> str | None:
    """Resolve the session id, in priority order:
    1. hook stdin payload (authoritative — always present in hook input)
    2. CLAUDE_CODE_SESSION_ID env (set by the CLI in tool/shell environments)
    3. legacy CLAUDE_SESSION_ID env (governed-wrapper runs)
    Returns None when unresolvable. Callers that gate behavior on session
    identity must fail OPEN (skip) on None — never fall back to a shared key.
    The 2026-06-09 gate-gaming incident was caused by a shared 'default' key."""
    if hook_input and hook_input.get("session_id"):
        return str(hook_input["session_id"])
    return (
        os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or None
    )


def _session_id() -> str:
    return resolve_session_id() or "default"


_VERIFY_LEDGER_DIR = Path(os.path.expanduser("~/.claude/state/verify-ledgers"))


def verify_ledger_path(session_id: str, suffix: str = "") -> Path:
    """Per-session verification-ledger path. Caller must pass a real session
    id (use resolve_session_id and skip on None)."""
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
    _VERIFY_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{sid}{suffix}.json" if not suffix.endswith(".pid") else f"{sid}{suffix}"
    return _VERIFY_LEDGER_DIR / name


def cleanup_verify_ledgers(max_age_hours: float = 24.0) -> None:
    """Drop ledger files older than max_age_hours. Best-effort."""
    cutoff = time.time() - max_age_hours * 3600
    try:
        for p in _VERIFY_LEDGER_DIR.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                continue
    except OSError:
        pass


def case_dir(session_id: str | None = None) -> Path:
    sid = session_id or _session_id()
    # sanitize
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", sid) or "default"
    p = Path(os.path.expanduser("~/.claude/state/cases")) / sid
    p.mkdir(parents=True, exist_ok=True)
    return p


def events_path(session_id: str | None = None) -> Path:
    return case_dir(session_id) / "events.jsonl"


def summary_path(session_id: str | None = None) -> Path:
    return case_dir(session_id) / "summary.json"


def completion_path(session_id: str | None = None) -> Path:
    return case_dir(session_id) / "completion.json"


def classify_command(cmd: str) -> str | None:
    """Return 'test'|'typecheck'|'lint'|'build'|'git_commit'|... or None."""
    for pat, label in VERIFY_PATTERNS + STATE_PATTERNS:
        if pat.search(cmd):
            return label
    return None


def is_zero_test_output(text: str) -> bool:
    lower = text.lower()
    return any(ind in lower for ind in ZERO_TEST_INDICATORS)


def append_event(event: dict, session_id: str | None = None) -> None:
    """Append a tool event. Required fields: ts, tool. Optional: file,
    command, exit, summary, kind."""
    event.setdefault("ts", time.time())
    p = events_path(session_id)
    try:
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
    except IOError:
        pass


def read_events(session_id: str | None = None) -> list[dict]:
    p = events_path(session_id)
    if not p.exists():
        return []
    events = []
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except IOError:
        return []
    return events


def rebuild_summary(session_id: str | None = None) -> dict:
    """Walk events.jsonl and materialize a rollup."""
    events = read_events(session_id)
    files_touched: set[str] = set()
    commands_run: list[dict] = []
    verifications: list[dict] = []
    state_mutations: list[dict] = []
    last_edit_at = 0.0
    last_verify_at = 0.0
    last_passing_verify_at = 0.0

    for ev in events:
        tool = ev.get("tool")
        ts = ev.get("ts", 0)
        if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            fp = ev.get("file")
            if fp:
                files_touched.add(fp)
            last_edit_at = max(last_edit_at, ts)
        elif tool == "Bash":
            cmd = ev.get("command", "")
            exit_code = ev.get("exit", 0)
            kind = ev.get("kind") or classify_command(cmd)
            entry = {
                "cmd": cmd,
                "exit": exit_code,
                "kind": kind,
                "ts": ts,
                "summary": ev.get("summary", ""),
            }
            commands_run.append(entry)
            if kind in ("test", "typecheck", "lint", "build"):
                verifications.append(entry)
                last_verify_at = max(last_verify_at, ts)
                if exit_code == 0 and not is_zero_test_output(entry["summary"]):
                    last_passing_verify_at = max(last_passing_verify_at, ts)
            elif kind in ("git_commit", "git_push", "pr_merge", "pr_create"):
                state_mutations.append(entry)

    summary = {
        "files_touched": sorted(files_touched),
        "commands_run": commands_run,
        "verifications": verifications,
        "state_mutations": state_mutations,
        "last_edit_at": last_edit_at,
        "last_verify_at": last_verify_at,
        "last_passing_verify_at": last_passing_verify_at,
        "event_count": len(events),
    }
    p = summary_path(session_id)
    try:
        with p.open("w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
    except IOError:
        pass
    return summary


def read_summary(session_id: str | None = None) -> dict:
    p = summary_path(session_id)
    if not p.exists():
        return rebuild_summary(session_id)
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (IOError, json.JSONDecodeError):
        return rebuild_summary(session_id)


def write_completion(record: dict, session_id: str | None = None) -> Path:
    record.setdefault("ts", time.time())
    p = completion_path(session_id)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    return p


def read_completion(session_id: str | None = None) -> dict | None:
    p = completion_path(session_id)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (IOError, json.JSONDecodeError):
        return None


# Bash summary extraction — best-effort, used for zero-test detection.
def summarize_bash_output(output: str, max_chars: int = 400) -> str:
    if not output:
        return ""
    # Keep first + last lines, trim middle
    lines = output.splitlines()
    if len(lines) <= 10:
        text = output
    else:
        text = "\n".join(lines[:5] + ["..."] + lines[-5:])
    if len(text) > max_chars:
        text = text[:max_chars]
    return text
