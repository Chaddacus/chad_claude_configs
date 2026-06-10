#!/usr/bin/env python3
"""rate_limit_guard.py — shared rate-limit + wall-clock guard for autonomous loops.

Runtime guard for any autonomous loop routing through goose ACP → Codex/Claude
Pro-Max subscription, anthropic-concurrency-system, or claude-direct. Watches
combined stdout/stderr for quota signals, sleeps until the subscription
window resets, and enforces a wall-clock cap.

Usage:
    from rate_limit_guard import check_rate_limit_signals

    wall_start = time.time()
    ok = check_rate_limit_signals(combined_output, wall_start, wallclock_hours=24)
    if not ok:
        # wall-clock cap exceeded; abort
        ...

Plan: ~/.claude/plans/users-chadsimon-thoughts-md-take-a-giggly-moore.md
(cross-cutting requirement #3)
"""
from __future__ import annotations

import logging
import re
import time

# Default 5h Pro/Max quota window; override via env if needed.
DEFAULT_RATE_LIMIT_SLEEP_SECONDS = 18_000

# Patterns observed across goose ACP relay (Codex CLI / Claude Code), direct
# Anthropic API, and OpenRouter. Case-insensitive substring match on combined
# stdout+stderr.
_RATE_LIMIT_PATTERNS = re.compile(
    r"(429|quota.?exceeded|rate.?limit|usage_limit_reached|overloaded)",
    re.IGNORECASE,
)

_log = logging.getLogger("rate_limit_guard")


def detect_rate_limit_signal(combined_output: str) -> bool:
    """Return True if combined_output contains a quota/rate-limit signal."""
    return bool(_RATE_LIMIT_PATTERNS.search(combined_output or ""))


def check_rate_limit_signals(
    combined_output: str,
    wall_start: float,
    wallclock_hours: float,
    sleep_seconds: int = DEFAULT_RATE_LIMIT_SLEEP_SECONDS,
    log_fn=None,
) -> bool:
    """Inspect output for rate-limit signals; sleep until quota window opens
    if found. Returns True if OK to continue, False if wall-clock cap is hit.

    Behavior:
        - On rate-limit detection: sleep min(sleep_seconds, remaining_wallclock - 60),
          then return True (resume).
        - If sleeping would exceed wallclock cap: log + return False (caller must exit).
        - No rate-limit signal: just check the wall-clock cap and return.
    """
    log = log_fn or _log.info

    if detect_rate_limit_signal(combined_output):
        elapsed_h = (time.time() - wall_start) / 3600.0
        remaining_h = wallclock_hours - elapsed_h
        sleep_s = min(sleep_seconds, remaining_h * 3600.0 - 60)
        if sleep_s <= 0:
            log("rate-limit hit but wallclock cap imminent — aborting")
            return False
        log(f"rate-limit signal detected; sleeping {sleep_s:.0f}s (~{sleep_s/3600:.1f}h quota window)")
        time.sleep(sleep_s)

    elapsed_h = (time.time() - wall_start) / 3600.0
    return elapsed_h < wallclock_hours
