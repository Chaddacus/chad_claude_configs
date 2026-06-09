"""Tests for edit_verify_async hook — PostToolUse edit tracking and async verification."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from conftest import EDIT_VERIFY_ASYNC

# ---------------------------------------------------------------------------
# Direct import setup — add bin dir to sys.path so we can import the module
# ---------------------------------------------------------------------------

BIN_DIR = str(Path.home() / ".claude" / "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)

import edit_verify_async  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def edit_input(file_path: str, tool: str = "Edit") -> dict:
    """Build an Edit/Write tool hook input payload."""
    return {"tool_name": tool, "tool_input": {"file_path": file_path}}


def bash_input(command: str, tool_response=None) -> dict:
    """Build a Bash tool hook input payload."""
    payload: dict = {"tool_name": "Bash", "tool_input": {"command": command}}
    if tool_response is not None:
        payload["tool_response"] = tool_response
    return payload


def read_ledger(ledger_path: str):
    """Read and parse the ledger file, or None if missing."""
    if not os.path.exists(ledger_path):
        return None
    with open(ledger_path, "r") as f:
        return json.load(f)


# ===========================================================================
# Ledger operations (direct import + monkeypatched LEDGER_PATH)
# ===========================================================================


@pytest.mark.unit
def test_load_default_when_missing(ledger_path, monkeypatch):
    """load_ledger returns dict with all 5 keys when file doesn't exist."""
    monkeypatch.setattr(edit_verify_async, "LEDGER_PATH", ledger_path)
    result = edit_verify_async.load_ledger()
    assert isinstance(result, dict)
    expected_keys = {"edits", "verifications", "last_edit_at", "last_verified_at", "verified_clean"}
    assert set(result.keys()) == expected_keys


@pytest.mark.unit
def test_load_reads_existing(ledger_path, monkeypatch, make_ledger):
    """load_ledger parses valid JSON correctly."""
    monkeypatch.setattr(edit_verify_async, "LEDGER_PATH", ledger_path)
    make_ledger(
        edits=[{"file": "a.py", "timestamp": 1, "tool": "Edit"}],
        last_edit_at=1,
        verified_clean=False,
    )
    result = edit_verify_async.load_ledger()
    assert len(result["edits"]) == 1
    assert result["edits"][0]["file"] == "a.py"
    assert result["last_edit_at"] == 1
    assert result["verified_clean"] is False


@pytest.mark.unit
def test_load_handles_corrupted(ledger_path, monkeypatch):
    """load_ledger returns default on bad JSON."""
    monkeypatch.setattr(edit_verify_async, "LEDGER_PATH", ledger_path)
    with open(ledger_path, "w") as f:
        f.write("{{{not valid json!!!")
    result = edit_verify_async.load_ledger()
    assert isinstance(result, dict)
    assert result["edits"] == []
    assert result["verified_clean"] is True


@pytest.mark.unit
def test_save_writes_valid_json(ledger_path, monkeypatch):
    """save_ledger writes parseable JSON to file."""
    monkeypatch.setattr(edit_verify_async, "LEDGER_PATH", ledger_path)
    ledger = {
        "edits": [{"file": "x.py", "timestamp": 99, "tool": "Write"}],
        "verifications": [],
        "last_edit_at": 99,
        "last_verified_at": 0,
        "verified_clean": False,
    }
    edit_verify_async.save_ledger(ledger)
    with open(ledger_path, "r") as f:
        loaded = json.load(f)
    assert loaded == ledger


@pytest.mark.unit
def test_record_edit_appends(ledger_path, monkeypatch):
    """record_edit appends to the edits list."""
    monkeypatch.setattr(edit_verify_async, "LEDGER_PATH", ledger_path)
    ledger = edit_verify_async.load_ledger()
    assert len(ledger["edits"]) == 0
    edit_verify_async.record_edit(ledger, "foo.py", "Edit")
    assert len(ledger["edits"]) == 1
    edit_verify_async.record_edit(ledger, "bar.ts", "Write")
    assert len(ledger["edits"]) == 2
    assert ledger["edits"][0]["file"] == "foo.py"
    assert ledger["edits"][1]["file"] == "bar.ts"


@pytest.mark.unit
def test_record_edit_sets_dirty(ledger_path, monkeypatch):
    """record_edit sets verified_clean=False."""
    monkeypatch.setattr(edit_verify_async, "LEDGER_PATH", ledger_path)
    ledger = edit_verify_async.load_ledger()
    assert ledger["verified_clean"] is True
    edit_verify_async.record_edit(ledger, "foo.py", "Edit")
    assert ledger["verified_clean"] is False


@pytest.mark.unit
def test_record_verify_pass_sets_clean(ledger_path, monkeypatch):
    """record_verification with passed=True sets verified_clean=True."""
    monkeypatch.setattr(edit_verify_async, "LEDGER_PATH", ledger_path)
    ledger = edit_verify_async.load_ledger()
    ledger["verified_clean"] = False
    edit_verify_async.record_verification(ledger, "npm test", passed=True)
    assert ledger["verified_clean"] is True
    assert len(ledger["verifications"]) == 1
    assert ledger["verifications"][0]["result"] == "pass"


