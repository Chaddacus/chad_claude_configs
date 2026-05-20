#!/usr/bin/env python3
"""Claude Auto Runtime — CLI Entrypoint.

Behavioral parity with Codex auto_runtime.py, adapted for Claude Code.

Usage:
    auto_runtime.py init --task "..." --cwd /path [--route R2] [--mode default]
    auto_runtime.py preflight --task "..." --cwd /path
    auto_runtime.py cycle --track-id abc123 [--max-cycles 1] [--dry-run]
    auto_runtime.py dispatch --track-id abc123
    auto_runtime.py refresh --track-id abc123
    auto_runtime.py replay --track-id abc123
    auto_runtime.py readiness
    auto_runtime.py scan
    auto_runtime.py update-node --track-id abc123 --node-id slice-1 --state accepted
    auto_runtime.py manager-run-task --task "..." --cwd /path [--route R2] [--max-actions 12] [--max-runtime 900]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add bin dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))

import auto_runtime_common as rt


def cmd_init(args: argparse.Namespace) -> None:
    result = rt.initialize_track(
        task=args.task,
        cwd=args.cwd or ".",
        mode=args.mode or "default",
        route_override=args.route,
        include_memory=not args.no_memory,
        invoker=args.invoker,
    )
    json.dump(result, sys.stdout, indent=2, default=str)
    print()


def cmd_preflight(args: argparse.Namespace) -> None:
    route = rt.classify_route(args.task, route_override=args.route)
    result = rt.build_preflight_report(args.task, args.cwd or ".", route)
    json.dump(result, sys.stdout, indent=2, default=str)
    print()


def cmd_cycle(args: argparse.Namespace) -> None:
    with rt.TrackLock(args.track_id):
        result = rt.cycle_track(
            args.track_id,
            max_cycles=args.max_cycles,
            dry_run=args.dry_run,
            shadow=getattr(args, "shadow", False),
        )
    json.dump(result, sys.stdout, indent=2, default=str)
    print()


def cmd_dispatch(args: argparse.Namespace) -> None:
    with rt.TrackLock(args.track_id):
        result = rt.dispatch_track(args.track_id)
    json.dump(result, sys.stdout, indent=2, default=str)
    print()


def cmd_refresh(args: argparse.Namespace) -> None:
    with rt.TrackLock(args.track_id):
        result = rt.refresh_frontier(args.track_id)
    json.dump(result, sys.stdout, indent=2, default=str)
    print()


def cmd_replay(args: argparse.Namespace) -> None:
    result = rt.replay_events(args.track_id)
    json.dump(result, sys.stdout, indent=2, default=str)
    print()


def cmd_readiness(args: argparse.Namespace) -> None:
    result = rt.check_readiness()
    json.dump(result, sys.stdout, indent=2, default=str)
    print()
    sys.exit(0 if result["ready"] else 1)


def cmd_scan(args: argparse.Namespace) -> None:
    result = rt.scan_tracks()
    json.dump(result, sys.stdout, indent=2, default=str)
    print()


def cmd_update_node(args: argparse.Namespace) -> None:
    evidence = args.evidence.split(",") if args.evidence else None
    with rt.TrackLock(args.track_id):
        result = rt.update_node_state(
            args.track_id,
            args.node_id,
            args.state,
            evidence_refs=evidence,
            acceptance_source=args.acceptance_source,
        )
    json.dump(result, sys.stdout, indent=2, default=str)
    print()


def cmd_wake(args: argparse.Namespace) -> None:
    with rt.TrackLock(args.track_id):
        result = rt.wake_ceremony(
            args.track_id,
            last_n_events=args.last_n_events,
            write_progress=args.progress,
        )
    json.dump(result, sys.stdout, indent=2, default=str)
    print()


def cmd_evaluate_verdict(args: argparse.Namespace) -> None:
    verdict = json.loads(args.verdict)
    with rt.TrackLock(args.track_id):
        result = rt.record_evaluator_verdict(args.track_id, args.slice_id, verdict)
    json.dump(result, sys.stdout, indent=2, default=str)
    print()


def cmd_effort_for_slice(args: argparse.Namespace) -> None:
    """Print the effective effort for a slice, accounting for retry escalation.

    Dispatchers (/govern prose, /drive prose, or any caller about to invoke
    an agent for a specific slice) should call this to get the correct effort
    setting. Prints just the effort string on stdout for easy shell use:

        EFFORT=$(auto_runtime.py effort-for-slice --track-id X --slice-id Y \\
                    --base-effort medium)
    """
    effort = rt.get_effective_effort(args.track_id, args.slice_id, args.base_effort)
    print(effort)


def cmd_manager_run_task(args: argparse.Namespace) -> None:
    result = rt.manager_run_task(
        task=args.task,
        cwd=args.cwd or ".",
        route_override=args.route,
        max_actions=args.max_actions,
        max_runtime_seconds=args.max_runtime,
        dry_run=args.dry_run,
        include_memory=not args.no_memory,
    )
    json.dump(result, sys.stdout, indent=2, default=str)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Claude Auto Runtime — bounded autonomous task orchestrator"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p = sub.add_parser("init", help="Initialize a new objective track")
    p.add_argument("--task", required=True)
    p.add_argument("--cwd")
    p.add_argument("--route", choices=["R1", "R2", "R3", "R4", "R5"])
    p.add_argument("--mode", default="default")
    p.add_argument("--no-memory", action="store_true")
    p.add_argument("--invoker", help="Slash-command name that triggered this track (e.g., 'drive', 'build'); recorded for orchestration audits")
    p.set_defaults(func=cmd_init)

    # preflight
    p = sub.add_parser("preflight", help="Run preflight analysis")
    p.add_argument("--task", required=True)
    p.add_argument("--cwd")
    p.add_argument("--route", choices=["R1", "R2", "R3", "R4", "R5"])
    p.set_defaults(func=cmd_preflight)

    # cycle
    p = sub.add_parser("cycle", help="Run cycle(s) on a track")
    p.add_argument("--track-id", required=True)
    p.add_argument("--max-cycles", type=int, default=1)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--shadow", action="store_true",
        help="Record would-be decisions without executing them (Layer-3 validation)",
    )
    p.set_defaults(func=cmd_cycle)

    # dispatch
    p = sub.add_parser("dispatch", help="Dispatch next ready slice")
    p.add_argument("--track-id", required=True)
    p.set_defaults(func=cmd_dispatch)

    # refresh
    p = sub.add_parser("refresh", help="Refresh frontier and rebuild views")
    p.add_argument("--track-id", required=True)
    p.set_defaults(func=cmd_refresh)

    # replay
    p = sub.add_parser("replay", help="Replay events and show summary")
    p.add_argument("--track-id", required=True)
    p.set_defaults(func=cmd_replay)

    # readiness
    p = sub.add_parser("readiness", help="Check runtime infrastructure health")
    p.set_defaults(func=cmd_readiness)

    # scan
    p = sub.add_parser("scan", help="Scan all autonomy tracks")
    p.set_defaults(func=cmd_scan)

    # wake
    p = sub.add_parser("wake", help="Reconstruct context after session boundary")
    p.add_argument("--track-id", required=True)
    p.add_argument("--last-n-events", type=int, default=20)
    p.add_argument("--progress", action="store_true", help="Write PROGRESS.md")
    p.set_defaults(func=cmd_wake)

    # evaluate-verdict
    p = sub.add_parser("evaluate-verdict", help="Record evaluator verdict for a slice")
    p.add_argument("--track-id", required=True)
    p.add_argument("--slice-id", required=True)
    p.add_argument("--verdict", required=True, help="JSON verdict payload")
    p.set_defaults(func=cmd_evaluate_verdict)

    # update-node
    p = sub.add_parser("update-node", help="Update a graph node state")
    p.add_argument("--track-id", required=True)
    p.add_argument("--node-id", required=True)
    p.add_argument("--state", required=True, choices=sorted(rt.GRAPH_NODE_STATES))
    p.add_argument("--evidence")
    p.add_argument("--acceptance-source")
    p.set_defaults(func=cmd_update_node)

    # effort-for-slice
    p = sub.add_parser("effort-for-slice", help="Resolve effort for a slice (accounts for retry escalation)")
    p.add_argument("--track-id", required=True)
    p.add_argument("--slice-id", required=True)
    p.add_argument("--base-effort", required=True,
                   help="Base effort from the route manifest (medium|high|xhigh etc)")
    p.set_defaults(func=cmd_effort_for_slice)

    # manager-run-task
    p = sub.add_parser("manager-run-task", help="End-to-end bounded task execution")
    p.add_argument("--task", required=True)
    p.add_argument("--cwd")
    p.add_argument("--route", choices=["R1", "R2", "R3", "R4", "R5"])
    p.add_argument("--max-actions", type=int, default=12)
    p.add_argument("--max-runtime", type=float, default=900.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-memory", action="store_true")
    p.set_defaults(func=cmd_manager_run_task)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
