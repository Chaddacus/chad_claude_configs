"""Slice 1a — phase register tests.

Tests the primitives added to ~/.claude/bin/auto_runtime_common.py:
- _append_phase_event
- current_phase (fold)
- phase_transition_allowed (predicate)
- route_change_reconcile (predicate)

These are PURE primitives in Slice 1a. No dispatch, no prompt assembly,
no enforcement. Just events + folds + predicates.

Plan ref: ~/.codex-spar/stage-aware-orchestrator-loop/plan-final.md §1
"""

import json
import sys
from pathlib import Path

import pytest

# Import the module under test directly.
sys.path.insert(0, str(Path.home() / ".claude" / "bin"))
import auto_runtime_common as rt  # noqa: E402


# ===========================================================================
# current_phase fold
# ===========================================================================

class TestCurrentPhaseFold:
    def test_empty_events_returns_initial(self):
        assert rt.current_phase([]) == rt.PHASE_INITIAL

    def test_single_phase_event(self):
        events = [{"event": "phase_changed", "to_phase": "build"}]
        assert rt.current_phase(events) == "build"

    def test_returns_latest_phase(self):
        events = [
            {"event": "phase_changed", "to_phase": "design"},
            {"event": "phase_changed", "to_phase": "build"},
            {"event": "phase_changed", "to_phase": "verify"},
        ]
        assert rt.current_phase(events) == "verify"

    def test_ignores_non_phase_events(self):
        events = [
            {"event": "dispatch_blocked", "to_phase": "build"},  # wrong event type
            {"event": "evaluator_verdict", "to_phase": "closeout"},
        ]
        assert rt.current_phase(events) == rt.PHASE_INITIAL

    def test_ignores_malformed_phase_event(self):
        events = [
            {"event": "phase_changed", "to_phase": "build"},
            {"event": "phase_changed", "to_phase": "nonexistent_phase"},
        ]
        # malformed is ignored; latest valid wins
        assert rt.current_phase(events) == "build"

    def test_phase_event_without_to_phase_ignored(self):
        events = [
            {"event": "phase_changed", "to_phase": "design"},
            {"event": "phase_changed"},  # missing to_phase
        ]
        assert rt.current_phase(events) == "design"


# ===========================================================================
# _append_phase_event
# ===========================================================================