@pytest.mark.unit
def test_record_verify_fail_stays_dirty(ledger_path, monkeypatch):
    """record_verification with passed=False keeps verified_clean=False."""
    monkeypatch.setattr(edit_verify_async, "LEDGER_PATH", ledger_path)
    ledger = edit_verify_async.load_ledger()
    # Record an edit first to set dirty
    edit_verify_async.record_edit(ledger, "foo.py", "Edit")
    assert ledger["verified_clean"] is False
    # Verification fails
    edit_verify_async.record_verification(ledger, "pytest", passed=False)
    assert ledger["verified_clean"] is False
    assert len(ledger["verifications"]) == 1
    assert ledger["verifications"][0]["result"] == "fail"


# ===========================================================================
# Verify command pattern matching (is_verify_command)
# ===========================================================================


@pytest.mark.unit
def test_npm_test():
    assert edit_verify_async.is_verify_command("npm test") is True


@pytest.mark.unit
def test_npx_jest():
    assert edit_verify_async.is_verify_command("npx jest") is True


@pytest.mark.unit
def test_npx_vitest():
    assert edit_verify_async.is_verify_command("npx vitest") is True


@pytest.mark.unit
def test_pytest():
    assert edit_verify_async.is_verify_command("pytest") is True


@pytest.mark.unit
def test_cargo_test():
    assert edit_verify_async.is_verify_command("cargo test") is True


@pytest.mark.unit
def test_go_test():
    assert edit_verify_async.is_verify_command("go test ./...") is True


@pytest.mark.unit
def test_make_test():
    assert edit_verify_async.is_verify_command("make test") is True


@pytest.mark.unit
def test_ruff_check():
    assert edit_verify_async.is_verify_command("ruff check") is True


@pytest.mark.unit
def test_ls_no_match():
    assert edit_verify_async.is_verify_command("ls -la") is False


@pytest.mark.unit
def test_npm_install_no_match():
    assert edit_verify_async.is_verify_command("npm install") is False


@pytest.mark.unit
def test_npm_run_build_no_match():
    assert edit_verify_async.is_verify_command("npm run build") is False


# ===========================================================================
# Main entry code paths (subprocess using run_hook)
# ===========================================================================


@pytest.mark.unit
def test_edit_py_records_ledger(run_hook, ledger_path, session_id):
    """Edit tool on .py file records an edit in the ledger."""
    result = run_hook(EDIT_VERIFY_ASYNC, stdin_json=edit_input("/tmp/x.py", "Edit"))
    assert result["exit_code"] == 0
    ledger = read_ledger(ledger_path)
    assert ledger is not None
    assert len(ledger["edits"]) == 1
    assert ledger["edits"][0]["file"] == "/tmp/x.py"
    assert ledger["edits"][0]["tool"] == "Edit"


@pytest.mark.unit
def test_edit_md_no_record(run_hook, ledger_path, session_id):
    """Edit tool on .md file does not record — .md not in CODE_EXTENSIONS."""
    result = run_hook(EDIT_VERIFY_ASYNC, stdin_json=edit_input("README.md", "Edit"))
    assert result["exit_code"] == 0
    ledger = read_ledger(ledger_path)
    assert ledger is None  # Ledger file should not be created


@pytest.mark.unit
def test_write_ts_records(run_hook, ledger_path, session_id):
    """Write tool on .ts file records an edit in the ledger."""
    result = run_hook(EDIT_VERIFY_ASYNC, stdin_json=edit_input("/tmp/x.ts", "Write"))
    assert result["exit_code"] == 0
    ledger = read_ledger(ledger_path)
    assert ledger is not None
    assert len(ledger["edits"]) == 1
    assert ledger["edits"][0]["file"] == "/tmp/x.ts"
    assert ledger["edits"][0]["tool"] == "Write"


@pytest.mark.unit
def test_bash_verify_records_pass(run_hook, ledger_path, session_id):
    """Bash npm test with exit_code 0 records passing verification."""
    result = run_hook(
        EDIT_VERIFY_ASYNC,
        stdin_json=bash_input("npm test", tool_response={"exit_code": 0}),
    )
    assert result["exit_code"] == 0
    ledger = read_ledger(ledger_path)
    assert ledger is not None
    assert ledger["verified_clean"] is True
    assert len(ledger["verifications"]) == 1
    assert ledger["verifications"][0]["result"] == "pass"


@pytest.mark.unit
def test_bash_verify_records_fail(run_hook, ledger_path, session_id, make_ledger):
    """Bash pytest with exit_code 1 records failing verification, stays dirty."""
    # Seed ledger with a dirty state (an edit was made, not yet verified)
    make_ledger(
        edits=[{"file": "x.py", "timestamp": 1, "tool": "Edit"}],
        last_edit_at=1,
        verified_clean=False,
    )
    result = run_hook(
        EDIT_VERIFY_ASYNC,
        stdin_json=bash_input("pytest", tool_response={"exit_code": 1}),
    )
    assert result["exit_code"] == 0
    ledger = read_ledger(ledger_path)
    assert ledger is not None
    assert ledger["verified_clean"] is False


