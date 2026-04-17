from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "skills" / "autoconfig" / "scripts"))

import analyze_experiments
import eval_harness
import experiment_daemon


def test_r1_v3_rejects_semantic_labels_via_schema() -> None:
    benchmark = eval_harness.load_benchmark("r1_factual")
    variant = next(v for v in benchmark["variants"] if v["id"] == "r1_v3")

    acceptance = eval_harness.check_acceptance(
        benchmark,
        variant,
        variant["acceptance_checks"],
        json.dumps(
            {
                "execution_shapes": {
                    "single_lane": ["coordinator", "worker"],
                    "bounded_swarm": ["planner", "reviewer"],
                }
            }
        ),
        workspace=None,
        context_root=Path("/tmp"),
    )

    assert acceptance["pass_rate"] == 0.0
    assert "must be one of" in acceptance["details"][0]["message"]


def test_compute_phase3_readiness_prefers_calibration_source(monkeypatch) -> None:
    def fake_recent(benchmark_id, limit, evaluation_version, run_mode=None):
        assert benchmark_id == "r3_feature"
        if run_mode == experiment_daemon.RUN_MODE_PHASE3_CALIBRATION:
            return (
                [{"variant_id": "r3_v1", "completed_cleanly": True, "deterministic_gate_passed": True} for _ in range(5)]
                + [{"variant_id": "r3_v2", "completed_cleanly": True, "deterministic_gate_passed": True} for _ in range(5)]
                + [{"variant_id": "r3_v3", "completed_cleanly": True, "deterministic_gate_passed": True} for _ in range(5)]
            )
        return []

    monkeypatch.setattr(
        experiment_daemon,
        "get_recent_benchmark_results",
        fake_recent,
    )
    monkeypatch.setattr(
        experiment_daemon,
        "load_benchmark",
        lambda _benchmark_id: {
            "variants": [{"id": "r3_v1"}, {"id": "r3_v2"}, {"id": "r3_v3"}]
        },
    )

    readiness = experiment_daemon._compute_phase3_readiness(
        "v5_1_variant_gated_calibration"
    )

    assert readiness["ready"] is True
    assert readiness["source"] == experiment_daemon.RUN_MODE_PHASE3_CALIBRATION
    assert readiness["variants"]["r3_v2"]["sample_count"] == 5


def test_select_phase3_calibration_variant_balances_counts(monkeypatch) -> None:
    monkeypatch.setattr(
        experiment_daemon,
        "load_benchmark",
        lambda _benchmark_id: {
            "variants": [{"id": "r3_v1"}, {"id": "r3_v2"}, {"id": "r3_v3"}]
        },
    )
    monkeypatch.setattr(
        experiment_daemon,
        "get_recent_benchmark_results",
        lambda *args, **kwargs: [
            {"variant_id": "r3_v1"},
            {"variant_id": "r3_v1"},
            {"variant_id": "r3_v2"},
        ],
    )

    assert (
        experiment_daemon._select_phase3_calibration_variant(
            "v5_1_variant_gated_calibration"
        )
        == "r3_v3"
    )


