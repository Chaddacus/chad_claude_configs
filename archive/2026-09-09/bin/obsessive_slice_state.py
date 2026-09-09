#!/usr/bin/env python3
"""obsessive_slice_state.py — JSONL-backed slice state CLI for the orchestrator.

Tracks slice lifecycle across a run of the obsessive_loop where the orchestrator
(LLM in the driver session) decomposes work into slices, dispatches each to
goose via goose_dispatch.py, and decides accept/reject/block from the returned
SliceReport.json.

State layout:
    ~/.claude/state/obsessive-loop/<run_id>/
        run.json              — run metadata (repo, branch, baseline_scorecard, started_at)
        events.jsonl          — append-only event log (the source of truth)
        slices/<slice_id>.json   — full SliceSpec for each registered slice
        reports/<slice_id>.json  — SliceReport from worker_post.sh (written by acceptance script)
        .base_sha-<slice_id>     — recorded base SHA for the slice (used by acceptance script)

Subcommands:
    init          --repo <path> [--branch <name>] [--baseline-scorecard <path>]
                  Creates a run dir, captures baseline, prints run_id + path.
    register      --run <run_id> --slice <slice_id> --spec <json_or_file> [--rationale <text>]
                  Records a SliceSpec under slices/<slice_id>.json.
    next          --run <run_id>
                  Prints next runnable slice_id (status=pending, deps satisfied).
    mark          --run <run_id> --slice <slice_id> --status accepted|rejected|blocked
                  [--evidence <path>] [--commit-sha <sha>] [--reason <text>]
                  Appends event; on `accepted`, records commit-sha if given.
    summary       --run <run_id>
                  Prints scoreboard: per-slice status, deltas, accepted commits.

All commands emit JSON on stdout for clean orchestrator parsing.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME = Path(os.path.expanduser("~"))
STATE_ROOT = HOME / ".claude" / "state" / "obsessive-loop"

VALID_STATUSES = {"pending", "in_flight", "accepted", "rejected", "blocked"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit(d: Any) -> None:
    print(json.dumps(d, indent=2), flush=True)


def _run_dir(run_id: str) -> Path:
    return STATE_ROOT / run_id


def _events_path(run_id: str) -> Path:
    return _run_dir(run_id) / "events.jsonl"


def _append_event(run_id: str, event: dict[str, Any]) -> None:
    event = {"ts": utc_now(), **event}
    p = _events_path(run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(event) + "\n")


def _replay_state(run_id: str) -> dict[str, dict[str, Any]]:
    """Replay events.jsonl → {slice_id: latest-state-record}."""
    state: dict[str, dict[str, Any]] = {}
    events_path = _events_path(run_id)
    if not events_path.exists():
        return state
    with events_path.open() as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = ev.get("slice_id")
            if not sid:
                continue
            kind = ev.get("kind")
            if kind == "register":
                state[sid] = {
                    "slice_id": sid,
                    "status": "pending",
                    "registered_at": ev["ts"],
                    "spec": ev.get("spec", {}),
                    "rationale": ev.get("rationale", ""),
                    "history": [ev],
                }
            elif sid in state:
                state[sid]["history"].append(ev)
                if kind == "status_change":
                    state[sid]["status"] = ev.get("status", state[sid]["status"])
                    if ev.get("commit_sha"):
                        state[sid]["commit_sha"] = ev["commit_sha"]
                    if ev.get("evidence"):
                        state[sid]["evidence"] = ev["evidence"]
                    if ev.get("reason"):
                        state[sid]["reason"] = ev["reason"]
                    if ev.get("rubric_delta") is not None:
                        state[sid]["rubric_delta"] = ev["rubric_delta"]
    return state


# ----------------------------------------------------------------------------
# Subcommands
# ----------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        _emit({"error": f"repo not a directory: {repo}"})
        return 2
    run_id = args.run_id or f"obs-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    rd = _run_dir(run_id)
    (rd / "slices").mkdir(parents=True, exist_ok=True)
    (rd / "reports").mkdir(parents=True, exist_ok=True)

    # Capture baseline scorecard. Search order:
    #  1. --baseline-scorecard <path>      (explicit override)
    #  2. <repo>/.artifacts/rubric-suite/scorecard.json   (worktree-local; usually
    #     missing for fresh worktrees because .artifacts/ is gitignored)
    #  3. <source_repo>/.artifacts/rubric-suite/scorecard.json where source_repo
    #     is the worktree's main repo. Walks `git worktree list` to find it.
    candidates: list[Path] = []
    if args.baseline_scorecard:
        candidates.append(Path(args.baseline_scorecard).expanduser())
    candidates.append(repo / ".artifacts" / "rubric-suite" / "scorecard.json")
    # Try the source repo if `repo` is a worktree
    try:
        import subprocess
        out = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            # First "worktree <path>" entry is the main repo
            for line in out.stdout.splitlines():
                if line.startswith("worktree "):
                    main_repo = Path(line.split(maxsplit=1)[1])
                    if main_repo != repo:
                        candidates.append(main_repo / ".artifacts" / "rubric-suite" / "scorecard.json")
                    break
    except Exception:  # noqa: BLE001
        pass

    baseline_recorded = None
    for src in candidates:
        if src.is_file():
            (rd / "baseline-scorecard.json").write_bytes(src.read_bytes())
            baseline_recorded = str(src)
            break

    run_meta = {
        "run_id": run_id,
        "repo": str(repo),
        "branch": args.branch,
        "baseline_scorecard": baseline_recorded,
        "started_at": utc_now(),
    }
    (rd / "run.json").write_text(json.dumps(run_meta, indent=2))
    _append_event(run_id, {"kind": "run_init", "slice_id": None, **run_meta})
    _emit({"run_id": run_id, "run_dir": str(rd), "baseline_scorecard": baseline_recorded})
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    run_id = args.run
    rd = _run_dir(run_id)
    if not rd.is_dir():
        _emit({"error": f"run {run_id} not found"})
        return 2

    spec_arg = args.spec
    if spec_arg.startswith("@"):
        spec = json.loads(Path(spec_arg[1:]).read_text())
    elif Path(spec_arg).is_file():
        spec = json.loads(Path(spec_arg).read_text())
    else:
        spec = json.loads(spec_arg)

    sid = args.slice
    spec.setdefault("slice_id", sid)
    spec.setdefault("rationale", args.rationale or "")

    state = _replay_state(run_id)
    if sid in state:
        _emit({"error": f"slice {sid} already registered"})
        return 2

    (rd / "slices" / f"{sid}.json").write_text(json.dumps(spec, indent=2))
    _append_event(run_id, {
        "kind": "register",
        "slice_id": sid,
        "spec": spec,
        "rationale": spec.get("rationale", ""),
    })
    _emit({"run_id": run_id, "slice_id": sid, "status": "pending",
           "spec_path": str(rd / "slices" / f"{sid}.json")})
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    state = _replay_state(args.run)
    if not state:
        _emit({"error": "no slices registered"})
        return 2
    runnable = []
    for sid, s in state.items():
        if s["status"] != "pending":
            continue
        deps = s.get("spec", {}).get("depends_on", []) or []
        if all(state.get(d, {}).get("status") == "accepted" for d in deps):
            runnable.append(sid)
    if not runnable:
        # Are there any pending-but-blocked slices?
        pending_blocked = [sid for sid, s in state.items() if s["status"] == "pending"]
        _emit({"runnable": None, "reason": "no runnable slice (deps unmet or all done)",
               "pending_blocked": pending_blocked,
               "summary": {sid: s["status"] for sid, s in state.items()}})
        return 0
    sid = runnable[0]  # FIFO; orchestrator can re-prioritize via re-register
    _emit({"runnable": sid, "spec": state[sid]["spec"]})
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    if args.status not in VALID_STATUSES:
        _emit({"error": f"invalid status: {args.status}", "valid": sorted(VALID_STATUSES)})
        return 2
    state = _replay_state(args.run)
    if args.slice not in state:
        _emit({"error": f"slice {args.slice} not registered"})
        return 2

    ev = {
        "kind": "status_change",
        "slice_id": args.slice,
        "status": args.status,
    }
    if args.commit_sha:
        ev["commit_sha"] = args.commit_sha
    if args.evidence:
        ev["evidence"] = args.evidence
    if args.reason:
        ev["reason"] = args.reason
    if args.rubric_delta is not None:
        ev["rubric_delta"] = args.rubric_delta

    _append_event(args.run, ev)
    _emit({"run_id": args.run, "slice_id": args.slice, "status": args.status,
           "commit_sha": ev.get("commit_sha"), "ts": ev.get("ts", utc_now())})
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    state = _replay_state(args.run)
    rd = _run_dir(args.run)
    run_meta = {}
    if (rd / "run.json").is_file():
        run_meta = json.loads((rd / "run.json").read_text())
    by_status: dict[str, list[str]] = {}
    for sid, s in state.items():
        by_status.setdefault(s["status"], []).append(sid)

    accepted_with_commits = [
        {"slice_id": sid, "commit_sha": s.get("commit_sha"), "rubric_delta": s.get("rubric_delta")}
        for sid, s in state.items() if s["status"] == "accepted"
    ]

    _emit({
        "run_id": args.run,
        "run_meta": run_meta,
        "total_slices": len(state),
        "by_status": {k: len(v) for k, v in by_status.items()},
        "by_status_ids": by_status,
        "accepted_with_commits": accepted_with_commits,
    })
    return 0


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Obsessive-loop slice state CLI.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init")
    pi.add_argument("--repo", required=True)
    pi.add_argument("--branch", default=None)
    pi.add_argument("--baseline-scorecard", default=None)
    pi.add_argument("--run-id", default=None)
    pi.set_defaults(fn=cmd_init)

    pr = sub.add_parser("register")
    pr.add_argument("--run", required=True)
    pr.add_argument("--slice", required=True)
    pr.add_argument("--spec", required=True, help="JSON string, @path, or path to JSON file")
    pr.add_argument("--rationale", default="")
    pr.set_defaults(fn=cmd_register)

    pn = sub.add_parser("next")
    pn.add_argument("--run", required=True)
    pn.set_defaults(fn=cmd_next)

    pm = sub.add_parser("mark")
    pm.add_argument("--run", required=True)
    pm.add_argument("--slice", required=True)
    pm.add_argument("--status", required=True)
    pm.add_argument("--evidence", default=None)
    pm.add_argument("--commit-sha", default=None)
    pm.add_argument("--reason", default=None)
    pm.add_argument("--rubric-delta", type=float, default=None)
    pm.set_defaults(fn=cmd_mark)

    ps = sub.add_parser("summary")
    ps.add_argument("--run", required=True)
    ps.set_defaults(fn=cmd_summary)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
