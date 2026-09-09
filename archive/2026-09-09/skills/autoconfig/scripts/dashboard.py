"""Terminal-based real-time monitoring dashboard for the autoconfig experiment daemon.

Single-file TUI using only the Python standard library (curses).
Refreshes every 5 seconds and shows a comprehensive view of the autoconfig system.

Keyboard controls:
    q / Ctrl-C  Quit
    r           Force refresh
    p           Pause / resume auto-refresh
    d           Toggle detailed view (full mutation JSON for selected experiment)
    Up / Down   Scroll through recent experiments
"""

from __future__ import annotations

import curses
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Make sibling modules importable
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent))

from experiment_db import (
    get_consecutive_discards,
    get_cumulative_improvement,
    get_daily_stats,
    get_experiment_count,
    get_experiments_by_phase,
    get_kept_experiments,
    get_recent_experiments,
    get_total_kept,
    get_tried_mutations,
)
from config_snapshot import compute_config_hash, get_snapshot_meta, snapshot_exists
from rate_limiter import get_program_state, get_rate_limit_stats, should_wait
from analyze_experiments import (
    get_improvement_curve,
    get_knob_attribution,
    get_phase_summary,
    generate_report,
)
from config_mutator import get_mutation_count

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DAEMON_LOG = Path.home() / ".claude" / "state" / "autoconfig" / "daemon.log"
REFRESH_INTERVAL = 5  # seconds
MAX_PHASE = 5
CONVERGENCE_THRESHOLD = 20

# Block characters for sparklines (ascending height)
SPARK_CHARS = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"

# Box-drawing characters
H_LINE = "\u2500"
V_LINE = "\u2502"
TL_CORNER = "\u250c"
TR_CORNER = "\u2510"
BL_CORNER = "\u2514"
BR_CORNER = "\u2518"
T_LEFT = "\u251c"
T_RIGHT = "\u2524"

# Status icons
ICON_KEPT = "\u2713"      # checkmark
ICON_DISCARD = "\u2717"   # cross
ICON_CONFIRM = "\u27f3"   # rotating arrow
ICON_RUNNING = "\u23f3"   # hourglass
ICON_REFRESH = "\u27f3"   # rotating arrow


# ---------------------------------------------------------------------------
# Color pair indices
# ---------------------------------------------------------------------------

COLOR_NORMAL = 0
COLOR_GREEN = 1
COLOR_RED = 2
COLOR_YELLOW = 3
COLOR_CYAN = 4
COLOR_WHITE_BOLD = 5
COLOR_DIM = 6


def init_colors() -> None:
    """Initialize curses color pairs."""
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(COLOR_GREEN, curses.COLOR_GREEN, -1)
    curses.init_pair(COLOR_RED, curses.COLOR_RED, -1)
    curses.init_pair(COLOR_YELLOW, curses.COLOR_YELLOW, -1)
    curses.init_pair(COLOR_CYAN, curses.COLOR_CYAN, -1)
    curses.init_pair(COLOR_WHITE_BOLD, curses.COLOR_WHITE, -1)
    curses.init_pair(COLOR_DIM, curses.COLOR_WHITE, -1)


# ---------------------------------------------------------------------------
# Data gathering helpers
# ---------------------------------------------------------------------------


