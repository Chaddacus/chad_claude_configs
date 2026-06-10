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
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "agent_type": agent_type,
        "agent_id": payload.get("agent_id"),
        "session_id": payload.get("session_id"),
        "stop_reason": stop_reason,
        "model": model,
    }
    _append_log(record)
    _bump_counter(agent_type, stop_reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
