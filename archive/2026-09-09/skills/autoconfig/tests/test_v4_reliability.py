from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "skills" / "autoconfig" / "scripts"))

import eval_harness
import experiment_daemon


def test_parse_output_json_recovers_wrapped_json() -> None:
    parsed, error = eval_harness._parse_output_json(
        'Here is the review.\n{"findings":[{"severity":"HIGH","title":"Hardcoded secret","description":"secret in config","impact":"token forgery","remediation":"move to env"}],"summary":"Risk exists","overall_risk":"HIGH"}\nThanks.'
    )

    assert error is None
    assert isinstance(parsed, dict)
    assert parsed["overall_risk"] == "HIGH"


def test_output_json_schema_accepts_recoverable_wrapped_json() -> None:
    benchmark = eval_harness.load_benchmark("r4_auth_review")
    variant = next(v for v in benchmark["variants"] if v["id"] == "r4_v1")

    wrapped = (
        "Security review follows.\n"
        "{\"findings\":[{\"severity\":\"HIGH\",\"title\":\"Hardcoded secret\","
        "\"description\":\"JWT secret is hardcoded\",\"impact\":\"Tokens can be forged\","
        "\"remediation\":\"Load the secret from environment\"},"
        "{\"severity\":\"MEDIUM\",\"title\":\"Weak hashing\","
        "\"description\":\"bcrypt rounds are too low\",\"impact\":\"Passwords are easier to crack\","
        "\"remediation\":\"Increase bcrypt cost\"}],"
        "\"summary\":\"The auth surface has multiple meaningful issues.\","
        "\"overall_risk\":\"HIGH\"}\n"
        "End of review."
    )

    acceptance = eval_harness.check_acceptance(
        benchmark,
        variant,
        variant["acceptance_checks"],
        wrapped,
        workspace=None,
        context_root=Path("/tmp"),
    )

    assert acceptance["pass_rate"] == 1.0


def test_execute_benchmark_with_retry_retries_once_on_incomplete_terminal_failure(
    monkeypatch,
) -> None:
    attempts = iter(
        [
            {
                "benchmark_id": "r3_feature",
                "variant_id": "r3_v1",
                "route": "R3",
                "output": "",
                "wall_time_seconds": 10.0,
                "acceptance": {"pass_rate": 0.0},
                "exit_code": 0,
                "speed_baseline_seconds": 120.0,
                "terminal_state": "error_max_turns",
                "completed_cleanly": False,
                "deterministic_gate_passed": False,
                "deterministic_quality_score": 0.0,
                "semantic_quality_score": None,
                "judge_summary": None,
                "judge_flags": None,
                "judge_failures": None,
                "timed_out": False,
                "error": "max turns",
                "agent": "worker",
                "model": "claude-sonnet-4-6",
                "effort": "medium",
                "retryable_benchmark_failure": True,
                "benchmark_retry_count": 0,
                "retry_count": 0,
            },
            {
                "benchmark_id": "r3_feature",
                "variant_id": "r3_v1",
                "route": "R3",
                "output": "ok",
                "wall_time_seconds": 8.0,
                "acceptance": {"pass_rate": 1.0},
                "exit_code": 0,
                "speed_baseline_seconds": 120.0,
                "terminal_state": "completed",
                "completed_cleanly": True,
                "deterministic_gate_passed": True,
                "deterministic_quality_score": 100.0,
                "semantic_quality_score": 92.0,
                "judge_summary": "good",
                "judge_flags": [],
                "judge_failures": [],
                "timed_out": False,
                "error": None,
                "agent": "worker",
                "model": "claude-sonnet-4-6",
                "effort": "medium",
                "retryable_benchmark_failure": False,
                "benchmark_retry_count": 0,
                "retry_count": 0,
            },
        ]
    )

    monkeypatch.setattr(
        eval_harness,
        "_execute_benchmark_attempt",
        lambda _benchmark, _variant: next(attempts),
    )

    result = eval_harness._execute_benchmark_with_retry(
        {"retry_on_incomplete_terminal_state": True},
        {"id": "r3_v1"},
    )

    assert result["completed_cleanly"] is True
    assert result["benchmark_retry_count"] == 1
    assert result["retry_count"] == 1
    assert result["retry_history"][0]["terminal_state"] == "error_max_turns"


