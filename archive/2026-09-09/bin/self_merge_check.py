#!/usr/bin/env python3
"""Stop hook: advisory check for CR-INV-003 worker-no-self-merge.

When a session dispatched workers via the Task tool AND subsequently
performed a merge action (git merge / gh pr merge / git push to main)
WITHOUT an intermediate reviewer hand-off marker, surface a non-blocking
advisory via stopReason. The rule from AgentOps INV-FLEET-NO-SELF-MERGE:
the entity that produced work cannot accept its own work.

Detection (heuristic, transcript-based):
  - Count Task tool_use invocations in the session
  - Count merge-shaped Bash commands (`git merge`, `gh pr merge`, push to main)
  - Count reviewer-marker Task invocations (subagent_type contains "reviewer")
  - If (workers >= 1) AND (merges >= 1) AND (reviewer markers < 1), advise

False-positive cases (acknowledged, not blocked):
  - A merge of an external PR you reviewed in this session — heuristic
    misses the review unless it went through a Task with reviewer agent
  - A self-merge of trivial / non-worker-produced work — caller has to
    ignore the advisory in that case
  - Worker dispatched but didn't actually merge — counter-balanced by
    the merge-action presence check

Promotion path to full enforcement (future):
  PR-side GitHub Action that fails the workflow if the merging actor
  matches the PR author OR a worker bot account; that's per-repo
  deployment, out of scope for this hook.

Reference: ~/.claude/standards/CHAD_RUNTIME_INVARIANTS.md row CR-INV-003;
AgentOps INV-FLEET-NO-SELF-MERGE.
"""

from __future__ import annotations

import json
import os
import sys

HOOK_PROFILE_ID = "self_merge_check"
MERGE_BASH_PATTERNS = (
    "git merge ",
    "git push origin main",
    "git push origin master",
    "gh pr merge",
)

# Hook profile guard (best-effort)
try:
    sys.path.insert(0, os.path.join(
        os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"
    ))
    from hook_profile import should_run  # type: ignore
    if not should_run(HOOK_PROFILE_ID):
        sys.exit(0)
except Exception:
    pass


def _read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _iter_transcript(path: str):
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


def _scan(path: str) -> dict:
    """Return counts of {tasks, merges, reviewer_tasks}."""
    tasks = 0
    reviewer_tasks = 0
    merges = 0
    for row in _iter_transcript(path):
        if row.get("type") != "assistant":
            continue
        msg = row.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                name = block.get("name") or ""
                input_obj = block.get("input") or {}
                if name == "Task":
                    tasks += 1
                    subagent = (input_obj.get("subagent_type") or "").lower()
                    if "reviewer" in subagent:
                        reviewer_tasks += 1
                elif name == "Bash":
                    cmd = (input_obj.get("command") or "").strip()
                    for pat in MERGE_BASH_PATTERNS:
                        if pat in cmd:
                            merges += 1
                            break
    return {"tasks": tasks, "reviewer_tasks": reviewer_tasks, "merges": merges}


def _emit_advisory(counts: dict) -> None:
    msg = (
        "⚠️  CR-INV-003 advisory: this session dispatched "
        f"{counts['tasks']} worker Task(s) and performed {counts['merges']} "
        f"merge-shaped action(s) with {counts['reviewer_tasks']} reviewer "
        "Task(s). The runtime invariant CR-INV-003-WORKER-NO-SELF-MERGE says "
        "workers do not accept their own work — acceptance authority is the "
        "supervisor/reviewer. If you (the supervisor) merged worker output "
        "without an intermediate reviewer Task, that crosses the boundary. "
        "If a reviewer ran outside the Task tool (e.g. cross-model via codex "
        "or human review), this advisory is a false positive — dismiss. "
        "Advisory only — not blocking."
    )
    print(json.dumps({"stopReason": msg}))


def main() -> int:
    payload = _read_stdin_json()
    transcript_path = payload.get("transcript_path") or ""
    if not transcript_path or not os.path.exists(transcript_path):
        return 0
    counts = _scan(transcript_path)
    if counts["tasks"] >= 1 and counts["merges"] >= 1 and counts["reviewer_tasks"] < 1:
        _emit_advisory(counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
