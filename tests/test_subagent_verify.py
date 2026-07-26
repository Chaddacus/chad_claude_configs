"""Tests for subagent_verify.py hook.

The hook reports only edits it can PROVE this subagent authored, matching the
`agent_id` that edit_verify_async.record_edit stamps onto each ledger entry from
its PostToolUse payload (absent = main thread).

Superseded contract (pre-2026-07-26): ownership was inferred from a start-time
floor — "edited after the subagent began" was treated as "edited BY it". That is
false whenever the parent keeps working while it fans out, which is the normal
supervisor pattern; two read-only explorers were both flagged for a file the
parent wrote mid-run. The floor is retained as a secondary bound but is no
longer what establishes ownership.

Firing tests must therefore both stamp their intended edits with `agent_id` and
identify the subagent in the hook input — see `_owned` and `_stdin`. An edit the
hook cannot attribute is nobody's problem and stays silent."""

import json
import os

import pytest

from conftest import SUBAGENT_VERIFY

# Identity of the subagent under test.
SUB = "test-subagent-aaa"


def _owned(*files, timestamp=200):
    """Ledger edits attributed to SUB — i.e. this subagent's own work."""
    return [{"file": f, "timestamp": timestamp, "agent_id": SUB} for f in files]


def _stdin(transcript=None, **extra):
    """Hook input identifying this subagent (and optionally its transcript)."""
    payload = {"agent_id": SUB}
    if transcript:
        payload["transcript_path"] = transcript
    payload.update(extra)
    return payload


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
        edits=_owned("src/app.py"),
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json=_stdin(subagent_transcript()))
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
        edits=_owned("src/a.ts", "src/b.ts", "src/c.ts"),
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json=_stdin(subagent_transcript()))
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
        edits=_owned("src/dup.py", "src/dup.py"),
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json=_stdin(subagent_transcript()))
    assert result["exit_code"] == 0
    parsed = result["parsed_json"]
    assert parsed is not None
    context = parsed["hookSpecificOutput"]["additionalContext"]
    assert context.count("src/dup.py") == 1


@pytest.mark.unit
def test_sorted(run_hook, make_ledger, subagent_transcript):
    """Files appear in alphabetical order."""
    make_ledger(
        edits=_owned("c.py", "a.py", "b.py"),
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json=_stdin(subagent_transcript()))
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
    make_ledger(
        edits=_owned(*[f"src/file_{i:02d}.py" for i in range(15)]),
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json=_stdin(subagent_transcript()))
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
        edits=_owned("src/old1.py", "src/old2.py", timestamp=50)
        + _owned("src/new.py", timestamp=200),
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json=_stdin(subagent_transcript()))
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
def test_subagent_own_edit_warns(run_hook, make_ledger, subagent_transcript):
    """An edit stamped with this subagent's agent_id IS its own work and must
    still be flagged — the attribution filter must not silence real findings."""
    make_ledger(
        edits=[{"file": "bin/parent_edit.py", "timestamp": 1000}]  # unstamped = parent
        + _owned("src/subagent_made.py", timestamp=6000),
        last_edit_at=6000,
        last_verified_at=0,
    )
    transcript = subagent_transcript(start_iso="1970-01-01T01:23:20Z")  # epoch 5000
    result = run_hook(SUBAGENT_VERIFY, stdin_json=_stdin(transcript))
    assert result["exit_code"] == 0
    parsed = result["parsed_json"]
    assert parsed is not None
    context = parsed["hookSpecificOutput"]["additionalContext"]
    assert "src/subagent_made.py" in context
    assert "bin/parent_edit.py" not in context


@pytest.mark.regression
def test_parent_edit_during_subagent_run_not_attributed(
    run_hook, make_ledger, subagent_transcript
):
    """The 2026-07-26 case the start-time floor could never catch: the parent
    edits a file WHILE the subagent runs. Its timestamp is after the subagent
    started, so the floor admits it, but it is not the subagent's work. Only
    attribution can reject it — two read-only explorers were flagged this way."""
    make_ledger(
        # Both after the subagent's start (epoch 5000); only one is the subagent's.
        edits=[{"file": "bin/parent_wrote_midrun.py", "timestamp": 6000}]
        + _owned("src/subagent_made.py", timestamp=6001),
        last_edit_at=6001,
        last_verified_at=0,
    )
    transcript = subagent_transcript(start_iso="1970-01-01T01:23:20Z")  # epoch 5000
    result = run_hook(SUBAGENT_VERIFY, stdin_json=_stdin(transcript))
    assert result["exit_code"] == 0
    context = result["parsed_json"]["hookSpecificOutput"]["additionalContext"]
    assert "bin/parent_wrote_midrun.py" not in context
    assert "src/subagent_made.py" in context


@pytest.mark.regression
def test_no_agent_id_suppresses(run_hook, make_ledger, subagent_transcript):
    """No agent_id in the hook input -> the subagent cannot be identified, so
    nothing can be attributed to it. Suppress rather than blame whoever ran."""
    make_ledger(
        edits=_owned("src/app.py"),
        last_edit_at=200,
        last_verified_at=100,
    )
    result = run_hook(SUBAGENT_VERIFY, stdin_json={"transcript_path": subagent_transcript()})
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == ""


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
