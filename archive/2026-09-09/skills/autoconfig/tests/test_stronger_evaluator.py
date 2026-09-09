from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "skills" / "autoconfig" / "scripts"))

import eval_harness
import experiment_daemon
import score_experiment


def test_error_max_turns_is_not_completed_cleanly() -> None:
    payload = {"type": "result", "subtype": "error_max_turns"}
    terminal_state, completed_cleanly = eval_harness._classify_terminal_state(
        payload,
        timed_out=False,
        error=None,
        exit_code=0,
        output_text=json.dumps(payload),
    )

    assert terminal_state == "error_max_turns"
    assert completed_cleanly is False


def test_output_json_schema_rejects_keyword_only_output() -> None:
    benchmark = eval_harness.load_benchmark("r4_auth_review")
    variant = next(v for v in benchmark["variants"] if v["id"] == "r4_v1")

    acceptance = eval_harness.check_acceptance(
        benchmark,
        variant,
        variant["acceptance_checks"],
        '{"findings":"severity remediation"}',
        workspace=None,
        context_root=Path("/tmp"),
    )

    assert acceptance["pass_rate"] == 0.0
    assert "must be an array" in acceptance["details"][0]["message"]


def test_truth_match_uses_runtime_snapshot(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "agents").mkdir()
    (tmp_path / "state" / "route_manifest.json").write_text(
        json.dumps({"rules": []}),
        encoding="utf-8",
    )
    for name, sandbox in {
        "planner": "read-only",
        "worker": "workspace-write",
    }.items():
        (tmp_path / "agents" / f"{name}.md").write_text(
            f"---\nsandbox: {sandbox}\n---\n",
            encoding="utf-8",
        )

    variant = {
        "truth_extractor": {"type": "agent_roles"},
        "output_schema": {
            "type": "object",
            "required": ["roles"],
            "properties": {"roles": {"type": "array"}},
            "additionalProperties": False,
        },
        "acceptance_checks": [{"type": "truth_match"}],
    }

    acceptance = eval_harness.check_acceptance(
        benchmark={},
        variant=variant,
        checks=variant["acceptance_checks"],
        output=json.dumps(
            {
                "roles": [
                    {"name": "planner", "sandbox": "read-only"},
                    {"name": "worker", "sandbox": "workspace-write"},
                ]
            }
        ),
        workspace=None,
        context_root=tmp_path,
    )

    assert acceptance["pass_rate"] == 1.0


def test_failed_deterministic_gate_zeroes_quality_even_with_semantic_score() -> None:
    result = score_experiment.compute_composite(
        [
            {
                "benchmark_id": "r3_feature",
                "route": "R3",
                "acceptance": {"pass_rate": 1.0},
                "wall_time_seconds": 80.0,
                "completed_cleanly": False,
                "deterministic_gate_passed": False,
                "deterministic_quality_score": 100.0,
                "semantic_quality_score": 95.0,
                "exit_code": 0,
                "timed_out": False,
                "error": None,
            }
        ]
    )

    assert result["quality"] == 0.0
    assert result["per_benchmark"]["r3_feature"]["quality"] == 0.0


def test_evaluate_experiment_requires_confirmation_for_clear_improvement() -> None:
    evaluation = score_experiment.evaluate_experiment(
        current_results=[
            {
                "benchmark_id": "r2_small_impl",
                "route": "R2",
                "acceptance": {"pass_rate": 1.0},
                "wall_time_seconds": 1.0,
                "completed_cleanly": True,
                "deterministic_gate_passed": True,
                "deterministic_quality_score": 100.0,
                "semantic_quality_score": None,
                "exit_code": 0,
                "timed_out": False,
                "error": None,
            }
        ],
        baseline_score=10.0,
    )

    assert evaluation["classification"] == "clear"
    assert evaluation["decision"] == "needs_confirmation"


def test_advance_phase_completes_after_last_enabled_phase(monkeypatch) -> None:
    monkeypatch.setattr(
        experiment_daemon,
        "get_program_state",
        lambda: {"enabled_phases": [1, 3]},
    )
    captured: dict = {}
    monkeypatch.setattr(
        experiment_daemon,
        "update_program_state",
        lambda **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(
        experiment_daemon,
        "get_experiment_count",
        lambda *_args, **_kwargs: 12,
    )
    monkeypatch.setattr(
        experiment_daemon,
        "get_total_kept",
        lambda *_args, **_kwargs: 3,
    )
    monkeypatch.setattr(
        experiment_daemon,
        "get_cumulative_improvement",
        lambda *_args, **_kwargs: 9.5,
    )

    result = experiment_daemon.advance_phase(3)

    assert result["status"] == "run_completed"
    assert captured["status"] == "completed"
    assert captured["last_completed_phase"] == 3
