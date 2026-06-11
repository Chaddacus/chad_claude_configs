"""Tests for subagent_verify.py hook.

The hook scopes its unverified-code warning to edits made AFTER the subagent
started (via its own transcript_path). Firing tests therefore supply a
transcript whose start is epoch 0, so the synthetic positive-timestamp edits
count as "made by this subagent" — exercising the `> last_verified` logic.
No transcript -> the hook fails open (suppresses), which is its own test."""

import json
import os

import pytest

from conftest import SUBAGENT_VERIFY


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_ledger_silent(run_hook, ledger_path):
    """No ledger file on disk -> exit 0, no output."""
    assert not os.path.exists(ledger_path)
    result = run_hook(SUBAGENT_VERIFY, stdin_json={})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""


@pytest.mark.unit
def test_empty_ledger_silent(run_hook, make_ledger):
    """Default empty ledger (last_edit_at=0) -> exit 0, no output."""
    make_ledger()
    result = run_hook(SUBAGENT_VERIFY, stdin_json={})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""


@pytest.mark.unit
def test_all_verified_silent(run_hook, make_ledger, subagent_transcript):
    """All edits before last_verified_at -> exit 0, no output."""
    make_ledger(
        edits=[{"file": "src/app.py", "timestamp": 100}],
        last_edit_at=100,
        last_verified_at=200,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json={"transcript_path": subagent_transcript()})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""


@pytest.mark.unit
def test_unverified_code_warns(run_hook, make_ledger, subagent_transcript):
    """Unverified .py edit -> hookSpecificOutput with filename."""
    make_ledger(
        edits=[{"file": "src/app.py", "timestamp": 200}],
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json={"transcript_path": subagent_transcript()})
    assert result["exit_code"] == 0
    parsed = result["parsed_json"]
    assert parsed is not None
    assert "hookSpecificOutput" in parsed
    context = parsed["hookSpecificOutput"]["additionalContext"]
    assert "src/app.py" in context


@pytest.mark.unit
def test_unverified_md_silent(run_hook, make_ledger, subagent_transcript):
    """.md is not in CODE_EXTENSIONS -> exit 0, no output."""
    make_ledger(
        edits=[{"file": "docs/README.md", "timestamp": 200}],
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json={"transcript_path": subagent_transcript()})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""


@pytest.mark.unit
def test_multiple_files_listed(run_hook, make_ledger, subagent_transcript):
    """3 .ts edits -> all 3 filenames in output."""
    make_ledger(
        edits=[
            {"file": "src/a.ts", "timestamp": 200},
            {"file": "src/b.ts", "timestamp": 200},
            {"file": "src/c.ts", "timestamp": 200},
        ],
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json={"transcript_path": subagent_transcript()})
    assert result["exit_code"] == 0
    parsed = result["parsed_json"]
    assert parsed is not None
    context = parsed["hookSpecificOutput"]["additionalContext"]
    assert "src/a.ts" in context
    assert "src/b.ts" in context
    assert "src/c.ts" in context


@pytest.mark.unit
def test_dedup(run_hook, make_ledger, subagent_transcript):
    """Same file edited twice -> listed only once."""
    make_ledger(
        edits=[
            {"file": "src/dup.py", "timestamp": 200},
            {"file": "src/dup.py", "timestamp": 200},
        ],
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json={"transcript_path": subagent_transcript()})
    assert result["exit_code"] == 0
    parsed = result["parsed_json"]
    assert parsed is not None
    context = parsed["hookSpecificOutput"]["additionalContext"]
    assert context.count("src/dup.py") == 1


@pytest.mark.unit
def test_sorted(run_hook, make_ledger, subagent_transcript):
    """Files appear in alphabetical order."""
    make_ledger(
        edits=[
            {"file": "c.py", "timestamp": 200},
            {"file": "a.py", "timestamp": 200},
            {"file": "b.py", "timestamp": 200},
        ],
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json={"transcript_path": subagent_transcript()})
    assert result["exit_code"] == 0
    parsed = result["parsed_json"]
    assert parsed is not None
    context = parsed["hookSpecificOutput"]["additionalContext"]
    idx_a = context.index("a.py")
    idx_b = context.index("b.py")
    idx_c = context.index("c.py")
    assert idx_a < idx_b < idx_c


