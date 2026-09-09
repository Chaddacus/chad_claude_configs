"""Tests for cross-hook ledger state transitions.

Simulates real editing sessions by calling hook scripts sequentially
with a shared ledger (same session_id) and verifying final ledger state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import EDIT_VERIFY_ASYNC, COMPLETION_GATE, SUBAGENT_VERIFY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def read_ledger(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


# ===========================================================================
# Cross-hook ledger state machine tests
# ===========================================================================


@pytest.mark.unit
def test_clean_session(ledger_path):
    """Fresh session has no ledger file."""
    assert read_ledger(ledger_path) is None


@pytest.mark.unit
def test_edit_marks_dirty(run_hook, ledger_path):
    """Single edit creates ledger with verified_clean=False and 1 edit."""
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test/x.py"}})

    ledger = read_ledger(ledger_path)
    assert ledger is not None
    assert ledger["verified_clean"] is False
    assert len(ledger["edits"]) == 1
    assert ledger["edits"][0]["file"] == "/tmp/test/x.py"


@pytest.mark.unit
def test_edit_verify_pass_clean(run_hook, ledger_path):
    """Edit followed by passing verification sets verified_clean=True."""
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test/x.py"}})
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Bash", "tool_input": {"command": "npm test"}, "tool_response": {"exit_code": 0}})

    ledger = read_ledger(ledger_path)
    assert ledger is not None
    assert ledger["verified_clean"] is True


@pytest.mark.unit
def test_edit_verify_fail_dirty(run_hook, ledger_path):
    """Edit followed by failing verification keeps verified_clean=False."""
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test/x.py"}})
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Bash", "tool_input": {"command": "npm test"}, "tool_response": {"exit_code": 1}})

    ledger = read_ledger(ledger_path)
    assert ledger is not None
    assert ledger["verified_clean"] is False


@pytest.mark.unit
def test_edit_verify_edit_dirty(run_hook, ledger_path):
    """Edit, verify pass, then another edit resets to dirty."""
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test/x.py"}})
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Bash", "tool_input": {"command": "npm test"}, "tool_response": {"exit_code": 0}})
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test/y.py"}})

    ledger = read_ledger(ledger_path)
    assert ledger is not None
    assert ledger["verified_clean"] is False
    assert len(ledger["edits"]) == 2


@pytest.mark.unit
def test_multi_edit_single_verify(run_hook, ledger_path):
    """Multiple edits followed by single passing verify marks clean with 2 edits."""
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test/x.py"}})
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test/y.py"}})
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Bash", "tool_input": {"command": "npm test"}, "tool_response": {"exit_code": 0}})

    ledger = read_ledger(ledger_path)
    assert ledger is not None
    assert ledger["verified_clean"] is True
    assert len(ledger["edits"]) == 2


@pytest.mark.unit
def test_gate_after_dirty(run_hook, ledger_path, fake_project, session_id, subagent_transcript):
    """Completion gate on dirty ledger reads ledger and runs without crashing.

    Uses a fake project with a Makefile test target so the gate resolves
    a command. The gate should produce output (pass or fail) and update
    the ledger with a verification entry.

    The `--event task-completed` path resolves a subagent start floor from the
    hook payload and exits 0 silently when it cannot, so the transcript on
    stdin is what lets this reach the verification path at all.
    """
    # Record an edit to make ledger dirty
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test/x.py"}})

    # Create a fake project with a Makefile test target
    project_dir = fake_project({"Makefile": "\ntest:\n\techo ok\n"})

    # Run completion gate directly with cwd set to the fake project
    env = os.environ.copy()
    env["CLAUDE_CODE_SESSION_ID"] = session_id
    env["CLAUDE_SESSION_ID"] = session_id
    result = subprocess.run(
        [sys.executable, str(COMPLETION_GATE), "--event", "task-completed"],
        input=json.dumps({
            "transcript_path": subagent_transcript(),
            "agent_id": "test-agent-gate",
        }),
        capture_output=True,
        text=True,
        env=env,
        cwd=project_dir,
        timeout=30,
    )

    assert result.returncode == 0
    # Gate should produce output since ledger is dirty and project has commands
    assert result.stdout.strip() != ""

    # Ledger should be updated with verification entries from the gate
    ledger = read_ledger(ledger_path)
    assert ledger is not None
    assert len(ledger["verifications"]) >= 1
    # The gate should have run make test and recorded the result
    gate_verifications = [v for v in ledger["verifications"] if v.get("source") == "hook"]
    assert len(gate_verifications) >= 1


@pytest.mark.unit
def test_gate_after_clean(run_hook, ledger_path, subagent_transcript):
    """Completion gate on clean ledger exits 0 with no output.

    Carries a transcript so the silence is attributable to the CLEAN LEDGER.
    With an empty payload the task path bails before ever reading the ledger,
    and this would pass no matter what state the ledger was in."""
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test/x.py"}})
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Bash", "tool_input": {"command": "npm test"}, "tool_response": {"exit_code": 0}})

    result = run_hook(
        COMPLETION_GATE,
        {"transcript_path": subagent_transcript(), "agent_id": "test-agent-clean"},
        args=["--event", "task-completed"],
    )

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""


@pytest.mark.unit
def test_subagent_after_dirty(run_hook, ledger_path, subagent_transcript):
    """Subagent verify on dirty ledger produces warning output.

    Both calls carry the same agent_id: since 2026-07-26 the hook reports only
    edits it can prove this subagent authored, so the edit must be recorded
    under that identity for it to be attributable back. The transcript starting
    at epoch 0 keeps the secondary time bound satisfied."""
    agent = "test-subagent-dirty"
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Edit", "agent_id": agent,
                                 "tool_input": {"file_path": "/tmp/test/x.py"}})

    result = run_hook(SUBAGENT_VERIFY, {"transcript_path": subagent_transcript(),
                                        "agent_id": agent})

    assert result["exit_code"] == 0
    assert result["stdout"].strip() != ""
    assert "unverified" in result["stdout"].lower()


@pytest.mark.unit
def test_subagent_after_clean(run_hook, ledger_path, subagent_transcript):
    """Subagent verify on clean ledger produces no output."""
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test/x.py"}})
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Bash", "tool_input": {"command": "npm test"}, "tool_response": {"exit_code": 0}})

    result = run_hook(SUBAGENT_VERIFY, {"transcript_path": subagent_transcript()})

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""


@pytest.mark.unit
def test_full_workflow(run_hook, ledger_path, fake_project, session_id, subagent_transcript):
    """Full workflow: edit, edit, verify fail, edit, verify pass, gate.

    Sequence: edit(a.py) -> edit(b.py) -> verify(fail) -> edit(c.py) ->
              verify(pass) -> completion_gate

    Final state: 3 edits, 2+ verifications, clean.
    """
    # Phase 1: two edits
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test/a.py"}})
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test/b.py"}})

    ledger = read_ledger(ledger_path)
    assert ledger["verified_clean"] is False
    assert len(ledger["edits"]) == 2

    # Phase 2: verify fails
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Bash", "tool_input": {"command": "npm test"}, "tool_response": {"exit_code": 1}})

    ledger = read_ledger(ledger_path)
    assert ledger["verified_clean"] is False
    assert len(ledger["verifications"]) == 1

    # Phase 3: another edit
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test/c.py"}})

    ledger = read_ledger(ledger_path)
    assert len(ledger["edits"]) == 3

    # Phase 4: verify passes
    run_hook(EDIT_VERIFY_ASYNC, {"tool_name": "Bash", "tool_input": {"command": "npm test"}, "tool_response": {"exit_code": 0}})

    ledger = read_ledger(ledger_path)
    assert ledger["verified_clean"] is True
    assert len(ledger["verifications"]) == 2

    # Phase 5: completion gate — already clean, should exit silently.
    # The payload matters: without a transcript the task path exits 0 silently
    # no matter what the ledger says, so this assertion would pass vacuously
    # and stop testing "clean ledger => skip" entirely.
    project_dir = fake_project({"Makefile": "\ntest:\n\techo ok\n"})

    env = os.environ.copy()
    env["CLAUDE_CODE_SESSION_ID"] = session_id
    env["CLAUDE_SESSION_ID"] = session_id
    result = subprocess.run(
        [sys.executable, str(COMPLETION_GATE), "--event", "task-completed"],
        input=json.dumps({
            "transcript_path": subagent_transcript(),
            "agent_id": "test-agent-workflow",
        }),
        capture_output=True,
        text=True,
        env=env,
        cwd=project_dir,
        timeout=30,
    )

    assert result.returncode == 0
    # Gate should skip since verified_clean=True and last_verified > last_edit
    assert result.stdout.strip() == ""

    # Final ledger state
    ledger = read_ledger(ledger_path)
    assert len(ledger["edits"]) == 3
    assert len(ledger["verifications"]) >= 2
    assert ledger["verified_clean"] is True
