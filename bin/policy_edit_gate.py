#!/usr/bin/env python3
"""policy_edit_gate.py — PreToolUse hook gating edits to global policy files.

Wired in ~/.claude/settings.json as a PreToolUse hook on Edit|Write tool calls.
Reads the hook payload from stdin. If the tool is editing a watched policy
path, runs the autoconfig benchmark matrix to score baseline vs candidate
config; blocks if any rubric regresses by more than the configured threshold.

Watched paths (anything matching is gated):
  - ~/.claude/CLAUDE.md
  - ~/.claude/state/route_manifest.json
  - ~/.claude/state/control_plane.json
  - ~/.claude/agents/**.md

Plan: ~/.claude/plans/users-chadsimon-thoughts-md-take-a-giggly-moore.md (slice 6)

Bypass: set POLICY_EDIT_GATE_BYPASS=1 in env (recorded in evidence log).
Async mode: set POLICY_EDIT_GATE_ASYNC=1 to log proposals + allow without
blocking (useful when matrix latency is unacceptable). Async-allow records
the proposal for later batch review at ~/.claude/state/policy-edit-gate/queue/.

Hook contract: exit 0 = allow; exit 2 = block (proposed edit aborts);
exit 1 = non-blocking error (Claude proceeds but is informed).
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME = Path(os.path.expanduser("~"))
CLAUDE_HOME = HOME / ".claude"
STATE_DIR = CLAUDE_HOME / "state" / "policy-edit-gate"
QUEUE_DIR = STATE_DIR / "queue"
EVIDENCE_DIR = STATE_DIR / "evidence"
LOG_FILE = STATE_DIR / "gate.log"

WATCHED_PATTERNS = [
    str(CLAUDE_HOME / "CLAUDE.md"),
    str(CLAUDE_HOME / "state" / "route_manifest.json"),
    str(CLAUDE_HOME / "state" / "control_plane.json"),
    str(CLAUDE_HOME / "agents") + "/*.md",
]

REGRESSION_THRESHOLD_PP = float(os.environ.get("POLICY_EDIT_GATE_REGRESSION_PP", "1.0"))
# Async mode: env var forces it ON ("1") or forces it OFF ("0").
# When unset, the hook auto-classifies per-target (high-stakes paths run sync,
# lower-stakes — e.g. agents/*.md — run async).
_ASYNC_ENV = os.environ.get("POLICY_EDIT_GATE_ASYNC")
ASYNC_MODE_FORCED = _ASYNC_ENV == "1"
SYNC_MODE_FORCED = _ASYNC_ENV == "0"
BYPASS_MODE = os.environ.get("POLICY_EDIT_GATE_BYPASS") == "1"

# High-stakes paths always run sync (block on regression). Lower-stakes paths
# default to async unless POLICY_EDIT_GATE_ASYNC=0 forces sync.
HIGH_STAKES_PATHS = {
    str(CLAUDE_HOME / "CLAUDE.md"),
    str(CLAUDE_HOME / "state" / "route_manifest.json"),
    str(CLAUDE_HOME / "state" / "control_plane.json"),
}
MATRIX_SCRIPT = CLAUDE_HOME / "skills" / "autoconfig" / "scripts" / "run_benchmark_matrix.py"
MATRIX_TIMEOUT_S = int(os.environ.get("POLICY_EDIT_GATE_MATRIX_TIMEOUT", "1800"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(line: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(f"[{_now()}] {line}\n")


def _is_watched(target: str) -> bool:
    if not target:
        return False
    abs_target = str(Path(target).expanduser().resolve())
    for pattern in WATCHED_PATTERNS:
        if abs_target == pattern:
            return True
        if "*" in pattern and fnmatch.fnmatch(abs_target, pattern):
            return True
    return False


def _extract_target(payload: dict[str, Any]) -> str:
    """Pull the file path the tool wants to edit out of common payload shapes."""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    return (
        tool_input.get("file_path")
        or tool_input.get("filePath")
        or tool_input.get("path")
        or payload.get("file_path")
        or ""
    )


def _proposed_content(payload: dict[str, Any], current: str) -> str:
    """Reconstruct what the file would look like after the proposed tool call."""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    tool_name = payload.get("tool_name") or payload.get("toolName") or ""

    if tool_name == "Write":
        return tool_input.get("content", "")

    # Edit / replace_all
    old = tool_input.get("old_string", "")
    new = tool_input.get("new_string", "")
    replace_all = tool_input.get("replace_all", False)
    if replace_all:
        return current.replace(old, new)
    return current.replace(old, new, 1) if old else current


def _stage_candidate_home(target_path: Path, proposed_content: str) -> Path:
    """Snapshot CLAUDE_HOME (lightweight: just policy-relevant files) into a tmp
    dir, apply the proposed change to target_path within it, and return the
    candidate home path. Heavy assets (state/, plugins/, projects/, cache/,
    backups/, file-history/, history.jsonl) are excluded — the matrix only
    reads CLAUDE.md, settings.json, route_manifest.json, agents/*.md."""
    tmp_root = Path(tempfile.mkdtemp(prefix="policy-edit-candidate-"))
    keep_files = [
        CLAUDE_HOME / "CLAUDE.md",
        CLAUDE_HOME / "settings.json",
        CLAUDE_HOME / "state" / "route_manifest.json",
        CLAUDE_HOME / "state" / "control_plane.json",
    ]
    for f in keep_files:
        if f.is_file():
            dest = tmp_root / f.relative_to(CLAUDE_HOME)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)

    agents_src = CLAUDE_HOME / "agents"
    if agents_src.is_dir():
        agents_dest = tmp_root / "agents"
        agents_dest.mkdir(parents=True, exist_ok=True)
        for md in agents_src.glob("*.md"):
            shutil.copy2(md, agents_dest / md.name)

    # Apply the proposed edit
    candidate_target = tmp_root / target_path.relative_to(CLAUDE_HOME)
    candidate_target.parent.mkdir(parents=True, exist_ok=True)
    candidate_target.write_text(proposed_content)
    return tmp_root


def _record_evidence(record: dict[str, Any]) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE_DIR / f"evidence-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.json"
    out.write_text(json.dumps(record, indent=2, default=str))
    return out


def _async_queue(record: dict[str, Any]) -> Path:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    out = QUEUE_DIR / f"proposal-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.json"
    out.write_text(json.dumps(record, indent=2, default=str))
    return out


def _run_matrix(candidate_home: Path) -> dict[str, Any]:
    """Run the benchmark matrix comparing current vs candidate; return a
    summary dict with per-rubric regression deltas."""
    if not MATRIX_SCRIPT.is_file():
        return {"ok": False, "error": f"matrix script missing: {MATRIX_SCRIPT}"}
    cmd = [
        "python3", str(MATRIX_SCRIPT),
        "--preset", "current",
        "--preset", "candidate",
        "--candidate-home", str(candidate_home),
        "--repeats", "1",  # smoke mode — gate latency floor
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=MATRIX_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"matrix timed out after {MATRIX_TIMEOUT_S}s"}
    except OSError as exc:
        return {"ok": False, "error": f"matrix invocation failed: {exc}"}

    if proc.returncode != 0:
        return {"ok": False, "error": f"matrix exit {proc.returncode}: {proc.stderr[-500:]}"}
    try:
        summary = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"unparseable matrix output: {exc}", "raw": proc.stdout[-500:]}

    # Compute regression deltas from the canonical matrix schema:
    #   {"preset_summaries": {<preset_id>: {"median_composite_score": ...}}}
    # See run_benchmark_matrix.py::_build_summary.
    summaries = summary.get("preset_summaries", {}) if isinstance(summary, dict) else {}
    cur = summaries.get("current", {}) if isinstance(summaries, dict) else {}
    cand = summaries.get("candidate", {}) if isinstance(summaries, dict) else {}
    cur_score = cur.get("median_composite_score")
    cand_score = cand.get("median_composite_score")

    delta = None
    if cur_score is not None and cand_score is not None:
        delta = float(cand_score) - float(cur_score)

    return {
        "ok": True,
        "current_score": cur_score,
        "candidate_score": cand_score,
        "delta_pp": delta,
        "regressed": (delta is not None and delta < -REGRESSION_THRESHOLD_PP),
        "raw_summary": summary,
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        # If we can't parse, don't block — log and allow.
        _log("WARN unparseable hook payload; allowing")
        return 0

    target = _extract_target(payload)
    if not _is_watched(target):
        return 0  # not our problem; fast-path allow

    target_path = Path(target).expanduser().resolve()

    # Decide sync/async per target. Forced env vars win; otherwise high-stakes
    # paths run sync, everything else runs async.
    is_high_stakes = str(target_path) in HIGH_STAKES_PATHS
    if SYNC_MODE_FORCED:
        async_mode = False
    elif ASYNC_MODE_FORCED:
        async_mode = True
    else:
        async_mode = not is_high_stakes
    _log(f"GATE target={target_path} async={async_mode} high_stakes={is_high_stakes} bypass={BYPASS_MODE}")

    if BYPASS_MODE:
        _log("BYPASS allowing edit (POLICY_EDIT_GATE_BYPASS=1)")
        _record_evidence({
            "ts": _now(), "action": "bypass", "target": str(target_path),
            "reason": "POLICY_EDIT_GATE_BYPASS=1",
        })
        return 0

    if not target_path.exists():
        # New file — no baseline to compare against. Record + allow.
        _log(f"NEW_FILE allowing creation of {target_path}")
        _record_evidence({"ts": _now(), "action": "new_file_allow", "target": str(target_path)})
        return 0

    try:
        current = target_path.read_text()
    except OSError as exc:
        _log(f"WARN cannot read {target_path}: {exc}; allowing")
        return 0

    proposed = _proposed_content(payload, current)
    if proposed == current:
        _log("NO_OP proposed content matches current; allowing")
        return 0

    if async_mode:
        out = _async_queue({
            "ts": _now(), "target": str(target_path),
            "tool_name": payload.get("tool_name") or payload.get("toolName"),
            "tool_input": payload.get("tool_input") or payload.get("toolInput"),
        })
        _log(f"ASYNC queued proposal {out}; allowing without matrix run")
        return 0

    # Sync gate — run the matrix
    candidate_home = _stage_candidate_home(target_path, proposed)
    try:
        result = _run_matrix(candidate_home)
    finally:
        shutil.rmtree(candidate_home, ignore_errors=True)

    record = {
        "ts": _now(), "target": str(target_path),
        "tool_name": payload.get("tool_name") or payload.get("toolName"),
        "matrix_result": result,
    }
    evidence_path = _record_evidence(record)

    if not result.get("ok"):
        _log(f"MATRIX_ERROR {result.get('error')}; allowing (fail-open per gate-not-running policy)")
        return 1  # non-blocking error

    if result.get("regressed"):
        msg = (
            f"policy_edit_gate: BLOCKED — proposed edit to {target_path.name} regresses "
            f"benchmark composite by {result['delta_pp']:.2f}pp (threshold "
            f"{REGRESSION_THRESHOLD_PP}pp). Evidence: {evidence_path}. "
            f"To override: set POLICY_EDIT_GATE_BYPASS=1 in env."
        )
        print(msg, file=sys.stderr)
        _log(f"BLOCK target={target_path} delta_pp={result['delta_pp']}")
        return 2  # block

    _log(f"PASS target={target_path} delta_pp={result.get('delta_pp')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
