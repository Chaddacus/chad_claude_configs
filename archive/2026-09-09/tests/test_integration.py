"""Integration tests — real scripts, real projects, real async behavior."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

from conftest import (
    CLASSIFY_PROMPT,
    COMPLETION_GATE,
    EDIT_VERIFY_ASYNC,
    GOVERN_SCRIPTS,
    HOOK_BIN,
    PRE_TOOL_GUARD,
)

# ---------------------------------------------------------------------------
# Direct imports for function-level integration tests
# ---------------------------------------------------------------------------
sys.path.insert(0, str(GOVERN_SCRIPTS))
import classify_prompt as cp_module  # noqa: E402

sys.path.insert(0, str(HOOK_BIN))
import completion_gate  # noqa: E402


# ===========================================================================
# classify_prompt — real-world prompt classification
# ===========================================================================


@pytest.mark.integration
class TestClassifyPromptRealWorld:
    """Run classify_prompt against realistic prompts to verify route selection."""

    def test_real_refactor(self):
        result = cp_module.classify_prompt(
            "refactor src/hooks/a.ts and src/hooks/b.ts"
        )
        assert result["route_hint"] == "R3", (
            f"Two-file refactor should route to R3, got {result['route_hint']} "
            f"(reason: {result['reason']})"
        )

    def test_real_simple(self):
        result = cp_module.classify_prompt("how does the build system work?")
        assert result["route_hint"] == "R1", (
            f"Simple question should route to R1, got {result['route_hint']} "
            f"(reason: {result['reason']})"
        )

    def test_real_auth(self):
        result = cp_module.classify_prompt(
            "add JWT token validation middleware"
        )
        assert result["route_hint"] == "R4", (
            f"Auth keyword prompt should route to R4, got {result['route_hint']} "
            f"(reason: {result['reason']})"
        )

    def test_real_broad_feature_no_file_mentions(self):
        result = cp_module.classify_prompt(
            "Implement a customer onboarding workflow with dashboard feedback, persistence, and tests."
        )
        assert result["route_hint"] == "R3", (
            f"Broad feature prompt should route to R3, got {result['route_hint']} "
            f"(reason: {result['reason']})"
        )


# ===========================================================================
# completion_gate — resolve_commands against real project directories
# ===========================================================================

CLAUDE_MEM_DIR = os.path.expanduser("~/code/claude-mem")
AIFL_DIR = os.path.expanduser("~/code/aifl")


@pytest.mark.integration
@pytest.mark.skipif(
    not os.path.exists(os.path.join(CLAUDE_MEM_DIR, "package.json")),
    reason="~/code/claude-mem/package.json not found",
)
def test_claude_mem_commands():
    """resolve_commands finds test commands in the claude-mem Node project."""
    commands = completion_gate.resolve_commands(CLAUDE_MEM_DIR)
    assert isinstance(commands, list)
    assert len(commands) > 0, (
        f"Expected non-empty command list for {CLAUDE_MEM_DIR}, got {commands}"
    )


@pytest.mark.integration
@pytest.mark.skipif(
    not os.path.exists(os.path.join(AIFL_DIR, "pyproject.toml")),
    reason="~/code/aifl/pyproject.toml not found",
)
def test_aifl_commands():
    """resolve_commands finds pytest commands in the aifl Python project."""
    commands = completion_gate.resolve_commands(AIFL_DIR)
    assert isinstance(commands, list)
    assert len(commands) > 0, (
        f"Expected non-empty command list for {AIFL_DIR}, got {commands}"
    )


# ===========================================================================
# pre_tool_guard — subprocess blocked/allowed tests
# ===========================================================================


def bash_input(command: str) -> dict:
    """Build a Bash tool hook input payload."""
    return {"tool_name": "Bash", "tool_input": {"command": command}}


@pytest.mark.integration
def test_blocked_subprocess(run_hook):
    """rm -rf / is blocked with exit code 2."""
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("rm -rf /"))
    assert result["exit_code"] == 2, (
        f"Expected exit 2 for blocked command, got {result['exit_code']}"
    )


@pytest.mark.integration
def test_allowed_subprocess(run_hook):
    """ls is allowed with exit code 0."""
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("ls"))
    assert result["exit_code"] == 0, (
        f"Expected exit 0 for allowed command, got {result['exit_code']}"
    )


# ===========================================================================
# edit_verify_async — async timing tests (slow)
# ===========================================================================


def _verify_ledger_base() -> str:
    """Ledger dir (see case_file.verify_ledger_path — moved out of /tmp
    after the 2026-06-09 shared-key incident)."""
    base = os.path.expanduser("~/.claude/state/verify-ledgers")
    os.makedirs(base, exist_ok=True)
    return base


def _async_results_path(session_id: str) -> str:
    """Compute the async results path for a given session."""
    return os.path.join(_verify_ledger_base(), f"{session_id}-async.json")


def _debounce_pid_path(session_id: str) -> str:
    """Compute the debounce PID path for a given session."""
    return os.path.join(_verify_ledger_base(), f"{session_id}-debounce.pid")


@pytest.mark.integration
def test_async_py_syntax_check(run_hook, session_id):
    """Edit a real .py file, wait for async syntax check to produce results."""
    async_path = _async_results_path(session_id)
    pid_path = _debounce_pid_path(session_id)
    tmp_py = None
    try:
        # Create a real .py file in /tmp
        with tempfile.NamedTemporaryFile(
            suffix=".py", dir="/tmp", delete=False, mode="w"
        ) as f:
            f.write('print("hello")\n')
            tmp_py = f.name

        # Send an Edit tool event pointing to the real file
        hook_input = {
            "tool_name": "Edit",
            "tool_input": {"file_path": tmp_py},
        }
        result = run_hook(EDIT_VERIFY_ASYNC, stdin_json=hook_input, timeout=15)
        assert result["exit_code"] == 0

        # Wait for debounce (3s) + background execution
        time.sleep(5)

        # The async results file should have been created
        assert os.path.exists(async_path), (
            f"Expected async results at {async_path} after 5s wait"
        )

        with open(async_path, "r") as f:
            data = json.load(f)
        assert "results" in data
        assert len(data["results"]) > 0
        # Our valid .py file should pass syntax check
        assert data["results"][0]["passed"] is True
    finally:
        if tmp_py and os.path.exists(tmp_py):
            os.unlink(tmp_py)
        if os.path.exists(async_path):
            os.unlink(async_path)
        if os.path.exists(pid_path):
            os.unlink(pid_path)


@pytest.mark.integration
def test_debounce_replaces(run_hook, session_id):
    """Second edit within debounce window replaces the first; results file has results."""
    async_path = _async_results_path(session_id)
    pid_path = _debounce_pid_path(session_id)
    tmp_py1 = None
    tmp_py2 = None
    try:
        # Create two real .py files
        with tempfile.NamedTemporaryFile(
            suffix=".py", prefix="debounce1_", dir="/tmp", delete=False, mode="w"
        ) as f:
            f.write('x = 1\n')
            tmp_py1 = f.name

        with tempfile.NamedTemporaryFile(
            suffix=".py", prefix="debounce2_", dir="/tmp", delete=False, mode="w"
        ) as f:
            f.write('y = 2\n')
            tmp_py2 = f.name

        # First edit
        result1 = run_hook(
            EDIT_VERIFY_ASYNC,
            stdin_json={"tool_name": "Edit", "tool_input": {"file_path": tmp_py1}},
            timeout=15,
        )
        assert result1["exit_code"] == 0

        # Short pause, then second edit (within 3s debounce)
        time.sleep(1)

        result2 = run_hook(
            EDIT_VERIFY_ASYNC,
            stdin_json={"tool_name": "Edit", "tool_input": {"file_path": tmp_py2}},
            timeout=15,
        )
        assert result2["exit_code"] == 0

        # Wait for debounce (3s from second edit) + execution
        time.sleep(5)

        # Results file should exist with results from the latest batch
        assert os.path.exists(async_path), (
            f"Expected async results at {async_path} after debounce replacement"
        )

        with open(async_path, "r") as f:
            data = json.load(f)
        assert "results" in data
        assert len(data["results"]) > 0
    finally:
        for p in [tmp_py1, tmp_py2]:
            if p and os.path.exists(p):
                os.unlink(p)
        if os.path.exists(async_path):
            os.unlink(async_path)
        if os.path.exists(pid_path):
            os.unlink(pid_path)
