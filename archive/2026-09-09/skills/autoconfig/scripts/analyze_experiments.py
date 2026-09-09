"""Trend detection and reporting module for autoconfig experiments."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from experiment_db import (
    get_kept_experiments,
    get_experiments_by_phase,
    get_experiment_count,
    get_total_kept,
    get_cumulative_improvement,
    get_daily_stats,
    get_recent_experiments,
    get_recent_benchmark_results,
)
from rate_limiter import get_program_state
from config_snapshot import compute_config_hash, get_snapshot_meta


# ---------------------------------------------------------------------------
# Phase summary
# ---------------------------------------------------------------------------

def get_phase_summary(
    phase: int,
    evaluation_version: str | None = None,
    run_mode: str | None = None,
) -> dict:
    """Query experiment DB for the given phase and return an aggregate summary."""
    experiments = get_experiments_by_phase(
        phase,
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    )
    total = len(experiments)
    completed = [e for e in experiments if e.get("status") != "running"]
    completed_total = len(completed)
    running = total - completed_total
    kept = [e for e in completed if e.get("kept")]
    discarded = len(
        [e for e in completed if not e.get("kept") and e.get("status") == "completed"]
    )

    scores = [e["composite_score"] for e in completed if e.get("composite_score") is not None]
    best_score = max(scores) if scores else 0.0
    worst_score = min(scores) if scores else 0.0
    avg_score = sum(scores) / len(scores) if scores else 0.0

    improvements = [e.get("improvement_pct", 0.0) for e in kept]
    total_improvement_pct = sum(improvements)

    state = get_program_state()

    # Convergence heuristic
    if run_mode == "phase3_calibration" and phase == 3:
        if completed_total == 0 and running == 0:
            convergence_status = "not started"
        elif state.get("status") == "completed":
            convergence_status = "completed"
        else:
            convergence_status = "active"
    elif completed_total == 0 and running == 0:
        convergence_status = "not started"
    elif detect_plateau(
        phase,
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    ):
        convergence_status = "converged"
    elif completed_total >= 200 and completed_total > 0 and len(kept) / completed_total < 0.02:
        convergence_status = "exhausted"
    else:
        convergence_status = "active"

    # Top 5 mutations by improvement
    top_mutations = sorted(kept, key=lambda e: e.get("improvement_pct", 0.0), reverse=True)[:5]
    top_mutations = [
        {"summary": e.get("mutation_summary", ""), "improvement_pct": e.get("improvement_pct", 0.0)}
        for e in top_mutations
    ]

    return {
        "phase": phase,
        "total_experiments": total,
        "completed_experiments": completed_total,
        "running_experiments": running,
        "kept": len(kept),
        "discarded": discarded,
        "best_score": best_score,
        "worst_score": worst_score,
        "avg_score": round(avg_score, 4),
        "total_improvement_pct": round(total_improvement_pct, 4),
        "convergence_status": convergence_status,
        "top_mutations": top_mutations,
    }


# ---------------------------------------------------------------------------
# Knob attribution
# ---------------------------------------------------------------------------

def get_knob_attribution(
    evaluation_version: str | None = None,
    run_mode: str | None = None,
) -> list[dict]:
    """Analyze kept experiments to determine which knobs contributed most improvement.

    Groups by change type (model assignment, effort level, topology) and returns
    a sorted list from highest to lowest total improvement.
    """
    kept = get_kept_experiments(
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    )
    attribution: dict[str, dict[str, float | int]] = {}

    for exp in kept:
        summary = exp.get("mutation_summary", "unknown")
        # Extract knob type from mutation summary (e.g., "worker model R2: ..." -> "model")
        knob = "unknown"
        for keyword in ("model", "effort", "lane_cap", "swarm_cap", "dispatch_order", "parallelism"):
            if keyword in summary.lower():
                knob = keyword
                break
        if knob not in attribution:
            attribution[knob] = {"total_improvement": 0.0, "experiments_kept": 0}
        attribution[knob]["total_improvement"] += exp.get("improvement_pct", 0.0)
        attribution[knob]["experiments_kept"] += 1

    result = [
        {
            "knob": knob,
            "total_improvement": round(data["total_improvement"], 4),
            "experiments_kept": int(data["experiments_kept"]),
        }
        for knob, data in attribution.items()
    ]
    result.sort(key=lambda x: x["total_improvement"], reverse=True)
    return result


# ---------------------------------------------------------------------------
# Improvement curve
# ---------------------------------------------------------------------------

def get_improvement_curve(
    evaluation_version: str | None = None,
    run_mode: str | None = None,
) -> list[dict]:
    """Return the cumulative improvement over time (kept experiments only)."""
    kept = get_kept_experiments(
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    )
    kept.sort(key=lambda e: e.get("started_at", ""))

    curve: list[dict] = []
    cumulative = 0.0
    for exp in kept:
        cumulative += exp.get("improvement_pct", 0.0)
        curve.append({
            "experiment_id": exp.get("id", 0),
            "timestamp": exp.get("started_at", ""),
            "cumulative_improvement_pct": round(cumulative, 4),
            "phase": exp.get("phase", 0),
        })
    return curve


# ---------------------------------------------------------------------------
# Drift report
# ---------------------------------------------------------------------------

def get_drift_report() -> dict:
    """Compare current config to the original baseline snapshot."""
    import json as _json
    from config_snapshot import CLAUDE_HOME, CONFIG_FILES, SNAPSHOT_BASE

    baseline_dir = SNAPSHOT_BASE / "baseline"
    current_hash = compute_config_hash()
    baseline_meta = get_snapshot_meta("baseline")
    baseline_hash = baseline_meta.get("config_hash", "") if baseline_meta else ""

    changes: list[dict] = []

    # Compare each config file between baseline and live
    for rel_path in CONFIG_FILES:
        baseline_file = baseline_dir / rel_path
        live_file = CLAUDE_HOME / rel_path
        if not baseline_file.exists() or not live_file.exists():
            continue
        try:
            baseline_data = _json.loads(baseline_file.read_text())
            live_data = _json.loads(live_file.read_text())
            _diff_dicts("", baseline_data, live_data, changes)
        except (ValueError, OSError):
            pass

    return {
        "knobs_changed": len(changes),
        "changes": changes,
        "config_hash_original": baseline_hash,
        "config_hash_current": current_hash,
    }


def _diff_dicts(prefix: str, old: Any, new: Any, changes: list[dict]) -> None:
    """Recursively diff two dicts/values, appending differences to changes."""
    if isinstance(old, dict) and isinstance(new, dict):
        all_keys = set(list(old.keys()) + list(new.keys()))
        for key in sorted(all_keys):
            path = f"{prefix}.{key}" if prefix else key
            _diff_dicts(path, old.get(key), new.get(key), changes)
    elif old != new:
        changes.append({"path": prefix, "original": old, "current": new})


# ---------------------------------------------------------------------------
# Plateau detection
# ---------------------------------------------------------------------------

def detect_plateau(
    phase: int,
    window: int = 20,
    evaluation_version: str | None = None,
    run_mode: str | None = None,
) -> bool:
    """Check if the last `window` experiments in the phase were all discarded."""
    experiments = get_experiments_by_phase(
        phase,
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    )
    completed = [e for e in experiments if e.get("status") != "running"]
    if len(completed) < window:
        return False
    recent = completed[:window]
    return all(not e.get("kept", False) for e in recent)


def _r1_false_negative_counts(evaluation_version: str | None, run_mode: str) -> list[dict]:
    """Summarize R1 truth-match false negatives by variant."""
    counts: dict[str, int] = {}
    for result in get_recent_benchmark_results(
        "r1_factual",
        limit=1000,
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    ):
        if result.get("deterministic_gate_passed"):
            continue
        variant_id = str(result.get("variant_id") or "unknown")
        counts[variant_id] = counts.get(variant_id, 0) + 1
    return [
        {"variant_id": variant_id, "false_negatives": count}
        for variant_id, count in sorted(counts.items())
    ]


def _phase3_calibration_breakdown(evaluation_version: str | None) -> list[dict]:
    """Return per-variant calibration health for r3_feature."""
    state = get_program_state()
    readiness_variants = (
        state.get("phase_readiness", {}).get("3", {}).get("variants", {})
        if isinstance(state.get("phase_readiness", {}).get("3"), dict)
        else {}
    )
    rows = get_recent_benchmark_results(
        "r3_feature",
        limit=1000,
        evaluation_version=evaluation_version,
        run_mode="phase3_calibration",
    )
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("variant_id") or "unknown"), []).append(row)

    breakdown: list[dict] = []
    for variant_id, items in sorted(grouped.items()):
        total = len(items)
        clean = sum(1 for item in items if item.get("completed_cleanly"))
        gate = sum(1 for item in items if item.get("deterministic_gate_passed"))
        times = sorted(float(item.get("wall_time_seconds", 0.0)) for item in items)
        terminal_states: dict[str, int] = {}
        for item in items:
            terminal_state = str(item.get("terminal_state") or "unknown")
            terminal_states[terminal_state] = terminal_states.get(terminal_state, 0) + 1
        median_wall = times[len(times) // 2] if times else 0.0
        breakdown.append({
            "variant_id": variant_id,
            "samples": total,
            "target_samples": int(
                readiness_variants.get(variant_id, {}).get("target_sample_count", 5)
            ),
            "clean_rate": round((clean / total) if total else 0.0, 4),
            "gate_rate": round((gate / total) if total else 0.0, 4),
            "median_wall_time_seconds": round(median_wall, 2),
            "terminal_states": terminal_states,
            "ready": bool(readiness_variants.get(variant_id, {}).get("ready")),
            "reason": readiness_variants.get(variant_id, {}).get("reason"),
        })
    breakdown.sort(
        key=lambda item: (
            1 if item.get("ready") else 0,
            item.get("clean_rate", 0.0),
            item.get("gate_rate", 0.0),
            item.get("samples", 0),
            item.get("variant_id", ""),
        )
    )
    return breakdown


# ---------------------------------------------------------------------------
# Full report
# ---------------------------------------------------------------------------

def generate_report() -> str:
    """Generate a full text report suitable for display."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("AUTOCONFIG EXPERIMENT REPORT")
    lines.append(f"Generated: {datetime.now().isoformat()}")
    lines.append("=" * 72)

    # 1. Overall stats
    state = get_program_state()
    evaluation_version = state.get("evaluation_version")
    run_mode = state.get("run_mode", "search")
    total_experiments = get_experiment_count(evaluation_version, run_mode=run_mode)
    total_kept = get_total_kept(evaluation_version, run_mode=run_mode)
    cumulative_improvement = get_cumulative_improvement(
        evaluation_version,
        run_mode=run_mode,
    )

    lines.append("")
    lines.append("--- Overall Stats ---")
    lines.append(f"Evaluation version:      {state.get('evaluation_version', 'unknown')}")
    lines.append(f"Run mode:                {run_mode}")
    lines.append(f"Enabled phases:          {state.get('enabled_phases', [])}")
    if run_mode == "phase3_calibration":
        lines.append(f"Total calibration samples: {total_experiments}")
    else:
        lines.append(f"Total experiments run:   {total_experiments}")
        lines.append(f"Total kept:              {total_kept}")
        lines.append(f"Total discarded:         {total_experiments - total_kept}")
        lines.append(
            f"Keep rate:               "
            f"{(total_kept / total_experiments * 100) if total_experiments else 0:.1f}%"
        )
        lines.append(f"Cumulative improvement:  {cumulative_improvement:.2f}%")

    # 2. Per-phase summaries
    phases = _discover_phases(run_mode=run_mode)
    if phases:
        lines.append("")
        lines.append("--- Per-Phase Summaries ---")
        for phase in phases:
            summary = get_phase_summary(
                phase,
                evaluation_version=evaluation_version,
                run_mode=run_mode,
            )
            lines.append(f"")
            lines.append(f"  Phase {summary['phase']}:")
            if run_mode == "phase3_calibration":
                lines.append(
                    f"    Calibration samples: {summary['total_experiments']}"
                )
            else:
                lines.append(
                    f"    Experiments: {summary['total_experiments']}  "
                    f"(kept {summary['kept']}, discarded {summary['discarded']})"
                )
            lines.append(f"    Scores:      best={summary['best_score']:.4f}  worst={summary['worst_score']:.4f}  avg={summary['avg_score']:.4f}")
            if run_mode != "phase3_calibration":
                lines.append(f"    Improvement: {summary['total_improvement_pct']:.2f}%")
            lines.append(f"    Status:      {summary['convergence_status']}")

    phase_readiness = state.get("phase_readiness", {})
    phase3_readiness = phase_readiness.get("3")
    if isinstance(phase3_readiness, dict):
        lines.append("")
        lines.append("--- Phase Readiness ---")
        lines.append(
            f"  Phase 3 ready:        {'yes' if phase3_readiness.get('ready') else 'no'}"
        )
        lines.append(
            f"  Phase 3 sample count: {phase3_readiness.get('sample_count', 0)}/"
            f"{phase3_readiness.get('window', 0)}"
        )
        lines.append(
            f"  Clean completion:     {phase3_readiness.get('clean_completion_rate', 0.0):.2f}"
        )
        lines.append(
            f"  Gate pass rate:       {phase3_readiness.get('deterministic_gate_pass_rate', 0.0):.2f}"
        )
        if phase3_readiness.get("reason"):
            lines.append(f"  Reason:               {phase3_readiness.get('reason')}")
        variants = phase3_readiness.get("variants", {})
        if isinstance(variants, dict) and variants:
            for variant_id, snapshot in sorted(variants.items()):
                lines.append(
                    f"  {variant_id}:               "
                    f"{snapshot.get('sample_count', 0)}/"
                    f"{snapshot.get('target_sample_count', 0)} "
                    f"clean={snapshot.get('clean_completion_rate', 0.0):.2f} "
                    f"gate={snapshot.get('deterministic_gate_pass_rate', 0.0):.2f}"
                )

    # 3. Top-10 most impactful mutations
    kept = get_kept_experiments(
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    )
    top10 = sorted(kept, key=lambda e: e.get("improvement_pct", 0.0), reverse=True)[:10]
    if top10:
        lines.append("")
        lines.append("--- Top 10 Most Impactful Mutations ---")
        for i, exp in enumerate(top10, 1):
            trial_count = exp.get("trial_count", 1)
            lines.append(
                f"  {i:>2}. [{exp.get('improvement_pct', 0.0):+.2f}%] "
                f"{exp.get('mutation_summary', 'n/a')} (trials={trial_count})"
            )

    false_negatives = _r1_false_negative_counts(evaluation_version, "search")
    if false_negatives:
        lines.append("")
        lines.append("--- R1 False Negatives ---")
        for item in false_negatives:
            lines.append(
                f"  {item['variant_id']}: {item['false_negatives']} false negatives"
            )

    calibration = _phase3_calibration_breakdown(evaluation_version)
    if calibration:
        lines.append("")
        lines.append("--- Phase 3 Calibration ---")
        for item in calibration:
            lines.append(
                f"  {item['variant_id']}: samples={item['samples']}/"
                f"{item['target_samples']} "
                f"clean={item['clean_rate']:.2f} gate={item['gate_rate']:.2f} "
                f"median_wall={item['median_wall_time_seconds']:.2f}s "
                f"states={json.dumps(item['terminal_states'], sort_keys=True)}"
            )

    # 4. Config drift
    drift = get_drift_report()
    lines.append("")
    lines.append("--- Config Drift from Original ---")
    lines.append(f"  Knobs changed:  {drift['knobs_changed']}")
    lines.append(f"  Hash original:  {drift['config_hash_original']}")
    lines.append(f"  Hash current:   {drift['config_hash_current']}")
    if drift["changes"]:
        for change in drift["changes"][:20]:
            lines.append(f"    {change['path']}: {change['original']} -> {change['current']}")

    # 5. Convergence status per phase
    if phases:
        lines.append("")
        lines.append("--- Convergence Status ---")
        for phase in phases:
            summary = get_phase_summary(
                phase,
                evaluation_version=evaluation_version,
                run_mode=run_mode,
            )
            lines.append(f"  Phase {phase}: {summary['convergence_status']}")

    # 6. Rate limit stats (today)
    daily = get_daily_stats()
    if daily:
        lines.append("")
        lines.append("--- Today's Stats ---")
        lines.append(f"  Date:           {daily.get('date', 'n/a')}")
        lines.append(f"  Experiments:    {daily.get('experiment_count', 0)}")
        lines.append(f"  Improvements:   {daily.get('improvements_found', 0)}")
        lines.append(f"  Rate limit hits: {daily.get('rate_limit_hits', 0)}")

    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


