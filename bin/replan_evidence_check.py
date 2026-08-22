#!/usr/bin/env python3
"""Stop hook: advisory check for CR-INV-009 replan-cites-evidence.

Reads the session transcript provided by Claude Code on stdin, counts Task
tool_use blocks whose corresponding tool_result is_error=True, and — when
that count is >= the threshold — queries omni-mem for replan-* journal
entries authored by the tree agent (agent_for_cwd) since the session started. If at least one
worker dispatch failed and zero replan entries exist for the window, the
hook surfaces a *non-blocking* advisory via stopReason.

This is intentionally advisory, not blocking, because:
  1. The heuristic ("Task tool_use whose tool_result was an error") is the
     most reliable signal available from a transcript scan, but it is not
     a structured "the agent pivoted approach" event. A worker that errored
     once and was retried successfully is not a pivot; this hook would not
     fire on that case because the threshold is >= 2 failures.
  2. Real worker dispatches can legitimately fail without an approach change
     (transient API error, infrastructure blip, race condition that resolved
     on retry). Blocking on these would train the user to ignore the hook.
  3. Full enforcement of CR-INV-009 ("approach pivots must be recorded as
     structured replan-* journal entries") requires instrumented dispatch —
     the supervisor would emit a "pivot decision" event when its
     2-attempt rule fires, and the hook would assert a matching journal
     entry. That instrumentation does not exist in the current playbook (it is
     prompt-driven). This hook is the convention-tier promotion: advisory.

Promotion path to full enforcement (future, not this slice):
  - the supervisor protocol emits a structured "replan_pending" event
    when 2-attempt rule fires.
  - This hook reads those events instead of the transcript heuristic.
  - When a "replan_pending" event has no matching replan-* journal entry by
    Stop time, the hook exits with a blocking exit code (2) and a
    remediation prompt.

Reference: ~/.claude/standards/CHAD_RUNTIME_INVARIANTS.md row CR-INV-009;
~/.claude/standards/REPLAN_DECISION_PROTOCOL.md for the journal_write shape;
~/automation_architecture/docs/ARCHITECTURE_INVARIANTS.md row
INV-REPLAN-CITES-EVIDENCE for the AgentOps analog.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from omni_mem_route import agent_for_cwd, container_for_cwd

FAILED_DISPATCH_THRESHOLD = 2  # >= this many failed Task results triggers the check
OMNI_MEM_QUERY_LIMIT = 50      # how many journal entries to fetch
OMNI_MEM_QUERY_TIMEOUT = 5     # seconds; query is best-effort
HOOK_PROFILE_ID = "replan_evidence_check"

# Sentinel-file enforcement (CR-INV-009 promotion to enforced).
# The supervisor protocol writes
# /tmp/claude-replan-pending-<session>.json when its 2-attempt rule fires
# (see ~/.claude/standards/REPLAN_DECISION_PROTOCOL.md and
# ~/.claude/standards/ORCHESTRATION_PLAYBOOK.md). When --strict is passed and the sentinel
# exists with no matching replan-* journal entry, the hook exits non-zero
# to block Stop until the user records the pivot.
SENTINEL_PATH_TEMPLATE = "/tmp/claude-replan-pending-{session}.json"

# Optional integration with the existing hook profile guard
try:
    sys.path.insert(0, os.path.join(
        os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"
    ))
    from hook_profile import should_run  # type: ignore
    if not should_run(HOOK_PROFILE_ID):
        sys.exit(0)
except Exception:
    pass  # missing or errored — proceed without throttling


def _read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _iter_transcript(path: str):
    """Yield parsed JSON rows from the transcript JSONL file."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _count_failed_task_dispatches(transcript_path: str) -> tuple[int, int]:
    """Return (total_task_invocations, failed_task_invocations).

    Walks the transcript twice: first to collect tool_use_ids of Task
    invocations, then to find matching tool_result blocks with is_error=True.
    Two passes because tool_result entries appear after the tool_use that
    spawned them, but the file is small enough that a two-pass scan is
    cheaper than maintaining a deferred-resolution map.
    """
    task_ids: set[str] = set()
    for row in _iter_transcript(transcript_path):
        if row.get("type") != "assistant":
            continue
        msg = row.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Task":
                use_id = block.get("id")
                if use_id:
                    task_ids.add(use_id)

    if not task_ids:
        return 0, 0

    failed = 0
    for row in _iter_transcript(transcript_path):
        if row.get("type") != "user":
            continue
        msg = row.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if (
                block.get("type") == "tool_result"
                and block.get("tool_use_id") in task_ids
                and block.get("is_error") is True
            ):
                failed += 1
    return len(task_ids), failed


def _session_start_ts(transcript_path: str) -> float:
    """Return the session start as a unix timestamp.

    Uses the transcript file's ctime as a proxy. Claude Code creates the
    transcript at session start, so this is a reasonable approximation.
    """
    try:
        return os.path.getctime(transcript_path)
    except OSError:
        return 0.0


