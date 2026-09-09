#!/usr/bin/env python3
"""Emit and verify frontloaded planning sufficiency artifacts for a plan payload."""

from __future__ import annotations

import argparse
import json

from common import (
    ensure_python_3_11,
    load_json_file,
    resolve_artifacts_root,
    sanitize_token,
    stable_review,
    validate_plan_contract,
    write_json_file,
    write_plan_frontloaded_artifacts,
)


def verify_plan_payload(*, plan_payload: dict, track_id: str, artifacts_root) -> dict:
    track_token = sanitize_token(track_id)
    written = write_plan_frontloaded_artifacts(
        plan_payload,
        track_id=track_id,
        artifacts_root=artifacts_root,
        include=("sufficiency",),
    )
    contract = validate_plan_contract(
        plan_payload,
        artifacts_root=artifacts_root,
        track_id=track_id,
    )
    if contract.blocked:
        status = "blocked"
        content = "Frontloaded plan verifier blocked the plan."
        next_step = "Fix blocked_fields and rerun compile_plan.py + verify_plan.py."
    elif contract.missing:
        status = "revise"
        content = "Frontloaded plan verifier requires plan hardening revisions."
        next_step = "Address missing_fields and rerun compile_plan.py + verify_plan.py."
    else:
        status = "approve"
        content = "Frontloaded plan verifier approved the execution-ready plan."
        next_step = "Run validate_plan.py."

    return stable_review(
        gate="plan-verifier",
        status=status,
        content=content,
        missing_fields=list(contract.missing),
        blocked_fields=list(contract.blocked),
        next_step=next_step,
        meta={
            "track_id": track_id,
            "track_token": track_token,
            "plan_status": contract.plan_status,
            "runtime_compatible": contract.runtime_compatible,
            "artifacts": written,
        },
    )


def main() -> int:
    ensure_python_3_11()

    parser = argparse.ArgumentParser(description="Verify frontloaded planning sufficiency.")
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--review-json-out", required=True)
    parser.add_argument("--track-id", required=True)
    parser.add_argument("--artifacts-root", default=None)
    args = parser.parse_args()

    artifacts_root = resolve_artifacts_root(args.artifacts_root)
    track_token = sanitize_token(args.track_id)

    try:
        plan_payload = load_json_file(args.plan_json)
        review = verify_plan_payload(
            plan_payload=plan_payload,
            track_id=args.track_id,
            artifacts_root=artifacts_root,
        )
    except Exception as exc:
        review = stable_review(
            gate="plan-verifier",
            status="blocked",
            content="Frontloaded plan verifier failed.",
            blocked_fields=[str(exc)],
            next_step="Fix the plan payload or artifacts and rerun verify_plan.py.",
        )
        write_json_file(args.review_json_out, review)
        print(json.dumps(review, sort_keys=True))
        return 1

    write_json_file(args.review_json_out, review)
    print(json.dumps(review, sort_keys=True))
    return 0 if review["status"] == "approve" else 1


if __name__ == "__main__":
    raise SystemExit(main())
