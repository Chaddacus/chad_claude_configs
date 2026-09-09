#!/usr/bin/env python3
"""Deterministic CI acceptance checker for Postflight Completion Gate."""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"payload_not_object:{path}")
    return payload


def parse_telemetry_line(line: str) -> dict[str, str]:
    if not line.startswith("[ralph-meta] "):
        return {}
    content = line[len("[ralph-meta] ") :].strip()
    out: dict[str, str] = {}
    for token in shlex.split(content):
        token = token.strip()
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        out[key] = value.strip().strip('"')
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate postflight acceptance predicate.")
    parser.add_argument("--run-summary", required=True, help="Path to run_summary.json")
    parser.add_argument("--out", required=True, help="Output result JSON")
    parser.add_argument("--mode", choices=["enforce", "audit"], default="enforce")
    args = parser.parse_args()

    summary_path = Path(args.run_summary).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        summary = load_json(summary_path)
    except Exception as exc:
        payload = {
            "schema_version": "postflight_acceptance_check.v1",
            "status": "blocked",
            "reason_code": "INVALID_RUN_SUMMARY",
            "reason": str(exc),
            "checks": {},
        }
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(payload, sort_keys=True))
        return 1

    telemetry_log = Path(str(summary.get("telemetry_log_path") or ""))
    finalize_json = Path(str(summary.get("finalize_json_path") or ""))
    acceptance_artifact = Path(str(summary.get("acceptance_artifact_path") or ""))
    blocker_artifact = Path(str(summary.get("blocker_artifact_path") or ""))
    run_id = str(summary.get("run_id") or "")
    route_task_id = str(summary.get("route_task_id") or "")
    track_id = str(summary.get("track_id") or "")
    exit_code_raw = summary.get("exit_code")
    exit_code = -1 if exit_code_raw is None else int(exit_code_raw)

    lines = telemetry_log.read_text(encoding="utf-8").splitlines() if telemetry_log.exists() else []
    parsed = [parse_telemetry_line(line) for line in lines]
    parsed = [item for item in parsed if item]

    def has_success_meta() -> bool:
        for item in parsed:
            if (
                item.get("run") == run_id
                and item.get("task") == route_task_id
                and item.get("track") == track_id
                and item.get("gate") == "planning_gate_finalize"
                and item.get("status") == "approve"
            ):
                return True
        return False

    def has_blocked_meta() -> bool:
        allowed_codes = {
            "BUDGET_EXHAUSTED",
            "FINALIZE_BLOCKED",
            "PLAN_BLOCKED",
            "IMPL_BLOCKED",
            "LOCK_BUSY",
            "ROUTE_CLASS_IMMUTABLE_VIOLATION",
            "INVALID_INPUT",
            "INTERNAL_ERROR",
        }
        for item in parsed:
            if (
                item.get("run") == run_id
                and item.get("task") == route_task_id
                and item.get("track") == track_id
                and item.get("gate") == "planning_gate_finalize"
                and item.get("status") == "blocked"
                and item.get("reason_code") in allowed_codes
            ):
                return True
        return False

    finalize_ok = False
    finalize_payload: dict[str, Any] = {}
    if finalize_json.exists():
        try:
            finalize_payload = load_json(finalize_json)
            finalize_ok = bool(finalize_payload.get("ok"))
        except Exception:
            finalize_ok = False

    success_branch = all(
        [
            has_success_meta(),
            finalize_json.exists() and finalize_ok,
            exit_code == 0,
            acceptance_artifact.exists(),
        ]
    )
    if success_branch:
        try:
            acceptance_payload = load_json(acceptance_artifact)
            required_acceptance = {
                "schema_version",
                "accepted_type",
                "run_id",
                "route_task_id",
                "track_id",
                "route_class",
                "finalize_json_path",
                "ok",
                "objective_closure_state",
                "migration_fallback_used",
            }
            success_branch = required_acceptance.issubset(acceptance_payload.keys())
            accepted_type = str(acceptance_payload.get("accepted_type") or "").strip()
            closure_state = str(acceptance_payload.get("objective_closure_state") or "").strip()
            migration_fallback_used = bool(acceptance_payload.get("migration_fallback_used"))
            allowed_mapping = {
                "OBJECTIVE_COMPLETE": "ACCEPTED_SUCCESS",
                "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK": "ACCEPTED_BLOCKED",
                "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED": "ACCEPTED_BLOCKED",
                "OBJECTIVE_BLOCKED_MIGRATION_DEFECT": "ACCEPTED_BLOCKED",
            }
            success_branch = success_branch and allowed_mapping.get(closure_state) == accepted_type
            if accepted_type == "ACCEPTED_SUCCESS":
                success_branch = success_branch and acceptance_artifact.name == "finalize.accepted.json"
            elif accepted_type == "ACCEPTED_BLOCKED":
                success_branch = success_branch and acceptance_artifact.name == "finalize.blocked.json"
            else:
                success_branch = False
            if finalize_payload:
                success_branch = success_branch and str(finalize_payload.get("accepted_type") or "").strip() == accepted_type
                success_branch = success_branch and str(finalize_payload.get("objective_closure_state") or "").strip() == closure_state
                success_branch = success_branch and bool(finalize_payload.get("migration_fallback_used")) == migration_fallback_used
        except Exception:
            success_branch = False
    blocked_branch = all(
        [
            has_blocked_meta(),
            exit_code in {20, 30},
            blocker_artifact.exists(),
        ]
    )
    if blocked_branch:
        try:
            blocker_payload = load_json(blocker_artifact)
            required_blocker = {
                "schema_version",
                "run_id",
                "route_task_id",
                "track_id",
                "route_class",
                "loop_count",
                "status",
                "reason_code",
                "missing_fields",
                "blocked_fields",
                "all_loop_artifact_paths",
            }
            blocked_branch = required_blocker.issubset(blocker_payload.keys())
        except Exception:
            blocked_branch = False
    exactly_one = (success_branch and not blocked_branch) or (blocked_branch and not success_branch)

    checks = {
        "success_branch": success_branch,
        "blocked_branch": blocked_branch,
        "exactly_one_branch": exactly_one,
    }

    if args.mode == "audit":
        status = "warn" if not exactly_one else "pass"
        payload = {
            "schema_version": "postflight_acceptance_check.v1",
            "mode": "audit",
            "status": status,
            "reason_code": "AUDIT_ONLY",
            "reason": "Audit mode never fails acceptance gate.",
            "checks": checks,
        }
        warning_path = summary_path.parent / "postflight.audit.warning.json"
        warning_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(payload, sort_keys=True))
        return 0

    status = "pass" if exactly_one else "fail"
    payload = {
        "schema_version": "postflight_acceptance_check.v1",
        "mode": "enforce",
        "status": status,
        "reason_code": "OK" if status == "pass" else "PREDICATE_FAILED",
        "reason": "Acceptance predicate satisfied." if status == "pass" else "Acceptance predicate failed.",
        "checks": checks,
    }
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