def test_run_phase3_calibration_sample_uses_fixed_variant_and_marks_completion(
    monkeypatch,
) -> None:
    captured_start: dict = {}
    captured_result: dict = {}
    updated: list[dict] = []

    monkeypatch.setattr(
        experiment_daemon,
        "get_program_state",
        lambda: {
            "evaluation_version": "v5_1_variant_gated_calibration",
            "phase_readiness": {},
            "run_mode": experiment_daemon.RUN_MODE_PHASE3_CALIBRATION,
            "status": "running",
        },
    )
    monkeypatch.setattr(
        experiment_daemon,
        "_calibration_window_complete",
        lambda _evaluation_version: False,
    )
    monkeypatch.setattr(
        experiment_daemon,
        "_select_phase3_calibration_variant",
        lambda _evaluation_version: "r3_v2",
    )
    monkeypatch.setattr(
        experiment_daemon,
        "log_experiment_start",
        lambda **kwargs: captured_start.update(kwargs) or 999,
    )
    monkeypatch.setattr(
        experiment_daemon,
        "run_full_suite",
        lambda benchmark_ids, variant_overrides=None: [
            {
                "benchmark_id": "r3_feature",
                "variant_id": variant_overrides["r3_feature"],
                "completed_cleanly": True,
                "deterministic_gate_passed": True,
                "wall_time_seconds": 12.0,
                "error": None,
            }
        ],
    )
    monkeypatch.setattr(
        experiment_daemon,
        "refresh_phase_readiness",
        lambda _evaluation_version, _recent_results=None: {
            "3": {
                "ready": False,
                "reason": "blocked: have 1",
                "window": 15,
                "variants": {},
            }
        },
    )
    monkeypatch.setattr(
        experiment_daemon,
        "_attach_phase_readiness_snapshot",
        lambda results, readiness: [
            result.update({"phase_readiness_snapshot": readiness["3"]})
            for result in results
        ],
    )
    monkeypatch.setattr(
        experiment_daemon,
        "evaluate_experiment",
        lambda **_kwargs: {
            "scores": {"composite": 80.0, "quality": 82.0, "speed": 74.0}
        },
    )
    monkeypatch.setattr(
        experiment_daemon,
        "log_experiment_result",
        lambda **kwargs: captured_result.update(kwargs),
    )
    monkeypatch.setattr(experiment_daemon, "record_success", lambda: None)
    monkeypatch.setattr(
        experiment_daemon,
        "get_experiment_count",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        experiment_daemon,
        "update_program_state",
        lambda **kwargs: updated.append(kwargs),
    )

    result = experiment_daemon.run_phase3_calibration_sample()

    assert result["status"] == "calibration_sampled"
    assert captured_start["run_mode"] == experiment_daemon.RUN_MODE_PHASE3_CALIBRATION
    assert captured_start["calibration_sample"] == 1
    assert captured_result["decision"] == "calibration"
    assert captured_result["kept"] == 0
    assert any(update.get("current_phase") == 3 for update in updated)