def _discover_phases(run_mode: str | None = None) -> list[int]:
    """Return sorted list of distinct phases present in the DB."""
    kept = get_kept_experiments(run_mode=run_mode)
    all_exp = get_recent_experiments(limit=10000, run_mode=run_mode)
    phases: set[int] = set()
    for e in kept:
        if "phase" in e:
            phases.add(e["phase"])
    for e in all_exp:
        if "phase" in e:
            phases.add(e["phase"])
    return sorted(phases)


# ---------------------------------------------------------------------------
# Memory logging
# ---------------------------------------------------------------------------

def log_to_memory(improvement_pct: float, mutation_summary: str) -> None:
    """Log significant improvements for cross-session visibility.

    If ~/.claude/bin/notify_done.sh exists, call it for improvements > 5%.
    Writes an observation line that could be consumed by chad-memory.
    """
    observation = (
        f"[autoconfig] improvement={improvement_pct:+.2f}% | {mutation_summary} | {datetime.now().isoformat()}"
    )

    # Write to a local observation log for cross-session pickup
    log_dir = Path(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude"))) / "state" / "autoconfig"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "observations.log"
    with open(log_path, "a") as f:
        f.write(observation + "\n")

    # Notify on significant improvements
    if improvement_pct > 5.0:
        notify_script = Path(os.path.expanduser("~/.claude/bin/notify_done.sh"))
        if notify_script.exists():
            try:
                subprocess.run(
                    [str(notify_script)],
                    env={**os.environ, "AUTOCONFIG_IMPROVEMENT": f"{improvement_pct:+.2f}%"},
                    timeout=10,
                    check=False,
                )
            except (subprocess.TimeoutExpired, OSError):
                pass  # Best-effort notification


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(generate_report())
