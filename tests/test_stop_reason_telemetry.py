"""Tests for stop_reason_telemetry hook (cert M2 observability)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


HOOK = Path(__file__).resolve().parent.parent / "bin" / "stop_reason_telemetry.py"


def _run_hook(payload: dict, env_overrides: dict | None = None) -> int:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    return result.returncode


def _make_transcript(path: Path, *, stop_reason: str = "end_turn") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": stop_reason,
            },
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_subagent_stop_writes_telemetry(tmp_path: Path) -> None:
    transcript = tmp_path / "agent.jsonl"
    _make_transcript(transcript, stop_reason="end_turn")
    home = tmp_path / "home"
    home.mkdir()
    payload = {
        "hook_event_name": "SubagentStop",
        "agent_id": "abc123",
        "agent_type": "explorer",
        "session_id": "s1",
        "agent_transcript_path": str(transcript),
    }
    rc = _run_hook(payload, {"CLAUDE_HOME": str(home)})
    assert rc == 0
    log = (home / "state" / "stop_reason_telemetry.jsonl").read_text().splitlines()
    assert len(log) == 1
    record = json.loads(log[0])
    assert record["event"] == "SubagentStop"
    assert record["agent_type"] == "explorer"
    assert record["stop_reason"] == "end_turn"
    assert record["model"] == "claude-opus-4-7"
    counters = json.loads((home / "state" / "stop_reason_counters.json").read_text())
    assert counters == {"explorer": {"end_turn": 1}}


def test_stop_event_reads_main_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / "main.jsonl"
    _make_transcript(transcript, stop_reason="end_turn")
    home = tmp_path / "home"
    payload = {
        "hook_event_name": "Stop",
        "session_id": "s1",
        "agent_type": "chad-twin",
        "transcript_path": str(transcript),
    }
    rc = _run_hook(payload, {"CLAUDE_HOME": str(home)})
    assert rc == 0
    counters = json.loads((home / "state" / "stop_reason_counters.json").read_text())
    assert counters == {"chad-twin": {"end_turn": 1}}


def test_max_tokens_is_distinguished_from_end_turn(tmp_path: Path) -> None:
    transcript = tmp_path / "agent.jsonl"
    _make_transcript(transcript, stop_reason="max_tokens")
    home = tmp_path / "home"
    payload = {
        "hook_event_name": "SubagentStop",
        "agent_type": "worker",
        "agent_transcript_path": str(transcript),
    }
    _run_hook(payload, {"CLAUDE_HOME": str(home)})
    counters = json.loads((home / "state" / "stop_reason_counters.json").read_text())
    assert counters == {"worker": {"max_tokens": 1}}


def test_counters_accumulate_across_invocations(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for stop_reason in ("end_turn", "end_turn", "max_tokens", "tool_use"):
        transcript = tmp_path / f"t_{stop_reason}_{os.getpid()}.jsonl"
        _make_transcript(transcript, stop_reason=stop_reason)
        payload = {
            "hook_event_name": "SubagentStop",
            "agent_type": "explorer",
            "agent_transcript_path": str(transcript),
        }
        _run_hook(payload, {"CLAUDE_HOME": str(home)})
    counters = json.loads((home / "state" / "stop_reason_counters.json").read_text())
    assert counters == {
        "explorer": {"end_turn": 2, "max_tokens": 1, "tool_use": 1}
    }


def test_no_transcript_path_is_no_op(tmp_path: Path) -> None:
    home = tmp_path / "home"
    rc = _run_hook(
        {"hook_event_name": "SubagentStop", "agent_type": "x"},
        {"CLAUDE_HOME": str(home)},
    )
    assert rc == 0
    assert not (home / "state" / "stop_reason_counters.json").exists()


def test_other_events_are_skipped(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    _make_transcript(transcript)
    home = tmp_path / "home"
    rc = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "agent_type": "x",
            "transcript_path": str(transcript),
        },
        {"CLAUDE_HOME": str(home)},
    )
    assert rc == 0
    assert not (home / "state" / "stop_reason_counters.json").exists()


def test_malformed_transcript_is_no_op(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("not json at all\n{broken\n")
    home = tmp_path / "home"
    rc = _run_hook(
        {
            "hook_event_name": "SubagentStop",
            "agent_type": "x",
            "agent_transcript_path": str(transcript),
        },
        {"CLAUDE_HOME": str(home)},
    )
    assert rc == 0
    assert not (home / "state" / "stop_reason_counters.json").exists()


def test_invalid_stdin_is_no_op(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_HOME": str(home)},
        timeout=10,
    )
    assert result.returncode == 0
    assert not (home / "state" / "stop_reason_counters.json").exists()


def test_only_last_assistant_message_is_used(tmp_path: Path) -> None:
    """If a transcript has multiple assistant messages, the last one's
    stop_reason is what we report."""
    transcript = tmp_path / "t.jsonl"
    events = [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "stop_reason": "tool_use",
                "content": [],
            },
        },
        {"type": "user", "message": {"role": "user", "content": "again"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "stop_reason": "end_turn",
                "content": [],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    home = tmp_path / "home"
    _run_hook(
        {
            "hook_event_name": "SubagentStop",
            "agent_type": "x",
            "agent_transcript_path": str(transcript),
        },
        {"CLAUDE_HOME": str(home)},
    )
    counters = json.loads((home / "state" / "stop_reason_counters.json").read_text())
    assert counters == {"x": {"end_turn": 1}}
