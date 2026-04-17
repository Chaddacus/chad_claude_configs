#!/usr/bin/env python3
"""Emit frontloaded planning compiler artifacts for a plan payload."""

from __future__ import annotations

import argparse
import json

from common import (
    ensure_python_3_11,
    load_json_file,
    objective_intent_matches_plan,
    PLAN_INTENT_SCHEMA_VERSION,
    plan_artifact_paths,
    resolve_artifacts_root,
    sanitize_token,
    stable_review,
    write_json_file,
    write_plan_frontloaded_artifacts,
)


def _intent_matches_plan(plan_payload: dict, objective_intent: dict) -> bool:
    return objective_intent_matches_plan(
        plan=plan_payload,
        objective_intent=objective_intent,
        track_id=str(objective_intent.get("track_id") or ""),
    )


def compile_plan_payload(*, plan_payload: dict, track_id: str, artifacts_root, objective_intent: dict | None = None) -> dict:
    track_token = sanitize_token(track_id)
    if objective_intent is not None and not _intent_matches_plan(plan_payload, objective_intent):
        raise ValueError("objective_intent_mismatch")
    include = ("objective_intent", "intent", "compiler", "coverage", "gaps", "readiness")
    if objective_intent is not None:
        include = ("compiler", "coverage", "gaps", "readiness")
    written = write_plan_frontloaded_artifacts(plan_payload, track_id=track_id, artifacts_root=artifacts_root, include=include)
    if objective_intent is not None:
        paths = plan_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
        write_json_file(paths["objective_intent"], objective_intent)
        write_json_file(
            paths["intent"],
            {
                "schema_version": PLAN_INTENT_SCHEMA_VERSION,
                "track_id": track_id,
                "objective_ref": track_token,
                "objective_intent_ref": "objective.intent.json",
                "intent_contract": objective_intent.get("intent_contract"),
                "clarification_governor": objective_intent.get("clarification_governor"),
                "autonomy_level": str(plan_payload.get("autonomy_level", "")).strip(),
            },
        )
        written["objective_intent"] = str(paths["objective_intent"])
        written["intent"] = str(paths["intent"])
    return stable_review(
        gate="plan-compiler",
        status="approve",
        content="Frontloaded plan compiler artifacts emitted.",
        next_step="Run verify_plan.py or validate_plan.py.",
        meta={
            "track_id": track_id,
            "track_token": track_token,
            "artifacts": written,
        },
    )


def main() -> int:
    ensure_python_3_11()

    parser = argparse.ArgumentParser(description="Emit frontloaded planning compiler artifacts.")
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--intent-json", default=None)
    parser.add_argument("--review-json-out", required=True)
    parser.add_argument("--track-id", required=True)
    parser.add_argument("--artifacts-root", default=None)
    args = parser.parse_args()

    artifacts_root = resolve_artifacts_root(args.artifacts_root)
    track_token = sanitize_token(args.track_id)

    try:
        plan_payload = load_json_file(args.plan_json)
        objective_intent = None
        if args.intent_json:
            objective_intent = load_json_file(args.intent_json)
        else:
            implicit_intent = plan_artifact_paths(artifacts_root=artifacts_root, track_id=args.track_id)["objective_intent"]
            if implicit_intent.exists():
                objective_intent = load_json_file(implicit_intent)
        review = compile_plan_payload(
            plan_payload=plan_payload,
            track_id=args.track_id,
            artifacts_root=artifacts_root,
            objective_intent=objective_intent,
        )
    except Exception as exc:
        review = stable_review(
            gate="plan-compiler",
            status="blocked",
            content="Frontloaded plan compiler failed.",
            blocked_fields=[str(exc)],
            next_step="Fix the plan payload and rerun compile_plan.py.",
        )
        write_json_file(args.review_json_out, review)
        print(json.dumps(review, sort_keys=True))
        return 1

    write_json_file(args.review_json_out, review)
    print(json.dumps(review, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
