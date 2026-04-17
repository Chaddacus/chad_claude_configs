#!/usr/bin/env python3
"""CLI wrapper for the governed swarm benchmark corpus."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import ensure_python_3_11
from swarm_evaluation import ARCHETYPES, run_benchmark_archetype, run_benchmark_corpus


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run governed swarm benchmark corpus.")
    parser.add_argument("--artifacts-root", default=None, help="Optional artifacts root override.")
    parser.add_argument("--codex-home", default=None, help="Optional Codex home override.")
    parser.add_argument(
        "--archetype",
        choices=ARCHETYPES,
        help="Run a single archetype benchmark instead of the full corpus.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional path to write the benchmark JSON. Defaults to the standard artifacts location.",
    )
    return parser


def main() -> int:
    ensure_python_3_11()
    args = _parser().parse_args()
    if args.archetype:
        payload = run_benchmark_archetype(
            archetype=args.archetype,
            artifacts_root=args.artifacts_root,
            codex_home=args.codex_home,
        )
    else:
        payload = run_benchmark_corpus(
            artifacts_root=args.artifacts_root,
            codex_home=args.codex_home,
        )
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