def _parse_iso(ts: str) -> float:
    try:
        # Handle the `Z` suffix some sources emit.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _replan_journal_count(workspace_id: str, since_ts: float) -> int | None:
    """Return number of replan-* journal entries since `since_ts`, or None on query failure."""
    try:
        result = subprocess.run(
            [
                # Vault routed by cwd: ~/chad_personal -> omni-mem-personal, else omni-mem.
                "docker", "exec", container_for_cwd(), "omni-mem", "journal_read",
                "--workspaceId", workspace_id,
                "--agentName", agent_for_cwd(),
                "--limit", str(OMNI_MEM_QUERY_LIMIT),
            ],
            capture_output=True,
            text=True,
            timeout=OMNI_MEM_QUERY_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(entries, list):
        return None
    count = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        topic = entry.get("topic") or ""
        if not topic.startswith("replan-"):
            continue
        created = _parse_iso(entry.get("createdAt") or "")
        if created >= since_ts:
            count += 1
    return count


def _emit_advisory(failed: int, total: int, replan_count: int | None) -> None:
    parts = [
        f"⚠️  CR-INV-009 advisory: detected {failed} failed Task-tool dispatches "
        f"(out of {total} total Task calls) this session.",
    ]
    if replan_count is None:
        parts.append(
            "Unable to query omni-mem for replan-* journal entries (docker exec omni-mem unreachable or timed out)."
        )
    else:
        parts.append(
            f"Found {replan_count} replan-* journal entries for the tree agent in the session window."
        )
    parts.append(
        "If any of those failures triggered an approach pivot, the pivot should be "
        "recorded per ~/.claude/standards/REPLAN_DECISION_PROTOCOL.md before closing "
        "(topic: replan-<slug>; fields: trigger_evidence, candidates_scored, threshold, "
        "selected, rejected_reasons, rationale). Advisory only — not blocking."
    )
    print(json.dumps({"stopReason": "\n".join(parts)}))


def _sentinel_path(session_id: str) -> str:
    return SENTINEL_PATH_TEMPLATE.format(session=session_id or "default")


def _sentinel_exists(session_id: str) -> bool:
    return os.path.exists(_sentinel_path(session_id))


def _emit_blocking(failed: int, total: int, replan_count: int | None, sentinel: str) -> None:
    """Emit a blocking stopReason when sentinel exists with no journal entry."""
    msg = (
        f"🛑 CR-INV-009 BLOCK: replan sentinel found at {sentinel} indicating "
        "an approach pivot fired during this session, but no matching replan-* "
        f"journal entry exists in omni-mem (found {replan_count}). "
        "Per ~/.claude/standards/REPLAN_DECISION_PROTOCOL.md, structured pivot "
        "evidence is required before Stop. Record the pivot with:\n"
        "  docker exec omni-mem omni-mem journal_write \\\n"
        "    --workspaceId <ws> --agentName <tree agent, e.g. chad-work> \\\n"
        "    --topic replan-<slug> --content '<trigger/candidates/threshold/selected/rejected/rationale>'\n"
        "Then remove the sentinel file and re-Stop."
    )
    print(json.dumps({"stopReason": msg, "decision": "block"}))


def main() -> int:
    parser = argparse.ArgumentParser(description="CR-INV-009 replan-cites-evidence Stop-hook check.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Block Stop (non-zero exit) when sentinel exists with no journal entry.",
    )
    args, _ = parser.parse_known_args()

    payload = _read_stdin_json()
    transcript_path = payload.get("transcript_path") or ""
    session_id = (
        payload.get("session_id")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or "default"
    )
    if not transcript_path or not os.path.exists(transcript_path):
        return 0

    workspace_id = os.environ.get("CLAUDE_WORKSPACE_ID") or os.path.basename(os.getcwd()) or "chadsimon"

    # Path 1 — strict mode + sentinel check (supervisor-instrumented pivot)
    sentinel = _sentinel_path(session_id)
    if _sentinel_exists(session_id):
        since = _session_start_ts(transcript_path)
        replan_count = _replan_journal_count(workspace_id, since)
        if replan_count is None or replan_count == 0:
            if args.strict:
                _emit_blocking(0, 0, replan_count, sentinel)
                return 2  # block Stop
            # Non-strict mode with sentinel + no entry: emit advisory anyway
            _emit_advisory(0, 0, replan_count)
            return 0
        # Sentinel exists AND a journal entry was recorded — clean state;
        # remove the sentinel so subsequent Stops don't re-fire.
        try:
            os.remove(sentinel)
        except OSError:
            pass
        return 0

    # Path 2 — heuristic mode (sentinel absent, scan transcript)
    total, failed = _count_failed_task_dispatches(transcript_path)
    if failed < FAILED_DISPATCH_THRESHOLD:
        return 0

    since = _session_start_ts(transcript_path)
    replan_count = _replan_journal_count(workspace_id, since)
    if replan_count == 0 or replan_count is None:
        _emit_advisory(failed, total, replan_count)

    return 0


if __name__ == "__main__":
    sys.exit(main())
