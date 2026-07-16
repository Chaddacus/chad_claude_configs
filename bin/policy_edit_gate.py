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

# Pointer-integrity gate: blocks edits that INTRODUCE a filesystem pointer that
# does not resolve on disk. Independent of the benchmark matrix and of async
# mode; covers a broader doc set (CLAUDE.md + standards/ + rules/ + agents/).
POINTER_CHECK_SCRIPT = CLAUDE_HOME / "bin" / "policy_pointer_check.py"
POINTER_WATCHED_GLOBS = [
    str(CLAUDE_HOME / "CLAUDE.md"),
    str(CLAUDE_HOME / "standards") + "/*.md",
    str(CLAUDE_HOME / "rules") + "/*.md",
    str(CLAUDE_HOME / "agents") + "/*.md",
]
POINTER_CHECK_TIMEOUT_S = int(os.environ.get("POLICY_EDIT_GATE_POINTER_TIMEOUT", "15"))


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


def _is_pointer_watched(target: str) -> bool:
    if not target:
        return False
    abs_target = str(Path(target).expanduser().resolve())
    for pattern in POINTER_WATCHED_GLOBS:
        if abs_target == pattern:
            return True
        if "*" in pattern and fnmatch.fnmatch(abs_target, pattern):
            return True
    return False