def test_main_exits_immediately_when_program_state_completed(monkeypatch) -> None:
    updates: list[dict] = []

    monkeypatch.setattr(experiment_daemon, "setup_logging", lambda: None)
    monkeypatch.setattr(experiment_daemon.signal, "signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(experiment_daemon, "crash_recovery", lambda: None)
    monkeypatch.setattr(experiment_daemon, "snapshot_exists", lambda _name: True)
    monkeypatch.setattr(
        experiment_daemon,
        "get_program_state",
        lambda: {
            "status": "completed",
            "run_mode": experiment_daemon.RUN_MODE_SEARCH,
            "terminal_reason": "phase_1_converged",
        },
    )
    monkeypatch.setattr(
        experiment_daemon,
        "update_program_state",
        lambda **kwargs: updates.append(kwargs),
    )

    experiment_daemon.main()

    assert updates == []


def test_knob_attribution_uses_run_mode_filter(monkeypatch) -> None:
    monkeypatch.setattr(
        analyze_experiments,
        "get_kept_experiments",
        lambda evaluation_version=None, run_mode=None: (
            [{"mutation_summary": "worker model R2: a -> b", "improvement_pct": 3.0}]
            if run_mode == "search"
            else []
        ),
    )

    assert analyze_experiments.get_knob_attribution("v5", run_mode="search") == [
        {"knob": "model", "total_improvement": 3.0, "experiments_kept": 1}
    ]
    assert analyze_experiments.get_knob_attribution("v5", run_mode="phase3_calibration") == []


def test_compute_phase3_readiness_blocks_on_failing_variant(monkeypatch) -> None:
    monkeypatch.setattr(
        experiment_daemon,
        "load_benchmark",
        lambda _benchmark_id: {
            "variants": [{"id": "r3_v1"}, {"id": "r3_v2"}, {"id": "r3_v3"}]
        },
    )
    monkeypatch.setattr(
        experiment_daemon,
        "get_recent_benchmark_results",
        lambda *_args, **_kwargs: (
            [{"variant_id": "r3_v1", "completed_cleanly": True, "deterministic_gate_passed": True} for _ in range(5)]
            + [{"variant_id": "r3_v2", "completed_cleanly": False, "deterministic_gate_passed": False} for _ in range(2)]
            + [{"variant_id": "r3_v2", "completed_cleanly": True, "deterministic_gate_passed": True} for _ in range(3)]
            + [{"variant_id": "r3_v3", "completed_cleanly": True, "deterministic_gate_passed": True} for _ in range(5)]
        ),
    )

    readiness = experiment_daemon._compute_phase3_readiness(
        "v5_1_variant_gated_calibration"
    )

    assert readiness["ready"] is False
    assert "r3_v2 clean=0.60 gate=0.60 below threshold" in readiness["reason"]
    assert readiness["variants"]["r3_v2"]["ready"] is False


def test_calibration_window_complete_requires_five_per_variant(monkeypatch) -> None:
    monkeypatch.setattr(
        experiment_daemon,
        "load_benchmark",
        lambda _benchmark_id: {
            "variants": [{"id": "r3_v1"}, {"id": "r3_v2"}, {"id": "r3_v3"}]
        },
    )
    monkeypatch.setattr(
        experiment_daemon,
        "get_recent_benchmark_results",
        lambda *_args, **_kwargs: (
            [{"variant_id": "r3_v1"} for _ in range(5)]
            + [{"variant_id": "r3_v2"} for _ in range(4)]
            + [{"variant_id": "r3_v3"} for _ in range(6)]
        ),
    )

    assert experiment_daemon._calibration_window_complete("v5_1_variant_gated_calibration") is False


def test_execute_benchmark_attempt_honors_variant_workspace_template(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        eval_harness,
        "prepare_workspace",
        lambda benchmark: captured.update({"workspace_template": benchmark.get("workspace_template")}) or Path("/tmp/fake-workspace"),
    )
    monkeypatch.setattr(
        eval_harness,
        "run_benchmark",
        lambda _benchmark, _variant, _workspace=None: {
            "benchmark_id": "r3_feature",
            "variant_id": "r3_v2",
            "output": "ok",
            "wall_time_seconds": 1.0,
            "agent": "worker",
            "model": "claude-opus-4-6",
            "effort": "medium",
            "timed_out": False,
            "error": None,
            "exit_code": 0,
            "terminal_state": "completed",
            "completed_cleanly": True,
            "benchmark_home": None,
        },
    )
    monkeypatch.setattr(
        eval_harness,
        "check_acceptance",
        lambda *_args, **_kwargs: {
            "passed": 1,
            "failed": 0,
            "total": 1,
            "pass_rate": 1.0,
            "details": [],
        },
    )
    monkeypatch.setattr(
        eval_harness,
        "_run_semantic_judge",
        lambda *_args, **_kwargs: {
            "score": 100.0,
            "summary": "ok",
            "missed_expectations": [],
            "issues": [],
        },
    )
    monkeypatch.setattr(
        eval_harness,
        "_classify_terminal_state",
        lambda _output, _timed_out, _exit_code, _error: ("completed", True),
    )
    monkeypatch.setattr(
        eval_harness,
        "_resolve_speed_baseline_seconds",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(eval_harness, "cleanup_workspace", lambda _workspace: None)

    result = eval_harness._execute_benchmark_attempt(
        {
            "id": "r3_feature",
            "output": "ok",
            "route": "R3",
            "workspace_template": "r3_workspace",
            "judge_profile": "implementation_review",
        },
        {
            "id": "r3_v2",
            "workspace_template": "r3_workspace_mounted_users",
            "acceptance_checks": [],
        },
    )

    assert captured["workspace_template"] == "r3_workspace_mounted_users"
    assert result["terminal_state"] == "completed"
