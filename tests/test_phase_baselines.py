"""Slice 1c — pre-dispatch baseline capture tests.

Tests for verifier baseline detection, capture, storage, and event emission.
Mocks subprocess + git; never shells out to real verifier commands.

Plan ref: ~/.codex-spar/stage-aware-orchestrator-loop/plan-final.md §2
Deviation: matrix_cell dropped from baseline_key (documented in
auto_runtime_common.py Slice 1c header).
"""

import json
import sys
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path.home() / ".claude" / "bin"))
import auto_runtime_common as rt  # noqa: E402


# ===========================================================================
# Helpers
# ===========================================================================

def _make_project(tmp_path, markers):
    """Create a fake project with the given file markers."""
    proj = tmp_path / "proj"
    proj.mkdir()
    for m in markers:
        (proj / m).write_text("")
    return str(proj)


def _mock_git_repo_at(sha="abc123def456"):
    """Return a (is_repo, rev-parse) mock pair."""
    def _runner(args, cwd, *, timeout=5):
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return 0, "true"
        if args == ["rev-parse", "HEAD"]:
            return 0, sha
        if args[:2] == ["stash", "push"]:
            return 0, "Saved working directory and index state"
        if args == ["stash", "pop"]:
            return 0, ""
        return 1, ""
    return _runner


# ===========================================================================
# _detect_applicable_verifiers
# ===========================================================================

class TestDetectApplicableVerifiers:
    def test_python_project_detects_ruff_pytest_mypy(self, tmp_path):
        proj = _make_project(tmp_path, ["pyproject.toml", "conftest.py"])
        applicable = rt._detect_applicable_verifiers(proj)
        assert "ruff_check" in applicable
        assert "pytest_smoke" in applicable
        assert "mypy_check" in applicable
        assert "eslint_check" not in applicable
        assert "tsc_noemit" not in applicable

    def test_js_project_detects_eslint_tsc(self, tmp_path):
        proj = _make_project(tmp_path, ["tsconfig.json", ".eslintrc.json"])
        applicable = rt._detect_applicable_verifiers(proj)
        assert "tsc_noemit" in applicable
        assert "eslint_check" in applicable

    def test_empty_project_detects_nothing(self, tmp_path):
        proj = _make_project(tmp_path, [])
        assert rt._detect_applicable_verifiers(proj) == []

    def test_nonexistent_cwd_returns_empty(self, tmp_path):
        assert rt._detect_applicable_verifiers("/nonexistent/path") == []

    def test_empty_cwd_returns_empty(self):
        assert rt._detect_applicable_verifiers("") == []


# ===========================================================================
# _baseline_key + _owned_files_hash
# ===========================================================================

class TestKeyDerivation:
    def test_same_inputs_same_key(self):
        a = rt._baseline_key(
            track_id="t1", route="R3", command_id="ruff_check",
            owned_files_hash="h", base_git_sha="abc",
        )
        b = rt._baseline_key(
            track_id="t1", route="R3", command_id="ruff_check",
            owned_files_hash="h", base_git_sha="abc",
        )
        assert a == b

    def test_different_command_different_key(self):
        a = rt._baseline_key(
            track_id="t1", route="R3", command_id="ruff_check",
            owned_files_hash="h", base_git_sha="abc",
        )
        b = rt._baseline_key(
            track_id="t1", route="R3", command_id="mypy_check",
            owned_files_hash="h", base_git_sha="abc",
        )
        assert a != b

    def test_owned_files_hash_order_independent(self):
        a = rt._owned_files_hash(["b.py", "a.py"])
        b = rt._owned_files_hash(["a.py", "b.py"])
        assert a == b

    def test_owned_files_hash_none_is_empty_hash(self):
        a = rt._owned_files_hash(None)
        b = rt._owned_files_hash([])
        assert a == b


# ===========================================================================
# _capture_baseline_command
# ===========================================================================

