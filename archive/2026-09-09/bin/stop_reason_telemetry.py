#!/usr/bin/env python3
"""Stop-reason telemetry hook — cert M2 observability.

Cert Task 1.1 anti-pattern: "Setting arbitrary iteration caps as the primary
stopping mechanism." A correctly-terminating agentic loop ends on
``stop_reason == "end_turn"``; if ``max_turns`` is firing in production it
means the loop logic didn't recognize a natural stop, and the cap is masking
the bug. The audit's M2 finding says we should INSTRUMENT this so we can
tell whether it ever happens.

This hook runs on both ``Stop`` and ``SubagentStop``. The hook payload does
NOT include ``stop_reason`` (verified 2026-05-13), so we read the last
``assistant`` message out of the transcript file referenced by the payload
and pull ``stop_reason`` from there.

Output:
  - One JSONL line appended to ``~/.claude/state/stop_reason_telemetry.jsonl``
  - Rolling counter at ``~/.claude/state/stop_reason_counters.json``
    ({agent_type: {stop_reason: count}}) so weekly review is one cat.

Hot path budget: 5ms typical (last-line scan of the transcript). The hook
exits silently on any error — telemetry must never block the agent loop.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

HOME = Path(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")))
STATE_DIR = HOME / "state"
TELEMETRY_LOG = STATE_DIR / "stop_reason_telemetry.jsonl"
COUNTERS = STATE_DIR / "stop_reason_counters.json"
REWARDS_LOG = STATE_DIR / "route_rewards.jsonl"  # Slice 5: bandit training data

# --- Slice 5: bandit reward signal -----------------------------------------
# stop_reason → scalar reward ∈ [-1, +1]. Read at session Stop, joined to the
# route decision via decision_id (written by classify_prompt.py).
_REWARD_MAP = {
    "end_turn": 1.0,       # natural termination — agent completed cleanly
    "stop_sequence": 1.0,  # explicit stop_sequence — also natural completion
    "max_turns": -1.0,     # iteration cap fired — loop didn't self-terminate
    # tool_use as the FINAL session stop_reason = the session ended while a
    # tool call was pending. This is abnormal (not a normal mid-turn tool
    # call, which this hook never observes — it only sees the session's last
    # stop reason). The run was cut off.
    "tool_use": -0.5,
}


def _route_reward(stop_reason: str) -> float:
    """Scalar reward for bandit training. Range [-1, +1]. Unknown = 0.0."""
    return _REWARD_MAP.get(stop_reason, 0.0)


def _read_route_context(session_id: str) -> dict:
    """Read the per-session route temp file. Returns {} on any error so the
    caller treats a missing decision_id as 'no join available'."""
    route_file = Path(f"/tmp/claude-route-{session_id}.json")
    try:
        with route_file.open() as f:
            ctx = json.load(f)
        return ctx if isinstance(ctx, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _append_rewards_log(record: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with REWARDS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass
# ---------------------------------------------------------------------------

# Read the tail of the transcript without slurping the whole file. 64KB is
# plenty for the last assistant message and well within hook latency budget.
TAIL_BYTES = 65536


def _read_tail(path: Path, n: int = TAIL_BYTES) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > n:
                f.seek(size - n)
            return f.read().decode("utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return ""


def _last_assistant_stop_reason(transcript_path: Path) -> tuple[str | None, str | None]:
    """Return (stop_reason, model) from the last assistant message in the
    transcript JSONL. ``None`` if not found / malformed."""
    tail = _read_tail(transcript_path)
    if not tail:
        return None, None
    # JSONL: each line is one event. Walk backwards.
    lines = tail.splitlines()
    for line in reversed(lines):
        if not line or '"stop_reason"' not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = event.get("message") if isinstance(event, dict) else None
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        stop_reason = msg.get("stop_reason")
        model = msg.get("model")
        if isinstance(stop_reason, str):
            return stop_reason, model if isinstance(model, str) else None
    return None, None


def _bump_counter(agent_type: str, stop_reason: str) -> None:
    """Atomic-ish increment of the per-agent stop_reason counter."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        counters: dict[str, dict[str, int]] = {}
        if COUNTERS.exists():
            try:
                counters = json.loads(COUNTERS.read_text())
                if not isinstance(counters, dict):
                    counters = {}
            except json.JSONDecodeError:
                counters = {}
        agent_bucket = counters.setdefault(agent_type, {})
        if not isinstance(agent_bucket, dict):
            agent_bucket = {}
            counters[agent_type] = agent_bucket
        agent_bucket[stop_reason] = int(agent_bucket.get(stop_reason, 0)) + 1
        # Write to temp then rename for a poor-man's atomic swap.
        tmp = COUNTERS.with_suffix(".tmp")
        tmp.write_text(json.dumps(counters, sort_keys=True, indent=2))
        tmp.replace(COUNTERS)
    except OSError:
        pass


def _append_log(record: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with TELEMETRY_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0

    event = payload.get("hook_event_name", "")
    if event not in ("Stop", "SubagentStop"):
        return 0

    # SubagentStop has its own per-agent transcript at agent_transcript_path;
    # Stop reads the main session transcript_path.
    if event == "SubagentStop":
        transcript_str = payload.get("agent_transcript_path") or payload.get("transcript_path")
    else:
        transcript_str = payload.get("transcript_path")
    if not isinstance(transcript_str, str):
        return 0
    transcript = Path(transcript_str)

    stop_reason, model = _last_assistant_stop_reason(transcript)
    if stop_reason is None:
        return 0

    agent_type = str(payload.get("agent_type") or "unknown")
    session_id = str(payload.get("session_id") or "unknown")

    # --- Slice 5: reward record with join-key --------------------------------
    # Only a main-session Stop can join to a route decision (the route file is
    # written per session by classify_prompt.py). SubagentStop has no route
    # decision of its own → decision_id stays None (contributes no gradient).
    route_ctx = _read_route_context(session_id) if event == "Stop" else {}
    reward = _route_reward(stop_reason)
    decision_id = route_ctx.get("decision_id")
    # -------------------------------------------------------------------------

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "agent_type": agent_type,
        "agent_id": payload.get("agent_id"),
        "session_id": session_id,
        "stop_reason": stop_reason,
        "model": model,
        # Bandit join fields (None when no route context is available):
        "decision_id": decision_id,
        "route_hint_at_decision": route_ctx.get("route_hint"),
        "reward": reward,
    }
    _append_log(record)
    _bump_counter(agent_type, stop_reason)

    # Write the separate rewards log only when a join exists — keeps the
    # bandit training set free of SubagentStop rows with no decision.
    if decision_id is not None:
        _append_rewards_log({
            "ts": record["ts"],
            "session_id": session_id,
            "agent_type": agent_type,
            "decision_id": decision_id,
            "route_hint_at_decision": route_ctx.get("route_hint"),
            "stop_reason": stop_reason,
            "reward": reward,
        })
    return 0


if __name__ == "__main__":
    sys.exit(main())
