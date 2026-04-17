#!/usr/bin/env python3
"""Extract structured observations from dispatch result JSONs + supervisor-takeover log.

Called by evolve_run.py at the end of each task run. Writes one observation
record to ~/.claude/evolve/history.jsonl.

Usage:
    evolve_extract.py \\
        --task-id <id> \\
        --workspace <abs-path> \\
        --dispatch-logs <comma-separated glob or file list> \\
        --supervisor-takeovers <int> \\
        --started-at <iso-ts> \\
        --ended-at <iso-ts>

The `dispatch-logs` arg lists the JSON result files (one per slice dispatch).
Each is parsed for outcome, attempts, cheat flags, etc.

History format (JSONL, one record per task run):
{
    "task_id": str,
    "started_at": iso-ts,
    "ended_at": iso-ts,
    "wall_time_seconds": int,
    "slices": [ {slice_id, outcome, attempts, cheat_flags, files_changed_count}, ... ],
    "metrics": {first_try_pass_rate, supervisor_takeovers, cheat_count,
                infra_down_count, sandbox_violation_count, avg_attempts_per_slice},
    "failure_categories": { "model-capability": int, "spec-ambiguity": int,
                            "gate-bug": int, "infra": int, "test-cheat": int },
    "evolve_version": int,  # .goosehints line count + skills count + presets count
}
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

HOME = Path(os.path.expanduser("~"))
HISTORY = HOME / ".claude" / "evolve" / "history.jsonl"
GOOSEHINTS = HOME / ".goosehints"
SKILLS_DIR = HOME / ".config" / "goose" / "skills"
PRESETS_DIR = HOME / ".claude" / "bin" / "presets"


def _classify_slice(slice_record: dict, supervisor_took_over: bool) -> str:
    """Classify a slice's failure category. Returns '' for passes."""
    outcome = slice_record.get("outcome")
    if outcome == "pass":
        return ""
    if outcome == "infra_down":
        return "infra"
    if outcome == "gate_cheat_suspected":
        return "test-cheat"
    if outcome == "escalate":
        return "sandbox-violation"
    if supervisor_took_over:
        return "model-capability"
    # outcome == "fail": could be spec-ambiguity, gate-bug, or model-capability.
    # Heuristic: if goose's attempts maxed out and last error mentions a test
    # assertion, probably spec-ambiguity or gate-bug. Without code-aware
    # inspection we lump these; analyze.py can refine.
    return "model-capability"


def evolve_version() -> int:
    """Hash-like int representing the current state of prompts/skills/presets.
    Bumps whenever a self-apply edits something. Used to detect whether a run
    ran against the pre- or post-lesson version."""
    n = 0
    if GOOSEHINTS.exists():
        n += len(GOOSEHINTS.read_text(encoding="utf-8").splitlines())
    if SKILLS_DIR.exists():
        for f in SKILLS_DIR.glob("*.md"):
            n += len(f.read_text(encoding="utf-8").splitlines())
    if PRESETS_DIR.exists():
        for f in PRESETS_DIR.glob("*.sh"):
            n += 1
    return n


def parse_dispatch_log(path: Path) -> dict | None:
    """Parse a single dispatcher output file. The dispatcher's stdout is a JSON
    object; if the caller captured it verbatim, this just loads it."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    # Strip common prefixes (shell output before the JSON)
    brace = text.find("{")
    if brace < 0:
        return None
    try:
        return json.loads(text[brace:])
    except json.JSONDecodeError:
        return None


def extract(
    task_id: str,
    workspace: str,
    dispatch_result_files: list[Path],
    supervisor_takeovers: list[str],
    started_at: str,
    ended_at: str,
) -> dict:
    slices = []
    for f in dispatch_result_files:
        rec = parse_dispatch_log(f)
        if rec is None:
            continue
        sid = rec.get("slice_id", f.stem)
        took_over = sid in supervisor_takeovers
        slices.append({
            "slice_id": sid,
            "outcome": rec.get("outcome"),
            "attempts": rec.get("attempts"),
            "cheat_flags": rec.get("gate_cheat_flags", []),
            "files_changed_count": len(rec.get("files_changed", [])),
            "supervisor_took_over": took_over,
            "failure_category": _classify_slice(rec, took_over),
        })

    n = len(slices) or 1
    metrics = {
        "first_try_pass_rate":
            sum(1 for s in slices if s["outcome"] == "pass" and s["attempts"] == 1) / n,
        "supervisor_takeovers":
            sum(1 for s in slices if s["supervisor_took_over"]),
        "cheat_count":
            sum(1 for s in slices if s["cheat_flags"]),
        "infra_down_count":
            sum(1 for s in slices if s["outcome"] == "infra_down"),
        "sandbox_violation_count":
            sum(1 for s in slices if s["outcome"] == "escalate"),
        "avg_attempts_per_slice":
            sum((s["attempts"] or 0) for s in slices) / n,
    }

    categories: dict[str, int] = {}
    for s in slices:
        fc = s["failure_category"]
        if fc:
            categories[fc] = categories.get(fc, 0) + 1

    try:
        t0 = dt.datetime.fromisoformat(started_at)
        t1 = dt.datetime.fromisoformat(ended_at)
        wall = int((t1 - t0).total_seconds())
    except Exception:
        wall = -1

    return {
        "task_id": task_id,
        "workspace": workspace,
        "started_at": started_at,
        "ended_at": ended_at,
        "wall_time_seconds": wall,
        "slices": slices,
        "metrics": metrics,
        "failure_categories": categories,
        "evolve_version": evolve_version(),
    }


def append_history(record: dict) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--task-id", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--dispatch-logs", required=True,
                   help="Comma-separated list of dispatcher output files")
    p.add_argument("--supervisor-takeovers", default="",
                   help="Comma-separated slice_ids that the supervisor took over")
    p.add_argument("--started-at", required=True)
    p.add_argument("--ended-at", required=True)
    p.add_argument("--print", action="store_true", help="Print observation JSON")
    args = p.parse_args()

    files = [Path(x.strip()) for x in args.dispatch_logs.split(",") if x.strip()]
    takeovers = [x.strip() for x in args.supervisor_takeovers.split(",") if x.strip()]

    obs = extract(
        task_id=args.task_id,
        workspace=args.workspace,
        dispatch_result_files=files,
        supervisor_takeovers=takeovers,
        started_at=args.started_at,
        ended_at=args.ended_at,
    )
    append_history(obs)
    if args.print:
        print(json.dumps(obs, indent=2))
    else:
        print(f"recorded {args.task_id}: {len(obs['slices'])} slices, "
              f"first_try={obs['metrics']['first_try_pass_rate']:.0%}, "
              f"takeovers={obs['metrics']['supervisor_takeovers']}, "
              f"cheats={obs['metrics']['cheat_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
