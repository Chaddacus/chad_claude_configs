from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.home() / ".claude" / "skills" / "autoconfig" / "scripts"))

import eval_harness
import run_benchmark_matrix


def _seed_config_home(root: Path, label: str) -> Path:
    home = root / label
    (home / "state").mkdir(parents=True, exist_ok=True)
    (home / "agents").mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"env": {"CLAUDE_HOME": str(home)}, "permissions": {}}) + "\n",
        encoding="utf-8",
    )
    (home / "state" / "route_manifest.json").write_text(
        json.dumps({"rules": [{"id": "R3", "model": "claude-opus-4-6"}]}) + "\n",
        encoding="utf-8",
    )
    for agent in ("worker", "planner", "reviewer", "explorer", "validator"):
        (home / "agents" / f"{agent}.md").write_text(
            f"---\nmodel: claude-opus-4-6\neffort: high\nsandbox: {'workspace-write' if agent == 'worker' else 'read-only'}\n---\n",
            encoding="utf-8",
        )
    return home


def test_rubik_benchmark_contract_loads_with_expected_defaults() -> None:
    benchmark = eval_harness.load_benchmark("r3_rubik_app")

    assert benchmark["route"] == "R3"
    assert benchmark["workspace_template"] == "r3_rubik_workspace"
    assert benchmark["contract_ref"].endswith("rubik-benchmark.md")
    assert [variant["id"] for variant in benchmark["variants"]] == [
        "r3_rubik_v1",
        "r3_rubik_v2",
        "r3_rubik_v3",
    ]
    assert "Reachable scramble-state solving is required." in benchmark["judge_rubric"]["solver_expectation"]
    assert "generalized puzzle engine" in benchmark["judge_rubric"]["non_goals"]


def test_prepare_workspace_supports_rubik_template(monkeypatch: pytest.MonkeyPatch) -> None:
    benchmark = eval_harness.load_benchmark("r3_rubik_app")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class DummyProc:
            returncode = 0
            stdout = ""
            stderr = ""

        return DummyProc()

    monkeypatch.setattr(eval_harness.subprocess, "run", fake_run)

    workspace = eval_harness.prepare_workspace(benchmark)
    try:
        assert workspace is not None
        assert (workspace / "package.json").exists()
        assert (workspace / "src" / "rubik" / "cubeState.ts").exists()
        assert (workspace / "tests" / "rubik-smoke.spec.ts").exists()
        assert calls and calls[0][:2] == ["npm", "install"]
    finally:
        if workspace is not None:
            eval_harness.cleanup_workspace(workspace)


