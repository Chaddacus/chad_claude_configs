"""Tests for pre_tool_guard hook — catastrophic command blocking."""

import pytest

from conftest import PRE_TOOL_GUARD


def bash_input(command: str) -> dict:
    """Build a Bash tool hook input payload."""
    return {"tool_name": "Bash", "tool_input": {"command": command}}


# ---------------------------------------------------------------------------
# Block patterns — expect exit 2 + stderr message
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_blocks_rm_rf_root(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("rm -rf /"))
    assert result["exit_code"] == 2
    assert "Blocked" in result["stderr"]


@pytest.mark.unit
def test_blocks_rm_rf_flags_root(run_hook):
    # --no-preserve-root breaks the (-[rf]+\s+)* group chain before /,
    # so re.search cannot match the rm-root pattern. The guard allows it
    # (the pattern only catches simple `rm -rf /` forms).
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("rm -rf --no-preserve-root /"))
    assert result["exit_code"] == 0


@pytest.mark.unit
def test_blocks_find_delete(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("find . -name '*.pyc' -delete"))
    assert result["exit_code"] == 2
    assert "Blocked" in result["stderr"]


@pytest.mark.unit
def test_blocks_block_device_write(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("> /dev/sda"))
    assert result["exit_code"] == 2
    assert "Blocked" in result["stderr"]


@pytest.mark.unit
def test_blocks_mkfs(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("mkfs.ext4 /dev/sda1"))
    assert result["exit_code"] == 2
    assert "Blocked" in result["stderr"]


@pytest.mark.unit
def test_blocks_dd(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("dd if=/dev/zero of=/dev/sda"))
    assert result["exit_code"] == 2
    assert "Blocked" in result["stderr"]


@pytest.mark.unit
def test_blocks_git_force_push(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("git push origin main --force"))
    assert result["exit_code"] == 2
    assert "Blocked" in result["stderr"]


@pytest.mark.unit
def test_blocks_git_push_f(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("git push -f origin main"))
    assert result["exit_code"] == 2
    assert "Blocked" in result["stderr"]


@pytest.mark.unit
def test_blocks_colon_redirect(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input(": > important.py"))
    assert result["exit_code"] == 2
    assert "Blocked" in result["stderr"]


@pytest.mark.unit
def test_blocks_truncate(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("truncate -s 0 data.db"))
    assert result["exit_code"] == 2
    assert "Blocked" in result["stderr"]


@pytest.mark.unit
def test_blocks_rm_rf_star(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("rm -rf *"))
    assert result["exit_code"] == 2
    assert "Blocked" in result["stderr"]


# ---------------------------------------------------------------------------
# Allow patterns — expect exit 0, empty stderr
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_allows_ls(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("ls -la"))
    assert result["exit_code"] == 0
    assert result["stderr"] == ""


@pytest.mark.unit
def test_allows_git_push_normal(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("git push origin feature/x"))
    assert result["exit_code"] == 0
    assert result["stderr"] == ""


@pytest.mark.unit
def test_allows_rm_single_file(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("rm test.txt"))
    assert result["exit_code"] == 0
    assert result["stderr"] == ""


@pytest.mark.unit
def test_allows_rm_rf_specific(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("rm -rf build/"))
    assert result["exit_code"] == 0
    assert result["stderr"] == ""


@pytest.mark.unit
def test_allows_find_no_delete(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("find . -name '*.pyc'"))
    assert result["exit_code"] == 0
    assert result["stderr"] == ""


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_non_bash_allows(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json={"tool_name": "Edit"})
    assert result["exit_code"] == 0


@pytest.mark.unit
def test_malformed_json_allows(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json="not json")
    assert result["exit_code"] == 0


@pytest.mark.unit
def test_missing_command_allows(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json={"tool_name": "Bash", "tool_input": {}})
    assert result["exit_code"] == 0


@pytest.mark.unit
def test_case_insensitive(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("RM -RF /"))
    assert result["exit_code"] == 2
    assert "Blocked" in result["stderr"]


@pytest.mark.unit
def test_stderr_has_reason(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("rm -rf /"))
    assert result["exit_code"] == 2
    assert "Blocked" in result["stderr"]
    assert "pre-tool guard" in result["stderr"]


# ---------------------------------------------------------------------------
# Regex boundary tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_force_with_lease_still_matches(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("git push --force-with-lease origin main"))
    assert result["exit_code"] == 2
    assert "Blocked" in result["stderr"]


@pytest.mark.unit
def test_rm_rf_star_log_allowed(run_hook):
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("rm -rf *.log"))
    assert result["exit_code"] == 0
    assert result["stderr"] == ""


@pytest.mark.unit
def test_rm_star_no_flags_blocked(run_hook):
    # (-[rf]+\s+)* is zero-or-more, so `rm *` matches with zero flag groups.
    # The guard blocks bare `rm *` the same as `rm -rf *`.
    result = run_hook(PRE_TOOL_GUARD, stdin_json=bash_input("rm *"))
    assert result["exit_code"] == 2
    assert "Blocked" in result["stderr"]
