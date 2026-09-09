"""Rate limit tracking and exponential backoff for autoconfig.

State is persisted in ~/.claude/state/autoconfig/program_state.json under
the ``rate_limit`` key.  Other top-level keys in that file belong to the
broader autoconfig program and are preserved/accessible through the
``get_program_state`` / ``update_program_state`` helpers.

Backoff schedule (consecutive 429s):
    1 -> 60 s
    2 -> 120 s
    3 -> 300 s
    4+ -> 600 s  (cap)
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_FILE: Path = Path.home() / ".claude" / "state" / "autoconfig" / "program_state.json"

_BACKOFF_SCHEDULE: list[int] = [60, 120, 300, 600]

_DEFAULT_RATE_LIMIT: dict[str, Any] = {
    "consecutive_429s": 0,
    "last_429_at": None,
    "total_429s": 0,
    "current_backoff_seconds": 0,
}

_DEFAULT_STATE: dict[str, Any] = {
    "current_phase": 1,
    "enabled_phases": [1],
    "phase3_mutation_families": ["lane_cap"],
    "run_mode": "search",
    "experiment_count": 0,
    "baseline_score": 0,
    "best_score": 0,
    "status": "stopped",
    "terminal_reason": None,
    "last_completed_phase": None,
    "evaluation_version": "v5_1_variant_gated_calibration",
    "scoring_version": "v5_1_variant_gated_calibration",
    "cutover_after_experiment_id": None,
    "phase_readiness": {},
    "phase_readiness_source": {},
    "phase_3_blocked_reason": None,
    "last_calibration_completed_at": None,
    "rate_limit": dict(_DEFAULT_RATE_LIMIT),
}


# ---------------------------------------------------------------------------
# Low-level state I/O
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    """Read program_state.json and return the full state dict.

    If the file is missing or corrupt, return a fresh default state.
    """
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _fresh_default()
        # Ensure rate_limit sub-key is well-formed.
        rl = data.get("rate_limit")
        if not isinstance(rl, dict):
            data["rate_limit"] = dict(_DEFAULT_RATE_LIMIT)
        else:
            for key, default in _DEFAULT_RATE_LIMIT.items():
                data["rate_limit"].setdefault(key, default)
        # Ensure top-level program keys exist.
        for key, default in _DEFAULT_STATE.items():
            if key not in data:
                data[key] = default if key != "rate_limit" else dict(_DEFAULT_RATE_LIMIT)
        if not isinstance(data.get("enabled_phases"), list):
            data["enabled_phases"] = list(_DEFAULT_STATE["enabled_phases"])
        if not isinstance(data.get("phase3_mutation_families"), list):
            data["phase3_mutation_families"] = list(
                _DEFAULT_STATE["phase3_mutation_families"]
            )
        if not isinstance(data.get("phase_readiness"), dict):
            data["phase_readiness"] = {}
        if not isinstance(data.get("phase_readiness_source"), dict):
            data["phase_readiness_source"] = {}
        if "phase_3_blocked_reason" not in data:
            data["phase_3_blocked_reason"] = None
        if "run_mode" not in data:
            data["run_mode"] = _DEFAULT_STATE["run_mode"]
        if "last_calibration_completed_at" not in data:
            data["last_calibration_completed_at"] = None
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _fresh_default()


def save_state(state: dict[str, Any]) -> None:
    """Atomically write *state* to program_state.json.

    Writes to a temporary file in the same directory then renames, so a
    crash mid-write cannot corrupt the file.
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=STATE_FILE.parent,
        prefix=".program_state_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, default=str)
            fh.write("\n")
        os.replace(tmp_path, STATE_FILE)
    except BaseException:
        # Best-effort cleanup of the temp file on failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Rate-limit helpers
# ---------------------------------------------------------------------------

def record_rate_limit() -> None:
    """Record a 429 response.

    Increments counters, sets the timestamp, and computes the backoff
    duration from the schedule.
    """
    state = load_state()
    rl = state["rate_limit"]
    rl["consecutive_429s"] += 1
    rl["total_429s"] += 1
    rl["last_429_at"] = datetime.now(timezone.utc).isoformat()
    idx = min(rl["consecutive_429s"], len(_BACKOFF_SCHEDULE)) - 1
    rl["current_backoff_seconds"] = _BACKOFF_SCHEDULE[idx]
    save_state(state)


def record_success() -> None:
    """Record a successful (non-429) response.

    Resets the consecutive counter and backoff while preserving totals.
    """
    state = load_state()
    rl = state["rate_limit"]
    rl["consecutive_429s"] = 0
    rl["current_backoff_seconds"] = 0
    save_state(state)


def get_backoff_seconds() -> int:
    """Return the current backoff duration in seconds (0 if no recent 429s)."""
    state = load_state()
    return int(state["rate_limit"].get("current_backoff_seconds", 0))


def should_wait() -> tuple[bool, int]:
    """Determine whether the caller should wait before the next request.

    Returns ``(should_wait, seconds_remaining)``.  The wait is needed when
    ``last_429_at + current_backoff_seconds`` is still in the future.
    """
    state = load_state()
    rl = state["rate_limit"]
    backoff = int(rl.get("current_backoff_seconds", 0))
    last_raw = rl.get("last_429_at")

    if backoff == 0 or last_raw is None:
        return False, 0

    try:
        last_ts = datetime.fromisoformat(last_raw)
        # Ensure timezone-aware comparison.
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False, 0

    now = datetime.now(timezone.utc)
    elapsed = (now - last_ts).total_seconds()
    remaining = backoff - elapsed

    if remaining > 0:
        return True, int(remaining) + 1  # round up to avoid under-waiting
    return False, 0


def get_rate_limit_stats() -> dict[str, Any]:
    """Return the ``rate_limit`` portion of the program state."""
    return dict(load_state()["rate_limit"])


# ---------------------------------------------------------------------------
# Program-state helpers
# ---------------------------------------------------------------------------

def get_program_state() -> dict[str, Any]:
    """Return the full program state dict."""
    return load_state()


def update_program_state(**kwargs: Any) -> None:
    """Update specific top-level keys in the program state.

    Example::

        update_program_state(current_phase=2, baseline_score=75.3)

    The ``rate_limit`` key can be passed but is normally managed by the
    rate-limit functions above.
    """
    state = load_state()
    state.update(kwargs)
    save_state(state)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _fresh_default() -> dict[str, Any]:
    """Return a deep copy of the default state."""
    return {
        **_DEFAULT_STATE,
        "rate_limit": dict(_DEFAULT_RATE_LIMIT),
    }
