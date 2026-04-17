"""Composite scoring module for autoconfig experiments.

Computes a composite metric from benchmark results and implements
the confirmation-trial logic that decides whether an experiment
should be kept, discarded, or re-run for confirmation.

Composite formula:
    composite = (quality * 0.75) + (speed * 0.25)

All sub-scores are normalized to [0, 100].
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QUALITY_WEIGHT: float = 0.75
SPEED_WEIGHT: float = 0.25

NOISE_BAND: float = 3.0  # Improvements below this are inconclusive
CLEAR_IMPROVEMENT_THRESHOLD: float = 15.0  # Skip confirmation above this

ROUTE_WEIGHTS: dict[str, float] = {
    "R1": 0.10,
    "R2": 0.25,
    "R3": 0.40,
    "R4": 0.25,
}

DEFAULT_BASELINES: dict[str, float] = {
    "r1_factual": 15.0,
    "r2_small_impl": 60.0,
    "r3_feature": 180.0,
    "r4_auth_review": 120.0,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _route_key(route_or_benchmark: str) -> str:
    """Normalize a route string to the canonical Rn key used in ROUTE_WEIGHTS.

    Accepts 'R1', 'r1', 'R1', or benchmark IDs like 'r1_factual'.
    """
    upper = route_or_benchmark.upper()
    for key in ROUTE_WEIGHTS:
        if upper.startswith(key):
            return key
    return upper[:2] if len(upper) >= 2 else upper


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Clamp *value* into [lo, hi]."""
    return max(lo, min(hi, value))


def _compute_improvement_pct(improvement: float, baseline_score: float) -> float:
    """Return a finite percentage-like improvement metric.

    For baseline scores above zero, this is the standard relative percentage.
    For the bootstrap case where the baseline is zero or unset, fall back to
    the composite delta itself. Composite scores already live on a 0-100 scale,
    so this behaves like "percentage points gained from zero" instead of
    producing infinities that poison dashboards and aggregate analysis.
    """
    if baseline_score > 0.0:
        return (improvement / baseline_score) * 100.0
    return improvement


def _benchmark_execution_failed(result: dict) -> bool:
    """Return True when a benchmark run failed before producing valid signal."""
    if result.get("completed_cleanly") is False:
        return True
    if result.get("timed_out"):
        return True
    if result.get("error"):
        return True
    exit_code = result.get("exit_code")
    return exit_code not in (None, 0)


def _compute_benchmark_quality(result: dict) -> float:
    """Compute a benchmark-level quality score from deterministic and semantic signals."""
    acceptance = result.get("acceptance", {})
    pass_rate = float(acceptance.get("pass_rate", 0.0))
    retry_count = int(result.get("retry_count", 0))
    deterministic_score = float(
        result.get("deterministic_quality_score", pass_rate * 100.0)
    )
    semantic_score = result.get("semantic_quality_score")

    if (
        not result.get("completed_cleanly", False)
        or not result.get("deterministic_gate_passed", pass_rate == 1.0)
    ):
        benchmark_quality = 0.0
    elif semantic_score is None:
        benchmark_quality = deterministic_score
    else:
        benchmark_quality = (float(semantic_score) * 0.8) + (deterministic_score * 0.2)

    retry_penalty = min(retry_count * 10, 30)
    return max(benchmark_quality - retry_penalty, 0.0)


def _resolve_result_baseline_time(
    result: dict,
    baseline_times: Optional[dict[str, float]] = None,
) -> float:
    """Resolve speed baseline using result metadata first, then fallbacks."""
    result_baseline = result.get("speed_baseline_seconds")
    if result_baseline is not None:
        try:
            return float(result_baseline)
        except (TypeError, ValueError):
            pass

    benchmark_id = result.get("benchmark_id", "")
    if baseline_times and benchmark_id in baseline_times:
        try:
            return float(baseline_times[benchmark_id])
        except (TypeError, ValueError):
            pass

    try:
        return float(DEFAULT_BASELINES.get(benchmark_id, 0.0))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------

def compute_quality_score(benchmark_results: list[dict]) -> float:
    """Compute weighted quality score from benchmark results.

    Each element in *benchmark_results* must have at least::

        {
            "benchmark_id": str,
            "route": str,            # e.g. "R2" or "r2_small_impl"
            "acceptance": {"pass_rate": float, ...},
        }

    ``pass_rate`` is expected in [0.0, 1.0].

    Returns a score in [0, 100].
    """
    if not benchmark_results:
        return 0.0

    weighted_sum = 0.0
    weight_sum = 0.0

    for result in benchmark_results:
        route = _route_key(result.get("route", result.get("benchmark_id", "")))
        weight = ROUTE_WEIGHTS.get(route, 0.0)
        if weight == 0.0:
            continue

        benchmark_quality = _compute_benchmark_quality(result)

        weighted_sum += benchmark_quality * weight
        weight_sum += weight

    if weight_sum == 0.0:
        return 0.0

    return _clamp(weighted_sum / weight_sum)


# ---------------------------------------------------------------------------
# Speed scoring
# ---------------------------------------------------------------------------