def _pointer_danglers(content: str) -> set[str] | None:
    """Run policy_pointer_check.py over `content`; return the set of dangling
    pointer strings, or None if the check could not run (fail-open)."""
    if not POINTER_CHECK_SCRIPT.is_file():
        return None
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tf:
            tf.write(content)
            tmp = tf.name
        proc = subprocess.run(
            ["python3", str(POINTER_CHECK_SCRIPT), tmp],
            capture_output=True, text=True, timeout=POINTER_CHECK_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    danglers: set[str] = set()
    for line in proc.stdout.splitlines():
        if line.startswith("DANGLING POINTER:") and " -> " in line:
            danglers.add(line.split(" -> ", 1)[1].strip())
    return danglers


def _pointer_block_if_new_danglers(target_path: Path, current: str, proposed: str) -> int | None:
    """Return 2 (block) if `proposed` introduces danglers absent from `current`.
    Returns None to allow (no new danglers, or the check could not run)."""
    base = _pointer_danglers(current)
    prop = _pointer_danglers(proposed)
    if base is None or prop is None:
        _log(f"POINTER_CHECK skipped (checker unavailable) target={target_path}")
        return None
    new = prop - base
    if not new:
        return None
    listing = "\n  ".join(sorted(new))
    print(
        f"policy_edit_gate: BLOCKED — proposed edit to {target_path.name} introduces "
        f"pointer(s) that do not resolve on disk:\n  {listing}\n"
        f"Fix the path, or set POLICY_EDIT_GATE_BYPASS=1 to override.",
        file=sys.stderr,
    )
    _log(f"POINTER_BLOCK target={target_path} new_danglers={sorted(new)}")
    _record_evidence({
        "ts": _now(), "action": "pointer_block",
        "target": str(target_path), "new_danglers": sorted(new),
    })
    return 2


def review_queue(archive: bool = False) -> int:
    """Operator CLI: triage the async-allow proposal queue (2026-07-16 audit M6).

    The async path records every allowed policy edit as a queue proposal "for
    later batch review" — and that review step had never happened (65
    unreviewed entries at audit time), making the gate log-only in practice.
    This turns review into one command:

        python3 policy_edit_gate.py --review-queue             # report only
        python3 policy_edit_gate.py --review-queue --archive   # report + archive

    Status per proposal, computed against the CURRENT file content:
        APPLIED      proposed content is present in the file today
        NOT-APPLIED  old content still present (edit was undone/never landed)
        SUPERSEDED   neither old nor new present (later edits rewrote the area)
        MISSING      target file no longer exists

    --archive moves entries to reviewed/<utc-date>/ with a summary JSON, so
    the queue only ever holds unreviewed items.
    """
    entries = sorted(QUEUE_DIR.glob("proposal-*.json")) if QUEUE_DIR.exists() else []
    if not entries:
        print("policy-edit-gate queue: empty")
        return 0

    rows = []
    for path in entries:
        try:
            p = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            rows.append((path, "UNREADABLE", "", ""))
            continue
        target = p.get("target", "")
        ti = p.get("tool_input") or {}
        tp = Path(target)
        if not tp.exists():
            status = "MISSING"
        else:
            current = tp.read_text(errors="replace")
            if p.get("tool_name") == "Write":
                status = "APPLIED" if ti.get("content", "") == current else "SUPERSEDED"
            else:
                new_s, old_s = ti.get("new_string", ""), ti.get("old_string", "")
                if new_s and new_s in current:
                    status = "APPLIED"
                elif old_s and old_s in current:
                    status = "NOT-APPLIED"
                else:
                    status = "SUPERSEDED"
        rows.append((path, status, p.get("ts", ""), target))

    by_status: dict[str, int] = {}
    for _, status, _, _ in rows:
        by_status[status] = by_status.get(status, 0) + 1
    print(f"policy-edit-gate queue: {len(rows)} proposal(s)  {by_status}")
    for path, status, ts, target in rows:
        print(f"  {status:<12} {ts[:19]:<19} {Path(target).name:<28} {path.name}")

    if archive:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        reviewed_dir = STATE_DIR / "reviewed" / day
        reviewed_dir.mkdir(parents=True, exist_ok=True)
        summary = [{"proposal": p.name, "status": s, "ts": ts, "target": t}
                   for p, s, ts, t in rows]
        (reviewed_dir / "review-summary.json").write_text(
            json.dumps({"reviewed_at": _now(), "entries": summary}, indent=2) + "\n")
        for path, _, _, _ in rows:
            shutil.move(str(path), str(reviewed_dir / path.name))
        _log(f"REVIEW archived {len(rows)} proposal(s) -> {reviewed_dir}")
        print(f"archived {len(rows)} -> {reviewed_dir}")
    return 0


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        # If we can't parse, don't block — log and allow.
        _log("WARN unparseable hook payload; allowing")
        return 0

    target = _extract_target(payload)
    matrix_watched = _is_watched(target)
    pointer_watched = _is_pointer_watched(target)
    if not matrix_watched and not pointer_watched:
        return 0  # not our problem; fast-path allow

    target_path = Path(target).expanduser().resolve()

    if BYPASS_MODE:
        _log(f"BYPASS allowing edit to {target_path} (POLICY_EDIT_GATE_BYPASS=1)")
        _record_evidence({
            "ts": _now(), "action": "bypass", "target": str(target_path),
            "reason": "POLICY_EDIT_GATE_BYPASS=1",
        })
        return 0

    # Pointer-integrity gate (fail-closed on NEW danglers). Runs for any
    # pointer-watched policy doc, independent of the benchmark matrix and async
    # mode. Errors fail open (never crash-block a policy edit).
    if pointer_watched:
        try:
            current_pc = target_path.read_text() if target_path.exists() else ""
            proposed_pc = _proposed_content(payload, current_pc)
            if proposed_pc != current_pc:
                rc = _pointer_block_if_new_danglers(target_path, current_pc, proposed_pc)
                if rc is not None:
                    return rc
        except Exception as exc:  # noqa: BLE001 — never crash-block
            _log(f"POINTER_CHECK error on {target_path}: {exc}; allowing")

    # The benchmark-matrix gate applies only to matrix-watched paths.
    if not matrix_watched:
        return 0

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
    # Operator CLI (audit M6): triage the async-allow proposal queue.
    # Hook invocations carry no argv, so this never interferes with gating.
    if "--review-queue" in sys.argv:
        sys.exit(review_queue(archive="--archive" in sys.argv))
    sys.exit(main())