class TestAppendPhaseEvent:
    def test_appends_well_formed_event(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        rt._append_phase_event(
            "trk-test",
            from_phase=None,
            to_phase="discovery",
            triggered_by="initial",
        )
        events = rt._read_events("trk-test")
        assert len(events) == 1
        assert events[0]["event"] == "phase_changed"
        assert events[0]["from_phase"] is None
        assert events[0]["to_phase"] == "discovery"
        assert events[0]["triggered_by"] == "initial"
        assert events[0]["track_id"] == "trk-test"
        assert "timestamp" in events[0]

    def test_evidence_recorded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        rt._append_phase_event(
            "trk-test",
            from_phase="discovery",
            to_phase="design",
            evidence={"repo_facts": "ev-001", "scope": "ev-002"},
        )
        events = rt._read_events("trk-test")
        assert events[0]["evidence"] == {"repo_facts": "ev-001", "scope": "ev-002"}

    def test_rejects_unknown_phase(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        with pytest.raises(ValueError, match="unknown phase"):
            rt._append_phase_event(
                "trk-test", from_phase=None, to_phase="not_a_real_phase"
            )


# ===========================================================================
# Round-trip: append → read → fold
# ===========================================================================

class TestRoundTrip:
    def test_full_traversal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        track = "trk-traversal"
        rt._append_phase_event(track, from_phase=None, to_phase="discovery", triggered_by="initial")
        rt._append_phase_event(track, from_phase="discovery", to_phase="design")
        rt._append_phase_event(track, from_phase="design", to_phase="build")
        rt._append_phase_event(track, from_phase="build", to_phase="verify")
        rt._append_phase_event(track, from_phase="verify", to_phase="closeout")

        events = rt._read_events(track)
        assert len(events) == 5
        assert rt.current_phase(events) == "closeout"

    def test_retry_loop_visible_in_log(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        track = "trk-retry"
        rt._append_phase_event(track, from_phase="build", to_phase="verify")
        rt._append_phase_event(track, from_phase="verify", to_phase="build", triggered_by="retry")
        rt._append_phase_event(track, from_phase="build", to_phase="verify")

        events = rt._read_events(track)
        retry_events = [e for e in events if e.get("triggered_by") == "retry"]
        assert len(retry_events) == 1
        assert rt.current_phase(events) == "verify"


# ===========================================================================
# phase_transition_allowed
# ===========================================================================

class TestPhaseTransitionAllowed:
    def test_initial_entry_to_discovery_allowed(self):
        result = rt.phase_transition_allowed(None, "discovery", "R3", set())
        assert result["allowed"] is True

    def test_initial_entry_to_non_initial_blocked(self):
        result = rt.phase_transition_allowed(None, "build", "R3", set())
        assert result["allowed"] is False
        assert "initial_phase_must_be" in result["reason"]

    def test_legal_edge_with_full_evidence_allowed(self):
        result = rt.phase_transition_allowed(
            "discovery", "design", "R3",
            {"repo_facts", "scope", "constraints"},
        )
        assert result["allowed"] is True
        assert result["missing"] == []

    def test_legal_edge_missing_evidence_blocked(self):
        result = rt.phase_transition_allowed(
            "discovery", "design", "R3",
            {"repo_facts"},  # missing scope, constraints
        )
        assert result["allowed"] is False
        assert set(result["missing"]) == {"scope", "constraints"}
        assert result["reason"] == "missing_required_evidence"

    def test_illegal_edge_blocked(self):
        # build->discovery is not in the monotonic table
        result = rt.phase_transition_allowed(
            "build", "discovery", "R3", {"repo_facts", "scope", "constraints"},
        )
        assert result["allowed"] is False
        assert "illegal_edge" in result["reason"]

    def test_verify_to_build_retry_within_budget(self):
        result = rt.phase_transition_allowed(
            "verify", "build", "R3", {"introduced_failure"}, retry_count=2,
        )
        assert result["allowed"] is True

    def test_verify_to_build_retry_over_budget(self):
        result = rt.phase_transition_allowed(
            "verify", "build", "R3", {"introduced_failure"}, retry_count=3,
        )
        assert result["allowed"] is False
        assert "retry_budget_exhausted" in result["reason"]

    def test_verify_to_build_missing_evidence_blocked(self):
        result = rt.phase_transition_allowed(
            "verify", "build", "R3", set(), retry_count=0,
        )
        assert result["allowed"] is False
        assert result["missing"] == ["introduced_failure"]


# ===========================================================================
# route_change_reconcile
# ===========================================================================

class TestRouteChangeReconcile:
    def test_same_route_noop(self):
        result = rt.route_change_reconcile("R3", "R3", set())
        assert result["applies"] is False
        assert result["rule"] == "noop"

    def test_r2_to_r3_generic_stricter(self):
        result = rt.route_change_reconcile("R2", "R3", set())
        assert result["applies"] is True
        assert result["rule"] == "generic_stricter"
        assert result["dispatch"] == "PAUSE"
        # R3 doesn't add route-level required evidence, so target is the
        # first phase whose inbound evidence isn't satisfied (= design).
        assert result["target_phase"] == "design"

    def test_r3_to_r4_promotes_to_discovery_pause(self):
        # R3 task at build with most evidence — R4 promotion requires
        # threat_model + security_review, so target = discovery.
        result = rt.route_change_reconcile(
            "R3", "R4",
            {"repo_facts", "scope", "constraints", "plan_approved",
             "owned_files", "validation_plan"},
        )
        assert result["applies"] is True
        assert result["target_phase"] == "discovery"  # threat_model missing
        assert result["dispatch"] == "PAUSE"
        assert "threat_model" in result["required_backfill"]
        assert "security_review" in result["required_backfill"]

    def test_r1_to_r4_promotes_to_discovery(self):
        result = rt.route_change_reconcile("R1", "R4", set())
        assert result["applies"] is True
        assert result["target_phase"] == "discovery"
        assert result["dispatch"] == "PAUSE"

    def test_r5_to_r2_special_case_resume(self):
        result = rt.route_change_reconcile("R5", "R2", set())
        assert result["applies"] is True
        assert result["rule"] == "special_case"
        assert result["target_phase"] == "build"
        assert result["dispatch"] == "resume"
        assert result["required_backfill"] == []

    def test_r5_to_r3_special_case_pause(self):
        result = rt.route_change_reconcile("R5", "R3", set())
        assert result["applies"] is True
        assert result["rule"] == "special_case"
        assert result["target_phase"] == "discovery"
        assert result["dispatch"] == "PAUSE"
        assert "scope" in result["required_backfill"]

    def test_r5_to_r4_special_case_pause(self):
        result = rt.route_change_reconcile("R5", "R4", set())
        assert result["applies"] is True
        assert result["rule"] == "special_case"
        assert result["target_phase"] == "discovery"
        assert "threat_model" in result["required_backfill"]

    def test_r4_to_r2_downgrade_noop(self):
        # Slice 1a doesn't handle downgrades; out of scope.
        result = rt.route_change_reconcile("R4", "R2", set())
        assert result["applies"] is False
        assert result["rule"] == "noop"

    def test_invalidate_present_for_r4_promotion(self):
        result = rt.route_change_reconcile("R3", "R4", set())
        assert result["invalidate"]  # non-empty


# ===========================================================================
# REPLAYABLE_EVENTS registration
# ===========================================================================

class TestEventRegistration:
    def test_phase_changed_is_replayable(self):
        assert "phase_changed" in rt.REPLAYABLE_EVENTS


# ===========================================================================
# Backward-compat smoke: existing helpers untouched
# ===========================================================================

class TestBackwardCompatSmoke:
    """Slice 1a should not affect existing event types or fold behavior."""

    def test_append_and_read_unchanged_for_other_events(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        rt._append_event("trk-compat", {"event": "dispatch_blocked", "reason": "test"})
        events = rt._read_events("trk-compat")
        assert len(events) == 1
        assert events[0]["event"] == "dispatch_blocked"
        # No phase event => initial phase
        assert rt.current_phase(events) == rt.PHASE_INITIAL

    def test_phase_event_does_not_affect_other_event_types(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        track = "trk-mixed"
        rt._append_event(track, {"event": "dispatch_blocked", "reason": "test"})
        rt._append_phase_event(track, from_phase=None, to_phase="discovery")
        rt._append_event(track, {"event": "evaluator_verdict", "status": "ok"})
        events = rt._read_events(track)
        assert len(events) == 3
        event_types = [e["event"] for e in events]
        assert event_types == ["dispatch_blocked", "phase_changed", "evaluator_verdict"]
