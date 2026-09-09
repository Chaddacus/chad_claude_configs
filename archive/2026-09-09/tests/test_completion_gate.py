"""Tests for completion_gate hook — verification gate on task-completed / stop."""

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from conftest import COMPLETION_GATE

# ---------------------------------------------------------------------------
# Import the module directly for unit tests of pure functions
# ---------------------------------------------------------------------------
sys.path.insert(0, str(COMPLETION_GATE.parent))
import completion_gate  # noqa: E402


# ===========================================================================
# Early exits (subprocess via run_hook, must pass --event arg)
# ===========================================================================


@pytest.mark.unit
def test_no_edits_silent(run_hook, ledger_path):
    """No ledger file at all => exit 0, no output."""
    # Ensure no ledger exists
    if os.path.exists(ledger_path):
        os.unlink(ledger_path)
    result = run_hook(COMPLETION_GATE, args=["--event", "task-completed"])
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""


@pytest.mark.unit
def test_no_code_edits_silent(run_hook, make_ledger):
    """Ledger has only README.md edits => exit 0, no output."""
    make_ledger(
        edits=[{"file": "README.md", "timestamp": 100}],
        last_edit_at=100,
        last_verified_at=0,
        verified_clean=False,
    )
    result = run_hook(COMPLETION_GATE, args=["--event", "task-completed"])
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""


@pytest.mark.unit
def test_verified_clean_silent(run_hook, make_ledger):
    """Already verified_clean=True with last_verified > last_edit => exit 0."""
    make_ledger(
        edits=[{"file": "app.py", "timestamp": 100}],
        last_edit_at=100,
        last_verified_at=200,
        verified_clean=True,
    )
    result = run_hook(COMPLETION_GATE, args=["--event", "task-completed"])
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""


@pytest.mark.unit
def test_unverified_runs_checks(run_hook, make_ledger, fake_project, subagent_transcript, monkeypatch):
    """Unverified code edits with a project marker => runs commands, produces output.

    `--event task-completed` resolves a subagent start floor from the hook
    payload and exits 0 when it cannot (a subagent whose edits cannot be
    attributed is never nagged). A payload-less invocation therefore produces
    NO output regardless of ledger state, so the transcript is required to
    reach the verification path at all — without it this asserts nothing.
    """
    make_ledger(
        edits=[{"file": "main.py", "timestamp": 200}],
        last_edit_at=200,
        last_verified_at=100,
        verified_clean=False,
    )
    # Create a fake project with a Makefile containing a test target
    project_dir = fake_project({
        "Makefile": "\ntest:\n\techo ok\n",
    })
    # chdir so find_project_root picks up our fake project
    monkeypatch.chdir(project_dir)
    result = run_hook(
        COMPLETION_GATE,
        args=["--event", "task-completed"],
        stdin_json={"transcript_path": subagent_transcript(), "agent_id": "test-agent-a"},
    )
    # Should produce output (the envelope with hookSpecificOutput)
    assert result["stdout"].strip() != ""
    assert result["parsed_json"] is not None
    assert "hookSpecificOutput" in result["parsed_json"]


# ===========================================================================
# has_code_edits coverage (import and test directly)
# ===========================================================================


@pytest.mark.unit
def test_py_is_code():
    ledger = {"edits": [{"file": "app.py"}]}
    assert completion_gate.has_code_edits(ledger) is True


@pytest.mark.unit
def test_ts_is_code():
    ledger = {"edits": [{"file": "index.ts"}]}
    assert completion_gate.has_code_edits(ledger) is True


@pytest.mark.unit
def test_sh_is_code():
    ledger = {"edits": [{"file": "deploy.sh"}]}
    assert completion_gate.has_code_edits(ledger) is True


@pytest.mark.unit
def test_md_not_code():
    ledger = {"edits": [{"file": "README.md"}]}
    assert completion_gate.has_code_edits(ledger) is False


