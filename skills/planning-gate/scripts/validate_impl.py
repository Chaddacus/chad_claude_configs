#!/usr/bin/env python3
"""Validate implementation evidence against deterministic contracts."""

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
    validate_impl_contract,
    validate_plan_contract,
    write_json_file,
)
from score_progress import update_progress


def validate_impl_payload(
    *,
    plan_payload: dict,
    impl_payload: dict,
    track_id: str,
    artifacts_root: Path,
    stall_limit: int,
    ttl_hours: int,
) -> dict:
    track_token = sanitize_token(track_id)
    plan_contract = validate_plan_contract(
        plan_payload,
        artifacts_root=artifacts_root,
        track_id=track_token,
    )
    impl_contract = validate_impl_contract(
        impl_payload,
        artifacts_root=artifacts_root,
        track_id=track_token,
        plan=plan_payload,
    )

    missing = [f"plan:{item}" for item in plan_contract.missing] + list(impl_contract.missing)
    blocked = [f"plan:{item}" for item in plan_contract.blocked] + list(impl_contract.blocked)
    risks: list[str] = []

    progress = update_progress(
        artifacts_root=artifacts_root,
        track_id=track_token,
        kind="impl",
        score=impl_contract.evidence_quality_score,
        passed_100=impl_contract.smoke_100_pass,
        stall_limit=stall_limit,
        ttl_hours=ttl_hours,
    )

    if progress["regressed"]:
        missing.append("progression:implementation_evidence_regressed")
    if progress["stalled"]:
        missing.append("progression:implementation_evidence_stalled")

    if not impl_contract.smoke_100_pass:
        risks.append("Implementation 100% smoke stage is not pass.")

    if blocked:
        status = "blocked"
        content = "Implementation evidence blocked by schema/proof policy violations."
        next_step = "Fix blocked_fields and regenerate evidence with run_cmd_capture.py."
    elif missing:
        status = "revise"
        content = "Implementation evidence requires revision to satisfy deterministic gates."
        next_step = "Address missing_fields and rerun validate_impl.py."
    else:
        status = "approve"
        content = "Implementation evidence passed deterministic checks."
        next_step = "Run finalize_gate.py for final fail-closed approval."

    review = stable_review(
        gate="implementation",
        status=status,
        content=content,
        missing_fields=missing,
        blocked_fields=blocked,
        risks=risks,
        next_step=next_step,
        meta={
            "track_id": track_id,
            "track_token": track_token,
            "impl_quality": {
                "score": impl_contract.evidence_quality_score,
                "loop": progress["loop"],
                "best_score": progress["best_score"],
                "stagnant_loops": progress["stagnant_loops"],
                "stall_limit": progress["stall_limit"],
            },
            "impl_smoke_100_pass": impl_contract.smoke_100_pass,
            "budget_within_plan": impl_contract.budget_within_plan,
            "budget_exception_used": impl_contract.budget_exception_used,
        },
    )
    return review


def main() -> int:
    ensure_python_3_11()

    parser = argparse.ArgumentParser(description="Validate planning-gate implementation evidence JSON.")
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--impl-json", required=True)
    parser.add_argument("--review-json-out", required=True)
    parser.add_argument("--track-id", required=True)
    parser.add_argument("--artifacts-root", default=None)
    parser.add_argument("--stall-limit", type=int, default=2)
    parser.add_argument("--ttl-hours", type=int, default=24)
    args = parser.parse_args()

    artifacts_root = resolve_artifacts_root(args.artifacts_root)

    try:
        plan_payload = load_json_file(args.plan_json)
        impl_payload = load_json_file(args.impl_json)
    except Exception as exc:
        review = stable_review(
            gate="implementation",
            status="blocked",
            content="Plan or implementation JSON could not be parsed.",
            blocked_fields=[str(exc)],
            next_step="Provide valid JSON payloads and rerun validate_impl.py.",
        )
        write_json_file(args.review_json_out, review)
        print(json.dumps(review, sort_keys=True))
        return 1

    review = validate_impl_payload(
        plan_payload=plan_payload,
        impl_payload=impl_payload,
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