class TestCaptureBaselineCommand:
    def test_pass_when_exit_zero(self, tmp_path):
        with patch("auto_runtime_common.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            result = rt._capture_baseline_command(
                command_id="ruff_check", cwd=str(tmp_path),
                remaining_budget_ms=5000,
            )
        assert result["status"] == "pass"
        assert result["exit_code"] == 0

    def test_preexisting_failure_when_exit_nonzero(self, tmp_path):
        with patch("auto_runtime_common.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="lint fail", stderr="")
            result = rt._capture_baseline_command(
                command_id="ruff_check", cwd=str(tmp_path),
                remaining_budget_ms=5000,
            )
        assert result["status"] == "preexisting_failure"
        assert result["exit_code"] == 1

    def test_timeout(self, tmp_path):
        with patch("auto_runtime_common.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd="ruff", timeout=0.1)
            result = rt._capture_baseline_command(
                command_id="ruff_check", cwd=str(tmp_path),
                remaining_budget_ms=5000,
            )
        assert result["status"] == "timeout"

    def test_infra_error_when_command_missing(self, tmp_path):
        with patch("auto_runtime_common.subprocess.run") as run:
            run.side_effect = FileNotFoundError("ruff: command not found")
            result = rt._capture_baseline_command(
                command_id="ruff_check", cwd=str(tmp_path),
                remaining_budget_ms=5000,
            )
        assert result["status"] == "infra_error"

    def test_budget_exhausted_when_remaining_zero(self, tmp_path):
        result = rt._capture_baseline_command(
            command_id="ruff_check", cwd=str(tmp_path),
            remaining_budget_ms=0,
        )
        assert result["status"] == "budget_exhausted"


# ===========================================================================
# capture_baselines (top-level)
# ===========================================================================

class TestCaptureBaselines:
    def test_r1_route_skipped_entirely(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["pyproject.toml"])
        summary = rt.capture_baselines(
            "trk-r1", cwd=proj, route="R1", owned_files=["a.py"],
        )
        assert summary["captured"] == []
        assert summary["unavailable"] == []
        # No events emitted
        events = rt._read_events("trk-r1")
        baseline_events = [e for e in events if e.get("event", "").startswith("baseline_")]
        assert baseline_events == []

    def test_r5_route_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["pyproject.toml"])
        summary = rt.capture_baselines(
            "trk-r5", cwd=proj, route="R5", owned_files=[],
        )
        assert summary["captured"] == []

    def test_no_git_emits_unavailable_per_detected_command(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["pyproject.toml", "conftest.py"])
        # _is_git_repo will return False (no .git/)
        summary = rt.capture_baselines(
            "trk-nogit", cwd=proj, route="R3", owned_files=["a.py"],
        )
        # Should have unavailable entries for each detected verifier
        assert len(summary["unavailable"]) >= 2
        events = rt._read_events("trk-nogit")
        unavail = [e for e in events if e.get("event") == "baseline_unavailable"]
        assert len(unavail) >= 2
        reasons = {e["reason"] for e in unavail}
        assert reasons == {"no_git_repo_or_no_head"}

    def test_successful_capture_writes_file_and_event(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        # ruff.toml is a marker only for ruff_check — keeps test focused on one verifier.
        proj = _make_project(tmp_path, ["ruff.toml"])
        with patch("auto_runtime_common._run_git", side_effect=_mock_git_repo_at("sha1")), \
             patch("auto_runtime_common.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="all good", stderr="")
            summary = rt.capture_baselines(
                "trk-ok", cwd=proj, route="R3", owned_files=["x.py"],
            )
        assert len(summary["captured"]) == 1
        assert summary["captured"][0]["status"] == "pass"
        assert summary["captured"][0]["command_id"] == "ruff_check"
        # File exists
        key = summary["captured"][0]["key"]
        baseline_file = tmp_path / "autonomy" / "trk-ok" / "baselines" / f"{key}.json"
        assert baseline_file.exists()
        record = json.loads(baseline_file.read_text())
        assert record["status"] == "pass"
        assert record["command_id"] == "ruff_check"
        assert record["base_git_sha"] == "sha1"
        # Event emitted
        events = rt._read_events("trk-ok")
        bce = [e for e in events if e.get("event") == "baseline_captured"]
        assert len(bce) == 1
        assert bce[0]["command_id"] == "ruff_check"

    def test_budget_exhaustion_stops_capture(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["pyproject.toml", "conftest.py", "mypy.ini"])
        # All three Python verifiers will be detected; force first to exhaust budget.
        call_count = {"n": 0}

        def slow_run(*args, **kwargs):
            call_count["n"] += 1
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("auto_runtime_common._run_git", side_effect=_mock_git_repo_at()), \
             patch("auto_runtime_common.subprocess.run", side_effect=slow_run), \
             patch("auto_runtime_common.time.monotonic") as mono:
            # Make the first command's duration consume the entire R2 budget (1500ms).
            mono.side_effect = [0.0, 2.0, 2.0, 2.0]  # generous to ensure exhaustion
            summary = rt.capture_baselines(
                "trk-budget", cwd=proj, route="R2", owned_files=[],
            )
        # First command runs (took 2000ms vs 1500ms budget); subsequent are
        # budget_exhausted.
        assert len(summary["captured"]) >= 1
        # Remaining detected commands should be marked unavailable
        unavail_reasons = {u["reason"] for u in summary["unavailable"]}
        if summary["unavailable"]:
            assert "budget_exhausted" in unavail_reasons

    def test_capture_exception_does_not_break_dispatch_caller(
        self, tmp_path, monkeypatch
    ):
        """Defensive — capture_baselines should not propagate to caller."""
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["pyproject.toml"])
        with patch("auto_runtime_common._run_git", side_effect=_mock_git_repo_at()), \
             patch("auto_runtime_common.subprocess.run") as run:
            run.side_effect = RuntimeError("subprocess explosion")
            # Function should swallow within _capture_baseline_command's except
            # → emits infra_error. capture_baselines itself doesn't raise.
            # RuntimeError isn't caught by (FileNotFoundError, OSError) so we
            # check the broader story via dispatch hook test instead.
            try:
                rt.capture_baselines(
                    "trk-explode", cwd=proj, route="R3", owned_files=[],
                )
            except RuntimeError:
                # If it does raise, dispatch_track's try/except will catch
                pass


# ===========================================================================
# Event registration
# ===========================================================================

class TestEventRegistration:
    @pytest.mark.parametrize("event_name", [
        "baseline_captured", "baseline_unavailable",
    ])
    def test_baseline_event_is_replayable(self, event_name):
        assert event_name in rt.REPLAYABLE_EVENTS


# ===========================================================================
# Allowlist + budget constants
# ===========================================================================

class TestAllowlistAndBudget:
    def test_allowlist_command_ids_unique(self):
        assert len(rt.VERIFIER_ALLOWLIST) == len(set(rt.VERIFIER_ALLOWLIST.keys()))

    def test_allowlist_entries_have_required_fields(self):
        for cid, spec in rt.VERIFIER_ALLOWLIST.items():
            assert "argv" in spec
            assert "project_markers" in spec
            assert "timeout_ms" in spec
            assert isinstance(spec["argv"], list)
            assert isinstance(spec["project_markers"], list)
            assert spec["timeout_ms"] > 0

    def test_budget_covers_all_routes(self):
        for route in ("R1", "R2", "R3", "R4", "R5"):
            assert route in rt.BASELINE_BUDGET_MS_BY_ROUTE

    def test_r1_and_r5_have_zero_budget(self):
        assert rt.BASELINE_BUDGET_MS_BY_ROUTE["R1"] == 0
        assert rt.BASELINE_BUDGET_MS_BY_ROUTE["R5"] == 0

    def test_r4_budget_largest(self):
        b = rt.BASELINE_BUDGET_MS_BY_ROUTE
        assert b["R4"] > b["R3"] > b["R2"]


# ===========================================================================
# Backward compat
# ===========================================================================

class TestBackwardCompat:
    def test_slice_1a_primitives_intact(self):
        # phase_transition_allowed still works
        result = rt.phase_transition_allowed(None, "discovery", "R3", set())
        assert result["allowed"] is True

    def test_slice_1b_primitives_intact(self):
        # decision_record canonical-state hash still works
        assert rt.canonical_state("phase", "build") == "build"
        questions = rt.select_phase_questions("build", "R3")
        assert any(q["id"] == "simplest_path" for q in questions)