def test_run_benchmark_matrix_writes_rows_and_summaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_home = _seed_config_home(tmp_path, "current-home")
    snapshots = tmp_path / "snapshots"
    baseline_home = _seed_config_home(snapshots, "baseline")
    best_home = _seed_config_home(snapshots, "best")
    (baseline_home / ".snapshot_meta.json").write_text(
        json.dumps({"config_hash": "baseline-hash"}) + "\n",
        encoding="utf-8",
    )
    (best_home / ".snapshot_meta.json").write_text(
        json.dumps({"config_hash": "best-hash"}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(run_benchmark_matrix, "SOURCE_CLAUDE_HOME", current_home)
    monkeypatch.setattr(run_benchmark_matrix, "SNAPSHOT_BASE", snapshots)
    monkeypatch.setattr(run_benchmark_matrix, "DEFAULT_MATRIX_ROOT", tmp_path / "matrix")
    monkeypatch.setattr(run_benchmark_matrix, "_resolve_commit_hash", lambda: None)

    call_count = 0

    def fake_execute(benchmark, variant, *, benchmark_source_home=None):
        nonlocal call_count
        call_count += 1
        matrix_dirs = list((tmp_path / "matrix").glob("matrix-*"))
        assert len(matrix_dirs) == 1
        run_root = matrix_dirs[0]
        manifest_path = run_root / "manifest.json"
        rows_path = run_root / "rows.jsonl"
        summary_path = run_root / "summary.json"
        if call_count == 1:
            assert manifest_path.exists()
            assert rows_path.exists()
            assert summary_path.exists()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert manifest["status"] == "running"
            assert manifest["rows_written"] == 0
            assert manifest["completed_at"] is None
            assert manifest["active_attempt"] == {
                "preset_id": "current",
                "benchmark_id": "r3_rubik_app",
                "variant_id": "r3_rubik_v1",
                "repeat_index": 1,
            }
            assert rows_path.read_text(encoding="utf-8") == ""

        if call_count == 2:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            assert manifest["status"] == "running"
            assert manifest["rows_written"] == 1
            assert manifest["active_attempt"] == {
                "preset_id": "current",
                "benchmark_id": "r3_rubik_app",
                "variant_id": "r3_rubik_v1",
                "repeat_index": 2,
            }
            assert len(rows) == 1
            assert rows[0]["preset_id"] == "current"

        home_name = Path(benchmark_source_home).name
        wall_time = {"current-home": 30.0, "baseline": 40.0, "best": 20.0}[home_name]
        semantic = {"current-home": 88.0, "baseline": 80.0, "best": 95.0}[home_name]
        return {
            "benchmark_id": benchmark["id"],
            "variant_id": variant["id"],
            "route": benchmark["route"],
            "output": "done",
            "wall_time_seconds": wall_time,
            "acceptance": {"passed": 8, "failed": 0, "total": 8, "pass_rate": 1.0, "details": []},
            "exit_code": 0,
            "speed_baseline_seconds": benchmark["speed_baselines_seconds"]["variants"][variant["id"]],
            "terminal_state": "completed",
            "completed_cleanly": True,
            "deterministic_gate_passed": True,
            "deterministic_quality_score": 100.0,
            "semantic_quality_score": semantic,
            "judge_summary": "ok",
            "judge_flags": [],
            "judge_failures": [],
            "timed_out": False,
            "error": None,
            "agent": "worker",
            "model": "claude-opus-4-6",
            "effort": "high",
            "retryable_benchmark_failure": False,
            "benchmark_retry_count": 0,
            "retry_count": 0,
            "trial_clean": True,
        }

    monkeypatch.setattr(run_benchmark_matrix, "_execute_benchmark_with_retry", fake_execute)

    payload = run_benchmark_matrix.run_benchmark_matrix(
        repeats=2,
        output_root=tmp_path / "matrix",
    )

    rows = payload["rows"]
    assert len(rows) == 3 * 3 * 2
    first_row = rows[0]
    for field in run_benchmark_matrix.BENCHMARK_RESULT_FIELDS:
        assert field in first_row
    assert first_row["run_id"].startswith("matrix-")
    assert first_row["artifact_paths"]["benchmark_definition"].endswith("r3_rubik_app.json")
    assert first_row["preset_id"] in {"current", "baseline", "best"}
    assert "composite_score" in first_row

    run_root = Path(payload["run_root"])
    assert (run_root / "manifest.json").exists()
    assert (run_root / "rows.jsonl").exists()
    assert (run_root / "summary.json").exists()
    assert (run_root / "summary.csv").exists()

    summary = payload["summary"]
    assert summary["recommended_preset"] == "best"
    assert summary["preset_summaries"]["baseline"]["median_semantic_score"] == 80.0
    assert summary["pairwise_vs_baseline"]["best"]["median_semantic_score_delta"] == 15.0
    assert payload["manifest"]["status"] == "completed"
    assert payload["manifest"]["active_attempt"] is None
    assert payload["manifest"]["rows_written"] == len(rows)


def test_missing_preset_snapshot_records_failure_rows_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_home = _seed_config_home(tmp_path, "current-home")
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(run_benchmark_matrix, "SOURCE_CLAUDE_HOME", current_home)
    monkeypatch.setattr(run_benchmark_matrix, "SNAPSHOT_BASE", snapshots)
    monkeypatch.setattr(run_benchmark_matrix, "DEFAULT_MATRIX_ROOT", tmp_path / "matrix")
    monkeypatch.setattr(run_benchmark_matrix, "_resolve_commit_hash", lambda: None)
    monkeypatch.setattr(
        run_benchmark_matrix,
        "_execute_benchmark_with_retry",
        lambda benchmark, variant, *, benchmark_source_home=None: {
            "benchmark_id": benchmark["id"],
            "variant_id": variant["id"],
            "route": benchmark["route"],
            "output": "done",
            "wall_time_seconds": 10.0,
            "acceptance": {"passed": 8, "failed": 0, "total": 8, "pass_rate": 1.0, "details": []},
            "exit_code": 0,
            "speed_baseline_seconds": benchmark["speed_baselines_seconds"]["variants"][variant["id"]],
            "terminal_state": "completed",
            "completed_cleanly": True,
            "deterministic_gate_passed": True,
            "deterministic_quality_score": 100.0,
            "semantic_quality_score": 90.0,
            "judge_summary": "ok",
            "judge_flags": [],
            "judge_failures": [],
            "timed_out": False,
            "error": None,
            "agent": "worker",
            "model": "claude-opus-4-6",
            "effort": "high",
            "retryable_benchmark_failure": False,
            "benchmark_retry_count": 0,
            "retry_count": 0,
            "trial_clean": True,
        },
    )

    payload = run_benchmark_matrix.run_benchmark_matrix(
        preset_ids=["current", "best"],
        repeats=1,
        output_root=tmp_path / "matrix",
    )

    assert payload["manifest"]["preset_resolutions"]["best"]["status"] == "error"
    best_rows = [row for row in payload["rows"] if row["preset_id"] == "best"]
    assert len(best_rows) == 3
    assert all(row["terminal_state"] == "preset_resolution_error" for row in best_rows)
    assert all("Missing snapshot" in (row["error"] or "") for row in best_rows)