@pytest.mark.unit
def test_json_not_code():
    ledger = {"edits": [{"file": "config.json"}]}
    assert completion_gate.has_code_edits(ledger) is False


@pytest.mark.unit
def test_empty_list():
    ledger = {"edits": []}
    assert completion_gate.has_code_edits(ledger) is False


# ===========================================================================
# Project detection — resolve_commands with fake_project
# ===========================================================================


@pytest.mark.unit
def test_node_test_script(fake_project):
    root = fake_project({
        "package.json": {"scripts": {"test": "jest"}},
    })
    cmds = completion_gate.resolve_commands(root)
    cmd_strs = [c["cmd"] for c in cmds]
    assert "npm test" in cmd_strs


@pytest.mark.unit
def test_node_typecheck_script(fake_project):
    root = fake_project({
        "package.json": {"scripts": {"test": "jest", "typecheck": "tsc"}},
    })
    cmds = completion_gate.resolve_commands(root)
    cmd_strs = [c["cmd"] for c in cmds]
    assert "npm test" in cmd_strs
    assert "npm run typecheck" in cmd_strs


@pytest.mark.unit
def test_node_check_fallback(fake_project):
    root = fake_project({
        "package.json": {"scripts": {"test": "jest", "check": "tsc --noEmit"}},
    })
    cmds = completion_gate.resolve_commands(root)
    cmd_strs = [c["cmd"] for c in cmds]
    assert "npm run check" in cmd_strs
    assert "npm run typecheck" not in cmd_strs


@pytest.mark.unit
def test_node_tsc_fallback(fake_project):
    root = fake_project({
        "package.json": {"scripts": {"test": "jest"}},
        "tsconfig.json": "{}",
    })
    cmds = completion_gate.resolve_commands(root)
    cmd_strs = [c["cmd"] for c in cmds]
    assert "npx tsc --noEmit" in cmd_strs
    assert "npm run typecheck" not in cmd_strs
    assert "npm run check" not in cmd_strs


@pytest.mark.unit
def test_python_pytest(fake_project):
    root = fake_project({
        "pyproject.toml": "[tool.pytest]\nminversion = '6.0'\n",
    })
    cmds = completion_gate.resolve_commands(root)
    cmd_strs = [c["cmd"] for c in cmds]
    assert "python -m pytest" in cmd_strs


@pytest.mark.unit
def test_python_ruff(fake_project):
    root = fake_project({
        "pyproject.toml": "[tool.ruff]\nline-length = 120\n",
    })
    cmds = completion_gate.resolve_commands(root)
    cmd_strs = [c["cmd"] for c in cmds]
    assert "ruff check ." in cmd_strs


@pytest.mark.unit
def test_rust(fake_project):
    root = fake_project({
        "Cargo.toml": "[package]\nname = \"test\"\n",
    })
    cmds = completion_gate.resolve_commands(root)
    cmd_strs = [c["cmd"] for c in cmds]
    assert "cargo test" in cmd_strs
    assert "cargo clippy" in cmd_strs


@pytest.mark.unit
def test_go(fake_project):
    root = fake_project({
        "go.mod": "module example.com/test\n",
    })
    cmds = completion_gate.resolve_commands(root)
    cmd_strs = [c["cmd"] for c in cmds]
    assert "go test ./..." in cmd_strs
    assert "go vet ./..." in cmd_strs


@pytest.mark.unit
def test_makefile_test(fake_project):
    root = fake_project({
        "Makefile": "\ntest:\n\techo ok\n",
    })
    cmds = completion_gate.resolve_commands(root)
    cmd_strs = [c["cmd"] for c in cmds]
    assert "make test" in cmd_strs


@pytest.mark.unit
def test_makefile_only_fallback(fake_project):
    """package.json takes priority; Makefile fallback only when no other commands."""
    root = fake_project({
        "package.json": {"scripts": {"test": "jest"}},
        "Makefile": "\ntest:\n\techo ok\n",
    })
    cmds = completion_gate.resolve_commands(root)
    cmd_strs = [c["cmd"] for c in cmds]
    # package.json commands should be present
    assert "npm test" in cmd_strs
    # Makefile is a fallback, should NOT be used when package.json produces commands
    assert "make test" not in cmd_strs


