#!/usr/bin/env python3
"""CLI wrapper for a governed swarm canary run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import ensure_python_3_11
from swarm_evaluation import run_live_canary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a governed swarm canary against an isolated workspace.")
    parser.add_argument("--plan-json", required=True, help="Path to the plan JSON to execute.")
    parser.add_argument("--workspace-root", required=True, help="Workspace root to isolate and evaluate.")
    parser.add_argument("--artifacts-root", default=None, help="Optional artifacts root override.")
    parser.add_argument("--track-id", default="swarm-canary", help="Track id for the canary run.")
    parser.add_argument("--route", dest="route_hint", default=None, help="Optional route hint override.")
    parser.add_argument("--execution-shape", default=None, help="Optional execution-shape override.")
    parser.add_argument("--codex-home", default=None, help="Optional Codex home override.")
    parser.add_argument("--safety-mode", default="bounded", help="Canary safety mode label.")
    parser.add_argument("--output", default=None, help="Optional path to write the canary JSON.")
    return parser


def main() -> int:
    ensure_python_3_11()
    args = _parser().parse_args()
    payload = run_live_canary(
        plan_json=args.plan_json,
        workspace_root=args.workspace_root,
        artifacts_root=args.artifacts_root,
        track_id=args.track_id,
        route_hint=args.route_hint,
        execution_shape=args.execution_shape,
        codex_home=args.codex_home,
        safety_mode=args.safety_mode,
    )
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