@pytest.mark.unit
def test_bash_nonverify_ignored(run_hook, ledger_path, session_id):
    """Bash ls does not record verification; ledger not created."""
    result = run_hook(EDIT_VERIFY_ASYNC, stdin_json=bash_input("ls"))
    assert result["exit_code"] == 0
    ledger = read_ledger(ledger_path)
    assert ledger is None  # Ledger file should not be created


@pytest.mark.unit
def test_malformed_stdin_exits_0(run_hook, session_id):
    """Malformed stdin exits 0 without crashing."""
    result = run_hook(EDIT_VERIFY_ASYNC, stdin_json="broken")
    assert result["exit_code"] == 0


# ===========================================================================
# Async results injection (subprocess)
# ===========================================================================


def _async_results_path(session_id: str) -> str:
    """Compute the async results path for a given session (see
    case_file.verify_ledger_path — moved out of /tmp after the 2026-06-09
    shared-key incident)."""
    base = os.path.expanduser("~/.claude/state/verify-ledgers")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{session_id}-async.json")


@pytest.mark.unit
def test_failures_inject_context(run_hook, session_id):
    """Async results with failures inject hookSpecificOutput."""
    async_path = _async_results_path(session_id)
    try:
        results = {
            "timestamp": 1,
            "results": [
                {"file": "bad.py", "passed": False, "output": "SyntaxError: invalid syntax"},
                {"file": "ok.py", "passed": True, "output": ""},
            ],
        }
        with open(async_path, "w") as f:
            json.dump(results, f)

        # Trigger with a non-Edit/non-Bash tool
        result = run_hook(
            EDIT_VERIFY_ASYNC,
            stdin_json={"tool_name": "Read", "tool_input": {}, "tool_response": ""},
        )
        assert result["exit_code"] == 0
        parsed = result["parsed_json"]
        assert parsed is not None
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "bad.py" in ctx
        assert "SyntaxError" in ctx
    finally:
        if os.path.exists(async_path):
            os.unlink(async_path)


@pytest.mark.unit
def test_all_pass_no_output(run_hook, session_id):
    """Async results with all passes produce no stdout."""
    async_path = _async_results_path(session_id)
    try:
        results = {
            "timestamp": 1,
            "results": [
                {"file": "ok.py", "passed": True, "output": ""},
            ],
        }
        with open(async_path, "w") as f:
            json.dump(results, f)

        result = run_hook(
            EDIT_VERIFY_ASYNC,
            stdin_json={"tool_name": "Read", "tool_input": {}, "tool_response": ""},
        )
        assert result["exit_code"] == 0
        assert result["stdout"].strip() == ""
    finally:
        if os.path.exists(async_path):
            os.unlink(async_path)


@pytest.mark.unit
def test_results_consumed(run_hook, session_id):
    """Async results file is deleted after being read."""
    async_path = _async_results_path(session_id)
    try:
        results = {
            "timestamp": 1,
            "results": [
                {"file": "bad.py", "passed": False, "output": "error"},
            ],
        }
        with open(async_path, "w") as f:
            json.dump(results, f)

        result = run_hook(
            EDIT_VERIFY_ASYNC,
            stdin_json={"tool_name": "Read", "tool_input": {}, "tool_response": ""},
        )
        assert result["exit_code"] == 0
        assert not os.path.exists(async_path), "Async results file should be consumed (deleted)"
    finally:
        if os.path.exists(async_path):
            os.unlink(async_path)


@pytest.mark.unit
def test_corrupted_results_ignored(run_hook, session_id):
    """Corrupted async results file causes no crash, exit 0."""
    async_path = _async_results_path(session_id)
    try:
        with open(async_path, "w") as f:
            f.write("{{{bad json content")

        result = run_hook(
            EDIT_VERIFY_ASYNC,
            stdin_json={"tool_name": "Read", "tool_input": {}, "tool_response": ""},
        )
        assert result["exit_code"] == 0
    finally:
        if os.path.exists(async_path):
            os.unlink(async_path)


# ===========================================================================
# Regression
# ===========================================================================


@pytest.mark.regression
def test_ProcessLookupError_typo_fixed():
    """Verify the spawn_async_check except clause uses ProcessLookupError (not ProcessLookError)."""
    source_path = str(EDIT_VERIFY_ASYNC)
    with open(source_path, "r") as f:
        source = f.read()
    # The except clause in spawn_async_check must use the correct exception name
    assert "ProcessLookupError" in source, (
        "Expected 'ProcessLookupError' in source but not found"
    )
    assert "ProcessLookError" not in source.replace("ProcessLookupError", ""), (
        "Found misspelled 'ProcessLookError' in source"
    )
