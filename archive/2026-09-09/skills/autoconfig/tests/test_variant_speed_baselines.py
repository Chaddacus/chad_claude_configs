from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.home() / ".claude" / "skills" / "autoconfig" / "scripts"))

import eval_harness
import score_experiment


def _result(
    *,
    benchmark_id: str = "r2_small_impl",
    route: str = "R2",
    wall_time_seconds: float = 55.0,
    speed_baseline_seconds: float | None = None,
    timed_out: bool = False,
    error: str | None = None,
    exit_code: int | None = 0,
) -> dict:
    result = {
        "benchmark_id": benchmark_id,
        "route": route,
        "acceptance": {"pass_rate": 1.0},
        "wall_time_seconds": wall_time_seconds,
        "timed_out": timed_out,
        "error": error,
        "exit_code": exit_code,
    }
    if speed_baseline_seconds is not None:
        result["speed_baseline_seconds"] = speed_baseline_seconds
    return result


def test_variant_speed_baselines_change_speed_for_same_runtime() -> None:
    r2_v1 = score_experiment.compute_composite(
        [_result(speed_baseline_seconds=58.0)]
    )
    r2_v2 = score_experiment.compute_composite(
        [_result(speed_baseline_seconds=55.0)]
    )
    r2_v3 = score_experiment.compute_composite(
        [_result(speed_baseline_seconds=46.0)]
    )

    assert (
        r2_v1["per_benchmark"]["r2_small_impl"]["speed"]
        > r2_v2["per_benchmark"]["r2_small_impl"]["speed"]
        > r2_v3["per_benchmark"]["r2_small_impl"]["speed"]
    )


def test_result_baseline_beats_override_then_global_default() -> None:
    with_result_level = score_experiment.compute_composite(
        [_result(speed_baseline_seconds=46.0)],
        baseline_times={"r2_small_impl": 72.0},
    )
    with_override = score_experiment.compute_composite(
        [_result()],
        baseline_times={"r2_small_impl": 72.0},
    )
    with_default = score_experiment.compute_composite([_result()])

    assert (
        with_result_level["per_benchmark"]["r2_small_impl"][
            "speed_baseline_seconds"
        ]
        == 46.0
    )
    assert (
        with_override["per_benchmark"]["r2_small_impl"][
            "speed_baseline_seconds"
        ]
        == 72.0
    )
    assert (
        with_default["per_benchmark"]["r2_small_impl"][
            "speed_baseline_seconds"
        ]
        == 60.0
    )


def test_failed_runs_keep_zero_speed_even_with_variant_baseline() -> None:
    timed_out = score_experiment.compute_composite(
        [_result(speed_baseline_seconds=46.0, timed_out=True, exit_code=-1)]
    )
    errored = score_experiment.compute_composite(
        [_result(speed_baseline_seconds=46.0, error="boom", exit_code=1)]
    )

    assert timed_out["per_benchmark"]["r2_small_impl"]["speed"] == 0.0
    assert errored["per_benchmark"]["r2_small_impl"]["speed"] == 0.0


def test_run_benchmark_attaches_variant_speed_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    benchmark = eval_harness.load_benchmark("r2_small_impl")
    variant = next(v for v in benchmark["variants"] if v["id"] == "r2_v2")

    class DummyProc:
        returncode = 0
        stdout = json.dumps({"result": "ok"})
        stderr = ""

    monkeypatch.setattr(eval_harness.subprocess, "run", lambda *args, **kwargs: DummyProc())

    result = eval_harness.run_benchmark(benchmark, variant)

    assert result["speed_baseline_seconds"] == 55.0
    assert result["exit_code"] == 0
    assert result["timed_out"] is False


def test_run_benchmark_leaves_speed_baseline_unset_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark = eval_harness.load_benchmark("r1_factual")
    variant = next(v for v in benchmark["variants"] if v["id"] == "r1_v1")

    class DummyProc:
        returncode = 0
        stdout = json.dumps({"result": "ok"})
        stderr = ""

    monkeypatch.setattr(eval_harness.subprocess, "run", lambda *args, **kwargs: DummyProc())

    result = eval_harness.run_benchmark(benchmark, variant)

    assert result["speed_baseline_seconds"] is None


def test_run_full_suite_emits_speed_baseline_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    benchmark = eval_harness.load_benchmark("r2_small_impl")
    variant = next(v for v in benchmark["variants"] if v["id"] == "r2_v3")

    monkeypatch.setattr(eval_harness, "select_variant", lambda _benchmark: variant)
    monkeypatch.setattr(eval_harness, "prepare_workspace", lambda _benchmark: None)
    monkeypatch.setattr(
        eval_harness,
        "run_benchmark",
        lambda _benchmark, _variant, _workspace=None: {
            "benchmark_id": "r2_small_impl",
            "variant_id": "r2_v3",
            "output": "done",
            "wall_time_seconds": 46.0,
            "exit_code": 0,
            "timed_out": False,
            "error": None,
            "agent": "worker",
            "model": "claude-sonnet-4-6",
            "effort": "medium",
            "speed_baseline_seconds": 46.0,
        },
    )

    results = eval_harness.run_full_suite(["r2_small_impl"])

    assert len(results) == 1
    assert results[0]["variant_id"] == "r2_v3"
    assert results[0]["speed_baseline_seconds"] == 46.0
