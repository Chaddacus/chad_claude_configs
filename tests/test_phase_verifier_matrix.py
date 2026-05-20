"""Slice 3 — phase-aware verifier matrix + transition gate tests.

Covers:
  - matrix_for_transition lookup (per-route, per-transition)
  - classify_verifier_result truth table
  - run_verifier_matrix happy path + budget exhaustion + not_applicable
  - attempt_phase_transition: phase-guard block, verifier block, allow
  - unknown_failure policy: R2 advisory, R3/R4 block
  - shadow mode: emits shadow_decision, never emits phase_changed
  - Event registration

Mocks: _run_git, subprocess.run, file IO via tmp_path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path.home() / ".claude" / "bin"))
import auto_runtime_common as rt  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(tmp_path, markers):
    proj = tmp_path / "proj"
    proj.mkdir()
    for m in markers:
        (proj / m).write_text("")
    return str(proj)


def _mock_git_repo_at(sha="abc123"):
    def _runner(args, cwd, *, timeout=5):
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return 0, "true"
        if args == ["rev-parse", "HEAD"]:
            return 0, sha
        return 1, ""
    return _runner


def _write_baseline(tmp_path, track_id, *, route, command_id, owned_files_hash,
                    base_git_sha, status):
    """Write a baseline JSON file matching what capture_baselines would create."""
    key = rt._baseline_key(
        track_id=track_id, route=route, command_id=command_id,
        owned_files_hash=owned_files_hash, base_git_sha=base_git_sha,
    )
    bdir = tmp_path / "autonomy" / track_id / "baselines"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / f"{key}.json").write_text(json.dumps({
        "key": key, "track_id": track_id, "route": route,
        "command_id": command_id, "owned_files_hash": owned_files_hash,
        "base_git_sha": base_git_sha, "status": status,
        "exit_code": 0 if status == "pass" else 1,
        "output_excerpt": "", "duration_ms": 100, "captured_at": "now",
    }))
    return key


# ===========================================================================
# matrix_for_transition
# ===========================================================================

class TestMatrixLookup:
    def test_r2_build_to_verify_has_ruff_required(self):
        m = rt.matrix_for_transition("R2", "build", "verify")
        assert m["ruff_check"] == "required"
        assert m["pytest_smoke"] == "advisory"

    def test_r3_more_strict_than_r2(self):
        r2 = rt.matrix_for_transition("R2", "build", "verify")
        r3 = rt.matrix_for_transition("R3", "build", "verify")
        r2_required = {k for k, v in r2.items() if v == "required"}
        r3_required = {k for k, v in r3.items() if v == "required"}
        assert r2_required.issubset(r3_required)

    def test_r4_strictest(self):
        m = rt.matrix_for_transition("R4", "build", "verify")
        # Every command in R4 build→verify must be required
        assert all(v == "required" for v in m.values())

    def test_r1_empty(self):
        assert rt.matrix_for_transition("R1", "build", "verify") == {}

    def test_r5_empty(self):
        assert rt.matrix_for_transition("R5", "discovery", "design") == {}

    def test_unknown_transition_empty(self):
        # No matrix for design→build (no automated verifier gates required there)
        assert rt.matrix_for_transition("R3", "design", "build") == {}


# ===========================================================================
# classify_verifier_result
# ===========================================================================

class TestClassify:
    def test_current_pass_always_pass(self):
        for b in ("pass", "preexisting_failure", None, "timeout"):
            assert rt.classify_verifier_result("pass", b) == "pass"

    def test_baseline_pass_current_fail_is_introduced(self):
        assert rt.classify_verifier_result("preexisting_failure", "pass") == "introduced_failure"

    def test_baseline_preexisting_current_fail_is_preexisting(self):
        assert rt.classify_verifier_result("preexisting_failure", "preexisting_failure") == "preexisting_failure"

    def test_no_baseline_current_fail_is_unknown(self):
        assert rt.classify_verifier_result("preexisting_failure", None) == "unknown_failure"

    def test_infra_error_is_always_unknown(self):
        for b in ("pass", "preexisting_failure", None):
            assert rt.classify_verifier_result("infra_error", b) == "unknown_failure"

    def test_timeout_is_always_unknown(self):
        for b in ("pass", "preexisting_failure", None):
            assert rt.classify_verifier_result("timeout", b) == "unknown_failure"

    def test_budget_exhausted_is_unknown(self):
        assert rt.classify_verifier_result("budget_exhausted", "pass") == "unknown_failure"

    def test_baseline_timeout_current_fail_is_unknown(self):
        assert rt.classify_verifier_result("preexisting_failure", "timeout") == "unknown_failure"


# ===========================================================================
# run_verifier_matrix
# ===========================================================================

class TestRunVerifierMatrix:
    def test_empty_matrix_returns_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        # R1 has no matrix
        summary = rt.run_verifier_matrix(
            "trk-r1", cwd=proj, route="R1",
            from_phase="build", to_phase="verify", owned_files=[],
        )
        assert summary["transition_allowed"] is True
        assert summary["results"] == []

    def test_all_pass_with_baselines_allows_transition(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        ofh = rt._owned_files_hash([])
        _write_baseline(
            tmp_path, "trk-pass", route="R2", command_id="ruff_check",
            owned_files_hash=ofh, base_git_sha="abc123", status="pass",
        )
        with patch("auto_runtime_common._run_git", side_effect=_mock_git_repo_at("abc123")), \
             patch("auto_runtime_common.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            summary = rt.run_verifier_matrix(
                "trk-pass", cwd=proj, route="R2",
                from_phase="build", to_phase="verify", owned_files=[],
            )
        assert summary["transition_allowed"] is True
        ruff_result = next(r for r in summary["results"] if r["command_id"] == "ruff_check")
        assert ruff_result["classification"] == "pass"

    def test_introduced_failure_on_required_blocks(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        ofh = rt._owned_files_hash([])
        _write_baseline(
            tmp_path, "trk-bad", route="R3", command_id="ruff_check",
            owned_files_hash=ofh, base_git_sha="abc123", status="pass",
        )
        with patch("auto_runtime_common._run_git", side_effect=_mock_git_repo_at("abc123")), \
             patch("auto_runtime_common.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="error", stderr="")
            summary = rt.run_verifier_matrix(
                "trk-bad", cwd=proj, route="R3",
                from_phase="build", to_phase="verify", owned_files=[],
            )
        assert summary["transition_allowed"] is False
        assert any(b["command_id"] == "ruff_check" for b in summary["block_reasons"])

    def test_preexisting_failure_does_not_block(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        ofh = rt._owned_files_hash([])
        _write_baseline(
            tmp_path, "trk-pre", route="R3", command_id="ruff_check",
            owned_files_hash=ofh, base_git_sha="abc123",
            status="preexisting_failure",
        )
        with patch("auto_runtime_common._run_git", side_effect=_mock_git_repo_at("abc123")), \
             patch("auto_runtime_common.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="error", stderr="")
            summary = rt.run_verifier_matrix(
                "trk-pre", cwd=proj, route="R3",
                from_phase="build", to_phase="verify", owned_files=[],
            )
        assert summary["transition_allowed"] is True

    def test_advisory_failure_never_blocks(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        # mypy is advisory in R2 build→verify
        proj = _make_project(tmp_path, ["ruff.toml", "mypy.ini"])
        ofh = rt._owned_files_hash([])
        _write_baseline(
            tmp_path, "trk-adv", route="R2", command_id="ruff_check",
            owned_files_hash=ofh, base_git_sha="abc123", status="pass",
        )
        _write_baseline(
            tmp_path, "trk-adv", route="R2", command_id="mypy_check",
            owned_files_hash=ofh, base_git_sha="abc123", status="pass",
        )

        def runner(argv, **kwargs):
            # ruff passes, mypy fails
            rc = 0 if "ruff" in argv[0] or argv[0] == "ruff" else 1
            return MagicMock(returncode=rc, stdout="", stderr="")

        with patch("auto_runtime_common._run_git", side_effect=_mock_git_repo_at("abc123")), \
             patch("auto_runtime_common.subprocess.run", side_effect=runner):
            summary = rt.run_verifier_matrix(
                "trk-adv", cwd=proj, route="R2",
                from_phase="build", to_phase="verify", owned_files=[],
            )
        # Advisory mypy failure must not block
        assert summary["transition_allowed"] is True

    def test_unknown_failure_blocks_R3(self, tmp_path, monkeypatch):
        """R3 policy = block on unknown_failure."""
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        # No baseline written → unknown_failure path
        with patch("auto_runtime_common._run_git", side_effect=_mock_git_repo_at("abc123")), \
             patch("auto_runtime_common.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            summary = rt.run_verifier_matrix(
                "trk-r3-unk", cwd=proj, route="R3",
                from_phase="build", to_phase="verify", owned_files=[],
            )
        assert summary["transition_allowed"] is False

    def test_unknown_failure_advisory_for_R2(self, tmp_path, monkeypatch):
        """R2 policy = advisory on unknown_failure (do not block)."""
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        with patch("auto_runtime_common._run_git", side_effect=_mock_git_repo_at("abc123")), \
             patch("auto_runtime_common.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            summary = rt.run_verifier_matrix(
                "trk-r2-unk", cwd=proj, route="R2",
                from_phase="build", to_phase="verify", owned_files=[],
            )
        assert summary["transition_allowed"] is True

    def test_not_applicable_when_marker_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        # R3 matrix wants eslint+tsc but project is python-only
        proj = _make_project(tmp_path, ["ruff.toml", "pyproject.toml"])
        with patch("auto_runtime_common._run_git", side_effect=_mock_git_repo_at("abc123")), \
             patch("auto_runtime_common.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            summary = rt.run_verifier_matrix(
                "trk-na", cwd=proj, route="R3",
                from_phase="build", to_phase="verify", owned_files=[],
            )
        # eslint/tsc not applicable in a python project — they don't block
        na = [r for r in summary["results"] if r["current_status"] == "not_applicable"]
        assert len(na) >= 2  # at least eslint_check + tsc_noemit
        assert all(r["classification"] == "pass" for r in na)

    def test_emits_matrix_lifecycle_events(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        ofh = rt._owned_files_hash([])
        _write_baseline(
            tmp_path, "trk-evt", route="R2", command_id="ruff_check",
            owned_files_hash=ofh, base_git_sha="abc123", status="pass",
        )
        with patch("auto_runtime_common._run_git", side_effect=_mock_git_repo_at("abc123")), \
             patch("auto_runtime_common.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            rt.run_verifier_matrix(
                "trk-evt", cwd=proj, route="R2",
                from_phase="build", to_phase="verify", owned_files=[],
            )
        events = rt._read_events("trk-evt")
        names = {e["event"] for e in events}
        assert "verifier_matrix_started" in names
        assert "verifier_run" in names
        assert "verifier_classified" in names
        assert "verifier_matrix_completed" in names


# ===========================================================================
# attempt_phase_transition
# ===========================================================================

class TestAttemptPhaseTransition:
    def test_phase_guard_block_emits_phase_transition_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        # Try discovery→closeout: not a valid edge in the phase transition table
        decision = rt.attempt_phase_transition(
            "trk-bad-phase", to_phase="closeout", route="R3",
            cwd=proj, owned_files=[], evidence_keys=set(),
        )
        assert decision["allowed"] is False
        events = rt._read_events("trk-bad-phase")
        assert any(e["event"] == "phase_transition_blocked" for e in events)

    def test_full_sequence_discovery_to_verify(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        ofh = rt._owned_files_hash([])
        _write_baseline(
            tmp_path, "trk-seq", route="R2", command_id="ruff_check",
            owned_files_hash=ofh, base_git_sha="abc123", status="pass",
        )

        # Walk: discovery → design → build → verify
        rt._append_phase_event(
            "trk-seq", from_phase="discovery", to_phase="design",
            evidence=["repo_facts", "scope", "constraints"],
        )
        rt._append_phase_event(
            "trk-seq", from_phase="design", to_phase="build",
            evidence=["plan_approved", "owned_files", "validation_plan"],
        )
        with patch("auto_runtime_common._run_git", side_effect=_mock_git_repo_at("abc123")), \
             patch("auto_runtime_common.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            decision = rt.attempt_phase_transition(
                "trk-seq", to_phase="verify", route="R2",
                cwd=proj, owned_files=[],
                evidence_keys={"patch_applied", "lint_pass"},
            )
        assert decision["allowed"] is True
        events = rt._read_events("trk-seq")
        phase_changed = [e for e in events if e["event"] == "phase_changed"]
        # Should be 3: design, build, verify
        assert len(phase_changed) == 3
        assert phase_changed[-1]["to_phase"] == "verify"

    def test_verifier_block_emits_phase_transition_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        ofh = rt._owned_files_hash([])
        _write_baseline(
            tmp_path, "trk-vfail", route="R3", command_id="ruff_check",
            owned_files_hash=ofh, base_git_sha="abc123", status="pass",
        )
        rt._append_phase_event(
            "trk-vfail", from_phase="discovery", to_phase="design",
            evidence=["repo_facts", "scope", "constraints"],
        )
        rt._append_phase_event(
            "trk-vfail", from_phase="design", to_phase="build",
            evidence=["plan_approved", "owned_files", "validation_plan"],
        )
        with patch("auto_runtime_common._run_git", side_effect=_mock_git_repo_at("abc123")), \
             patch("auto_runtime_common.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="error", stderr="")
            decision = rt.attempt_phase_transition(
                "trk-vfail", to_phase="verify", route="R3",
                cwd=proj, owned_files=[],
                evidence_keys={"patch_applied", "lint_pass"},
            )
        assert decision["allowed"] is False
        assert decision["reason"] == "verifier_matrix_blocked"
        events = rt._read_events("trk-vfail")
        # phase_changed must NOT appear for verify (build was added explicitly)
        verify_phase_events = [e for e in events
                               if e["event"] == "phase_changed" and e["to_phase"] == "verify"]
        assert verify_phase_events == []
        blocked = [e for e in events if e["event"] == "phase_transition_blocked"]
        assert any(e.get("reason") == "verifier_matrix_blocked" for e in blocked)


# ===========================================================================
# Shadow mode
# ===========================================================================

class TestShadowMode:
    def test_shadow_never_emits_phase_changed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        ofh = rt._owned_files_hash([])
        _write_baseline(
            tmp_path, "trk-shadow", route="R2", command_id="ruff_check",
            owned_files_hash=ofh, base_git_sha="abc123", status="pass",
        )
        rt._append_phase_event(
            "trk-shadow", from_phase="discovery", to_phase="design",
            evidence=["repo_facts", "scope", "constraints"],
        )
        rt._append_phase_event(
            "trk-shadow", from_phase="design", to_phase="build",
            evidence=["plan_approved", "owned_files", "validation_plan"],
        )
        with patch("auto_runtime_common._run_git", side_effect=_mock_git_repo_at("abc123")), \
             patch("auto_runtime_common.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            decision = rt.attempt_phase_transition(
                "trk-shadow", to_phase="verify", route="R2",
                cwd=proj, owned_files=[],
                evidence_keys={"patch_applied", "lint_pass"},
                shadow=True,
            )
        assert decision["allowed"] is True
        assert decision["shadow"] is True
        events = rt._read_events("trk-shadow")
        # No phase_changed for "verify" in shadow mode
        verify_phases = [e for e in events
                         if e["event"] == "phase_changed" and e["to_phase"] == "verify"]
        assert verify_phases == []
        # shadow_decision must be present
        shadow_decisions = [e for e in events if e["event"] == "shadow_decision"]
        assert len(shadow_decisions) >= 1
        assert any(s["would_emit_phase_changed"] is True for s in shadow_decisions)

    def test_shadow_block_emits_shadow_not_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        ofh = rt._owned_files_hash([])
        _write_baseline(
            tmp_path, "trk-sh-block", route="R3", command_id="ruff_check",
            owned_files_hash=ofh, base_git_sha="abc123", status="pass",
        )
        rt._append_phase_event(
            "trk-sh-block", from_phase="discovery", to_phase="design",
            evidence=["repo_facts", "scope", "constraints"],
        )
        rt._append_phase_event(
            "trk-sh-block", from_phase="design", to_phase="build",
            evidence=["plan_approved", "owned_files", "validation_plan"],
        )
        with patch("auto_runtime_common._run_git", side_effect=_mock_git_repo_at("abc123")), \
             patch("auto_runtime_common.subprocess.run") as run:
            run.return_value = MagicMock(returncode=1, stdout="error", stderr="")
            decision = rt.attempt_phase_transition(
                "trk-sh-block", to_phase="verify", route="R3",
                cwd=proj, owned_files=[],
                evidence_keys={"patch_applied", "lint_pass"},
                shadow=True,
            )
        assert decision["allowed"] is False
        events = rt._read_events("trk-sh-block")
        # No phase_transition_blocked in shadow mode; shadow_decision instead.
        assert not [e for e in events if e["event"] == "phase_transition_blocked"]
        shadow = [e for e in events if e["event"] == "shadow_decision"]
        assert any(s.get("would_emit_phase_changed") is False for s in shadow)


# ===========================================================================
# Event registration
# ===========================================================================

class TestEventRegistration:
    @pytest.mark.parametrize("event_name", [
        "verifier_matrix_started", "verifier_run", "verifier_classified",
        "verifier_matrix_completed", "phase_transition_blocked",
        "shadow_decision",
    ])
    def test_slice3_events_replayable(self, event_name):
        assert event_name in rt.REPLAYABLE_EVENTS


# ===========================================================================
# Constants sanity
# ===========================================================================

class TestConstants:
    def test_matrix_covers_R2_R3_R4(self):
        for r in ("R2", "R3", "R4"):
            assert r in rt.VERIFIER_MATRIX

    def test_budget_covers_all_routes(self):
        for r in ("R1", "R2", "R3", "R4", "R5"):
            assert r in rt.VERIFIER_MATRIX_BUDGET_MS_BY_ROUTE

    def test_unknown_failure_policy_R2_advisory_R3_R4_block(self):
        assert rt.UNKNOWN_FAILURE_POLICY_BY_ROUTE["R2"] == "advisory"
        assert rt.UNKNOWN_FAILURE_POLICY_BY_ROUTE["R3"] == "block"
        assert rt.UNKNOWN_FAILURE_POLICY_BY_ROUTE["R4"] == "block"

    def test_R4_strictly_more_required_than_R3(self):
        for trans in ("build->verify", "verify->closeout"):
            r3 = rt.VERIFIER_MATRIX["R3"][trans]
            r4 = rt.VERIFIER_MATRIX["R4"][trans]
            r3_req = {k for k, v in r3.items() if v == "required"}
            r4_req = {k for k, v in r4.items() if v == "required"}
            assert r3_req.issubset(r4_req)


# ===========================================================================
# Backward compat
# ===========================================================================

class TestBackwardCompat:
    def test_slice_1a_phase_register_intact(self):
        assert rt.PHASE_ENUM == ("discovery", "design", "build", "verify", "closeout")

    def test_slice_1b_canonical_state_intact(self):
        assert rt.canonical_state("phase", "build") == "build"

    def test_slice_1c_capture_baselines_intact(self):
        # Capture function still exists with expected signature
        assert callable(rt.capture_baselines)