def compute_speed_score(
    benchmark_results: list[dict],
    baseline_times: Optional[dict[str, float]] = None,
) -> float:
    """Compute weighted speed score from benchmark results.

    *baseline_times* maps ``benchmark_id`` to the baseline wall-time in
    seconds.  Falls back to :data:`DEFAULT_BASELINES` when a key is missing.

    Speed per benchmark::

        speed = 100 - ((actual_time / baseline_time) - 0.5) * 66.7

    Clamped to [0, 100], then averaged using the same route weights as
    quality scoring.

    Returns a score in [0, 100].
    """
    if not benchmark_results:
        return 0.0

    weighted_sum = 0.0
    weight_sum = 0.0

    for result in benchmark_results:
        benchmark_id = result.get("benchmark_id", "")
        route = _route_key(result.get("route", benchmark_id))
        weight = ROUTE_WEIGHTS.get(route, 0.0)
        if weight == 0.0:
            continue

        actual_time = float(result.get("wall_time_seconds", 0.0))
        baseline_time = _resolve_result_baseline_time(result, baseline_times)

        if _benchmark_execution_failed(result):
            benchmark_speed = 0.0
        elif baseline_time <= 0.0:
            # No usable baseline — treat as perfect speed to avoid penalizing.
            benchmark_speed = 100.0
        else:
            ratio = actual_time / baseline_time
            benchmark_speed = 100.0 - ((ratio - 0.5) * 66.7)

        weighted_sum += _clamp(benchmark_speed) * weight
        weight_sum += weight

    if weight_sum == 0.0:
        return 0.0

    return _clamp(weighted_sum / weight_sum)


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------

def compute_composite(
    benchmark_results: list[dict],
    baseline_times: Optional[dict[str, float]] = None,
) -> dict:
    """Compute composite metric and per-benchmark breakdown.

    Returns::

        {
            "composite": float,
            "quality": float,
            "speed": float,
            "per_benchmark": {
                "<benchmark_id>": {
                    "quality": float,
                    "speed": float,
                    "pass_rate": float,
                    "wall_time": float,
                },
                ...
            },
        }
    """
    quality = compute_quality_score(benchmark_results)
    speed = compute_speed_score(benchmark_results, baseline_times)
    composite = (quality * QUALITY_WEIGHT) + (speed * SPEED_WEIGHT)

    per_benchmark: dict[str, dict] = {}
    for result in benchmark_results:
        benchmark_id = result.get("benchmark_id", "unknown")

        # Per-benchmark quality
        acceptance = result.get("acceptance", {})
        pass_rate = float(acceptance.get("pass_rate", 0.0))
        bm_quality = _compute_benchmark_quality(result)

        # Per-benchmark speed
        actual_time = float(result.get("wall_time_seconds", 0.0))
        baseline_time = _resolve_result_baseline_time(result, baseline_times)
        if _benchmark_execution_failed(result):
            bm_speed = 0.0
        elif baseline_time <= 0.0:
            bm_speed = 100.0
        else:
            ratio = actual_time / baseline_time
            bm_speed = _clamp(100.0 - ((ratio - 0.5) * 66.7))

        per_benchmark[benchmark_id] = {
            "quality": _clamp(bm_quality),
            "speed": bm_speed,
            "pass_rate": pass_rate,
            "deterministic_quality_score": float(
                result.get("deterministic_quality_score", pass_rate * 100.0)
            ),
            "semantic_quality_score": result.get("semantic_quality_score"),
            "terminal_state": result.get("terminal_state"),
            "completed_cleanly": bool(result.get("completed_cleanly", False)),
            "trial_clean": bool(result.get("trial_clean", False)),
            "benchmark_retry_count": int(result.get("benchmark_retry_count", 0)),
            "retryable_benchmark_failure": bool(
                result.get("retryable_benchmark_failure", False)
            ),
            "wall_time": actual_time,
            "speed_baseline_seconds": baseline_time,
        }

    return {
        "composite": _clamp(composite),
        "quality": quality,
        "speed": speed,
        "per_benchmark": per_benchmark,
    }


# ---------------------------------------------------------------------------
# Improvement classification
# ---------------------------------------------------------------------------

def classify_improvement(composite_delta: float) -> str:
    """Classify an improvement delta.

    Returns:
        ``"noise"``    — delta < NOISE_BAND
        ``"marginal"`` — NOISE_BAND <= delta < CLEAR_IMPROVEMENT_THRESHOLD
        ``"clear"``    — delta >= CLEAR_IMPROVEMENT_THRESHOLD
    """
    if composite_delta < NOISE_BAND:
        return "noise"
    if composite_delta < CLEAR_IMPROVEMENT_THRESHOLD:
        return "marginal"
    return "clear"


def needs_confirmation_trial(composite_delta: float) -> bool:
    """Return True if the delta is marginal and needs a confirmation trial.

    Marginal improvements (NOISE_BAND <= delta < CLEAR_IMPROVEMENT_THRESHOLD)
    are not conclusive enough to accept without re-running the experiment.
    """
    return classify_improvement(composite_delta) == "marginal"


# ---------------------------------------------------------------------------
# Full experiment evaluation
# ---------------------------------------------------------------------------

def evaluate_experiment(
    current_results: list[dict],
    baseline_score: float,
    baseline_times: Optional[dict[str, float]] = None,
) -> dict:
    """Evaluate an experiment against the baseline.

    Returns::

        {
            "scores": {...},          # from compute_composite
            "baseline_score": float,
            "improvement": float,     # composite - baseline_score
            "improvement_pct": float, # percentage if baseline > 0
            "classification": str,    # "noise" | "marginal" | "clear"
            "decision": str,          # "keep" | "discard" | "needs_confirmation"
        }
    """
    scores = compute_composite(current_results, baseline_times)
    composite = scores["composite"]

    improvement = composite - baseline_score
    improvement_pct = _compute_improvement_pct(improvement, baseline_score)

    classification = classify_improvement(improvement)

    if classification == "noise":
        decision = "discard"
    else:
        decision = "needs_confirmation"

    return {
        "scores": scores,
        "baseline_score": baseline_score,
        "improvement": improvement,
        "improvement_pct": improvement_pct,
        "classification": classification,
        "decision": decision,
    }
