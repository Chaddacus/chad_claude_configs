#!/usr/bin/env python3
"""Emit pre-plan session scaffolding for autonomous-completion governed objectives."""

from __future__ import annotations

import argparse
import json
from typing import Any

from common import (
    build_objective_intent_payload,
    ensure_python_3_11,
    load_json_file,
    resolve_artifacts_root,
    sanitize_token,
    stable_review,
    write_json_file,
    write_preplan_session_artifacts,
)


def initialize_session_payload(
    *,
    objective_intent: dict[str, Any] | None = None,
    plan_payload: dict[str, Any] | None = None,
    track_id: str,
    artifacts_root,
    cwd: str | None = None,
) -> dict:
    if objective_intent is None:
        if plan_payload is None:
            raise ValueError("objective_intent_or_plan_payload_required")
        objective_intent = build_objective_intent_payload(track_id=track_id, plan=plan_payload)
    track_token = sanitize_token(track_id)
    written = write_preplan_session_artifacts(
        objective_intent,
        track_id=track_id,
        artifacts_root=artifacts_root,
        cwd=cwd,
    )
    return stable_review(
        gate="session-initializer",
        status="approve",
        content="Pre-plan session scaffolding artifacts emitted.",
        next_step="Run compile_plan.py, verify_plan.py, and validate_plan.py.",
        meta={
            "track_id": track_id,
            "track_token": track_token,
            "artifacts": written,
        },
    )


def main() -> int:
    ensure_python_3_11()

    parser = argparse.ArgumentParser(description="Emit pre-plan session scaffolding.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--intent-json")
    source.add_argument("--plan-json")
    parser.add_argument("--review-json-out", required=True)
    parser.add_argument("--track-id", required=True)
    parser.add_argument("--artifacts-root", default=None)
    parser.add_argument("--cwd", default=None)
    args = parser.parse_args()

    artifacts_root = resolve_artifacts_root(args.artifacts_root)

    try:
        if args.intent_json:
            objective_intent = load_json_file(args.intent_json)
            plan_payload = None
        else:
            plan_payload = load_json_file(args.plan_json)
            objective_intent = None
        review = initialize_session_payload(
            objective_intent=objective_intent,
            plan_payload=plan_payload,
            track_id=args.track_id,
            artifacts_root=artifacts_root,
            cwd=args.cwd,
        )
    except Exception as exc:
        review = stable_review(
            gate="session-initializer",
            status="blocked",
            content="Session harness initialization failed.",
            blocked_fields=[str(exc)],
            next_step="Fix the intent artifact or harness configuration and rerun initialize_session.py.",
        )
        write_json_file(args.review_json_out, review)
        print(json.dumps(review, sort_keys=True))
        return 1

    write_json_file(args.review_json_out, review)
    print(json.dumps(review, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
