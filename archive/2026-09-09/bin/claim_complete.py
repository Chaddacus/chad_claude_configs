#!/usr/bin/env python3
"""Helper for filing a structured completion record.

Usage (called by the agent):

    python3 ~/.claude/bin/claim_complete.py <<'JSON'
    {
      "kind": "completion",
      "claim": "stop_gate.py L2 design complete",
      "files_modified": ["/path/to/foo.py"],
      "commands_run": [{"cmd": "pytest", "exit": 0, "summary": "47 passed"}],
      "slices_completed": ["slice_1"],
      "slices_remaining": []
    }
    JSON

Or for a non-completion stop:

    {"kind": "blocked", "blocker_type": "external_dependency",
     "description": "upstream API down"}
    {"kind": "fork", "options": [{"name": "A", "desc": "..."},
                                  {"name": "B", "desc": "..."}]}

Writes to ~/.claude/state/cases/${session_id}/completion.json.
Returns the path on success, exit 1 on schema error.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"))
from case_file import write_completion, read_merged_summary, resolve_session_id

ALLOWED_KINDS = {"completion", "blocked", "fork"}


def check_attribution(record: dict, summary: dict) -> list[str]:
    """A completion record's file evidence must match THIS session's recorded
    tool activity. Foreign evidence (files this session never touched) is how
    an agent files completion for work it didn't do (2026-06-09 incident).
    Returns list of unmatched files (empty = ok)."""
    if record.get("kind") != "completion":
        return []
    claimed = record.get("files_modified") or []
    if not isinstance(claimed, list):
        return []
    touched = set(summary.get("files_touched", []))
    touched_names = {os.path.basename(f) for f in touched}
    unmatched = []
    for f in claimed:
        if not isinstance(f, str):
            continue
        # Accept exact path or basename match (records often use relative paths)
        if f in touched or os.path.basename(f) in touched_names:
            continue
        unmatched.append(f)
    return unmatched


def validate(record: dict) -> tuple[bool, str]:
    if not isinstance(record, dict):
        return False, "record must be a JSON object"
    kind = record.get("kind")
    if kind not in ALLOWED_KINDS:
        return False, f"kind must be one of {sorted(ALLOWED_KINDS)}"
    if kind == "completion":
        # Soft schema — encourage but don't reject if optional fields missing
        for k in ("claim",):
            if not record.get(k):
                return False, f"completion requires non-empty '{k}'"
    elif kind == "blocked":
        for k in ("blocker_type", "description"):
            if not record.get(k):
                return False, f"blocked requires non-empty '{k}'"
    elif kind == "fork":
        opts = record.get("options")
        if not isinstance(opts, list) or len(opts) < 2:
            return False, "fork requires options=[{name,desc},...] with >=2 entries"
    return True, ""


def main() -> int:
    raw = sys.stdin.read()
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"claim_complete: invalid JSON ({e})", file=sys.stderr)
        return 1

    ok, err = validate(record)
    if not ok:
        print(f"claim_complete: {err}", file=sys.stderr)
        return 1

    sid = resolve_session_id()
    # Merged view (live turn + rotated turns/): completion records describe
    # task-level work, which spans the per-turn rotation done by case_rotator.
    summary = read_merged_summary(sid)

    unmatched = check_attribution(record, summary)
    if unmatched:
        print(
            "claim_complete: REJECTED — completion evidence not attributable "
            "to this session. These files appear in files_modified but were "
            f"never edited in this session's recorded activity: {unmatched}. "
            "If another agent did the work, report it as their work — do not "
            "file completion for it.",
            file=sys.stderr,
        )
        return 1

    record["session_id"] = sid
    p = write_completion(record, session_id=sid)
    print(json.dumps({
        "ok": True,
        "completion_path": str(p),
        "files_touched_this_session": len(summary.get("files_touched", [])),
        "verifications_this_session": len(summary.get("verifications", [])),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
