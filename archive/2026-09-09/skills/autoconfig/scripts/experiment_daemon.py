"""Persistent daemon that runs autoconfig experiments in a loop.

This is the main entry point for 24/7 operation.  launchd keeps it alive
via KeepAlive.  The daemon executes one experiment at a time:

    1.  Conflict check   -- sleep if interactive session active
    2.  Rate limit check -- exponential backoff on recent 429s
    3.  Snapshot config   -- checkpoint
    4.  Select mutation   -- from current phase
    5.  Validate mutation
    6.  Apply mutation
    7.  Run benchmarks
    8.  Score results
    9.  Keep or discard  (with optional confirmation trial)
   10.  Log to experiment DB
   11.  Sleep cooldown
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Make sibling modules importable
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent))

from experiment_db import (
    log_experiment_start,
    log_experiment_result,
    get_consecutive_discards,
    get_tried_mutations,
    get_experiment_count,
    get_total_kept,
    get_cumulative_improvement,
    get_recent_benchmark_results,
)
from config_snapshot import (
    save_snapshot,
    restore_snapshot,
    has_dirty_checkpoint,
    mark_checkpoint_clean,
    clear_checkpoint,
    compute_config_hash,
    snapshot_exists,
)
from rate_limiter import (
    should_wait,
    record_rate_limit,
    record_success,
    get_program_state,
    update_program_state,
    load_state,
)
from config_mutator import get_next_mutation, validate_mutation, apply_mutation
from eval_harness import load_benchmark, run_full_suite
from score_experiment import evaluate_experiment, NOISE_BAND

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COOLDOWN_SECONDS = 60
CONFLICT_WAIT_SECONDS = 120
NETWORK_RETRY_SECONDS = 300
CONVERGENCE_THRESHOLD = 20  # consecutive discards before phase advance
MAX_PHASE = 5  # Phases 1-5 for config optimization
DEFAULT_ENABLED_PHASES = (1,)
DEFAULT_PHASE3_FAMILIES = ("lane_cap",)
EVALUATION_VERSION = "v5_1_variant_gated_calibration"
SCORING_VERSION = EVALUATION_VERSION
RUN_MODE_SEARCH = "search"
RUN_MODE_PHASE3_CALIBRATION = "phase3_calibration"
PHASE3_READINESS_WINDOW = 15
PHASE3_MIN_CLEAN_RATE = 0.80
PHASE3_MIN_GATE_RATE = 0.80
PHASE3_CALIBRATION_PHASE = 3
PHASE3_CALIBRATION_MAX_PER_VARIANT = 5
CLAUDE_HOME = Path.home() / ".claude"
LOCK_DIR = CLAUDE_HOME / "state" / "locks"
LOG_FILE = CLAUDE_HOME / "state" / "autoconfig" / "daemon.log"

# ---------------------------------------------------------------------------
# Globals for signal handling
# ---------------------------------------------------------------------------

_shutdown_requested = False

log = logging.getLogger("autoconfig.daemon")


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def setup_logging() -> None:
    """Configure logging to both LOG_FILE and stdout."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Formatter
    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # File handler
    file_handler = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root_logger.addHandler(file_handler)

    # Stdout handler
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.setFormatter(fmt)
    root_logger.addHandler(stdout_handler)


# ---------------------------------------------------------------------------
# Interactive session detection
# ---------------------------------------------------------------------------


