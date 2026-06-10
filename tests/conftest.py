"""Shared fixtures for Claude Code hook system tests."""

import json
import os
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import pytest


@pytest.fixture
def session_id():
    """Unique per-test session ID for ledger isolation."""
    return f"test-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def ledger_path(session_id, tmp_path):
    """Returns the per-session ledger path for the current test; cleaned up after.

    Ledgers live under ~/.claude/state/verify-ledgers/<session_id>.json since
    the 2026-06-09 shared-key incident fix (see case_file.verify_ledger_path)."""
    base = Path(os.path.expanduser("~/.claude/state/verify-ledgers"))
    base.mkdir(parents=True, exist_ok=True)
    path = str(base / f"{session_id}.json")
    yield path
    # Cleanup
    for suffix in ["", "-async"]:
        p = base / f"{session_id}{suffix}.json"
        if p.exists():
            p.unlink()
    pid_path = base / f"{session_id}-debounce.pid"
    if pid_path.exists():
        pid_path.unlink()


@pytest.fixture
def make_ledger(ledger_path):
    """Factory: creates a ledger JSON with specified state."""
    def _make(
        edits=None,
        verifications=None,
        last_edit_at=0,
        last_verified_at=0,
        verified_clean=True,
    ):
        ledger = {
            "edits": edits or [],
            "verifications": verifications or [],
            "last_edit_at": last_edit_at,
            "last_verified_at": last_verified_at,
            "verified_clean": verified_clean,
        }
        with open(ledger_path, "w") as f:
            json.dump(ledger, f, indent=2)
        return ledger
    return _make


@pytest.fixture
def run_hook(session_id):
    """Runs a hook script as subprocess, returns {stdout, stderr, exit_code, parsed_json}."""
    def _run(script_path, stdin_json=None, env=None, args=None, timeout=10):
        run_env = os.environ.copy()
        # Pin BOTH session vars — the canonical CLI var (CLAUDE_CODE_SESSION_ID)
        # outranks the legacy one in case_file.resolve_session_id, and a real
        # value can leak in from the invoking Claude session's environment.
        run_env["CLAUDE_CODE_SESSION_ID"] = session_id
        run_env["CLAUDE_SESSION_ID"] = session_id
        if env:
            run_env.update(env)

        stdin_data = None
        if stdin_json is not None:
            if isinstance(stdin_json, dict):
                stdin_data = json.dumps(stdin_json)
            else:
                stdin_data = str(stdin_json)

        cmd = [sys.executable, str(script_path)]
        if args:
            cmd.extend(args)

        result = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            env=run_env,
            timeout=timeout,
        )

        parsed = None
        if result.stdout.strip():
            try:
                parsed = json.loads(result.stdout.strip())
            except json.JSONDecodeError:
                pass

        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "parsed_json": parsed,
        }
    return _run


@pytest.fixture
def mock_stdin():
    """Context manager replacing sys.stdin with StringIO."""
    @contextmanager
    def _mock(data):
        if isinstance(data, dict):
            data = json.dumps(data)
        old_stdin = sys.stdin
        sys.stdin = StringIO(data)
        try:
            yield sys.stdin
        finally:
            sys.stdin = old_stdin
    return _mock


@pytest.fixture
def fake_project(tmp_path):
    """Creates temp dir with project files based on markers dict."""
    def _make(markers=None):
        markers = markers or {}
        for filename, content in markers.items():
            filepath = tmp_path / filename
            filepath.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, dict):
                filepath.write_text(json.dumps(content))
            else:
                filepath.write_text(str(content))
        return str(tmp_path)
    return _make


@pytest.fixture
def env_prompt(monkeypatch):
    """Sets/clears CLAUDE_USER_PROMPT env var."""
    def _set(text=None):
        if text is not None:
            monkeypatch.setenv("CLAUDE_USER_PROMPT", text)
        else:
            monkeypatch.delenv("CLAUDE_USER_PROMPT", raising=False)
    return _set


# Paths to hook scripts
HOOK_BIN = Path.home() / ".claude" / "bin"
GOVERN_SCRIPTS = Path.home() / ".claude" / "skills" / "govern" / "scripts"

CLASSIFY_PROMPT = GOVERN_SCRIPTS / "classify_prompt.py"
PRE_TOOL_GUARD = HOOK_BIN / "pre_tool_guard.py"
EDIT_VERIFY_ASYNC = HOOK_BIN / "edit_verify_async.py"
TOOL_FAILURE_CONTEXT = HOOK_BIN / "tool_failure_context.py"
COMPLETION_GATE = HOOK_BIN / "completion_gate.py"
SUBAGENT_VERIFY = HOOK_BIN / "subagent_verify.py"
