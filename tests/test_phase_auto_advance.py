"""Cycle-flow auto-advance wiring tests.

Verifies _collect_evidence_keys + _maybe_auto_advance_phase walk the phase
chain correctly given the events the cycle flow actually produces, and
that the wiring in cycle_track is non-breaking when cwd is missing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path.home() / ".claude" / "bin"))
import auto_runtime_common as rt  # noqa: E402


def _make_project(tmp_path, markers):
    proj = tmp_path / "proj"
    proj.mkdir()
    for m in markers:
        (proj / m).write_text("")
    return str(proj)


def _mock_git(sha="abc123"):
    def runner(args, cwd, *, timeout=5):
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return 0, "true"
        if args == ["rev-parse", "HEAD"]:
            return 0, sha
        return 1, ""
    return runner


# ===========================================================================
# _collect_evidence_keys
# ===========================================================================

class TestCollectEvidenceKeys:
    def test_empty_log_yields_empty_set(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        assert rt._collect_evidence_keys("trk-empty") == set()

    def test_inline_dispatch_adds_discovery_design_build_evidence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        rt._append_event("trk-disp", {"event": "inline_dispatched", "slice_id": "s1"})
        ev = rt._collect_evidence_keys("trk-disp")
        assert {"repo_facts", "scope", "constraints",
                "plan_approved", "owned_files", "validation_plan",
                "patch_applied"} <= ev

    def test_governed_dispatch_same_evidence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        rt._append_event("trk-gdisp", {"event": "governed_dispatched", "slice_id": "s1"})
        ev = rt._collect_evidence_keys("trk-gdisp")
        assert "patch_applied" in ev

    def test_verifier_matrix_pass_adds_verify_evidence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        rt._append_event("trk-vp", {
            "event": "verifier_matrix_completed",
            "transition_allowed": True,
        })
        ev = rt._collect_evidence_keys("trk-vp")
        assert {"lint_pass", "tests_pass", "no_introduced_regressions"} <= ev

    def test_verifier_matrix_block_no_pass_evidence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        rt._append_event("trk-vb", {
            "event": "verifier_matrix_completed",
            "transition_allowed": False,
        })
        ev = rt._collect_evidence_keys("trk-vb")
        assert "tests_pass" not in ev

    def test_introduced_failure_marker(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        rt._append_event("trk-if", {
            "event": "verifier_classified",
            "classification": "introduced_failure",
        })
        ev = rt._collect_evidence_keys("trk-if")
        assert "introduced_failure" in ev


# ===========================================================================
# _maybe_auto_advance_phase
# ===========================================================================

class TestMaybeAutoAdvancePhase:
    def test_no_target_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        result = rt._maybe_auto_advance_phase(
            "trk-x", target_phase=None, route="R2", cwd=proj, owned_files=[],
        )
        assert result == []

    def test_no_cwd_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        result = rt._maybe_auto_advance_phase(
            "trk-x", target_phase="build", route="R2", cwd="", owned_files=[],
        )
        assert result == []

    def test_already_at_target_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        # Walk to build first
        rt._append_phase_event("trk-at", from_phase="discovery", to_phase="design",
                               evidence=["repo_facts", "scope", "constraints"])
        rt._append_phase_event("trk-at", from_phase="design", to_phase="build",
                               evidence=["plan_approved", "owned_files", "validation_plan"])
        result = rt._maybe_auto_advance_phase(
            "trk-at", target_phase="build", route="R2", cwd=proj, owned_files=[],
        )
        assert result == []

    def test_walks_discovery_to_build_with_dispatch_evidence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        # Synthetic dispatch event provides the evidence keys
        rt._append_event("trk-walk", {"event": "inline_dispatched", "slice_id": "s1"})
        result = rt._maybe_auto_advance_phase(
            "trk-walk", target_phase="build", route="R2", cwd=proj, owned_files=[],
        )
        # Two transitions: discovery→design, design→build
        assert len(result) == 2
        assert all(d["allowed"] for d in result)
        assert rt.current_phase(rt._read_events("trk-walk")) == "build"

    def test_stops_at_first_block(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        # No dispatch event → no evidence → discovery→design blocks
        result = rt._maybe_auto_advance_phase(
            "trk-block", target_phase="build", route="R2", cwd=proj, owned_files=[],
        )
        assert len(result) == 1
        assert result[0]["allowed"] is False

    def test_walks_through_verify_with_verifier_pass_evidence(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        ofh = rt._owned_files_hash([])
        # Seed a passing baseline so the verifier matrix on R2 build→verify allows it
        key = rt._baseline_key(
            track_id="trk-vw", route="R2", command_id="ruff_check",
            owned_files_hash=ofh, base_git_sha="abc123",
        )
        bdir = tmp_path / "autonomy" / "trk-vw" / "baselines"
        bdir.mkdir(parents=True, exist_ok=True)
        import json
        (bdir / f"{key}.json").write_text(json.dumps({
            "key": key, "status": "pass", "command_id": "ruff_check",
            "owned_files_hash": ofh, "base_git_sha": "abc123",
            "route": "R2", "exit_code": 0, "output_excerpt": "",
            "duration_ms": 100, "captured_at": "now",
        }))
        # Dispatch evidence + verifier-pass event would normally come from
        # prior cycles; synthesize them here.
        rt._append_event("trk-vw", {"event": "inline_dispatched", "slice_id": "s1"})
        rt._append_event("trk-vw", {
            "event": "verifier_matrix_completed", "transition_allowed": True,
        })
        with patch("auto_runtime_common._run_git", side_effect=_mock_git("abc123")), \
             patch("auto_runtime_common.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = rt._maybe_auto_advance_phase(
                "trk-vw", target_phase="verify", route="R2",
                cwd=proj, owned_files=[],
            )
        assert rt.current_phase(rt._read_events("trk-vw")) == "verify"
        assert all(d["allowed"] for d in result)

    def test_invalid_target_phase_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        proj = _make_project(tmp_path, ["ruff.toml"])
        assert rt._maybe_auto_advance_phase(
            "trk-bad", target_phase="not_a_phase",
            route="R2", cwd=proj, owned_files=[],
        ) == []


# ===========================================================================
# _active_slice_owned_files
# ===========================================================================

class TestActiveSliceOwnedFiles:
    def _state(self, *, nodes=None, next_slice_id=None):
        return {
            "views": {
                "graph": {"nodes": nodes or {}},
                "frontier": {"next_slice_id": next_slice_id},
            },
        }

    def test_dispatch_picks_action_result_slice(self):
        state = self._state(
            nodes={"s-just-dispatched": {"owned_scope": ["a.py", "b.py"]}},
            next_slice_id="s-other",
        )
        owned = rt._active_slice_owned_files(
            state, action="dispatch",
            action_result={"dispatch": {"slice_id": "s-just-dispatched"}},
            anticipation={},
        )
        assert sorted(owned) == ["a.py", "b.py"]

    def test_evaluate_picks_evaluator_dispatch_slice(self):
        state = self._state(
            nodes={"s-eval": {"owned_scope": ["c.py"]}},
            next_slice_id="s-other",
        )
        owned = rt._active_slice_owned_files(
            state, action="evaluate", action_result={},
            anticipation={"evaluator_dispatch": {"slice_id": "s-eval"}},
        )
        assert owned == ["c.py"]

    def test_close_falls_back_to_frontier(self):
        state = self._state(
            nodes={"s-front": {"owned_scope": ["x.py"]}},
            next_slice_id="s-front",
        )
        owned = rt._active_slice_owned_files(
            state, action="close", action_result={}, anticipation={},
        )
        assert owned == ["x.py"]

    def test_no_slice_resolvable_returns_empty(self):
        state = self._state(nodes={}, next_slice_id=None)
        owned = rt._active_slice_owned_files(
            state, action="dispatch", action_result={}, anticipation={},
        )
        assert owned == []

    def test_missing_node_returns_empty(self):
        state = self._state(nodes={}, next_slice_id="ghost-slice")
        owned = rt._active_slice_owned_files(
            state, action="dispatch", action_result={}, anticipation={},
        )
        assert owned == []

    def test_filters_falsy_entries(self):
        state = self._state(
            nodes={"s": {"owned_scope": ["a.py", "", None, "b.py"]}},
            next_slice_id="s",
        )
        owned = rt._active_slice_owned_files(
            state, action="dispatch",
            action_result={"dispatch": {"slice_id": "s"}},
            anticipation={},
        )
        assert sorted(owned) == ["a.py", "b.py"]


# ===========================================================================
# Action → phase intent map
# ===========================================================================

class TestActionPhaseIntent:
    def test_dispatch_targets_build(self):
        assert rt._ACTION_TO_PHASE_INTENT["dispatch"] == "build"

    def test_evaluate_targets_verify(self):
        assert rt._ACTION_TO_PHASE_INTENT["evaluate"] == "verify"

    def test_close_targets_closeout(self):
        assert rt._ACTION_TO_PHASE_INTENT["close"] == "closeout"

    def test_repair_bookkeeping_not_mapped(self):
        # repair_bookkeeping should not trigger phase advance
        assert "repair_bookkeeping" not in rt._ACTION_TO_PHASE_INTENT

    def test_halt_for_authority_not_mapped(self):
        assert "halt_for_authority" not in rt._ACTION_TO_PHASE_INTENT
