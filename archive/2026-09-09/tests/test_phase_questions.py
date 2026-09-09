"""Slice 1b — question selection + decision_record + cycle/track summary tests.

Tests the primitives added to ~/.claude/bin/auto_runtime_common.py for the
question registry, canonical-state decision records, and L2-validation
event emission (cycle_summary, track_summary).

Plan ref: ~/.codex-spar/stage-aware-orchestrator-loop/plan-final.md §3 §7
Observable decision kinds only (per Slice 1b grounding):
    phase, route, next_action, owned_files
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.home() / ".claude" / "bin"))
import auto_runtime_common as rt  # noqa: E402


# ===========================================================================
# canonical_state
# ===========================================================================

class TestCanonicalState:
    def test_phase_valid(self):
        assert rt.canonical_state("phase", "build") == "build"

    def test_phase_invalid_returns_none(self):
        assert rt.canonical_state("phase", "not_a_phase") is None

    def test_route_valid(self):
        assert rt.canonical_state("route", "R3") == "R3"

    def test_route_r5_allowed(self):
        assert rt.canonical_state("route", "R5") == "R5"

    def test_next_action_full(self):
        state = rt.canonical_state("next_action", {
            "action_kind": "dispatch", "target_ref": "slice-1",
        })
        assert state == {"action_kind": "dispatch", "target_ref": "slice-1"}

    def test_next_action_none(self):
        assert rt.canonical_state("next_action", None) is None

    def test_owned_files_sorts(self):
        result = rt.canonical_state("owned_files", ["b.py", "a.py", "c.py"])
        assert result == ["a.py", "b.py", "c.py"]

    def test_owned_files_none_returns_empty(self):
        assert rt.canonical_state("owned_files", None) == []

    def test_non_observable_kind_raises(self):
        with pytest.raises(ValueError, match="non-observable"):
            rt.canonical_state("scope", {"foo": "bar"})


# ===========================================================================
# state_hash
# ===========================================================================

class TestStateHash:
    def test_same_state_same_hash(self):
        a = rt.state_hash({"x": [1, 2, 3]})
        b = rt.state_hash({"x": [1, 2, 3]})
        assert a == b

    def test_order_independent_for_dict_keys(self):
        a = rt.state_hash({"x": 1, "y": 2})
        b = rt.state_hash({"y": 2, "x": 1})
        assert a == b

    def test_different_state_different_hash(self):
        a = rt.state_hash(["a", "b"])
        b = rt.state_hash(["a", "c"])
        assert a != b


# ===========================================================================
# _append_decision_record
# ===========================================================================

class TestAppendDecisionRecord:
    def test_changed_true_when_state_differs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        event = rt._append_decision_record(
            "trk-dr",
            decision_kind="owned_files",
            before_state=["a.py"],
            after_state=["a.py", "b.py"],
        )
        assert event["changed"] is True
        assert event["before_state_hash"] != event["after_state_hash"]

    def test_changed_false_when_state_equal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        event = rt._append_decision_record(
            "trk-dr",
            decision_kind="phase",
            before_state="build",
            after_state="build",
        )
        assert event["changed"] is False

    def test_changed_false_with_question_requires_no_change_reason(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        with pytest.raises(ValueError, match="no_change_reason"):
            rt._append_decision_record(
                "trk-dr",
                decision_kind="phase",
                before_state="build",
                after_state="build",
                triggered_by_question_id="simplest_path",
                no_change_reason=None,
            )

    def test_changed_false_with_question_and_reason_ok(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        event = rt._append_decision_record(
            "trk-dr",
            decision_kind="phase",
            before_state="build",
            after_state="build",
            triggered_by_question_id="simplest_path",
            no_change_reason="state_unchanged_after_dispatch",
        )
        assert event["changed"] is False
        assert event["no_change_reason"] == "state_unchanged_after_dispatch"

    def test_non_observable_kind_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        with pytest.raises(ValueError, match="non-observable"):
            rt._append_decision_record(
                "trk-dr", decision_kind="scope",
                before_state={}, after_state={},
            )

    def test_event_is_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        rt._append_decision_record(
            "trk-dr",
            decision_kind="route",
            before_state="R2",
            after_state="R3",
        )
        events = rt._read_events("trk-dr")
        decision_events = [e for e in events if e.get("event") == "decision_record"]
        assert len(decision_events) == 1
        assert decision_events[0]["decision_kind"] == "route"
        assert decision_events[0]["changed"] is True


# ===========================================================================
# select_phase_questions
# ===========================================================================

class TestSelectPhaseQuestions:
    def test_r1_bypassed_completely(self):
        assert rt.select_phase_questions("build", "R1") == []
        assert rt.select_phase_questions("discovery", "R1") == []

    def test_r5_gets_only_loop_invariant(self):
        # R5 (unresolved): phase questions skipped, only invariant
        questions = rt.select_phase_questions("discovery", "R5")
        ids = [q["id"] for q in questions]
        assert "prior_art" not in ids  # phase question — skipped for R5
        # loop_invariant questions in default registry: premise_check skips R1 only
        assert "premise_check" in ids

    def test_r3_build_phase_includes_both_phase_and_invariant(self):
        questions = rt.select_phase_questions("build", "R3")
        ids = [q["id"] for q in questions]
        assert "simplest_path" in ids
        assert "premise_check" in ids

    def test_r3_discovery_phase(self):
        questions = rt.select_phase_questions("discovery", "R3")
        ids = [q["id"] for q in questions]
        assert "prior_art" in ids
        assert "premise_check" in ids
        assert "simplest_path" not in ids  # wrong phase

    def test_unknown_phase_yields_only_invariant(self):
        questions = rt.select_phase_questions("closeout", "R3")
        # closeout not in default registry's phases → only invariant
        ids = [q["id"] for q in questions]
        assert "premise_check" in ids
        assert "simplest_path" not in ids

    def test_invariant_can_be_disabled(self):
        questions = rt.select_phase_questions("build", "R3", fire_invariant=False)
        ids = [q["id"] for q in questions]
        assert "simplest_path" in ids
        assert "premise_check" not in ids


# ===========================================================================
# _append_question_selection
# ===========================================================================

class TestAppendQuestionSelection:
    def test_records_selected_question_ids(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        questions = rt.select_phase_questions("build", "R3")
        rt._append_question_selection(
            "trk-q", phase="build", route="R3", questions=questions,
        )
        events = rt._read_events("trk-q")
        qs_events = [e for e in events if e.get("event") == "question_selection"]
        assert len(qs_events) == 1
        assert "simplest_path" in qs_events[0]["question_ids"]
        assert qs_events[0]["phase"] == "build"
        assert qs_events[0]["route"] == "R3"

    def test_empty_for_r1(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        questions = rt.select_phase_questions("build", "R1")
        assert questions == []
        # Don't even emit an event for R1 — caller's responsibility


# ===========================================================================
# cycle_summary / track_summary emission
# ===========================================================================

class TestCycleSummaryEmission:
    def test_emit_cycle_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        rt._emit_cycle_summary(
            "trk-cs",
            cycle_idx=0,
            route="R3",
            recommended_action="dispatch",
            action_status="dispatched",
            questions_fired=["simplest_path"],
            decisions_recorded=[{"kind": "owned_files", "changed": True}],
            phase_at_start="build",
            phase_at_end="build",
        )
        events = rt._read_events("trk-cs")
        cs = [e for e in events if e.get("event") == "cycle_summary"]
        assert len(cs) == 1
        assert cs[0]["questions_fired"] == ["simplest_path"]
        assert cs[0]["decisions_recorded"][0]["kind"] == "owned_files"
        # Token/wall-clock nulls — provided by Claude Code layer
        assert cs[0]["tokens_in"] is None
        assert cs[0]["wall_clock_ms"] is None

    def test_emit_track_summary_aggregates(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        track = "trk-ts"
        # Synthetic history
        rt._append_phase_event(track, from_phase=None, to_phase="discovery", triggered_by="initial")
        rt._append_phase_event(track, from_phase="discovery", to_phase="build")
        rt._append_question_selection(track, phase="build", route="R3", questions=[
            {"id": "simplest_path", "targets_decision_kind": "owned_files"},
        ])
        rt._append_decision_record(
            track, decision_kind="owned_files",
            before_state=[], after_state=["a.py"],
        )
        rt._emit_cycle_summary(
            track, cycle_idx=0, route="R3",
            recommended_action="dispatch", action_status="dispatched",
        )

        rt._emit_track_summary(track, closure_state="completed")
        events = rt._read_events(track)
        ts = [e for e in events if e.get("event") == "track_summary"]
        assert len(ts) == 1
        assert ts[0]["closure_state"] == "completed"
        assert ts[0]["cycle_count"] == 1
        assert ts[0]["phases_visited"] == ["discovery", "build"]
        assert ts[0]["question_selection_count"] == 1
        assert ts[0]["decision_record_count"] == 1
        assert ts[0]["decision_kinds_changed"] == ["owned_files"]


# ===========================================================================
# Replayable events registration
# ===========================================================================

class TestEventRegistration:
    @pytest.mark.parametrize("event_name", [
        "question_selection",
        "decision_record",
        "cycle_summary",
        "track_summary",
    ])
    def test_event_is_replayable(self, event_name):
        assert event_name in rt.REPLAYABLE_EVENTS


# ===========================================================================
# Gameability resistance: ship-gate semantics
# ===========================================================================

class TestGameabilityResistance:
    """Plan-final §3 anti-gaming guarantees.

    Implementation that always emits changed=True must fail when the
    actual state is unchanged. Implementation that always emits changed=False
    must fail when state differs. Hash equality is deterministic.
    """

    def test_always_true_implementation_fails_on_equal_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        # If an impl tried to emit changed=True with equal states, the
        # auto-computed `changed` would still be False (derived from hashes).
        event = rt._append_decision_record(
            "trk-game",
            decision_kind="owned_files",
            before_state=["a.py"],
            after_state=["a.py"],
            no_change_reason="state_unchanged",  # any reason
        )
        assert event["changed"] is False  # cannot fake

    def test_always_false_implementation_fails_on_differing_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "AUTONOMY_DIR", tmp_path / "autonomy")
        event = rt._append_decision_record(
            "trk-game",
            decision_kind="owned_files",
            before_state=["a.py"],
            after_state=["b.py"],
        )
        assert event["changed"] is True  # cannot fake


# ===========================================================================
# Backward-compat: existing events still flow through
# ===========================================================================

class TestBackwardCompat:
    def test_non_observable_helpers_intact(self):
        # Slice 1a primitives still present and working
        assert rt.PHASE_INITIAL == "discovery"
        assert "build" in rt.PHASE_ENUM
        # phase_transition_allowed still works
        result = rt.phase_transition_allowed(None, "discovery", "R3", set())
        assert result["allowed"] is True

    def test_inline_registry_present(self):
        assert "phases" in rt.QUESTION_REGISTRY_INLINE
        assert "loop_invariant" in rt.QUESTION_REGISTRY_INLINE
        assert rt.QUESTION_REGISTRY_INLINE["registry_version"].startswith("inline-")
