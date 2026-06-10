#!/usr/bin/env python3
"""Validate planning payload with fail-closed deterministic gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import (
    ensure_python_3_11,
    load_json_file,
    resolve_artifacts_root,
    sanitize_token,
    stable_review,
    validate_plan_contract,
    write_plan_frontloaded_artifacts,
    write_json_file,
)
from score_progress import update_progress


def validate_plan_payload(
    *,
    plan_payload: dict,
    track_id: str,
    artifacts_root: Path,
    stall_limit: int,
    ttl_hours: int,
) -> dict:
    track_token = sanitize_token(track_id)
    write_plan_frontloaded_artifacts(
        plan_payload,
        track_id=track_id,
        artifacts_root=artifacts_root,
    )
    contract = validate_plan_contract(
        plan_payload,
        artifacts_root=artifacts_root,
        track_id=track_id,
    )
    progress = update_progress(
        artifacts_root=artifacts_root,
        track_id=track_id,
        kind="plan",
        score=contract.smoke_quality_score,
        passed_100=contract.smoke_100_pass,
        stall_limit=stall_limit,
        ttl_hours=ttl_hours,
    )

    missing = list(contract.missing)
    blocked = list(contract.blocked)
    risks: list[str] = []

    if progress["regressed"]:
        missing.append("progression:smoke_quality_regressed")
    if progress["stalled"]:
        missing.append("progression:smoke_quality_stalled")

    if blocked:
        status = "blocked"
        content = "Plan blocked by deterministic frontloaded-planning, pre-delivery gap-review, or runtime-compatibility violations."
        next_step = "Fix blocked_fields, rerun compile/verify, and resubmit plan JSON."
    elif missing:
        status = "revise"
        content = "Plan requires revision to satisfy frontloaded sufficiency, pre-delivery gap-review, and deterministic contract checks."
        next_step = "Address missing_fields, rerun compile/verify, and resubmit plan JSON."
    else:
        status = "approve"
        content = "Plan contract, pre-delivery gap review, frontloaded sufficiency, and runtime-compatibility checks passed."
        next_step = "Proceed to implementation evidence capture and validation."

    if not contract.smoke_100_pass:
        risks.append("100% smoke gate is not pass.")

    review = stable_review(
        gate="plan",
        status=status,
        content=content,
        missing_fields=missing,
        blocked_fields=blocked,
        risks=risks,
        next_step=next_step,
        meta={
            "track_id": track_id,
            "track_token": track_token,
            "smoke_quality": {
                "score": contract.smoke_quality_score,
                "loop": progress["loop"],
                "best_score": progress["best_score"],
                "stagnant_loops": progress["stagnant_loops"],
                "stall_limit": progress["stall_limit"],
            },
            "smoke_100_pass": contract.smoke_100_pass,
            "plan_status": contract.plan_status,
            "runtime_compatible": contract.runtime_compatible,
            "pre_delivery_gap_review_performed": plan_payload.get("pre_delivery_gap_review", {}).get("performed") is True,
            "frontloaded_artifacts": {
                "compiler": f"{artifacts_root / track_token / 'plan.compiler.json'}",
                "gaps": f"{artifacts_root / track_token / 'plan.gaps.json'}",
                "coverage": f"{artifacts_root / track_token / 'plan.coverage.json'}",
                "sufficiency": f"{artifacts_root / track_token / 'plan.sufficiency.json'}",
            },
        },
    )
    return review


def main() -> int:
    ensure_python_3_11()

    parser = argparse.ArgumentParser(description="Validate planning-gate plan JSON.")
    parser.add_argument("--plan-json", required=True, help="Path to plan JSON file")
    parser.add_argument("--review-json-out", required=True, help="Output review JSON path")
    parser.add_argument("--track-id", required=True)
    parser.add_argument("--artifacts-root", default=None)
    parser.add_argument("--stall-limit", type=int, default=2)
    parser.add_argument("--ttl-hours", type=int, default=24)
    args = parser.parse_args()

    artifacts_root = resolve_artifacts_root(args.artifacts_root)

    try:
        plan_payload = load_json_file(args.plan_json)
    except Exception as exc:
        review = stable_review(
            gate="plan",
            status="blocked",
            content="Plan JSON could not be parsed.",
            blocked_fields=[str(exc)],
            next_step="Provide a valid JSON object and rerun validate_plan.py.",
        )
        write_json_file(args.review_json_out, review)
        print(json.dumps(review, sort_keys=True))
        return 1

    review = validate_plan_payload(
        plan_payload=plan_payload,
        track_id=args.track_id,
        artifacts_root=artifacts_root,
        stall_limit=args.stall_limit,
        ttl_hours=args.ttl_hours,
    )
    write_json_file(args.review_json_out, review)
    print(json.dumps(review, sort_keys=True))
    return 0 if review["status"] == "approve" else 1


if __name__ == "__main__":
    raise SystemExit(main())
