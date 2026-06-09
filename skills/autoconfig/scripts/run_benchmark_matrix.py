#!/usr/bin/env python3
"""Run repeated benchmark comparisons across named autoconfig presets."""

from __future__ import annotations

import argparse
import csv
import json
import platform
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Optional

from config_snapshot import SNAPSHOT_BASE
from eval_harness import (
    SOURCE_CLAUDE_HOME,
    _CLAUDE_CMD,
    _execute_benchmark_with_retry,
    load_benchmark,
)
from score_experiment import compute_composite

MATRIX_SCHEMA_VERSION = "benchmark-matrix.v1"
DEFAULT_BENCHMARK_IDS = ("r3_rubik_app",)
DEFAULT_PRESET_IDS = ("current", "baseline", "best")
# `candidate` is a synthetic preset id used by `policy_edit_gate.py` (slice 6 of
# the thoughts.md autonomy roadmap) to run the matrix against a candidate
# ~/.claude home directory containing a proposed policy edit. It must be paired
# with `--candidate-home <path>`. See ~/.claude/bin/policy_edit_gate.py.
VALID_PRESET_IDS = frozenset(DEFAULT_PRESET_IDS) | {"candidate"}
_CANDIDATE_HOME_OVERRIDE: Optional[Path] = None
DEFAULT_REPEAT_COUNT = 3
DEFAULT_MATRIX_ROOT = Path.home() / ".claude" / "state" / "autoconfig" / "benchmark-matrix"
SCORING_MODE_REF = "/Users/chadsimon/.claude/skills/autoconfig/references/metric_spec.md"
BENCHMARK_RESULT_FIELDS = {
    "benchmark_id",
    "variant_id",
    "route",
    "output",
    "wall_time_seconds",
    "acceptance",
    "exit_code",
    "speed_baseline_seconds",
    "terminal_state",
    "completed_cleanly",
    "deterministic_gate_passed",
    "deterministic_quality_score",
    "semantic_quality_score",
    "judge_summary",
    "judge_flags",
    "judge_failures",
    "timed_out",
    "error",
    "agent",
    "model",
    "effort",
    "retryable_benchmark_failure",
    "benchmark_retry_count",
    "retry_count",
    "trial_clean",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _suite_hash(benchmark_ids: list[str]) -> str:
    import hashlib

    digest = hashlib.sha256()
    for benchmark_id in benchmark_ids:
        path = Path.home() / ".claude" / "skills" / "autoconfig" / "benchmarks" / f"{benchmark_id}.json"
        digest.update(benchmark_id.encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _config_hash_for_home(source_home: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    paths = [
        source_home / "settings.json",
        source_home / "state" / "route_manifest.json",
    ]
    agents_dir = source_home / "agents"
    if agents_dir.is_dir():
        paths.extend(sorted(agents_dir.glob("*.md")))
    for path in sorted(paths):
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(source_home)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _resolve_commit_hash() -> Optional[str]:
    candidates = [
        Path.home() / ".claude",
        Path.home() / ".codex",
    ]
    for root in candidates:
        try:
            completed = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            continue
        if completed.returncode == 0:
            return completed.stdout.strip() or None
    return None


def _resolve_preset(preset_id: str) -> dict[str, Any]:
    if preset_id not in VALID_PRESET_IDS:
        raise ValueError(f"unknown_preset:{preset_id}")

    if preset_id == "candidate":
        # Slice 6: synthetic preset for policy-edit-gate. The caller stages a
        # full ~/.claude tree under --candidate-home, applies the proposed diff
        # there, and runs the matrix to compare candidate vs current.
        if _CANDIDATE_HOME_OVERRIDE is None:
            return {
                "preset_id": preset_id,
                "status": "error",
                "preset_source": "candidate",
                "source_home": None,
                "config_hash": None,
                "snapshot_meta": None,
                "error": "candidate preset requires --candidate-home <path>",
            }
        source_home = _CANDIDATE_HOME_OVERRIDE.expanduser().resolve()
        if not source_home.is_dir():
            return {
                "preset_id": preset_id,
                "status": "error",
                "preset_source": "candidate",
                "source_home": str(source_home),
                "config_hash": None,
                "snapshot_meta": None,
                "error": f"candidate-home not a directory: {source_home}",
            }
        return {
            "preset_id": preset_id,
            "status": "ok",
            "preset_source": "candidate_home",
            "source_home": str(source_home),
            "config_hash": _config_hash_for_home(source_home),
            "snapshot_meta": {"candidate": True},
        }

    if preset_id == "current":
        source_home = SOURCE_CLAUDE_HOME.expanduser().resolve()
        return {
            "preset_id": preset_id,
            "status": "ok",
            "preset_source": "live_home",
            "source_home": str(source_home),
            "config_hash": _config_hash_for_home(source_home),
            "snapshot_meta": None,
        }

    snapshot_dir = (SNAPSHOT_BASE / preset_id).expanduser().resolve()
    meta_path = snapshot_dir / ".snapshot_meta.json"
    meta = (
        json.loads(meta_path.read_text(encoding="utf-8"))
        if meta_path.is_file()
        else None
    )
    if not snapshot_dir.is_dir() or meta is None:
        return {
            "preset_id": preset_id,
            "status": "error",
            "preset_source": "snapshot",
            "source_home": str(snapshot_dir),
            "config_hash": None,
            "snapshot_meta": meta,
            "error": f"Missing snapshot for preset {preset_id}",
        }

    return {
        "preset_id": preset_id,
        "status": "ok",
        "preset_source": "snapshot",
        "source_home": str(snapshot_dir),
        "config_hash": str(meta.get("config_hash") or _config_hash_for_home(snapshot_dir)),
        "snapshot_meta": meta,
    }


def _score_row(result: dict[str, Any]) -> dict[str, float]:
    scores = compute_composite([result])
    return {
        "composite_score": float(scores["composite"]),
        "quality_score": float(scores["quality"]),
        "speed_score": float(scores["speed"]),
    }


def _base_failure_result(*, benchmark: dict[str, Any], variant: dict[str, Any], error: str) -> dict[str, Any]:
    return {
        "benchmark_id": benchmark.get("id", "unknown"),
        "variant_id": variant.get("id", "unknown"),
        "route": benchmark.get("route", "unknown"),
        "output": "",
        "wall_time_seconds": 0.0,
        "acceptance": {
            "passed": 0,
            "failed": 0,
            "total": len(variant.get("acceptance_checks", [])),
            "pass_rate": 0.0,
            "details": [],
        },
        "exit_code": -1,
        "speed_baseline_seconds": benchmark.get("speed_baselines_seconds", {}).get("variants", {}).get(variant.get("id"))
        or benchmark.get("speed_baselines_seconds", {}).get("default"),
        "terminal_state": "preset_resolution_error",
        "completed_cleanly": False,
        "deterministic_gate_passed": False,
        "deterministic_quality_score": 0.0,
        "semantic_quality_score": None,
        "judge_summary": None,
        "judge_flags": ["preset_resolution_error"],
        "judge_failures": [error],
        "timed_out": False,
        "error": error,
        "agent": None,
        "model": None,
        "effort": None,
        "retryable_benchmark_failure": False,
        "benchmark_retry_count": 0,
        "retry_count": 0,
        "trial_clean": False,
    }


def _artifact_paths_for_row(
    *,
    run_root: Path,
    benchmark_id: str,
    benchmark: dict[str, Any],
) -> dict[str, str]:
    return {
        "benchmark_definition": str(Path.home() / ".claude" / "skills" / "autoconfig" / "benchmarks" / f"{benchmark_id}.json"),
        "workspace_template": str(
            Path.home()
            / ".claude"
            / "skills"
            / "autoconfig"
            / "benchmarks"
            / "templates"
            / str(benchmark.get("workspace_template") or "")
        ),
        "matrix_run_root": str(run_root),
    }


def _row_from_result(
    *,
    result: dict[str, Any],
    run_id: str,
    preset: dict[str, Any],
    repeat_index: int,
    row_started_at: str,
    row_completed_at: str,
    artifact_paths: dict[str, str],
) -> dict[str, Any]:
    row = dict(result)
    row.update(_score_row(result))
    row["run_id"] = run_id
    row["preset_id"] = preset["preset_id"]
    row["preset_source"] = preset["preset_source"]
    row["preset_config_hash"] = preset.get("config_hash")
    row["repeat_index"] = repeat_index
    row["row_started_at"] = row_started_at
    row["row_completed_at"] = row_completed_at
    row["artifact_paths"] = artifact_paths
    return row


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return round(float(median(values)), 4)


def _rate(values: list[bool]) -> Optional[float]:
    if not values:
        return None
    return round(sum(1 for value in values if value) / len(values), 4)


def _summarize_preset(rows: list[dict[str, Any]]) -> dict[str, Any]:
    semantic_scores = [
        float(row["semantic_quality_score"])
        for row in rows
        if row.get("semantic_quality_score") is not None
    ]
    return {
        "attempt_count": len(rows),
        "median_composite_score": _median([float(row.get("composite_score", 0.0) or 0.0) for row in rows]),
        "median_quality_score": _median([float(row.get("quality_score", 0.0) or 0.0) for row in rows]),
        "median_speed_score": _median([float(row.get("speed_score", 0.0) or 0.0) for row in rows]),
        "median_wall_time_seconds": _median([float(row.get("wall_time_seconds", 0.0) or 0.0) for row in rows]),
        "clean_completion_rate": _rate([bool(row.get("completed_cleanly")) for row in rows]),
        "deterministic_gate_pass_rate": _rate([bool(row.get("deterministic_gate_passed")) for row in rows]),
        "median_semantic_score": _median(semantic_scores),
    }


def _delta(value: Optional[float], baseline: Optional[float]) -> Optional[float]:
    if value is None or baseline is None:
        return None
    return round(value - baseline, 4)


def _recommend_preset(summaries: dict[str, dict[str, Any]]) -> Optional[str]:
    candidates = [
        (preset_id, data)
        for preset_id, data in summaries.items()
        if data.get("attempt_count", 0) > 0 and any(
            data.get(metric) is not None
            for metric in ("median_composite_score", "clean_completion_rate", "deterministic_gate_pass_rate")
        )
    ]
    if not candidates:
        return None

    ranked = sorted(
        candidates,
        key=lambda item: (
            -(item[1].get("median_composite_score") or 0.0),
            -(item[1].get("clean_completion_rate") or 0.0),
            -(item[1].get("deterministic_gate_pass_rate") or 0.0),
            item[1].get("median_wall_time_seconds") or float("inf"),
            item[0],
        ),
    )
    return ranked[0][0]


def _write_rows_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = [json.dumps(row, sort_keys=True) for row in rows]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _write_summary_csv(path: Path, summaries: dict[str, dict[str, Any]]) -> None:
    fieldnames = [
        "preset_id",
        "attempt_count",
        "median_composite_score",
        "median_quality_score",
        "median_speed_score",
        "median_wall_time_seconds",
        "clean_completion_rate",
        "deterministic_gate_pass_rate",
        "median_semantic_score",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for preset_id in sorted(summaries.keys()):
            writer.writerow({"preset_id": preset_id, **summaries[preset_id]})


def _build_manifest(
    *,
    run_id: str,
    started_at: str,
    completed_at: Optional[str],
    benchmark_ids: list[str],
    preset_resolutions: dict[str, dict[str, Any]],
    repeats: int,
    variant_order: dict[str, list[str]],
    rows_written: int,
    status: str,
    active_attempt: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "schema_version": MATRIX_SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "benchmark_ids": benchmark_ids,
        "preset_resolutions": preset_resolutions,
        "repeat_count": repeats,
        "variant_order": variant_order,
        "scoring_mode_ref": SCORING_MODE_REF,
        "runner_version": MATRIX_SCHEMA_VERSION,
        "benchmark_suite_version": _suite_hash(benchmark_ids),
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "claude_cmd": _CLAUDE_CMD,
            "source_claude_home": str(SOURCE_CLAUDE_HOME),
        },
        "commit_hash": _resolve_commit_hash(),
        "rows_written": rows_written,
        "status": status,
        "active_attempt": active_attempt,
    }


def _build_summary(
    *,
    run_id: str,
    benchmark_ids: list[str],
    repeats: int,
    selected_presets: list[str],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    per_preset_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        per_preset_rows[str(row["preset_id"])].append(row)

    summaries = {
        preset_id: _summarize_preset(per_preset_rows.get(preset_id, []))
        for preset_id in selected_presets
    }
    baseline_summary = summaries.get("baseline")
    pairwise_vs_baseline = {
        preset_id: {
            "median_composite_score_delta": _delta(summary.get("median_composite_score"), baseline_summary.get("median_composite_score") if baseline_summary else None),
            "median_quality_score_delta": _delta(summary.get("median_quality_score"), baseline_summary.get("median_quality_score") if baseline_summary else None),
            "median_speed_score_delta": _delta(summary.get("median_speed_score"), baseline_summary.get("median_speed_score") if baseline_summary else None),
            "median_wall_time_seconds_delta": _delta(summary.get("median_wall_time_seconds"), baseline_summary.get("median_wall_time_seconds") if baseline_summary else None),
            "clean_completion_rate_delta": _delta(summary.get("clean_completion_rate"), baseline_summary.get("clean_completion_rate") if baseline_summary else None),
            "deterministic_gate_pass_rate_delta": _delta(summary.get("deterministic_gate_pass_rate"), baseline_summary.get("deterministic_gate_pass_rate") if baseline_summary else None),
            "median_semantic_score_delta": _delta(summary.get("median_semantic_score"), baseline_summary.get("median_semantic_score") if baseline_summary else None),
        }
        for preset_id, summary in summaries.items()
    }
    return {
        "schema_version": MATRIX_SCHEMA_VERSION,
        "run_id": run_id,
        "benchmarks": benchmark_ids,
        "repeats": repeats,
        "preset_summaries": summaries,
        "pairwise_vs_baseline": pairwise_vs_baseline,
        "recommended_preset": _recommend_preset(summaries),
    }


def _flush_run_artifacts(
    *,
    run_root: Path,
    manifest: dict[str, Any],
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    (run_root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_rows_jsonl(run_root / "rows.jsonl", rows)
    (run_root / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_summary_csv(run_root / "summary.csv", summary["preset_summaries"])


def run_benchmark_matrix(
    *,
    benchmark_ids: Optional[list[str]] = None,
    preset_ids: Optional[list[str]] = None,
    repeats: int = DEFAULT_REPEAT_COUNT,
    output_root: Optional[str | Path] = None,
) -> dict[str, Any]:
    if repeats <= 0:
        raise ValueError("repeats must be >= 1")

    selected_benchmarks = benchmark_ids or list(DEFAULT_BENCHMARK_IDS)
    selected_presets = preset_ids or list(DEFAULT_PRESET_IDS)
    benchmarks = [load_benchmark(benchmark_id) for benchmark_id in selected_benchmarks]
    preset_resolutions = {
        preset_id: _resolve_preset(preset_id)
        for preset_id in selected_presets
    }

    started_at = _now_iso()
    run_id = f"matrix-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    matrix_root = Path(output_root).expanduser() if output_root else DEFAULT_MATRIX_ROOT
    run_root = matrix_root / run_id
    run_root.mkdir(parents=True, exist_ok=True)

    variant_order = {
        benchmark["id"]: [str(variant.get("id")) for variant in benchmark.get("variants", [])]
        for benchmark in benchmarks
    }

    rows: list[dict[str, Any]] = []
    manifest = _build_manifest(
        run_id=run_id,
        started_at=started_at,
        completed_at=None,
        benchmark_ids=selected_benchmarks,
        preset_resolutions=preset_resolutions,
        repeats=repeats,
        variant_order=variant_order,
        rows_written=0,
        status="running",
        active_attempt=None,
    )
    summary = _build_summary(
        run_id=run_id,
        benchmark_ids=selected_benchmarks,
        repeats=repeats,
        selected_presets=selected_presets,
        rows=rows,
    )
    _flush_run_artifacts(run_root=run_root, manifest=manifest, summary=summary, rows=rows)

    try:
        for preset_id in selected_presets:
            preset = preset_resolutions[preset_id]
            source_home = Path(preset["source_home"])
            for benchmark in benchmarks:
                benchmark_id = str(benchmark["id"])
                artifact_paths = _artifact_paths_for_row(
                    run_root=run_root,
                    benchmark_id=benchmark_id,
                    benchmark=benchmark,
                )
                for variant in benchmark.get("variants", []):
                    for repeat_index in range(1, repeats + 1):
                        manifest = _build_manifest(
                            run_id=run_id,
                            started_at=started_at,
                            completed_at=None,
                            benchmark_ids=selected_benchmarks,
                            preset_resolutions=preset_resolutions,
                            repeats=repeats,
                            variant_order=variant_order,
                            rows_written=len(rows),
                            status="running",
                            active_attempt={
                                "preset_id": preset_id,
                                "benchmark_id": benchmark_id,
                                "variant_id": str(variant.get("id")),
                                "repeat_index": repeat_index,
                            },
                        )
                        summary = _build_summary(
                            run_id=run_id,
                            benchmark_ids=selected_benchmarks,
                            repeats=repeats,
                            selected_presets=selected_presets,
                            rows=rows,
                        )
                        _flush_run_artifacts(run_root=run_root, manifest=manifest, summary=summary, rows=rows)

                        row_started_at = _now_iso()
                        if preset["status"] != "ok":
                            result = _base_failure_result(
                                benchmark=benchmark,
                                variant=variant,
                                error=str(preset.get("error") or f"Preset {preset_id} failed to resolve"),
                            )
                        else:
                            result = _execute_benchmark_with_retry(
                                benchmark,
                                variant,
                                benchmark_source_home=source_home,
                            )
                        row_completed_at = _now_iso()
                        rows.append(
                            _row_from_result(
                                result=result,
                                run_id=run_id,
                                preset=preset,
                                repeat_index=repeat_index,
                                row_started_at=row_started_at,
                                row_completed_at=row_completed_at,
                                artifact_paths=artifact_paths,
                            )
                        )
                        manifest = _build_manifest(
                            run_id=run_id,
                            started_at=started_at,
                            completed_at=None,
                            benchmark_ids=selected_benchmarks,
                            preset_resolutions=preset_resolutions,
                            repeats=repeats,
                            variant_order=variant_order,
                            rows_written=len(rows),
                            status="running",
                            active_attempt=None,
                        )
                        summary = _build_summary(
                            run_id=run_id,
                            benchmark_ids=selected_benchmarks,
                            repeats=repeats,
                            selected_presets=selected_presets,
                            rows=rows,
                        )
                        _flush_run_artifacts(run_root=run_root, manifest=manifest, summary=summary, rows=rows)
    except Exception:
        manifest = _build_manifest(
            run_id=run_id,
            started_at=started_at,
            completed_at=_now_iso(),
            benchmark_ids=selected_benchmarks,
            preset_resolutions=preset_resolutions,
            repeats=repeats,
            variant_order=variant_order,
            rows_written=len(rows),
            status="failed",
            active_attempt=None,
        )
        summary = _build_summary(
            run_id=run_id,
            benchmark_ids=selected_benchmarks,
            repeats=repeats,
            selected_presets=selected_presets,
            rows=rows,
        )
        _flush_run_artifacts(run_root=run_root, manifest=manifest, summary=summary, rows=rows)
        raise

    manifest = _build_manifest(
        run_id=run_id,
        started_at=started_at,
        completed_at=_now_iso(),
        benchmark_ids=selected_benchmarks,
        preset_resolutions=preset_resolutions,
        repeats=repeats,
        variant_order=variant_order,
        rows_written=len(rows),
        status="completed",
        active_attempt=None,
    )
    summary = _build_summary(
        run_id=run_id,
        benchmark_ids=selected_benchmarks,
        repeats=repeats,
        selected_presets=selected_presets,
        rows=rows,
    )
    _flush_run_artifacts(run_root=run_root, manifest=manifest, summary=summary, rows=rows)
    return {
        "manifest": manifest,
        "summary": summary,
        "rows": rows,
        "run_root": str(run_root),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run repeated benchmark comparisons across named autoconfig presets.")
    parser.add_argument("--benchmark-id", action="append", dest="benchmark_ids", help="Benchmark ID to include. Repeat to add more.")
    parser.add_argument("--preset", action="append", dest="preset_ids", choices=sorted(VALID_PRESET_IDS), help="Preset to compare. Repeat to add more.")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEAT_COUNT, help="Attempt count per preset/variant.")
    parser.add_argument("--output-root", default=None, help="Optional matrix output root override.")
    parser.add_argument(
        "--candidate-home",
        default=None,
        help=("Path to a candidate ~/.claude tree (used by policy_edit_gate.py to score a proposed "
              "policy diff). Required when --preset includes 'candidate'."),
    )
    return parser


def main() -> int:
    global _CANDIDATE_HOME_OVERRIDE
    args = _parser().parse_args()
    if args.candidate_home:
        _CANDIDATE_HOME_OVERRIDE = Path(args.candidate_home).expanduser().resolve()
    payload = run_benchmark_matrix(
        benchmark_ids=args.benchmark_ids,
        preset_ids=args.preset_ids,
        repeats=args.repeats,
        output_root=args.output_root,
    )
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
