#!/usr/bin/env python3
"""Pop the next task from the queue, set up a fresh workspace, and print the
task details so the supervisor (Claude, in-session) can dispatch it.

This is NOT a full autonomous dispatcher — it's the task picker + workspace
scaffolder + observation recorder. The supervisor still plans slices and
writes acceptance scripts inside `/evolve` skill execution.

Usage:
    evolve_run.py pick           # pop next task, stage workspace, print details
    evolve_run.py record --task-id X --workspace P --dispatch-logs a,b,c \\
                          --supervisor-takeovers s1,s2 --started-at T0 --ended-at T1
    evolve_run.py done --task-id X        # mark task complete in queue

Task queue format (~/.claude/evolve/task_queue.jsonl):
    {"id": str, "goal": str, "difficulty": str, "expected_slices": int,
     "status": "pending" | "in_progress" | "complete"}
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
QUEUE = HOME / ".claude" / "evolve" / "task_queue.jsonl"
EVOLVE_DIR = HOME / ".claude" / "evolve"
EXTRACT = HOME / ".claude" / "bin" / "evolve_extract.py"
ANALYZE = HOME / ".claude" / "bin" / "evolve_analyze.py"
APPLY = HOME / ".claude" / "bin" / "evolve_apply.py"
FITNESS = HOME / ".claude" / "bin" / "evolve_fitness.py"


def load_queue() -> list[dict]:
    if not QUEUE.exists():
        return []
    return [json.loads(l) for l in QUEUE.read_text().splitlines() if l.strip()]


def save_queue(tasks: list[dict]) -> None:
    QUEUE.write_text("\n".join(json.dumps(t) for t in tasks) + "\n", encoding="utf-8")


def cmd_pick(args) -> int:
    tasks = load_queue()
    if not tasks:
        print(json.dumps({"error": "empty queue"}))
        return 1
    # Find the first "pending" (or no status) task
    nxt = None
    for t in tasks:
        if t.get("status", "pending") == "pending":
            nxt = t
            break
    if not nxt:
        print(json.dumps({"error": "no pending tasks; all complete or in_progress"}))
        return 1
    nxt["status"] = "in_progress"
    nxt["picked_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    save_queue(tasks)

    ws = Path.home() / "code" / f"evolve-{nxt['id']}"
    ws.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=ws, check=False)
    (ws / ".claude-gates").mkdir(exist_ok=True)
    (ws / "README.md").write_text(f"# {nxt['id']}\n\n{nxt['goal']}\n", encoding="utf-8")

    result = {
        "task": nxt,
        "workspace": str(ws),
        "started_at": nxt["picked_at"],
        "instructions_for_supervisor": (
            f"Task {nxt['id']} staged at {ws}. "
            f"Decompose into ~{nxt.get('expected_slices', 3)} bounded slices. "
            "Write acceptance scripts in .claude-gates/ FIRST, then dispatch via "
            "~/.claude/bin/goose_dispatch.py. Save dispatch result JSONs so "
            "evolve_run.py record can parse them."
        ),
    }
    print(json.dumps(result, indent=2))
    return 0


def cmd_record(args) -> int:
    """Call evolve_extract.py with the supplied args, then analyze + apply."""
    ended = dt.datetime.now(dt.timezone.utc).isoformat()
    rc = subprocess.call([
        sys.executable, str(EXTRACT),
        "--task-id", args.task_id,
        "--workspace", args.workspace,
        "--dispatch-logs", args.dispatch_logs,
        "--supervisor-takeovers", args.supervisor_takeovers,
        "--started-at", args.started_at,
        "--ended-at", ended,
    ])
    if rc != 0:
        return rc
    subprocess.call([sys.executable, str(ANALYZE), "--window", str(args.window)])
    if not args.no_apply:
        subprocess.call([sys.executable, str(APPLY)])
    subprocess.call([sys.executable, str(FITNESS), "--window", str(args.window)])

    # Mark task complete
    tasks = load_queue()
    for t in tasks:
        if t["id"] == args.task_id:
            t["status"] = "complete"
            t["ended_at"] = ended
    save_queue(tasks)
    return 0


def cmd_done(args) -> int:
    tasks = load_queue()
    for t in tasks:
        if t["id"] == args.task_id:
            t["status"] = "complete"
    save_queue(tasks)
    print(f"marked {args.task_id} complete")
    return 0


def cmd_status(args) -> int:
    tasks = load_queue()
    pending = sum(1 for t in tasks if t.get("status", "pending") == "pending")
    in_progress = sum(1 for t in tasks if t.get("status") == "in_progress")
    complete = sum(1 for t in tasks if t.get("status") == "complete")
    print(f"queue: {pending} pending, {in_progress} in-progress, {complete} complete")
    for t in tasks:
        st = t.get("status", "pending")
        print(f"  [{st}] {t['id']} ({t.get('difficulty', '?')}): {t['goal'][:60]}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pick")
    sub.add_parser("status")

    rec = sub.add_parser("record")
    rec.add_argument("--task-id", required=True)
    rec.add_argument("--workspace", required=True)
    rec.add_argument("--dispatch-logs", required=True)
    rec.add_argument("--supervisor-takeovers", default="")
    rec.add_argument("--started-at", required=True)
    rec.add_argument("--window", type=int, default=5)
    rec.add_argument("--no-apply", action="store_true")

    d = sub.add_parser("done")
    d.add_argument("--task-id", required=True)

    args = p.parse_args()
    if args.cmd == "pick":
        return cmd_pick(args)
    if args.cmd == "record":
        return cmd_record(args)
    if args.cmd == "done":
        return cmd_done(args)
    if args.cmd == "status":
        return cmd_status(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