@pytest.mark.unit
def test_no_markers_silent(fake_project):
    root = fake_project({})
    cmds = completion_gate.resolve_commands(root)
    assert cmds == []


# ===========================================================================
# Command execution (mock subprocess.run)
# ===========================================================================


@pytest.mark.unit
def test_all_pass():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "All tests passed\n"
    mock_result.stderr = ""
    with patch("completion_gate.subprocess.run", return_value=mock_result):
        result = completion_gate.run_command("npm test", "/tmp")
    assert result["passed"] is True
    assert result["exit_code"] == 0


@pytest.mark.unit
def test_one_fail():
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "FAIL src/app.test.ts\n"
    mock_result.stderr = "1 test failed\n"
    with patch("completion_gate.subprocess.run", return_value=mock_result):
        result = completion_gate.run_command("npm test", "/tmp")
    assert result["passed"] is False
    assert result["exit_code"] == 1


@pytest.mark.unit
def test_timeout():
    with patch(
        "completion_gate.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="npm test", timeout=25),
    ):
        result = completion_gate.run_command("npm test", "/tmp")
    assert result["passed"] is False
    assert result["exit_code"] == -1
    assert "timed out" in result["output"]


@pytest.mark.unit
def test_output_truncation():
    lines = [f"line {i}" for i in range(50)]
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "\n".join(lines) + "\n"
    mock_result.stderr = ""
    with patch("completion_gate.subprocess.run", return_value=mock_result):
        result = completion_gate.run_command("npm test", "/tmp")
    output_lines = result["output"].split("\n")
    # 30 content lines + 1 "more" line = 31
    assert len(output_lines) == 31
    assert "20 more lines" in output_lines[-1]


# ===========================================================================
# CLI args (subprocess via run_hook)
# ===========================================================================


@pytest.mark.unit
def test_task_completed_accepted(run_hook):
    """--event task-completed is a valid event, no argparse error."""
    result = run_hook(COMPLETION_GATE, args=["--event", "task-completed"])
    assert result["exit_code"] == 0


@pytest.mark.unit
def test_stop_accepted(run_hook):
    """--event stop is a valid event, no argparse error."""
    result = run_hook(COMPLETION_GATE, args=["--event", "stop"])
    assert result["exit_code"] == 0


@pytest.mark.unit
def test_invalid_rejected(run_hook):
    """Invalid --event value should cause argparse to fail."""
    result = run_hook(COMPLETION_GATE, args=["--event", "bogus"])
    assert result["exit_code"] != 0


@pytest.mark.unit
def test_event_in_envelope(run_hook, make_ledger, fake_project, subagent_transcript, monkeypatch):
    """task-completed runs emit a PostToolUse-shaped hookSpecificOutput
    envelope with additionalContext (completion_gate.py's documented
    contract). This test previously asserted "task_completed", a value the
    code never emitted — fixed 2026-06-09 to assert the actual contract.
    Payload added 2026-07-27: the task path needs a resolvable subagent start
    floor or it exits 0 silently (see test_unverified_runs_checks)."""
    make_ledger(
        edits=[{"file": "main.py", "timestamp": 200}],
        last_edit_at=200,
        last_verified_at=100,
        verified_clean=False,
    )
    project_dir = fake_project({
        "Makefile": "\ntest:\n\techo ok\n",
    })
    monkeypatch.chdir(project_dir)
    result = run_hook(
        COMPLETION_GATE,
        args=["--event", "task-completed"],
        stdin_json={"transcript_path": subagent_transcript(), "agent_id": "test-agent-b"},
    )
    assert result["parsed_json"] is not None
    hook_output = result["parsed_json"]["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "PostToolUse"
    assert hook_output["additionalContext"].strip() != ""
