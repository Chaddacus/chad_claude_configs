#!/usr/bin/env python3
"""Final fail-closed gate. Recomputes deterministic checks and ignores forged approve status."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import (
    ensure_python_3_11,
    load_json_file,
    resolve_artifacts_root,
    sanitize_token,
    validate_impl_contract,
    validate_plan_contract,
    write_json_file,
)


def finalize_decision(
    *,
    plan_payload: dict,
    impl_payload: dict,
    review_payload: dict,
    artifacts_root: Path,
    track_id: str | None,
) -> dict:
    track_token = sanitize_token(track_id or "") if track_id else None
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
    if not track_token:
        blocked.append("finalize:track_id_missing")
    if impl_contract.objective_closure_state and not impl_contract.accepted_type:
        blocked.append("finalize:objective_closure_not_acceptable")

    review_status = str(review_payload.get("status", "")).strip().lower()

    ok = not missing and not blocked and plan_contract.smoke_100_pass and impl_contract.smoke_100_pass
    status = "approve" if ok else ("blocked" if blocked else "revise")

    if ok:
        reason = "approved"
        next_step = "Gate passed. Finalization allowed."
    elif blocked:
        reason = "blocked_by_deterministic_checks"
        next_step = "Fix blocked_fields, regenerate proof artifacts, and rerun validation."
    else:
        reason = "missing_required_evidence"
        next_step = "Address missing_fields and rerun validate_plan.py + validate_impl.py."

    return {
        "type": "planning_gate_finalize",
        "ok": ok,
        "status": status,
        "reason": reason,
        "track_id": track_id or "",
        "track_token": track_token or "",
        "missing_fields": sorted(set(missing)),
        "blocked_fields": sorted(set(blocked)),
        "review_status_seen": review_status,
        "review_status_trusted": False,
        "objective_closure_state": impl_contract.objective_closure_state,
        "accepted_type": impl_contract.accepted_type,
        "migration_fallback_used": impl_contract.migration_fallback_used,
        "budget_within_plan": impl_contract.budget_within_plan,
        "budget_exception_used": impl_contract.budget_exception_used,
        "next_step": next_step,
    }


def main() -> int:
    ensure_python_3_11()

    parser = argparse.ArgumentParser(description="Run final deterministic planning gate.")
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--impl-json", required=True)
    parser.add_argument("--review-json", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--track-id", default="")
    parser.add_argument("--artifacts-root", default=None)
    args = parser.parse_args()

    artifacts_root = resolve_artifacts_root(args.artifacts_root)

    try:
        plan_payload = load_json_file(args.plan_json)
        impl_payload = load_json_file(args.impl_json)
        review_payload = load_json_file(args.review_json)
    except Exception as exc:
        result = {
            "type": "planning_gate_finalize",
            "ok": False,
            "status": "blocked",
            "reason": "invalid_input_json",
            "missing_fields": [],
            "blocked_fields": [str(exc)],
            "review_status_seen": "unknown",
            "review_status_trusted": False,
            "next_step": "Provide valid plan/impl/review JSON payloads.",
        }
        write_json_file(args.out, result)
        print(json.dumps(result, sort_keys=True))
        return 1

    result = finalize_decision(
        plan_payload=plan_payload,
        impl_payload=impl_payload,
        review_payload=review_payload,
        artifacts_root=artifacts_root,
        track_id=str(args.track_id or review_payload.get("meta", {}).get("track_id", "")).strip() or None,
    )
    write_json_file(args.out, result)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
