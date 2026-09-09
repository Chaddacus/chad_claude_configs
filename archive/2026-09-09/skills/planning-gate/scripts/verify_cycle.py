#!/usr/bin/env python3
"""Verify a single runtime cycle result against packet and scope policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import ensure_python_3_11, load_json_file, stable_review, write_json_file

VERIFIER_OUTPUTS = {
    "accepted": "accepted",
    "rejected_rework": "rejected_rework",
    "blocked_boundary": "blocked_boundary",
    "blocked_migration_defect": "blocked_migration_defect",
    "escalate": "escalate",
}
RUNTIME_STATE_BY_OUTPUT = {
    "accepted": "accepted",
    "rejected_rework": "rejected_rework",
    "blocked_boundary": "escalated",
    "blocked_migration_defect": "escalated",
    "escalate": "escalated",
}


def _non_empty_string(value: Any, minimum: int = 1) -> bool:
    return isinstance(value, str) and len(value.strip()) >= minimum


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []

def _matches_expected_artifacts(evidence_refs: list[str], expected_artifacts: list[str]) -> bool:
    if not expected_artifacts:
        return bool(evidence_refs)
    normalized_refs = [str(ref).strip() for ref in evidence_refs if str(ref).strip()]
    normalized_expected = [str(item).strip() for item in expected_artifacts if str(item).strip()]
    return all(
        any(ref.endswith(expected) or expected in ref for ref in normalized_refs)
        for expected in normalized_expected
    )


def _packet_support_status(*, packet: dict[str, Any], item: dict[str, Any]) -> tuple[str, str]:
    output = str(item.get("verifier_output") or item.get("outcome") or "").strip()
    if output != "accepted":
        return "not_applicable", ""
    strategy_name = str(item.get("strategy_name") or packet.get("execution_strategy") or "").strip()
    evidence_refs = _string_list(item.get("evidence_refs"))
    captured_commands = item.get("captured_commands") if isinstance(item.get("captured_commands"), list) else []
    produced_artifacts = item.get("produced_artifacts") if isinstance(item.get("produced_artifacts"), list) else []
    result_artifact_path = str(item.get("result_artifact_path") or "").strip()
    step_results = item.get("step_results") if isinstance(item.get("step_results"), list) else []
    fallback_reason = str(item.get("fallback_reason") or packet.get("fallback_reason") or "").strip()
    strategy_inputs = packet.get("strategy_inputs") if isinstance(packet.get("strategy_inputs"), dict) else {}
    if strategy_name == "multi_command_pipeline":
        if not step_results:
            return "unsupported", "pipeline_step_evidence_missing"
    elif strategy_name == "review_evidence_packet":
        if not produced_artifacts and not evidence_refs:
            return "unsupported", "review_evidence_missing"
    elif strategy_name == "codex_prompt_worker":
        expected_artifacts = _string_list(strategy_inputs.get("expected_artifacts"))
        combined_artifacts = list(dict.fromkeys([*evidence_refs, *[str(item).strip() for item in produced_artifacts if str(item).strip()]]))
        if not fallback_reason:
            return "unsupported", "fallback_claim_unsupported"
        if not combined_artifacts or not _matches_expected_artifacts(combined_artifacts, expected_artifacts):
            return "unsupported", "accepted_requires_external_support"
    elif strategy_name:
        if not captured_commands or not evidence_refs or not result_artifact_path:
            return "unsupported", "strategy_evidence_incomplete"
    return "supported", ""


def _validate_packet_result(item: dict[str, Any], packet: dict[str, Any], packet_id: str) -> list[str]:
    blocked_fields: list[str] = []
    prefix = f"cycle_result:packet_results:{packet_id}"
    if not _non_empty_string(item.get("summary"), 3):
        blocked_fields.append(f"{prefix}:summary_missing")
    if item.get("allowed_scope_status") not in {None, "", "within_scope", "out_of_scope"}:
        blocked_fields.append(f"{prefix}:allowed_scope_status_invalid")
    output = str(item.get("verifier_output") or item.get("outcome") or "").strip()
    if output == "accepted" and not _string_list(item.get("evidence_refs")):
        blocked_fields.append(f"{prefix}:accepted_requires_evidence")
    if not (
        _non_empty_string(item.get("result_artifact_path"), 3)
        or _string_list(item.get("evidence_refs"))
    ):
        blocked_fields.append(f"{prefix}:missing_result_artifact_or_evidence")
    if not _non_empty_string(item.get("strategy_name"), 3):
        blocked_fields.append(f"{prefix}:strategy_name_missing")
    if not _non_empty_string(item.get("runner_kind"), 3):
        blocked_fields.append(f"{prefix}:runner_kind_missing")
    support_status, support_reason = _packet_support_status(packet=packet, item=item)
    if support_status == "unsupported":
        blocked_fields.append(f"{prefix}:{support_reason}")
    return blocked_fields


def verify_cycle_payload(
    *,
    plan_payload: dict[str, Any],
    cycle_request: dict[str, Any],
    cycle_result: dict[str, Any],
    track_id: str,
) -> dict[str, Any]:
    packets = {
        str(packet.get("packet_id", "")).strip(): packet
        for packet in plan_payload.get("packets", [])
        if isinstance(packet, dict) and str(packet.get("packet_id", "")).strip()
    }
    requested_packet_ids = {
        str(packet_id).strip()
        for packet_id in cycle_request.get("packet_ids", [])
        if str(packet_id).strip()
    }
    result_items = cycle_result.get("packet_results") if isinstance(cycle_result.get("packet_results"), list) else []
    verdicts: list[dict[str, Any]] = []
    repacketization_requests: list[dict[str, Any]] = []
    blocked_fields: list[str] = []
    boundary_shrunk_remainder: list[str] = []
    blocked_by_authority: list[str] = []
    blocked_by_external_evidence: list[str] = []

    for item in result_items:
        if not isinstance(item, dict):
            blocked_fields.append("cycle_result:packet_results:item_not_object")
            continue
        packet_id = str(item.get("packet_id", "")).strip()
        if packet_id not in requested_packet_ids or packet_id not in packets:
            blocked_fields.append(f"cycle_result:packet_results:unknown_packet:{packet_id or 'missing'}")
            continue
        packet = packets[packet_id]
        blocked_fields.extend(_validate_packet_result(item, packet, packet_id))
        output = str(item.get("verifier_output") or item.get("outcome") or "").strip()
        if output not in VERIFIER_OUTPUTS:
            blocked_fields.append(f"cycle_result:packet_results:{packet_id}:verifier_output_invalid")
            continue
        allowed_scope_status = str(item.get("allowed_scope_status") or "within_scope").strip()
        blocker_class = str(item.get("blocked_reason") or item.get("blocker_class") or "").strip()
        if allowed_scope_status != "within_scope":
            output = "blocked_boundary"
        if output == "blocked_boundary" and blocker_class == "external_evidence":
            boundary_shrunk_remainder.append(packet_id)
            blocked_by_external_evidence.append(packet_id)
        elif output == "blocked_boundary":
            blocked_by_authority.append(packet_id)
        verdicts.append(
            {
                "packet_id": packet_id,
                "runtime_state": RUNTIME_STATE_BY_OUTPUT[output],
                "verifier_output": VERIFIER_OUTPUTS[output],
                "allowed_scope_status": allowed_scope_status,
                "blocker_class": blocker_class,
                "strategy_name": str(item.get("strategy_name") or "").strip(),
                "runner_kind": str(item.get("runner_kind") or "").strip(),
                "artifact_path": str(
                    Path(str(cycle_result.get("artifact_path") or cycle_result.get("result_path") or "").strip())
                ),
                "retry_mode": str(item.get("retry_mode") or "same_method").strip(),
                "summary": str(item.get("summary") or "").strip(),
                "changed_files": item.get("changed_files") if isinstance(item.get("changed_files"), list) else [],
                "evidence_refs": item.get("evidence_refs") if isinstance(item.get("evidence_refs"), list) else [],
                "captured_commands": item.get("captured_commands") if isinstance(item.get("captured_commands"), list) else [],
                "produced_artifacts": item.get("produced_artifacts") if isinstance(item.get("produced_artifacts"), list) else [],
                "fallback_used": item.get("fallback_used") is True,
                "step_results": item.get("step_results") if isinstance(item.get("step_results"), list) else [],
                "fallback_reason": str(item.get("fallback_reason") or "").strip(),
                "support_status": _packet_support_status(packet=packet, item=item)[0],
                "unsupported_risk_reason": _packet_support_status(packet=packet, item=item)[1],
            }
        )
        request = item.get("repacketization_request")
        if isinstance(request, dict):
            repacketization_requests.append(request)

    review = {
        "schema_version": "cycle-review.v1",
        "track_id": track_id,
        "cycle_id": str(cycle_request.get("cycle_id", "")).strip(),
        "packet_verdicts": verdicts,
        "repacketization_requests": repacketization_requests,
        "boundary_shrunk_remainder": sorted(set(boundary_shrunk_remainder)),
        "blocked_by_authority": sorted(set(blocked_by_authority)),
        "blocked_by_external_evidence": sorted(set(blocked_by_external_evidence)),
        "escalation_required": any(
            verdict.get("verifier_output") in {"blocked_migration_defect", "escalate"}
            or (
                verdict.get("verifier_output") == "blocked_boundary"
                and str(verdict.get("blocker_class") or "").strip() != "external_evidence"
            )
            for verdict in verdicts
        ),
        "migration_fallback_used": any(
            verdict.get("verifier_output") == "blocked_migration_defect" for verdict in verdicts
        ),
        "blocked_fields": blocked_fields,
    }
    return review


def main() -> int:
    ensure_python_3_11()
    parser = argparse.ArgumentParser(description="Verify a runtime cycle result.")
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--cycle-request-json", required=True)
    parser.add_argument("--cycle-result-json", required=True)
    parser.add_argument("--review-json-out", required=True)
    parser.add_argument("--track-id", required=True)
    args = parser.parse_args()

    try:
        plan_payload = load_json_file(args.plan_json)
        cycle_request = load_json_file(args.cycle_request_json)
        cycle_result = load_json_file(args.cycle_result_json)
        cycle_result["artifact_path"] = args.cycle_result_json
        review = verify_cycle_payload(
            plan_payload=plan_payload,
            cycle_request=cycle_request,
            cycle_result=cycle_result,
            track_id=args.track_id,
        )
        if review["blocked_fields"]:
            status = "blocked"
            review_payload = stable_review(
                gate="cycle-verifier",
                status=status,
                content="Cycle verifier blocked the result artifact.",
                blocked_fields=review["blocked_fields"],
                next_step="Fix the cycle result artifact and rerun verify_cycle.py.",
                meta={"cycle_review": review},
            )
        else:
            review_payload = stable_review(
                gate="cycle-verifier",
                status="approve",
                content="Cycle verifier produced packet verdicts.",
                next_step="Apply the cycle review inside objective_runtime.py.",
                meta={"cycle_review": review},
            )
    except Exception as exc:
        review_payload = stable_review(
            gate="cycle-verifier",
            status="blocked",
            content="Cycle verifier failed.",
            blocked_fields=[str(exc)],
            next_step="Fix the cycle inputs and rerun verify_cycle.py.",
        )

    write_json_file(args.review_json_out, review_payload)
    print(json.dumps(review_payload, sort_keys=True))
    return 0 if review_payload["status"] == "approve" else 1


if __name__ == "__main__":
    raise SystemExit(main())