def test_compute_phase3_readiness_uses_window_and_thresholds(monkeypatch) -> None:
    monkeypatch.setattr(
        experiment_daemon,
        "get_recent_benchmark_results",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        experiment_daemon,
        "load_benchmark",
        lambda _benchmark_id: {
            "variants": [{"id": "r3_v1"}, {"id": "r3_v2"}, {"id": "r3_v3"}]
        },
    )
    blocked = experiment_daemon._compute_phase3_readiness(
        "v5_1_variant_gated_calibration",
        recent_results=[
            {
                "variant_id": "r3_v1",
                "completed_cleanly": True,
                "deterministic_gate_passed": True,
            }
            for _ in range(4)
        ],
    )
    ready = experiment_daemon._compute_phase3_readiness(
        "v5_1_variant_gated_calibration",
        recent_results=[
            {
                "variant_id": "r3_v1",
                "completed_cleanly": True,
                "deterministic_gate_passed": True,
            }
            for _ in range(5)
        ]
        + [
            {
                "variant_id": "r3_v2",
                "completed_cleanly": True,
                "deterministic_gate_passed": True,
            }
            for _ in range(5)
        ]
        + [
            {
                "variant_id": "r3_v3",
                "completed_cleanly": True,
                "deterministic_gate_passed": False,
            },
            {
                "variant_id": "r3_v3",
                "completed_cleanly": True,
                "deterministic_gate_passed": True,
            },
            {
                "variant_id": "r3_v3",
                "completed_cleanly": True,
                "deterministic_gate_passed": True,
            },
            {
                "variant_id": "r3_v3",
                "completed_cleanly": True,
                "deterministic_gate_passed": True,
            },
            {
                "variant_id": "r3_v3",
                "completed_cleanly": True,
                "deterministic_gate_passed": True,
            },
        ],
    )

    assert blocked["ready"] is False
    assert "need 15 calibration samples" in blocked["reason"]
    assert ready["ready"] is True
    assert ready["clean_completion_rate"] == 1.0
    assert ready["deterministic_gate_pass_rate"] == round(14 / 15, 4)


def test_advance_phase_blocks_when_phase3_not_ready(monkeypatch) -> None:
    monkeypatch.setattr(
        experiment_daemon,
        "get_program_state",
        lambda: {
            "enabled_phases": [1, 3],
            "evaluation_version": "v4_reliability_first",
        },
    )
    monkeypatch.setattr(experiment_daemon, "get_experiment_count", lambda *_args: 5)
    monkeypatch.setattr(experiment_daemon, "get_total_kept", lambda *_args: 1)
    monkeypatch.setattr(
        experiment_daemon,
        "get_cumulative_improvement",
        lambda *_args: 4.2,
    )
    monkeypatch.setattr(
        experiment_daemon,
        "_phase_ready",
        lambda *_args, **_kwargs: (
            False,
            "blocked: need 10 recent r3_feature runs for calibration, have 0",
            {"3": {"ready": False, "reason": "blocked: need 10 recent r3_feature runs for calibration, have 0"}},
        ),
    )
    captured: dict = {}
    monkeypatch.setattr(
        experiment_daemon,
        "update_program_state",
        lambda **kwargs: captured.update(kwargs),
    )

    result = experiment_daemon.advance_phase(1)

    assert result["status"] == "run_completed"
    assert result["terminal_reason"] == "phase_3_blocked"
    assert captured["status"] == "completed"
    assert captured["phase_3_blocked_reason"].startswith("blocked:")


def test_confirmation_trials_accept_two_clean_trials(monkeypatch) -> None:
    trial_results = iter(
        [
            [
                {
                    "benchmark_id": "r3_feature",
                    "completed_cleanly": True,
                    "benchmark_retry_count": 1,
                    "wall_time_seconds": 10.0,
                }
            ],
            [
                {
                    "benchmark_id": "r3_feature",
                    "completed_cleanly": False,
                    "benchmark_retry_count": 0,
                    "wall_time_seconds": 20.0,
                }
            ],
        ]
    )
    eval_results = iter(
        [
            {
                "scores": {"composite": 96.0, "quality": 97.0, "speed": 80.0},
                "improvement_pct": 6.0,
            },
            {
                "scores": {"composite": 94.0, "quality": 83.0, "speed": 78.0},
                "improvement_pct": 4.0,
            },
        ]
    )

    monkeypatch.setattr(experiment_daemon, "restore_snapshot", lambda *_args: True)
    monkeypatch.setattr(experiment_daemon, "apply_mutation", lambda *_args: None)
    monkeypatch.setattr(
        experiment_daemon,
        "run_full_suite",
        lambda _benchmark_ids: next(trial_results),
    )
    monkeypatch.setattr(
        experiment_daemon,
        "evaluate_experiment",
        lambda **_kwargs: next(eval_results),
    )

    confirmation = experiment_daemon.run_confirmation_trials(
        mutation={"summary": "test mutation"},
        baseline_score=90.0,
        benchmark_ids=["r3_feature"],
        initial_benchmark_results=[
            {
                "benchmark_id": "r3_feature",
                "completed_cleanly": True,
                "benchmark_retry_count": 0,
                "wall_time_seconds": 9.0,
            }
        ],
        initial_eval_result={
            "scores": {"composite": 95.0, "quality": 95.0, "speed": 79.0},
            "improvement_pct": 5.0,
        },
    )

    assert confirmation["confirmed"] is True
    assert confirmation["clean_trial_count"] == 2
    assert confirmation["trials"][1]["benchmark_retry_count"] == 1