def is_interactive_session_active() -> bool:
    """Check whether an interactive Claude session is currently active.

    Detection methods:
    1. Recent .lock files in LOCK_DIR (mtime < 10 minutes old).
    2. Claude processes that are NOT benchmark runs (i.e. not carrying
       the AUTOCONFIG_BENCHMARK environment variable).

    Returns True if any interactive session is detected.
    """
    # Check lock files
    if LOCK_DIR.is_dir():
        now = time.time()
        try:
            for lock_file in LOCK_DIR.iterdir():
                if lock_file.suffix == ".lock":
                    try:
                        mtime = lock_file.stat().st_mtime
                        if (now - mtime) < 600:  # 10 minutes
                            log.debug(
                                "Active lock file detected: %s (age=%.0fs)",
                                lock_file.name,
                                now - mtime,
                            )
                            return True
                    except OSError:
                        continue
        except OSError:
            pass

    # Check for Claude CLI processes that aren't benchmark runs.
    # Use the executable name from ps output rather than pgrep -f, which can
    # match inherited environment variables like CLAUDE_HOME on unrelated
    # processes (for example npm dev/serve shells).
    try:
        result = subprocess.run(
            ["pgrep", "-x", "claude"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            my_pid = str(os.getpid())
            for pid in pids:
                pid = pid.strip()
                if not pid or pid == my_pid:
                    continue
                # Check if this process has the AUTOCONFIG_BENCHMARK env var
                try:
                    env_path = f"/proc/{pid}/environ"
                    if os.path.exists(env_path):
                        env_data = Path(env_path).read_bytes()
                        if b"AUTOCONFIG_BENCHMARK" in env_data:
                            continue  # This is our own benchmark run
                    else:
                        # macOS: inspect the actual executable, not env text.
                        ps_result = subprocess.run(
                            ["ps", "-p", pid, "-ww", "-o", "tty=,command="],
                            capture_output=True,
                            text=True,
                            timeout=3,
                        )
                        ps_line = ps_result.stdout.strip()
                        # Skip our benchmark processes and any non-Claude
                        # command that happened to inherit CLAUDE_* env vars.
                        if "AUTOCONFIG_BENCHMARK" in ps_line:
                            continue
                        if "pgrep" in ps_line:
                            continue
                        if not ps_line:
                            continue
                        parts = ps_line.split(None, 1)
                        tty = parts[0] if parts else "??"
                        cmd_line = parts[1] if len(parts) > 1 else ""
                        # Detached/orphaned Claude processes with no TTY are
                        # not interactive sessions. Active interactive CLI
                        # sessions have a TTY; GUI-driven flows are covered by
                        # the lock-file check above.
                        if tty == "??":
                            continue
                        if not cmd_line:
                            continue
                        try:
                            argv0 = shlex.split(cmd_line)[0]
                        except ValueError:
                            argv0 = cmd_line.split()[0]
                        if os.path.basename(argv0) != "claude":
                            continue
                    log.debug(
                        "Interactive Claude process detected: pid=%s", pid
                    )
                    return True
                except (OSError, subprocess.TimeoutExpired):
                    continue
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return False


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


def crash_recovery() -> None:
    """Restore from checkpoint if a previous experiment was interrupted.

    If has_dirty_checkpoint() is True, log a warning and restore the
    pre-experiment config to undo any partially-applied mutation.
    """
    if has_dirty_checkpoint():
        log.warning(
            "Dirty checkpoint detected — restoring pre-experiment config "
            "(daemon likely crashed mid-experiment)"
        )
        restored = restore_snapshot("checkpoint")
        if restored:
            log.info("Checkpoint restored successfully")
        else:
            log.error("Failed to restore checkpoint — snapshot may be empty")
        mark_checkpoint_clean()
        log.info("Checkpoint marked clean after crash recovery")


# ---------------------------------------------------------------------------
# Phase / benchmark selection
# ---------------------------------------------------------------------------


def select_benchmarks_for_phase(phase: int) -> Optional[list[str]]:
    """Return the benchmark IDs appropriate for the given phase.

    Phase 1-2: fast feedback on R1/R2 routes only.
    Phase 3:   R3/R4 routes (more expensive, longer running).
    Phase 4-5: None (all benchmarks — full suite).
    """
    if phase <= 2:
        return ["r1_factual", "r2_small_impl"]
    elif phase == 3:
        return ["r3_feature", "r4_auth_review"]
    else:
        return None  # All benchmarks


def _get_enabled_phases(state: Optional[dict] = None) -> list[int]:
    """Return the enabled optimization phases in ascending order."""
    state = state or get_program_state()
    raw = state.get("enabled_phases", list(DEFAULT_ENABLED_PHASES))
    if not isinstance(raw, list):
        return list(DEFAULT_ENABLED_PHASES)
    phases = sorted({
        int(phase)
        for phase in raw
        if isinstance(phase, int) and 1 <= phase <= MAX_PHASE
    })
    return phases or list(DEFAULT_ENABLED_PHASES)


def _get_phase3_allowed_families(state: Optional[dict] = None) -> set[str]:
    """Return the enabled phase-3 mutation families."""
    state = state or get_program_state()
    raw = state.get("phase3_mutation_families", list(DEFAULT_PHASE3_FAMILIES))
    if not isinstance(raw, list):
        return set(DEFAULT_PHASE3_FAMILIES)
    families = {str(family) for family in raw if family}
    return families or set(DEFAULT_PHASE3_FAMILIES)


def _get_run_mode(state: Optional[dict] = None) -> str:
    """Return the active autoconfig run mode."""
    state = state or get_program_state()
    raw = state.get("run_mode", RUN_MODE_SEARCH)
    if raw in {RUN_MODE_SEARCH, RUN_MODE_PHASE3_CALIBRATION}:
        return str(raw)
    return RUN_MODE_SEARCH


def _current_run_scope(state: Optional[dict] = None) -> tuple[Optional[str], str]:
    """Return the current evaluation version and run-mode filter."""
    state = state or get_program_state()
    return state.get("evaluation_version"), _get_run_mode(state)


def _current_timestamp() -> str:
    """Return a timezone-aware ISO8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()


def _compute_phase3_readiness(
    evaluation_version: Optional[str],
    recent_results: Optional[list[dict]] = None,
) -> dict:
    """Compute whether phase 3 is safe to run under the current evaluator."""
    samples = list(recent_results or [])
    readiness_source = RUN_MODE_PHASE3_CALIBRATION
    historical = get_recent_benchmark_results(
        "r3_feature",
        limit=1000,
        evaluation_version=evaluation_version,
        run_mode=RUN_MODE_PHASE3_CALIBRATION,
    )
    if len(samples) < PHASE3_READINESS_WINDOW:
        samples.extend(historical[: max(PHASE3_READINESS_WINDOW - len(samples), 0)])
    samples = samples[:PHASE3_READINESS_WINDOW]

    benchmark = load_benchmark("r3_feature")
    variant_ids = [
        str(variant.get("id"))
        for variant in benchmark.get("variants", [])
        if variant.get("id")
    ]

    total = len(samples)
    clean_count = sum(1 for sample in samples if sample.get("completed_cleanly"))
    gate_count = sum(1 for sample in samples if sample.get("deterministic_gate_passed"))
    clean_rate = (clean_count / total) if total else 0.0
    gate_rate = (gate_count / total) if total else 0.0

    variants: dict[str, dict] = {}
    failing_variants: list[str] = []
    progress_variants: list[str] = []
    for variant_id in variant_ids:
        variant_samples = [
            sample
            for sample in samples
            if str(sample.get("variant_id") or "") == variant_id
        ]
        variant_total = len(variant_samples)
        variant_clean_count = sum(
            1 for sample in variant_samples if sample.get("completed_cleanly")
        )
        variant_gate_count = sum(
            1 for sample in variant_samples if sample.get("deterministic_gate_passed")
        )
        variant_clean_rate = (
            (variant_clean_count / variant_total) if variant_total else 0.0
        )
        variant_gate_rate = (
            (variant_gate_count / variant_total) if variant_total else 0.0
        )
        if variant_total < PHASE3_CALIBRATION_MAX_PER_VARIANT:
            variant_ready = False
            variant_reason = (
                f"need {PHASE3_CALIBRATION_MAX_PER_VARIANT} calibration samples, "
                f"have {variant_total}"
            )
            progress_variants.append(
                f"{variant_id}={variant_total}/{PHASE3_CALIBRATION_MAX_PER_VARIANT}"
            )
        elif (
            variant_clean_rate < PHASE3_MIN_CLEAN_RATE
            or variant_gate_rate < PHASE3_MIN_GATE_RATE
        ):
            variant_ready = False
            variant_reason = (
                f"{variant_id} clean={variant_clean_rate:.2f} "
                f"gate={variant_gate_rate:.2f} below threshold"
            )
            failing_variants.append(variant_reason)
        else:
            variant_ready = True
            variant_reason = (
                f"ready: clean={variant_clean_rate:.2f}, gate={variant_gate_rate:.2f}"
            )

        variants[variant_id] = {
            "variant_id": variant_id,
            "ready": variant_ready,
            "sample_count": variant_total,
            "target_sample_count": PHASE3_CALIBRATION_MAX_PER_VARIANT,
            "clean_completion_rate": round(variant_clean_rate, 4),
            "deterministic_gate_pass_rate": round(variant_gate_rate, 4),
            "reason": variant_reason,
        }

    if total < PHASE3_READINESS_WINDOW:
        ready = False
        progress = ", ".join(progress_variants) if progress_variants else f"{total}/{PHASE3_READINESS_WINDOW}"
        reason = (
            f"blocked: need {PHASE3_READINESS_WINDOW} calibration samples "
            f"({PHASE3_CALIBRATION_MAX_PER_VARIANT} per variant); progress {progress}"
        )
    elif clean_rate < PHASE3_MIN_CLEAN_RATE or gate_rate < PHASE3_MIN_GATE_RATE:
        ready = False
        reason = (
            "blocked: aggregate r3_feature health below threshold "
            f"(clean={clean_rate:.2f}, gate={gate_rate:.2f}, "
            f"need>={PHASE3_MIN_CLEAN_RATE:.2f}/{PHASE3_MIN_GATE_RATE:.2f})"
        )
    elif failing_variants:
        ready = False
        reason = "blocked: " + "; ".join(failing_variants)
    else:
        ready = True
        reason = (
            "ready: r3_feature health meets threshold "
            f"(clean={clean_rate:.2f}, gate={gate_rate:.2f})"
        )

    return {
        "phase": 3,
        "ready": ready,
        "window": PHASE3_READINESS_WINDOW,
        "sample_count": total,
        "clean_completion_rate": round(clean_rate, 4),
        "deterministic_gate_pass_rate": round(gate_rate, 4),
        "thresholds": {
            "clean_completion_rate": PHASE3_MIN_CLEAN_RATE,
            "deterministic_gate_pass_rate": PHASE3_MIN_GATE_RATE,
        },
        "variants": variants,
        "source": readiness_source,
        "reason": reason,
    }


def refresh_phase_readiness(
    evaluation_version: Optional[str],
    recent_results: Optional[list[dict]] = None,
) -> dict[str, dict]:
    """Recompute readiness snapshots and persist them into program state."""
    readiness = {"3": _compute_phase3_readiness(evaluation_version, recent_results)}
    readiness_source = {"3": readiness["3"].get("source", RUN_MODE_SEARCH)}
    blocked_reason = None
    if not readiness["3"]["ready"]:
        blocked_reason = readiness["3"]["reason"]
    update_program_state(
        phase_readiness=readiness,
        phase_readiness_source=readiness_source,
        phase_3_blocked_reason=blocked_reason,
    )
    return readiness


def _phase_ready(
    phase: int,
    state: Optional[dict] = None,
    recent_results: Optional[list[dict]] = None,
) -> tuple[bool, Optional[str], dict[str, dict]]:
    """Return readiness status, reason, and the full readiness snapshot."""
    state = state or get_program_state()
    evaluation_version = state.get("evaluation_version")
    readiness = refresh_phase_readiness(evaluation_version, recent_results)
    if phase != 3:
        return True, None, readiness
    snapshot = readiness.get("3", {})
    reason = snapshot.get("reason")
    return bool(snapshot.get("ready")), str(reason) if reason else None, readiness


def _attach_phase_readiness_snapshot(
    benchmark_results: list[dict],
    readiness: Optional[dict[str, dict]],
) -> None:
    """Attach the current phase-readiness snapshot to benchmark result payloads."""
    if not readiness:
        return
    snapshot = readiness.get("3")
    if snapshot is None:
        return
    for result in benchmark_results:
        result["phase_readiness_snapshot"] = snapshot


def _get_phase3_calibration_history(
    evaluation_version: Optional[str],
) -> list[dict]:
    """Return recent phase-3 calibration samples for readiness computation."""
    return get_recent_benchmark_results(
        "r3_feature",
        limit=1000,
        evaluation_version=evaluation_version,
        run_mode=RUN_MODE_PHASE3_CALIBRATION,
    )


def _get_phase3_calibration_variant_counts(
    evaluation_version: Optional[str],
) -> dict[str, int]:
    """Count calibration samples already recorded per r3_feature variant."""
    benchmark = load_benchmark("r3_feature")
    counts = {
        str(variant.get("id")): 0
        for variant in benchmark.get("variants", [])
        if variant.get("id")
    }
    for sample in get_recent_benchmark_results(
        "r3_feature",
        limit=1000,
        evaluation_version=evaluation_version,
        run_mode=RUN_MODE_PHASE3_CALIBRATION,
    ):
        variant_id = str(sample.get("variant_id") or "")
        if variant_id in counts:
            counts[variant_id] += 1
    return counts


def _select_phase3_calibration_variant(
    evaluation_version: Optional[str],
) -> Optional[str]:
    """Choose the next r3_feature variant for calibration in stable rotation."""
    counts = _get_phase3_calibration_variant_counts(evaluation_version)
    if not counts:
        return None
    eligible = {
        variant_id: count
        for variant_id, count in counts.items()
        if count < PHASE3_CALIBRATION_MAX_PER_VARIANT
    }
    if not eligible:
        return None
    min_count = min(eligible.values())
    for variant_id in sorted(eligible.keys()):
        if eligible[variant_id] == min_count:
            return variant_id
    return None


def _calibration_window_complete(
    evaluation_version: Optional[str],
) -> bool:
    """Return True once enough calibration samples exist for phase 3."""
    counts = _get_phase3_calibration_variant_counts(evaluation_version)
    if not counts:
        return False
    total = sum(counts.values())
    return (
        total >= PHASE3_READINESS_WINDOW
        and all(count >= PHASE3_CALIBRATION_MAX_PER_VARIANT for count in counts.values())
    )


# ---------------------------------------------------------------------------
# Convergence detection
# ---------------------------------------------------------------------------


def check_convergence(phase: int) -> bool:
    """Return True if the current phase has converged.

    Convergence occurs when:
    - The number of consecutive discards meets or exceeds CONVERGENCE_THRESHOLD, OR
    - There are no more untried mutations for the current phase.
    """
    state = get_program_state()
    evaluation_version = state.get("evaluation_version")
    consecutive = get_consecutive_discards(
        phase,
        evaluation_version=evaluation_version,
        run_mode=RUN_MODE_SEARCH,
    )
    if consecutive >= CONVERGENCE_THRESHOLD:
        log.info(
            "Convergence: %d consecutive discards >= threshold %d",
            consecutive,
            CONVERGENCE_THRESHOLD,
        )
        return True

    tried = get_tried_mutations(
        phase,
        evaluation_version=evaluation_version,
        run_mode=RUN_MODE_SEARCH,
    )
    next_mut = get_next_mutation(
        phase,
        tried,
        phase3_allowed_families=_get_phase3_allowed_families(state),
    )
    if next_mut is None:
        log.info(
            "Convergence: no more untried mutations for phase %d "
            "(tried %d mutations)",
            phase,
            len(tried),
        )
        return True

    return False


# ---------------------------------------------------------------------------
# Phase advancement
# ---------------------------------------------------------------------------


def advance_phase(current_phase: int) -> dict:
    """Advance to the next enabled phase or mark the run completed."""
    state = get_program_state()
    evaluation_version = state.get("evaluation_version")
    enabled_phases = _get_enabled_phases(state)

    try:
        current_idx = enabled_phases.index(current_phase)
    except ValueError:
        current_idx = -1

    next_phase = (
        enabled_phases[current_idx + 1]
        if current_idx >= 0 and current_idx + 1 < len(enabled_phases)
        else None
    )

    if next_phase is not None:
        ready, blocked_reason, readiness = _phase_ready(next_phase, state)
        if not ready:
            terminal_reason = f"phase_{next_phase}_blocked"
            log.info(
                "Stopping after phase %d because phase %d is blocked: %s",
                current_phase,
                next_phase,
                blocked_reason,
            )
            update_program_state(
                status="completed",
                terminal_reason=terminal_reason,
                last_completed_phase=current_phase,
                phase_readiness=readiness,
                phase_3_blocked_reason=blocked_reason,
            )
            return {
                "status": "run_completed",
                "old_phase": current_phase,
                "new_phase": current_phase,
                "terminal_reason": terminal_reason,
                "blocked_phase": next_phase,
                "blocked_reason": blocked_reason,
            }

    if next_phase is None:
        terminal_reason = f"phase_{current_phase}_converged"
        log.info(
            "Completed run after phase %d (experiment_count=%d, total_kept=%d, "
            "cumulative_improvement=%.2f%%)",
            current_phase,
            get_experiment_count(evaluation_version, run_mode=RUN_MODE_SEARCH),
            get_total_kept(evaluation_version, run_mode=RUN_MODE_SEARCH),
            get_cumulative_improvement(evaluation_version, run_mode=RUN_MODE_SEARCH),
        )
        update_program_state(
            status="completed",
            terminal_reason=terminal_reason,
            last_completed_phase=current_phase,
        )
        return {
            "status": "run_completed",
            "old_phase": current_phase,
            "new_phase": current_phase,
            "terminal_reason": terminal_reason,
        }

    log.info(
        "Advancing from phase %d to phase %d (experiment_count=%d, "
        "total_kept=%d, cumulative_improvement=%.2f%%)",
        current_phase,
        next_phase,
        get_experiment_count(evaluation_version, run_mode=RUN_MODE_SEARCH),
        get_total_kept(evaluation_version, run_mode=RUN_MODE_SEARCH),
        get_cumulative_improvement(evaluation_version, run_mode=RUN_MODE_SEARCH),
    )
    update_program_state(
        current_phase=next_phase,
        status="running",
        terminal_reason=None,
        last_completed_phase=current_phase,
    )
    return {
        "status": "phase_advanced",
        "old_phase": current_phase,
        "new_phase": next_phase,
    }


# ---------------------------------------------------------------------------
# Confirmation trial
# ---------------------------------------------------------------------------


def _trial_record_from_eval(
    benchmark_results: list[dict],
    eval_result: dict,
    trial_index: int,
) -> dict:
    """Convert one scored benchmark run into a persisted trial record."""
    trial_clean = all(
        bool(result.get("completed_cleanly")) for result in benchmark_results
    )
    return {
        "trial_index": trial_index,
        "composite_score": eval_result["scores"]["composite"],
        "quality_score": eval_result["scores"]["quality"],
        "speed_score": eval_result["scores"]["speed"],
        "improvement_pct": eval_result["improvement_pct"],
        "trial_clean": trial_clean,
        "benchmark_retry_count": sum(
            int(result.get("benchmark_retry_count", 0))
            for result in benchmark_results
        ),
        "benchmark_results": benchmark_results,
    }


def run_confirmation_trials(
    mutation: dict,
    baseline_score: float,
    benchmark_ids: Optional[list[str]],
    initial_benchmark_results: list[dict],
    initial_eval_result: dict,
) -> dict:
    """Run two additional mutation trials and decide from median performance."""
    log.info(
        "Starting confirmation trials for mutation: %s",
        mutation.get("summary", "?"),
    )

    trial_records = [
        _trial_record_from_eval(initial_benchmark_results, initial_eval_result, 1)
    ]

    for trial_index in (2, 3):
        restore_snapshot("checkpoint")
        log.info("Confirmation trial %d: re-applying mutation", trial_index)
        apply_mutation(mutation)
        benchmark_results = run_full_suite(benchmark_ids)
        eval_result = evaluate_experiment(
            current_results=benchmark_results,
            baseline_score=baseline_score,
        )
        trial_records.append(
            _trial_record_from_eval(benchmark_results, eval_result, trial_index)
        )
        log.info(
            "Confirmation trial %d scored: composite=%.2f quality=%.2f speed=%.2f",
            trial_index,
            eval_result["scores"]["composite"],
            eval_result["scores"]["quality"],
            eval_result["scores"]["speed"],
        )

    composites = [trial["composite_score"] for trial in trial_records]
    qualities = [trial["quality_score"] for trial in trial_records]
    median_composite = statistics.median(composites)
    median_quality = statistics.median(qualities)
    quality_spread = max(qualities) - min(qualities) if qualities else 0.0
    median_improvement = median_composite - baseline_score
    clean_trial_count = sum(1 for trial in trial_records if trial.get("trial_clean"))
    confirmed = (
        median_improvement > NOISE_BAND
        and quality_spread <= 15.0
        and clean_trial_count >= 2
    )

    if confirmed:
        restore_snapshot("checkpoint")
        apply_mutation(mutation)
    else:
        restore_snapshot("checkpoint")

    log.info(
        "Confirmation trials result: %s (median_composite=%.2f, "
        "median_improvement=%.2f, quality_spread=%.2f, clean_trials=%d/3)",
        "CONFIRMED" if confirmed else "NOT CONFIRMED",
        median_composite,
        median_improvement,
        quality_spread,
        clean_trial_count,
    )

    return {
        "confirmed": confirmed,
        "trial_count": len(trial_records),
        "median_composite": median_composite,
        "median_quality": median_quality,
        "median_speed": statistics.median(
            [trial["speed_score"] for trial in trial_records]
        ),
        "median_improvement_pct": (
            ((median_composite - baseline_score) / baseline_score) * 100.0
            if baseline_score > 0.0
            else (median_composite - baseline_score)
        ),
        "quality_spread": quality_spread,
        "clean_trial_count": clean_trial_count,
        "trials": trial_records,
    }


def run_phase3_calibration_sample() -> dict:
    """Run one non-mutating r3_feature calibration sample."""
    state = get_program_state()
    evaluation_version, run_mode = _current_run_scope(state)
    if _calibration_window_complete(evaluation_version):
        readiness = refresh_phase_readiness(evaluation_version)
        terminal_reason = "phase_3_calibration_completed"
        update_program_state(
            status="completed",
            terminal_reason=terminal_reason,
            last_calibration_completed_at=_current_timestamp(),
            phase_readiness=readiness,
            phase_3_blocked_reason=(
                readiness.get("3", {}).get("reason")
                if not readiness.get("3", {}).get("ready")
                else None
            ),
        )
        return {
            "status": "run_completed",
            "phase": PHASE3_CALIBRATION_PHASE,
            "terminal_reason": terminal_reason,
        }

    variant_id = _select_phase3_calibration_variant(evaluation_version)
    if not variant_id:
        terminal_reason = "phase_3_calibration_exhausted"
        update_program_state(
            status="completed",
            terminal_reason=terminal_reason,
            last_calibration_completed_at=_current_timestamp(),
        )
        return {
            "status": "run_completed",
            "phase": PHASE3_CALIBRATION_PHASE,
            "terminal_reason": terminal_reason,
        }

    mutation_summary = f"calibration:{variant_id}"
    experiment_id = log_experiment_start(
        phase=PHASE3_CALIBRATION_PHASE,
        mutation_summary=mutation_summary,
        mutation_json=json.dumps(
            {
                "mode": RUN_MODE_PHASE3_CALIBRATION,
                "benchmark_id": "r3_feature",
                "variant_id": variant_id,
            }
        ),
        knobs_changed=0,
        evaluation_version=evaluation_version,
        run_mode=run_mode,
        calibration_sample=1,
        readiness_contribution=1,
    )
    log.info(
        "Calibration sample %d started for r3_feature/%s",
        experiment_id,
        variant_id,
    )

    try:
        benchmark_results = run_full_suite(
            ["r3_feature"],
            variant_overrides={"r3_feature": variant_id},
        )
        readiness = refresh_phase_readiness(
            evaluation_version,
            [
                result
                for result in benchmark_results
                if result.get("benchmark_id") == "r3_feature"
            ] or None,
        )
        _attach_phase_readiness_snapshot(benchmark_results, readiness)

        for br in benchmark_results:
            if _is_rate_limit_error(br.get("error")):
                raise _RateLimitError(
                    f"Rate limit hit during calibration benchmark: {br.get('error')}"
                )

        eval_result = evaluate_experiment(
            current_results=benchmark_results,
            baseline_score=0.0,
        )
        scores = eval_result["scores"]
        wall_time = sum(
            br.get("wall_time_seconds", 0.0) for br in benchmark_results
        )
        log_experiment_result(
            experiment_id=experiment_id,
            status="completed",
            composite_score=scores.get("composite"),
            quality_score=scores.get("quality"),
            speed_score=scores.get("speed"),
            wall_time_seconds=wall_time,
            benchmark_results=json.dumps(benchmark_results, default=str),
            kept=0,
            baseline_score_before=0.0,
            improvement_pct=None,
            decision="calibration",
            trial_count=1,
            config_hash=compute_config_hash(),
        )
        record_success()
        sample_count = get_experiment_count(evaluation_version, run_mode=run_mode)
        update_program_state(
            experiment_count=sample_count,
            phase_readiness=readiness,
            phase_3_blocked_reason=(
                readiness.get("3", {}).get("reason")
                if not readiness.get("3", {}).get("ready")
                else None
            ),
            current_phase=PHASE3_CALIBRATION_PHASE,
        )
        if _calibration_window_complete(evaluation_version):
            terminal_reason = "phase_3_calibration_completed"
            update_program_state(
                status="completed",
                terminal_reason=terminal_reason,
                last_calibration_completed_at=_current_timestamp(),
            )
            return {
                "status": "run_completed",
                "phase": PHASE3_CALIBRATION_PHASE,
                "terminal_reason": terminal_reason,
                "experiment_id": experiment_id,
            }
        return {
            "status": "calibration_sampled",
            "phase": PHASE3_CALIBRATION_PHASE,
            "variant_id": variant_id,
            "experiment_id": experiment_id,
        }
    except _RateLimitError:
        log_experiment_result(
            experiment_id=experiment_id,
            status="rate_limited",
            decision="calibration",
            error_message="Rate limit (429) hit during phase-3 calibration",
            config_hash=compute_config_hash(),
        )
        raise
    except Exception as exc:
        log_experiment_result(
            experiment_id=experiment_id,
            status="error",
            decision="calibration",
            error_message=str(exc)[:2000],
            config_hash=compute_config_hash(),
        )
        return {
            "status": "error",
            "phase": PHASE3_CALIBRATION_PHASE,
            "variant_id": variant_id,
            "experiment_id": experiment_id,
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Rate limit / network error detection
# ---------------------------------------------------------------------------


def _is_rate_limit_error(error_msg: Optional[str]) -> bool:
    """Check if an error message indicates a rate limit (429)."""
    if not error_msg:
        return False
    lower = error_msg.lower()
    return "429" in lower or "rate limit" in lower


def _is_network_error(error_msg: Optional[str]) -> bool:
    """Check if an error message indicates a network issue."""
    if not error_msg:
        return False
    lower = error_msg.lower()
    network_indicators = [
        "connection refused",
        "network",
        "timeout",
        "dns",
        "eof",
        "broken pipe",
        "connection reset",
        "unreachable",
    ]
    return any(indicator in lower for indicator in network_indicators)


# ---------------------------------------------------------------------------
# Core experiment
# ---------------------------------------------------------------------------


def run_one_experiment() -> dict:
    """Execute a single experiment iteration.

    Returns a result summary dict with keys: status, phase, mutation,
    decision, scores, improvement_pct, experiment_id, etc.
    """
    state = get_program_state()
    run_mode = _get_run_mode(state)
    if run_mode == RUN_MODE_PHASE3_CALIBRATION:
        return run_phase3_calibration_sample()

    enabled_phases = _get_enabled_phases(state)
    phase = state.get("current_phase", enabled_phases[0])
    if phase not in enabled_phases:
        phase = enabled_phases[0]
        update_program_state(current_phase=phase)
    baseline_score = state.get("baseline_score", 0.0)
    phase3_allowed_families = _get_phase3_allowed_families(state)
    evaluation_version = state.get("evaluation_version")

    if state.get("status") == "completed":
        return {
            "status": "run_completed",
            "phase": phase,
            "terminal_reason": state.get("terminal_reason"),
        }

    ready, blocked_reason, readiness = _phase_ready(phase, state)
    if not ready:
        terminal_reason = f"phase_{phase}_blocked"
        update_program_state(
            status="completed",
            terminal_reason=terminal_reason,
            phase_readiness=readiness,
            phase_3_blocked_reason=blocked_reason,
        )
        return {
            "status": "run_completed",
            "phase": phase,
            "terminal_reason": terminal_reason,
            "blocked_reason": blocked_reason,
        }

    log.info("=== Starting experiment (phase=%d, baseline=%.2f) ===", phase, baseline_score)

    # Advance immediately once the current phase has converged; do not keep
    # spending experiments after the discard threshold has already been met.
    if check_convergence(phase):
        return advance_phase(phase)

    # 1. Get tried mutations
    tried = get_tried_mutations(
        phase,
        evaluation_version=evaluation_version,
        run_mode=RUN_MODE_SEARCH,
    )
    log.debug("Phase %d: %d mutations already tried", phase, len(tried))

    # 2. Get next mutation
    mutation = get_next_mutation(
        phase,
        tried,
        phase3_allowed_families=phase3_allowed_families,
    )
    if mutation is None:
        log.info("No more mutations available for phase %d", phase)
        if check_convergence(phase):
            return advance_phase(phase)
        return {"status": "no_mutations", "phase": phase}

    mutation_summary = mutation.get("summary", json.dumps(mutation))
    knobs_changed = mutation.get("knobs_changed", 1)
    log.info("Selected mutation: %s (knobs=%d)", mutation_summary, knobs_changed)

    # 3. Save checkpoint
    save_snapshot("checkpoint")
    log.debug("Checkpoint saved")

    experiment_id = None
    result_summary = {
        "status": "error",
        "phase": phase,
        "mutation": mutation_summary,
    }

    try:
        # 4. Log experiment start
        experiment_id = log_experiment_start(
            phase=phase,
            mutation_summary=mutation_summary,
            mutation_json=json.dumps(mutation),
            knobs_changed=knobs_changed,
            evaluation_version=evaluation_version,
            run_mode=RUN_MODE_SEARCH,
            calibration_sample=0,
            readiness_contribution=0,
        )
        log.info("Experiment %d started", experiment_id)

        # 5. Validate mutation
        is_valid, validation_msg = validate_mutation(mutation)
        if not is_valid:
            log.warning(
                "Mutation validation failed: %s — skipping", validation_msg
            )
            log_experiment_result(
                experiment_id=experiment_id,
                status="skipped",
                error_message=f"Validation failed: {validation_msg}",
                config_hash=compute_config_hash(),
            )
            mark_checkpoint_clean()
            return {
                "status": "skipped",
                "phase": phase,
                "mutation": mutation_summary,
                "reason": validation_msg,
                "experiment_id": experiment_id,
            }

        # 6. Apply mutation
        log.info("Applying mutation: %s", mutation_summary)
        try:
            apply_mutation(mutation)
        except Exception as apply_err:
            log.error("Failed to apply mutation: %s", apply_err)
            restore_snapshot("checkpoint")
            mark_checkpoint_clean()
            log_experiment_result(
                experiment_id=experiment_id,
                status="error",
                error_message=f"Apply failed: {apply_err}",
                config_hash=compute_config_hash(),
            )
            return {
                "status": "apply_error",
                "phase": phase,
                "mutation": mutation_summary,
                "error": str(apply_err),
                "experiment_id": experiment_id,
            }

        # 7. Select benchmarks and run suite
        benchmark_ids = select_benchmarks_for_phase(phase)
        log.info(
            "Running benchmarks: %s",
            benchmark_ids if benchmark_ids else "ALL",
        )
        benchmark_results = run_full_suite(benchmark_ids)
        readiness = refresh_phase_readiness(
            evaluation_version,
            [
                result
                for result in benchmark_results
                if result.get("benchmark_id") == "r3_feature"
            ] or None,
        )
        _attach_phase_readiness_snapshot(benchmark_results, readiness)
        log.info("Benchmarks complete: %d results", len(benchmark_results))

        # Check for rate limit errors in benchmark results
        for br in benchmark_results:
            if _is_rate_limit_error(br.get("error")):
                raise _RateLimitError(
                    f"Rate limit hit during benchmark: {br.get('error')}"
                )

        # 8. Score results
        eval_result = evaluate_experiment(
            current_results=benchmark_results,
            baseline_score=baseline_score,
        )
        scores = eval_result["scores"]
        composite = scores["composite"]
        improvement = eval_result["improvement"]
        improvement_pct = eval_result["improvement_pct"]
        decision = eval_result["decision"]
        classification = eval_result["classification"]

        log.info(
            "Experiment %d scored: composite=%.2f, improvement=%.2f (%.2f%%), "
            "classification=%s, decision=%s",
            experiment_id,
            composite,
            improvement,
            improvement_pct,
            classification,
            decision,
        )

        wall_time = sum(
            br.get("wall_time_seconds", 0.0) for br in benchmark_results
        )

        # 9. Decision logic
        kept = 0
        final_decision = decision
        trial_count = 1
        confirmation_results_json = None

        if decision == "discard":
            log.info("Discarding mutation (improvement in noise band)")
            restore_snapshot("checkpoint")
            mark_checkpoint_clean()

        elif decision == "needs_confirmation":
            log.info(
                "Improvement cleared noise band — running additional confirmation trials"
            )
            confirmation = run_confirmation_trials(
                mutation=mutation,
                baseline_score=baseline_score,
                benchmark_ids=benchmark_ids,
                initial_benchmark_results=benchmark_results,
                initial_eval_result=eval_result,
            )
            trial_count = int(confirmation["trial_count"])
            confirmation_results_json = json.dumps(confirmation, default=str)
            composite = confirmation["median_composite"]
            scores["composite"] = composite
            scores["quality"] = confirmation["median_quality"]
            scores["speed"] = confirmation["median_speed"]
            improvement = composite - baseline_score
            improvement_pct = confirmation["median_improvement_pct"]
            median_trial = sorted(
                confirmation["trials"],
                key=lambda trial: trial["composite_score"],
            )[len(confirmation["trials"]) // 2]
            benchmark_results = median_trial["benchmark_results"]
            wall_time = sum(
                br.get("wall_time_seconds", 0.0) for br in benchmark_results
            )
            if confirmation["confirmed"]:
                log.info("Confirmation trial PASSED — keeping mutation")
                save_snapshot("baseline", score=composite)
                best_score = state.get("best_score", 0.0)
                if composite > best_score:
                    save_snapshot("best", score=composite)
                    log.info(
                        "New best score: %.2f (previous best: %.2f)",
                        composite,
                        best_score,
                    )
                    update_program_state(best_score=composite)
                mark_checkpoint_clean()
                kept = 1
                final_decision = "keep"
            else:
                log.info("Confirmation trial FAILED — discarding mutation")
                mark_checkpoint_clean()
                final_decision = "discard"

        # 10. Log experiment result to DB
        cumulative = get_cumulative_improvement(
            evaluation_version,
            run_mode=RUN_MODE_SEARCH,
        )
        if kept:
            # Add this experiment's improvement to cumulative
            cumulative = cumulative + improvement_pct

        log_experiment_result(
            experiment_id=experiment_id,
            status="completed",
            composite_score=composite,
            quality_score=scores.get("quality"),
            speed_score=scores.get("speed"),
            wall_time_seconds=wall_time,
            benchmark_results=json.dumps(benchmark_results, default=str),
            kept=kept,
            baseline_score_before=baseline_score,
            improvement_pct=improvement_pct,
            decision=final_decision,
            trial_count=trial_count,
            confirmation_results=confirmation_results_json,
            config_hash=compute_config_hash(),
            cumulative_improvement_pct=cumulative if kept else None,
        )

        # 11. Update program state
        new_state_updates = {
            "experiment_count": get_experiment_count(
                evaluation_version,
                run_mode=RUN_MODE_SEARCH,
            ),
            "phase_readiness": readiness,
            "phase_3_blocked_reason": (
                readiness.get("3", {}).get("reason")
                if readiness and not readiness.get("3", {}).get("ready")
                else None
            ),
        }
        if kept:
            new_state_updates["baseline_score"] = composite
        update_program_state(**new_state_updates)

        # 12. Record success (for rate limiter)
        record_success()

        # 13. Notify on significant improvement
        if kept and improvement_pct > 0:
            notify_improvement(experiment_id, improvement_pct, mutation_summary)

        result_summary = {
            "status": "completed",
            "phase": phase,
            "mutation": mutation_summary,
            "decision": final_decision,
            "composite_score": composite,
            "improvement_pct": improvement_pct,
            "kept": bool(kept),
            "trial_count": trial_count,
            "experiment_id": experiment_id,
        }
        log.info(
            "=== Experiment %d complete: decision=%s, kept=%s ===",
            experiment_id,
            final_decision,
            bool(kept),
        )
        return result_summary

    except _RateLimitError:
        # Re-raise so the main loop handles it specifically
        if experiment_id is not None:
            log_experiment_result(
                experiment_id=experiment_id,
                status="rate_limited",
                error_message="Rate limit (429) hit during benchmarks",
                config_hash=compute_config_hash(),
            )
        if has_dirty_checkpoint():
            restore_snapshot("checkpoint")
            mark_checkpoint_clean()
        raise

    except KeyboardInterrupt:
        log.warning("KeyboardInterrupt during experiment %s", experiment_id)
        if has_dirty_checkpoint():
            log.info("Restoring checkpoint after KeyboardInterrupt")
            restore_snapshot("checkpoint")
            mark_checkpoint_clean()
        raise

    except Exception as exc:
        log.error(
            "Experiment %s failed with error: %s",
            experiment_id,
            exc,
            exc_info=True,
        )
        if has_dirty_checkpoint():
            log.info("Restoring checkpoint after experiment error")
            restore_snapshot("checkpoint")
            mark_checkpoint_clean()
        if experiment_id is not None:
            log_experiment_result(
                experiment_id=experiment_id,
                status="error",
                error_message=str(exc)[:2000],
                config_hash=compute_config_hash(),
            )
        return {
            "status": "error",
            "phase": phase,
            "mutation": mutation_summary,
            "error": str(exc),
            "experiment_id": experiment_id,
        }


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------


def notify_improvement(
    experiment_id: int, improvement_pct: float, mutation_summary: str
) -> None:
    """Send a notification when a significant improvement is found.

    If improvement > 5%, send notification via ~/.claude/bin/notify_done.sh
    if that script exists.  Log the improvement regardless.
    """
    log.info(
        "Improvement found: experiment=%d, improvement=%.2f%%, mutation=%s",
        experiment_id,
        improvement_pct,
        mutation_summary,
    )

    if improvement_pct > 5.0:
        notify_script = CLAUDE_HOME / "bin" / "notify_done.sh"
        if notify_script.is_file():
            message = (
                f"Autoconfig experiment {experiment_id}: "
                f"+{improvement_pct:.1f}% improvement — {mutation_summary}"
            )
            try:
                subprocess.run(
                    [str(notify_script), message],
                    capture_output=True,
                    timeout=10,
                )
                log.info("Notification sent for experiment %d", experiment_id)
            except (subprocess.TimeoutExpired, OSError) as exc:
                log.warning("Failed to send notification: %s", exc)
        else:
            log.debug(
                "Notification script not found at %s — skipping",
                notify_script,
            )


# ---------------------------------------------------------------------------
# Internal exception for rate limit propagation
# ---------------------------------------------------------------------------


class _RateLimitError(Exception):
    """Raised when a rate limit (429) is detected during benchmarks."""
    pass


class _NetworkError(Exception):
    """Raised when a network error is detected during benchmarks."""
    pass


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------


def _handle_signal(signum: int, frame) -> None:
    """Handle SIGTERM and SIGINT gracefully."""
    global _shutdown_requested
    sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    log.info("Received signal %s — initiating graceful shutdown", sig_name)
    _shutdown_requested = True


# ---------------------------------------------------------------------------
# Main daemon loop
# ---------------------------------------------------------------------------


def main() -> None:
    """Main daemon entry point.  Runs experiments in a loop forever."""
    global _shutdown_requested

    # Set up logging first
    setup_logging()

    # Install signal handlers
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Crash recovery
    crash_recovery()

    # Initialize baseline snapshot if none exists
    if not snapshot_exists("baseline"):
        log.info("No baseline snapshot found — creating initial baseline")
        save_snapshot("baseline")

    state = get_program_state()
    run_mode = _get_run_mode(state)
    if state.get("status") == "completed":
        log.info(
            "Program state already completed (run_mode=%s, terminal_reason=%s) — exiting",
            run_mode,
            state.get("terminal_reason"),
        )
        return

    update_program_state(
        status="running",
        terminal_reason=None,
        evaluation_version=EVALUATION_VERSION,
        scoring_version=SCORING_VERSION,
        run_mode=run_mode,
    )

    log.info(
        "Autoconfig daemon started (pid=%d, cooldown=%ds, "
        "conflict_wait=%ds, convergence_threshold=%d, enabled_phases=%s, run_mode=%s)",
        os.getpid(),
        COOLDOWN_SECONDS,
        CONFLICT_WAIT_SECONDS,
        CONVERGENCE_THRESHOLD,
        _get_enabled_phases(),
        run_mode,
    )

    state = get_program_state()
    run_mode = _get_run_mode(state)
    evaluation_version = state.get("evaluation_version")
    enabled_phases = _get_enabled_phases(state)
    readiness = refresh_phase_readiness(evaluation_version)
    if get_experiment_count(evaluation_version, run_mode=run_mode) == 0:
        log.info(
            "No experiments found for evaluation version %s in run_mode %s — bootstrapping state",
            evaluation_version,
            run_mode,
        )
        updates = {
            "experiment_count": 0,
            "last_completed_phase": None,
            "status": "running",
            "terminal_reason": None,
            "phase_readiness": readiness,
            "phase_3_blocked_reason": (
                readiness.get("3", {}).get("reason")
                if not readiness.get("3", {}).get("ready")
                else None
            ),
        }
        if run_mode == RUN_MODE_SEARCH:
            save_snapshot("baseline")
            save_snapshot("best", score=0.0)
            updates.update(
                current_phase=enabled_phases[0],
                baseline_score=0.0,
                best_score=0.0,
            )
        else:
            updates.update(current_phase=PHASE3_CALIBRATION_PHASE)
        update_program_state(**updates)
        state = get_program_state()
        evaluation_version = state.get("evaluation_version")
    else:
        update_program_state(
            phase_readiness=readiness,
            phase_3_blocked_reason=(
                readiness.get("3", {}).get("reason")
                if not readiness.get("3", {}).get("ready")
                else None
            ),
        )
    log.info(
        "Current state: phase=%d, run_mode=%s, experiment_count=%d, baseline_score=%.2f, "
        "best_score=%.2f, cumulative_improvement=%.2f%%",
        state.get("current_phase", 1),
        run_mode,
        state.get("experiment_count", 0),
        state.get("baseline_score", 0.0),
        state.get("best_score", 0.0),
        get_cumulative_improvement(evaluation_version, run_mode=RUN_MODE_SEARCH),
    )

    while not _shutdown_requested:
        try:
            # a. Check for interactive session
            if is_interactive_session_active():
                log.info(
                    "Interactive session active — waiting %ds before retrying",
                    CONFLICT_WAIT_SECONDS,
                )
                time.sleep(CONFLICT_WAIT_SECONDS)
                continue

            # b. Check rate limit backoff
            wait_needed, wait_seconds = should_wait()
            if wait_needed:
                log.info(
                    "Rate limit backoff active — sleeping %ds", wait_seconds
                )
                time.sleep(wait_seconds)
                continue

            # c. Run one experiment
            result = run_one_experiment()
            log.info("Experiment result: %s", result.get("status", "unknown"))
            if result.get("status") == "run_completed":
                log.info(
                    "Run reached terminal state: %s",
                    result.get("terminal_reason", "completed"),
                )
                _shutdown_requested = True
                continue

        except _RateLimitError as exc:
            log.warning("Rate limit detected: %s", exc)
            record_rate_limit()
            _, backoff = should_wait()
            log.info("Rate limit backoff: sleeping %ds", backoff)
            time.sleep(backoff if backoff > 0 else COOLDOWN_SECONDS)
            continue

        except _NetworkError as exc:
            log.warning("Network error: %s — sleeping %ds", exc, NETWORK_RETRY_SECONDS)
            time.sleep(NETWORK_RETRY_SECONDS)
            continue

        except KeyboardInterrupt:
            log.info("KeyboardInterrupt received — shutting down")
            _shutdown_requested = True
            break

        except Exception as exc:
            log.error(
                "Unexpected error in main loop: %s", exc, exc_info=True
            )
            # Ensure checkpoint is clean
            if has_dirty_checkpoint():
                log.info("Restoring checkpoint after unexpected error")
                try:
                    restore_snapshot("checkpoint")
                    mark_checkpoint_clean()
                except Exception as restore_err:
                    log.error("Failed to restore checkpoint: %s", restore_err)

            # Check if this was a rate limit or network error hiding in
            # a generic exception
            error_str = str(exc)
            if _is_rate_limit_error(error_str):
                record_rate_limit()
                _, backoff = should_wait()
                log.info("Rate limit in exception — sleeping %ds", backoff)
                time.sleep(backoff if backoff > 0 else COOLDOWN_SECONDS)
                continue
            if _is_network_error(error_str):
                log.info("Network error in exception — sleeping %ds", NETWORK_RETRY_SECONDS)
                time.sleep(NETWORK_RETRY_SECONDS)
                continue

            time.sleep(COOLDOWN_SECONDS)
            continue

        # g. Cooldown between experiments
        if not _shutdown_requested:
            log.debug("Cooldown: sleeping %ds", COOLDOWN_SECONDS)
            time.sleep(COOLDOWN_SECONDS)

    # Graceful shutdown
    log.info("Daemon shutting down")
    if has_dirty_checkpoint():
        log.warning("Dirty checkpoint found at shutdown — restoring")
        try:
            restore_snapshot("checkpoint")
            mark_checkpoint_clean()
        except Exception as exc:
            log.error("Failed to restore checkpoint during shutdown: %s", exc)

    log.info(
        "Daemon stopped (total_experiments=%d, total_kept=%d, "
        "cumulative_improvement=%.2f%%)",
        get_experiment_count(evaluation_version, run_mode=run_mode),
        get_total_kept(evaluation_version, run_mode=RUN_MODE_SEARCH),
        get_cumulative_improvement(evaluation_version, run_mode=RUN_MODE_SEARCH),
    )


if __name__ == "__main__":
    main()