@pytest.mark.unit
def test_10_file_cap(run_hook, make_ledger, subagent_transcript):
    """15 files -> 10 shown, output contains 'and 5 more'."""
    edits = [{"file": f"src/file_{i:02d}.py", "timestamp": 200} for i in range(15)]
    make_ledger(
        edits=edits,
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json={"transcript_path": subagent_transcript()})
    assert result["exit_code"] == 0
    parsed = result["parsed_json"]
    assert parsed is not None
    context = parsed["hookSpecificOutput"]["additionalContext"]
    listed = [line for line in context.splitlines() if line.strip().startswith("- src/file_")]
    assert len(listed) == 10
    assert "and 5 more" in context


@pytest.mark.unit
def test_mixed_verified_unverified(run_hook, make_ledger, subagent_transcript):
    """2 edits before last_verified, 1 after -> only 1 file in output."""
    make_ledger(
        edits=[
            {"file": "src/old1.py", "timestamp": 50},
            {"file": "src/old2.py", "timestamp": 50},
            {"file": "src/new.py", "timestamp": 200},
        ],
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json={"transcript_path": subagent_transcript()})
    assert result["exit_code"] == 0
    parsed = result["parsed_json"]
    assert parsed is not None
    context = parsed["hookSpecificOutput"]["additionalContext"]
    assert "src/new.py" in context
    assert "src/old1.py" not in context
    assert "src/old2.py" not in context


# ---------------------------------------------------------------------------
# Subagent-scoping regression (2026-06-10 fleet-audit misattribution)
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_parent_edits_before_subagent_not_attributed(run_hook, make_ledger, subagent_transcript):
    """The bug: subagents inherit the parent session_id, so they read the
    parent's ledger. A read-only subagent must NOT be nagged about the parent's
    code edits that happened before the subagent started."""
    # Parent edited .py code well before the subagent began (epoch 1000 vs start 5000).
    make_ledger(
        edits=[{"file": "bin/stop_gate.py", "timestamp": 1000}],
        last_edit_at=1000,
        last_verified_at=0,
    )
    transcript = subagent_transcript(start_iso="1970-01-01T01:23:20Z")  # epoch 5000
    result = run_hook(SUBAGENT_VERIFY, stdin_json={"transcript_path": transcript})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""  # suppressed — not the subagent's edit


@pytest.mark.regression
def test_no_transcript_path_suppresses(run_hook, make_ledger):
    """No transcript_path -> floor unresolvable -> fail open (suppress), rather
    than misattribute the parent ledger's edits to the subagent."""
    make_ledger(
        edits=[{"file": "src/app.py", "timestamp": 200}],
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json={})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""


@pytest.mark.regression
def test_subagent_own_edit_after_start_warns(run_hook, make_ledger, subagent_transcript):
    """A code edit made AFTER the subagent started IS the subagent's own work
    and must still be flagged."""
    make_ledger(
        edits=[
            {"file": "bin/parent_edit.py", "timestamp": 1000},   # parent, pre-start
            {"file": "src/subagent_made.py", "timestamp": 6000},  # after start
        ],
        last_edit_at=6000,
        last_verified_at=0,
    )
    transcript = subagent_transcript(start_iso="1970-01-01T01:23:20Z")  # epoch 5000
    result = run_hook(SUBAGENT_VERIFY, stdin_json={"transcript_path": transcript})
    assert result["exit_code"] == 0
    parsed = result["parsed_json"]
    assert parsed is not None
    context = parsed["hookSpecificOutput"]["additionalContext"]
    assert "src/subagent_made.py" in context
    assert "bin/parent_edit.py" not in context


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_malformed_stdin(run_hook, make_ledger):
    """Malformed stdin (not JSON) -> exit 0."""
    make_ledger(
        edits=[{"file": "src/app.py", "timestamp": 200}],
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json="broken")
    assert result["exit_code"] == 0


@pytest.mark.unit
def test_corrupted_ledger(run_hook, ledger_path):
    """Corrupted (non-JSON) ledger file -> exit 0."""
    with open(ledger_path, "w") as f:
        f.write("not valid json {{{")
    result = run_hook(SUBAGENT_VERIFY, stdin_json={})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""


@pytest.mark.unit
def test_missing_edits_key(run_hook, ledger_path):
    """Ledger with last_edit_at but no edits key -> exit 0."""
    with open(ledger_path, "w") as f:
        json.dump({"last_edit_at": 5}, f)
    result = run_hook(SUBAGENT_VERIFY, stdin_json={})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""


@pytest.mark.unit
def test_edit_missing_file(run_hook, make_ledger, subagent_transcript):
    """Edit entry with no 'file' key -> graceful exit 0."""
    make_ledger(
        edits=[{"timestamp": 200}],
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json={"transcript_path": subagent_transcript()})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""