def get_daemon_status() -> dict:
    """Check if the daemon is running via pgrep.

    Returns {"running": bool, "pid": int|None, "uptime": str|None}.
    """
    result: dict[str, Any] = {"running": False, "pid": None, "uptime": None}
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "experiment_daemon.py"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            pids = proc.stdout.strip().split("\n")
            # Filter out our own process
            my_pid = str(os.getpid())
            for pid_str in pids:
                pid_str = pid_str.strip()
                if pid_str and pid_str != my_pid:
                    result["running"] = True
                    result["pid"] = int(pid_str)
                    break

            if result["pid"] is not None:
                try:
                    etime_proc = subprocess.run(
                        ["ps", "-p", str(result["pid"]), "-o", "etime="],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if etime_proc.returncode == 0:
                        result["uptime"] = etime_proc.stdout.strip()
                except (subprocess.TimeoutExpired, OSError):
                    pass
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return result


def get_last_log_line() -> str:
    """Read the last non-empty line from the daemon log file."""
    try:
        if not DAEMON_LOG.is_file():
            return "No daemon log found"
        with open(DAEMON_LOG, "rb") as f:
            # Seek to end, then scan backwards for last non-empty line
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return "Daemon log is empty"
            # Read up to last 4 KB
            read_size = min(size, 4096)
            f.seek(size - read_size)
            data = f.read(read_size).decode("utf-8", errors="replace")
            lines = data.strip().split("\n")
            for line in reversed(lines):
                line = line.strip()
                if line:
                    return line
        return "Daemon log is empty"
    except (OSError, UnicodeDecodeError):
        return "Error reading daemon log"


def format_time_ago(iso_timestamp: str) -> str:
    """Convert an ISO timestamp to a human-readable 'X ago' format."""
    if not iso_timestamp:
        return "N/A"
    try:
        ts = datetime.fromisoformat(iso_timestamp)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = (now - ts).total_seconds()
        if delta < 0:
            return "just now"
        if delta < 60:
            return f"{int(delta)}s ago"
        if delta < 3600:
            return f"{int(delta / 60)}m ago"
        if delta < 86400:
            return f"{int(delta / 3600)}h ago"
        return f"{int(delta / 86400)}d ago"
    except (ValueError, TypeError, OverflowError):
        return "N/A"


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------


def draw_progress_bar(width: int, current: int, total: int) -> str:
    """ASCII progress bar using block chars.

    Returns a string of exactly *width* characters.
    """
    if total <= 0 or width <= 0:
        return "\u2591" * width
    fraction = min(current / total, 1.0)
    filled = int(fraction * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def draw_sparkline(values: list[float], width: int) -> str:
    """Single-row ASCII sparkline using block-element characters.

    Scales *values* into the vertical range covered by SPARK_CHARS and
    returns a string of exactly *width* characters.
    """
    if not values or width <= 0:
        return " " * width

    # Resample values to fit width
    if len(values) > width:
        step = len(values) / width
        resampled = [values[int(i * step)] for i in range(width)]
    elif len(values) < width:
        resampled = list(values)
        # Pad on the left with the first value
        resampled = [values[0]] * (width - len(values)) + resampled
    else:
        resampled = list(values)

    lo = min(resampled)
    hi = max(resampled)
    span = hi - lo if hi != lo else 1.0
    n_chars = len(SPARK_CHARS) - 1

    out: list[str] = []
    for v in resampled:
        idx = int(((v - lo) / span) * n_chars)
        idx = max(0, min(n_chars, idx))
        out.append(SPARK_CHARS[idx])
    return "".join(out)


def draw_multi_sparkline(values: list[float], width: int, height: int) -> list[str]:
    """Multi-row ASCII chart using block-element characters.

    Returns *height* strings of *width* characters each, top row first.
    """
    if not values or width <= 0 or height <= 0:
        return [" " * width] * height

    # Resample values to fit width
    if len(values) > width:
        step = len(values) / width
        resampled = [values[int(i * step)] for i in range(width)]
    elif len(values) < width:
        resampled = [values[0]] * (width - len(values)) + list(values)
    else:
        resampled = list(values)

    lo = min(resampled)
    hi = max(resampled)
    span = hi - lo if hi != lo else 1.0

    rows: list[str] = []
    for row_idx in range(height):
        # Row 0 is the top; row (height-1) is the bottom
        threshold = 1.0 - (row_idx + 0.5) / height
        chars: list[str] = []
        for v in resampled:
            normalized = (v - lo) / span
            if normalized >= threshold + 0.4 / height:
                chars.append("\u2588")  # full block
            elif normalized >= threshold:
                chars.append("\u2584")  # lower half
            elif normalized >= threshold - 0.4 / height:
                chars.append("\u2581")  # lower eighth
            else:
                chars.append(" ")
        rows.append("".join(chars))
    return rows


def safe_addstr(stdscr, y: int, x: int, text: str,
                attr: int = 0, max_width: int = 0) -> None:
    """Write text to the screen, silently truncating if it would overflow."""
    max_y, max_x = stdscr.getmaxyx()
    if y < 0 or y >= max_y or x < 0 or x >= max_x:
        return
    available = max_x - x - 1  # leave 1 col margin to avoid scroll
    if available <= 0:
        return
    if max_width > 0:
        available = min(available, max_width)
    truncated = text[:available]
    try:
        stdscr.addstr(y, x, truncated, attr)
    except curses.error:
        pass


def draw_hline(stdscr, y: int, x: int, width: int, attr: int = 0) -> None:
    """Draw a horizontal line using box-drawing characters."""
    safe_addstr(stdscr, y, x, H_LINE * width, attr)


def draw_box_top(stdscr, y: int, x: int, width: int, attr: int = 0) -> None:
    """Draw the top edge of a box."""
    if width < 2:
        return
    safe_addstr(stdscr, y, x, TL_CORNER + H_LINE * (width - 2) + TR_CORNER, attr)


def draw_box_bottom(stdscr, y: int, x: int, width: int, attr: int = 0) -> None:
    """Draw the bottom edge of a box."""
    if width < 2:
        return
    safe_addstr(stdscr, y, x, BL_CORNER + H_LINE * (width - 2) + BR_CORNER, attr)


def draw_box_separator(stdscr, y: int, x: int, width: int, attr: int = 0) -> None:
    """Draw a horizontal separator within a box."""
    if width < 2:
        return
    safe_addstr(stdscr, y, x, T_LEFT + H_LINE * (width - 2) + T_RIGHT, attr)


def draw_box_line(stdscr, y: int, x: int, width: int, text: str,
                  attr: int = 0) -> None:
    """Draw a line within a box: vertical bars on each side, text inside."""
    if width < 4:
        return
    inner = width - 4  # 2 for V_LINE borders + 2 for padding spaces
    padded = f" {text}" if text else ""
    padded = padded[:inner].ljust(inner)
    safe_addstr(stdscr, y, x, V_LINE, attr)
    safe_addstr(stdscr, y, x + 1, " " + padded + " ", attr)
    safe_addstr(stdscr, y, x + width - 1, V_LINE, attr)


# ---------------------------------------------------------------------------
# Dashboard state
# ---------------------------------------------------------------------------


class DashboardState:
    """Mutable state for the dashboard UI."""

    def __init__(self) -> None:
        self.paused: bool = False
        self.detail_mode: bool = False
        self.selected_idx: int = 0
        self.recent_experiments: list[dict] = []
        self.last_refresh: float = 0.0

    def scroll_up(self) -> None:
        if self.selected_idx > 0:
            self.selected_idx -= 1

    def scroll_down(self) -> None:
        max_idx = max(0, len(self.recent_experiments) - 1)
        if self.selected_idx < max_idx:
            self.selected_idx += 1


# ---------------------------------------------------------------------------
# Data snapshot (one refresh cycle)
# ---------------------------------------------------------------------------


def gather_data() -> dict[str, Any]:
    """Collect all data needed for one dashboard frame.

    Returns a dict of pre-fetched values so we minimise I/O during drawing.
    """
    data: dict[str, Any] = {}

    # Daemon status
    data["daemon"] = get_daemon_status()

    # Program state
    try:
        state = get_program_state()
    except Exception:
        state = {
            "current_phase": 1,
            "enabled_phases": [1],
            "run_mode": "search",
            "baseline_score": 0,
            "best_score": 0,
            "experiment_count": 0,
            "status": "stopped",
        }
    data["state"] = state
    enabled_phases = state.get("enabled_phases", [1])
    if not isinstance(enabled_phases, list) or not enabled_phases:
        enabled_phases = [1]
    data["enabled_phases"] = enabled_phases
    run_mode = state.get("run_mode", "search")
    data["run_mode"] = run_mode
    data["max_enabled_phase"] = 3 if run_mode == "phase3_calibration" else max(enabled_phases)
    evaluation_version = state.get("evaluation_version")
    data["phase_readiness"] = state.get("phase_readiness", {})
    data["phase_3_blocked_reason"] = state.get("phase_3_blocked_reason")

    # Experiment counts
    try:
        data["total_experiments"] = get_experiment_count(
            evaluation_version,
            run_mode=run_mode,
        )
    except Exception:
        data["total_experiments"] = 0

    try:
        data["total_kept"] = get_total_kept(
            evaluation_version,
            run_mode=run_mode,
        )
    except Exception:
        data["total_kept"] = 0

    try:
        data["cumulative_improvement"] = get_cumulative_improvement(
            evaluation_version,
            run_mode=run_mode,
        )
    except Exception:
        data["cumulative_improvement"] = 0.0

    # Recent experiments
    try:
        data["recent"] = get_recent_experiments(
            limit=30,
            evaluation_version=evaluation_version,
            run_mode=run_mode,
        )
    except Exception:
        data["recent"] = []

    # Mutation count for current phase (total possible)
    phase = state.get("current_phase", 1)
    if run_mode == "phase3_calibration":
        phase = 3
    data["phase"] = phase
    try:
        if run_mode == "phase3_calibration":
            data["mutation_total"] = int(
                state.get("phase_readiness", {}).get("3", {}).get("window", 10)
            )
        else:
            phase3_families = state.get("phase3_mutation_families", ["lane_cap"])
            phase3_allowed = set(phase3_families) if isinstance(phase3_families, list) else None
            data["mutation_total"] = get_mutation_count(
                phase,
                phase3_allowed_families=phase3_allowed,
            )
    except Exception:
        data["mutation_total"] = 0

    # Tried mutations for current phase
    try:
        if run_mode == "phase3_calibration":
            data["mutations_tried"] = get_experiment_count(
                evaluation_version,
                run_mode=run_mode,
            )
        else:
            tried = get_tried_mutations(
                phase,
                evaluation_version=evaluation_version,
                run_mode=run_mode,
            )
            data["mutations_tried"] = len(tried)
    except Exception:
        data["mutations_tried"] = 0

    # Baseline / best scores
    data["baseline_score"] = state.get("baseline_score", 0.0)
    data["best_score"] = state.get("best_score", 0.0)

    # Current phase summary
    try:
        data["current_phase_summary"] = get_phase_summary(
            phase,
            evaluation_version=evaluation_version,
            run_mode=run_mode,
        )
    except Exception:
        data["current_phase_summary"] = {
            "phase": phase,
            "total_experiments": 0,
            "completed_experiments": 0,
            "running_experiments": 0,
            "kept": 0,
            "discarded": 0,
            "convergence_status": "not started",
        }

    # Knob attribution
    try:
        data["knob_attribution"] = get_knob_attribution(
            evaluation_version,
            run_mode=run_mode,
        )
    except Exception:
        data["knob_attribution"] = []

    # Improvement curve
    try:
        curve = get_improvement_curve(
            evaluation_version,
            run_mode=run_mode,
        )
        data["improvement_values"] = [
            p.get("cumulative_improvement_pct", 0.0) for p in curve
        ]
    except Exception:
        data["improvement_values"] = []

    # Phase summaries for convergence display
    phase_summaries: list[dict] = []
    for p in range(1, MAX_PHASE + 1):
        try:
            phase_summaries.append(
                get_phase_summary(
                    p,
                    evaluation_version=evaluation_version,
                    run_mode=run_mode,
                )
            )
        except Exception:
            phase_summaries.append({
                "phase": p,
                "total_experiments": 0,
                "convergence_status": "not started",
            })
    data["phase_summaries"] = phase_summaries

    # Consecutive discards (current streak)
    try:
        data["consecutive_discards"] = get_consecutive_discards(
            phase,
            evaluation_version=evaluation_version,
            run_mode=run_mode,
        )
    except Exception:
        data["consecutive_discards"] = 0

    # Rate limit stats
    try:
        rl = get_rate_limit_stats()
    except Exception:
        rl = {"total_429s": 0, "current_backoff_seconds": 0, "consecutive_429s": 0}
    data["rate_limit"] = rl

    # Daily stats
    try:
        data["daily"] = get_daily_stats()
    except Exception:
        data["daily"] = {"rate_limit_hits": 0, "experiment_count": 0}

    # Backoff
    try:
        waiting, secs = should_wait()
        data["backoff_active"] = waiting
        data["backoff_seconds"] = secs
    except Exception:
        data["backoff_active"] = False
        data["backoff_seconds"] = 0

    # Last log line
    data["last_log"] = get_last_log_line()

    return data


# ---------------------------------------------------------------------------
# Main drawing function
# ---------------------------------------------------------------------------


def draw_dashboard(stdscr, ds: DashboardState, data: dict[str, Any]) -> None:
    """Render the full dashboard to the curses screen."""
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()

    # Minimum terminal size check
    if max_y < 10 or max_x < 40:
        safe_addstr(stdscr, 0, 0, "Terminal too small (need 40x10 minimum)")
        stdscr.refresh()
        return

    w = min(max_x, 120)  # cap width for readability
    x0 = 0
    y = 0
    cyan = curses.color_pair(COLOR_CYAN) | curses.A_BOLD
    green = curses.color_pair(COLOR_GREEN)
    green_bold = curses.color_pair(COLOR_GREEN) | curses.A_BOLD
    red = curses.color_pair(COLOR_RED)
    red_bold = curses.color_pair(COLOR_RED) | curses.A_BOLD
    yellow = curses.color_pair(COLOR_YELLOW)
    yellow_bold = curses.color_pair(COLOR_YELLOW) | curses.A_BOLD
    white_bold = curses.color_pair(COLOR_WHITE_BOLD) | curses.A_BOLD
    dim = curses.color_pair(COLOR_DIM) | curses.A_DIM
    normal = curses.color_pair(COLOR_NORMAL)

    daemon = data["daemon"]
    state = data["state"]
    phase = data["phase"]
    max_enabled_phase = data.get("max_enabled_phase", MAX_PHASE)
    run_status = state.get("status", "stopped")
    run_mode = data.get("run_mode", "search")

    # ── HEADER ─────────────────────────────────────────────────────────
    draw_box_top(stdscr, y, x0, w, cyan)
    y += 1

    pause_indicator = " [PAUSED]" if ds.paused else ""
    header_left = f"  AUTOCONFIG DASHBOARD{pause_indicator}"
    header_right = f"{ICON_REFRESH} refreshing every {REFRESH_INTERVAL}s  "
    padding = w - 4 - len(header_left) - len(header_right)
    if padding < 0:
        padding = 0
    header_line = header_left + " " * padding + header_right
    draw_box_line(stdscr, y, x0, w, "", normal)
    safe_addstr(stdscr, y, x0 + 1, " " + header_left, cyan)
    safe_addstr(stdscr, y, x0 + w - 2 - len(header_right), header_right, dim)
    safe_addstr(stdscr, y, x0, V_LINE, cyan)
    safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
    y += 1

    # Status line
    if daemon["running"]:
        status_str = "RUNNING"
        status_attr = green_bold
    elif run_status == "completed":
        status_str = "COMPLETED"
        status_attr = green_bold
    else:
        status_str = "STOPPED"
        status_attr = red_bold

    pid_str = str(daemon["pid"]) if daemon["pid"] else "N/A"
    uptime_str = daemon["uptime"] if daemon["uptime"] else "N/A"
    status_line = f"  Status: "
    phase_part = f" {V_LINE} Phase: {phase}/{max_enabled_phase}"
    mode_part = f" {V_LINE} Mode: {'CAL' if run_mode == 'phase3_calibration' else 'SEARCH'}"
    pid_part = f" {V_LINE} PID: {pid_str}"
    uptime_part = f" {V_LINE} Uptime: {uptime_str}"
    full_status = status_line + status_str + phase_part + mode_part + pid_part + uptime_part

    draw_box_line(stdscr, y, x0, w, "", normal)
    safe_addstr(stdscr, y, x0, V_LINE, cyan)
    safe_addstr(stdscr, y, x0 + 2, status_line, white_bold)
    safe_addstr(stdscr, y, x0 + 2 + len(status_line), status_str, status_attr)
    rest = phase_part + mode_part + pid_part + uptime_part
    safe_addstr(stdscr, y, x0 + 2 + len(status_line) + len(status_str), rest, normal)
    safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
    y += 1

    # ── PROGRESS ───────────────────────────────────────────────────────
    draw_box_separator(stdscr, y, x0, w, cyan)
    y += 1

    draw_box_line(stdscr, y, x0, w, "", normal)
    safe_addstr(stdscr, y, x0, V_LINE, cyan)
    safe_addstr(stdscr, y, x0 + 2, "  PROGRESS", cyan)
    safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
    y += 1

    # Progress bar
    phase_summary = data["current_phase_summary"]
    total_exp = phase_summary.get("completed_experiments", 0)
    mut_total = data["mutation_total"]
    mut_tried = data["mutations_tried"]
    bar_width = max(w - 40, 10)
    pct = (mut_tried / mut_total * 100) if mut_total > 0 else 0
    bar_str = draw_progress_bar(bar_width, mut_tried, mut_total)
    noun = "samples" if run_mode == "phase3_calibration" else "mutations"
    bar_label = f"  {bar_str}  {mut_tried}/{mut_total} {noun} ({pct:.0f}%)"

    draw_box_line(stdscr, y, x0, w, "", normal)
    safe_addstr(stdscr, y, x0, V_LINE, cyan)
    safe_addstr(stdscr, y, x0 + 2, "  " + bar_str, green)
    label_part = f"  {mut_tried}/{mut_total} {noun} ({pct:.0f}%)"
    safe_addstr(stdscr, y, x0 + 4 + bar_width, label_part, normal)
    safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
    y += 1

    # Score summary
    baseline = data["baseline_score"]
    best = data["best_score"]
    cum_imp = data["cumulative_improvement"]
    if run_mode == "phase3_calibration":
        score_line = "  Calibration: phase-3 readiness sampling"
    elif baseline > 0:
        imp_str = f"+{cum_imp:.1f}%" if cum_imp > 0 else f"{cum_imp:.1f}%"
        score_line = f"  Baseline: {baseline:.1f} -> Best: {best:.1f} ({imp_str})"
    else:
        score_line = "  Baseline: N/A -> Best: N/A"

    draw_box_line(stdscr, y, x0, w, score_line, normal)
    safe_addstr(stdscr, y, x0, V_LINE, cyan)
    safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
    y += 1

    # Kept/Discarded counts
    total_kept = phase_summary.get("kept", 0)
    total_discarded = phase_summary.get("discarded", 0)
    keep_rate = (total_kept / total_exp * 100) if total_exp > 0 else 0
    running_count = phase_summary.get("running_experiments", 0)
    if run_mode == "phase3_calibration":
        readiness = data.get("phase_readiness", {}).get("3", {})
        kd_line = (
            f"  Clean: {readiness.get('clean_completion_rate', 0.0):.2f} "
            f"{V_LINE} Gate: {readiness.get('deterministic_gate_pass_rate', 0.0):.2f} "
            f"{V_LINE} Source: {readiness.get('source', 'n/a')}"
        )
    else:
        kd_line = (
            f"  Phase kept: {total_kept} {V_LINE} Discarded: {total_discarded} "
            f"{V_LINE} Keep rate: {keep_rate:.1f}%"
        )
    if running_count > 0:
        kd_line += f" {V_LINE} Running: {running_count}"
    draw_box_line(stdscr, y, x0, w, kd_line, normal)
    safe_addstr(stdscr, y, x0, V_LINE, cyan)
    safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
    y += 1

    # ── RECENT EXPERIMENTS ─────────────────────────────────────────────
    draw_box_separator(stdscr, y, x0, w, cyan)
    y += 1

    draw_box_line(stdscr, y, x0, w, "", normal)
    safe_addstr(stdscr, y, x0, V_LINE, cyan)
    safe_addstr(stdscr, y, x0 + 2, "  RECENT EXPERIMENTS", cyan)
    safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
    y += 1

    recent = data["recent"]
    ds.recent_experiments = recent

    # Clamp selected index
    if ds.selected_idx >= len(recent):
        ds.selected_idx = max(0, len(recent) - 1)

    # Show up to 10 experiments, limited by available vertical space
    exp_display_count = min(10, len(recent), max(0, max_y - y - 14))
    if not recent:
        draw_box_line(stdscr, y, x0, w, "  No experiments yet", dim)
        safe_addstr(stdscr, y, x0, V_LINE, cyan)
        safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
        y += 1
    else:
        for i in range(exp_display_count):
            exp = recent[i]
            exp_id = exp.get("id", "?")
            status = exp.get("status", "unknown")
            kept = exp.get("kept", 0)
            imp_pct = exp.get("improvement_pct", 0.0) or 0.0
            summary = exp.get("mutation_summary", "unknown")
            completed_at = exp.get("completed_at", "")
            exp_run_mode = exp.get("run_mode", "search")
            calibration_sample = bool(exp.get("calibration_sample", 0))

            # Status icon and color
            if calibration_sample or exp_run_mode == "phase3_calibration":
                if status == "running":
                    icon = ICON_RUNNING
                    decision_text = "CAL "
                    line_attr = yellow
                elif status == "completed":
                    icon = ICON_CONFIRM
                    decision_text = "CAL "
                    line_attr = cyan
                else:
                    icon = ICON_DISCARD
                    decision_text = status[:4].upper()
                    line_attr = dim if status == "skipped" else red
            elif status == "running":
                icon = ICON_RUNNING
                decision_text = "RUN "
                line_attr = yellow
            elif kept:
                icon = ICON_KEPT
                decision_text = "KEPT"
                line_attr = green
            elif status == "completed" and not kept:
                # Check if it was a confirmation trial that failed
                if imp_pct and imp_pct > 0:
                    icon = ICON_CONFIRM
                    decision_text = "CONF"
                    line_attr = yellow
                else:
                    icon = ICON_DISCARD
                    decision_text = "DISC"
                    line_attr = red
            elif status == "error":
                icon = ICON_DISCARD
                decision_text = "ERR "
                line_attr = red
            elif status == "skipped":
                icon = ICON_DISCARD
                decision_text = "SKIP"
                line_attr = dim
            else:
                icon = " "
                decision_text = status[:4].upper()
                line_attr = normal

            # Improvement percentage
            if imp_pct is not None and imp_pct != 0:
                imp_str = f"{imp_pct:+.1f}%"
            else:
                imp_str = "     "

            time_ago = format_time_ago(completed_at) if completed_at else "running"

            # Truncate summary to fit
            summary_max = w - 48
            if summary_max < 10:
                summary_max = 10
            if len(summary) > summary_max:
                summary = summary[:summary_max - 1] + "\u2026"

            exp_line = f"  #{exp_id:<4} {icon} {decision_text}  {imp_str:>7}  {summary:<{summary_max}}  {time_ago:>8}"

            # Highlight selected row
            is_selected = (i == ds.selected_idx)
            row_attr = line_attr | curses.A_REVERSE if is_selected else line_attr

            draw_box_line(stdscr, y, x0, w, "", normal)
            safe_addstr(stdscr, y, x0, V_LINE, cyan)
            safe_addstr(stdscr, y, x0 + 2, exp_line, row_attr)
            safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
            y += 1

    # ── DETAIL VIEW (toggle with 'd') ─────────────────────────────────
    if ds.detail_mode and recent and 0 <= ds.selected_idx < len(recent):
        draw_box_separator(stdscr, y, x0, w, cyan)
        y += 1
        draw_box_line(stdscr, y, x0, w, "", normal)
        safe_addstr(stdscr, y, x0, V_LINE, cyan)
        safe_addstr(stdscr, y, x0 + 2, "  DETAIL VIEW (press 'd' to close)", cyan)
        safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
        y += 1

        exp = recent[ds.selected_idx]
        mutation_json = exp.get("mutation_json", "{}")
        try:
            parsed = json.loads(mutation_json)
            detail_text = json.dumps(parsed, indent=2)
        except (json.JSONDecodeError, TypeError):
            detail_text = mutation_json

        detail_lines = detail_text.split("\n")
        max_detail_lines = min(len(detail_lines), max(0, max_y - y - 10))
        for dl in detail_lines[:max_detail_lines]:
            draw_box_line(stdscr, y, x0, w, "  " + dl, dim)
            safe_addstr(stdscr, y, x0, V_LINE, cyan)
            safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
            y += 1

    # Check if we have vertical space left for remaining sections
    remaining_rows = max_y - y - 4  # need 4 rows minimum for system + bottom border
    if remaining_rows < 6:
        # Not enough space -- skip to system section
        pass
    else:
        # ── IMPROVEMENT CURVE + TOP KNOB CHANGES ──────────────────────
        draw_box_separator(stdscr, y, x0, w, cyan)
        y += 1

        half_w = (w - 3) // 2  # two columns with a divider

        # Left column: Improvement curve
        draw_box_line(stdscr, y, x0, w, "", normal)
        safe_addstr(stdscr, y, x0, V_LINE, cyan)
        safe_addstr(stdscr, y, x0 + 2, "  IMPROVEMENT CURVE", cyan)
        safe_addstr(stdscr, y, x0 + half_w + 2, V_LINE, dim)
        safe_addstr(stdscr, y, x0 + half_w + 4, "TOP KNOB CHANGES", cyan)
        safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
        y += 1

        # Draw sparkline (multi-row)
        improvement_values = data["improvement_values"]
        chart_height = min(4, max(0, remaining_rows - 8))
        chart_width = max(half_w - 6, 5)

        if improvement_values and chart_height > 0:
            spark_rows = draw_multi_sparkline(improvement_values, chart_width, chart_height)

            lo = min(improvement_values) if improvement_values else 0
            hi = max(improvement_values) if improvement_values else 0

            for ri, spark_row in enumerate(spark_rows):
                # Y-axis label
                if ri == 0:
                    label = f"{hi:>5.1f}"
                elif ri == chart_height - 1:
                    label = f"{lo:>5.1f}"
                else:
                    label = "     "

                draw_box_line(stdscr, y, x0, w, "", normal)
                safe_addstr(stdscr, y, x0, V_LINE, cyan)
                safe_addstr(stdscr, y, x0 + 2, f"  {label}{V_LINE}", dim)
                safe_addstr(stdscr, y, x0 + 9, spark_row, green)

                # Right column: knob attribution (fill rows alongside chart)
                knobs = data["knob_attribution"]
                safe_addstr(stdscr, y, x0 + half_w + 2, V_LINE, dim)
                if ri < len(knobs):
                    k = knobs[ri]
                    knob_name = k.get("knob", "?")
                    knob_imp = k.get("total_improvement", 0.0)
                    knob_kept = k.get("experiments_kept", 0)
                    knob_line = f"  {ri + 1}. {knob_name:<12} {knob_imp:>+6.1f}% ({knob_kept} kept)"
                    safe_addstr(stdscr, y, x0 + half_w + 3, knob_line, normal)
                safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
                y += 1
        else:
            # No data
            draw_box_line(stdscr, y, x0, w, "", normal)
            safe_addstr(stdscr, y, x0, V_LINE, cyan)
            safe_addstr(stdscr, y, x0 + 2, "  No improvement data", dim)
            safe_addstr(stdscr, y, x0 + half_w + 2, V_LINE, dim)
            safe_addstr(stdscr, y, x0 + half_w + 3, "  No knob data", dim)
            safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
            y += 1

        # Convergence section (right column continues, or below chart)
        conv_y = y
        remaining_conv = max(0, remaining_rows - (y - (max_y - remaining_rows - 4)) - 4)

        # CONVERGENCE heading
        draw_box_line(stdscr, y, x0, w, "", normal)
        safe_addstr(stdscr, y, x0, V_LINE, cyan)

        # X-axis label under chart
        x_axis = "  " + " " * 5 + H_LINE * min(chart_width, half_w - 8)
        safe_addstr(stdscr, y, x0 + 2, x_axis, dim)

        safe_addstr(stdscr, y, x0 + half_w + 2, V_LINE, dim)
        safe_addstr(stdscr, y, x0 + half_w + 4, "CONVERGENCE", cyan)
        safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
        y += 1

        # Show convergence per phase
        phase_summaries = data["phase_summaries"]
        phase_readiness = data.get("phase_readiness", {})
        consec_discards = data["consecutive_discards"]
        conv_lines_shown = 0
        max_conv_lines = min(len(phase_summaries), max(0, max_y - y - 4))

        for ps in phase_summaries[:max_conv_lines]:
            p_num = ps.get("phase", 0)
            p_total = ps.get("completed_experiments", ps.get("total_experiments", 0))
            convergence = ps.get("convergence_status", "not started")
            readiness_snapshot = phase_readiness.get(str(p_num), {})

            if p_num == 3 and readiness_snapshot and not readiness_snapshot.get("ready"):
                conv_label = "blocked"
                conv_attr = yellow
            elif run_mode == "phase3_calibration" and p_num == phase and run_status == "completed":
                conv_label = "completed"
                conv_attr = green
            elif convergence == "converged":
                conv_label = "converged"
                conv_attr = green
            elif convergence == "exhausted":
                conv_label = "exhausted"
                conv_attr = yellow
            elif convergence == "not started":
                conv_label = "not started"
                conv_attr = dim
            elif p_total > 0:
                if p_num == phase and run_status == "running":
                    conv_label = f"{consec_discards}/{CONVERGENCE_THRESHOLD} consec discards"
                else:
                    conv_label = f"active ({p_total} completed)"
                conv_attr = normal
            else:
                conv_label = "not started"
                conv_attr = dim

            draw_box_line(stdscr, y, x0, w, "", normal)
            safe_addstr(stdscr, y, x0, V_LINE, cyan)
            safe_addstr(stdscr, y, x0 + half_w + 2, V_LINE, dim)
            conv_text = f"  Phase {p_num}: {conv_label}"
            safe_addstr(stdscr, y, x0 + half_w + 3, conv_text, conv_attr)
            safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
            y += 1
            conv_lines_shown += 1

        blocked_reason = data.get("phase_3_blocked_reason")
        if blocked_reason and y < max_y - 4:
            reason_max = max(16, w - half_w - 8)
            short_reason = blocked_reason
            if len(short_reason) > reason_max:
                short_reason = short_reason[: reason_max - 1] + "\u2026"
            draw_box_line(stdscr, y, x0, w, "", normal)
            safe_addstr(stdscr, y, x0, V_LINE, cyan)
            safe_addstr(stdscr, y, x0 + half_w + 2, V_LINE, dim)
            safe_addstr(stdscr, y, x0 + half_w + 3, f"  {short_reason}", dim)
            safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
            y += 1

        if run_mode == "phase3_calibration":
            variants = phase_readiness.get("3", {}).get("variants", {})
            for variant_id, snapshot in sorted(
                variants.items(),
                key=lambda item: (
                    1 if item[1].get("ready") else 0,
                    item[1].get("clean_completion_rate", 0.0),
                    item[1].get("deterministic_gate_pass_rate", 0.0),
                    item[0],
                ),
            ):
                if y >= max_y - 4:
                    break
                target = snapshot.get("target_sample_count", 0)
                sample_count = snapshot.get("sample_count", 0)
                clean_rate = snapshot.get("clean_completion_rate", 0.0)
                gate_rate = snapshot.get("deterministic_gate_pass_rate", 0.0)
                variant_attr = green if snapshot.get("ready") else yellow
                variant_line = (
                    f"  {variant_id}: {sample_count}/{target} "
                    f"clean={clean_rate:.2f} gate={gate_rate:.2f}"
                )
                draw_box_line(stdscr, y, x0, w, "", normal)
                safe_addstr(stdscr, y, x0, V_LINE, cyan)
                safe_addstr(stdscr, y, x0 + half_w + 2, V_LINE, dim)
                safe_addstr(stdscr, y, x0 + half_w + 3, variant_line, variant_attr)
                safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
                y += 1

    # ── SYSTEM STATUS ──────────────────────────────────────────────────
    if y < max_y - 3:
        draw_box_separator(stdscr, y, x0, w, cyan)
        y += 1

        rl = data["rate_limit"]
        daily = data["daily"]
        rate_hits = daily.get("rate_limit_hits", 0)

        backoff_str = "none"
        if data["backoff_active"]:
            backoff_str = f"{data['backoff_seconds']}s"
        elif rl.get("current_backoff_seconds", 0) > 0:
            backoff_str = f"{rl['current_backoff_seconds']}s (clear)"

        sys_line = (
            f"  SYSTEM {V_LINE} Rate limits: {rate_hits} today "
            f"{V_LINE} Backoff: {backoff_str} "
            f"{V_LINE} Conflicts: {rl.get('consecutive_429s', 0)}"
        )
        draw_box_line(stdscr, y, x0, w, sys_line, normal)
        safe_addstr(stdscr, y, x0, V_LINE, cyan)
        # Highlight SYSTEM label
        safe_addstr(stdscr, y, x0 + 2, "  SYSTEM", cyan)
        safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
        y += 1

        # Last daemon log line
        log_line = data["last_log"]
        log_max = w - 22
        if len(log_line) > log_max:
            log_line = log_line[:log_max - 1] + "\u2026"
        daemon_log_text = f"  Daemon log: {log_line}"
        draw_box_line(stdscr, y, x0, w, daemon_log_text, dim)
        safe_addstr(stdscr, y, x0, V_LINE, cyan)
        safe_addstr(stdscr, y, x0 + w - 1, V_LINE, cyan)
        y += 1

    # ── BOTTOM BORDER ──────────────────────────────────────────────────
    if y < max_y:
        draw_box_bottom(stdscr, y, x0, w, cyan)
        y += 1

    # Help line at the very bottom
    if y < max_y:
        help_text = "  q:Quit  r:Refresh  p:Pause  d:Detail  \u2191\u2193:Scroll"
        safe_addstr(stdscr, y, x0, help_text, dim)

    stdscr.refresh()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point: launch the curses TUI dashboard."""
    curses.wrapper(_main_inner)


def _main_inner(stdscr) -> None:
    """Curses main loop (called by curses.wrapper for cleanup safety)."""
    init_colors()
    curses.curs_set(0)  # hide cursor
    stdscr.timeout(200)  # getch timeout in ms for responsive key handling

    ds = DashboardState()
    data: dict[str, Any] = {}

    while True:
        now = time.time()

        # Refresh data if needed
        needs_refresh = (now - ds.last_refresh) >= REFRESH_INTERVAL
        if needs_refresh and not ds.paused:
            try:
                data = gather_data()
            except Exception:
                # If data gathering fails, keep the old data and show what we have
                if not data:
                    data = {
                        "daemon": {"running": False, "pid": None, "uptime": None},
                        "state": {"current_phase": 1, "baseline_score": 0, "best_score": 0},
                        "phase": 1,
                        "total_experiments": 0,
                        "total_kept": 0,
                        "cumulative_improvement": 0.0,
                        "recent": [],
                        "mutation_total": 0,
                        "mutations_tried": 0,
                        "baseline_score": 0.0,
                        "best_score": 0.0,
                        "knob_attribution": [],
                        "improvement_values": [],
                        "phase_summaries": [],
                        "consecutive_discards": 0,
                        "rate_limit": {},
                        "daily": {},
                        "backoff_active": False,
                        "backoff_seconds": 0,
                        "last_log": "Error gathering data",
                    }
            ds.last_refresh = now

        # Draw if we have data
        if data:
            try:
                draw_dashboard(stdscr, ds, data)
            except curses.error:
                pass

        # Handle input
        try:
            ch = stdscr.getch()
        except curses.error:
            ch = -1

        if ch == -1:
            continue

        if ch == ord("q"):
            break

        if ch == ord("r"):
            # Force refresh
            try:
                data = gather_data()
            except Exception:
                pass
            ds.last_refresh = time.time()

        elif ch == ord("p"):
            ds.paused = not ds.paused

        elif ch == ord("d"):
            ds.detail_mode = not ds.detail_mode

        elif ch == curses.KEY_UP:
            ds.scroll_up()

        elif ch == curses.KEY_DOWN:
            ds.scroll_down()

        elif ch == curses.KEY_RESIZE:
            stdscr.clear()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not sys.stdout.isatty():
        # Non-TTY: output a static report and exit
        try:
            print(generate_report())
        except Exception as exc:
            print(f"Error generating report: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        main()
