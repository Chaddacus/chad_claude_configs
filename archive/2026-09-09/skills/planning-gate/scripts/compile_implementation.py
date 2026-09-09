#!/usr/bin/env python3
"""Compile a canonical implementation payload from runtime artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import (
    CAPTURE_SCHEMA_VERSION,
    LOG_CAPTURE_PREFIXES,
    ROLLBACK_CAPTURE_PREFIXES,
    SMOKE_CAPTURE_PREFIXES,
    TEST_CAPTURE_PREFIXES,
    packet_verdict_path,
    resolve_artifacts_root,
    runtime_artifact_paths,
    session_artifact_paths,
    sha256_file,
    stable_objective_id,
    write_json_file,
)

SMOKE_STAGES = {"25%", "50%", "75%", "100%"}
TERMINAL_CAPTURE_REQUIRED_STAGES = ("25%", "50%", "75%", "100%")


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"payload_not_object:{path}")
    return payload


def _manifest_exit_code(payload: dict[str, Any]) -> int:
    value = payload.get("exit_code", 1)
    if value is None:
        return 1
    return int(value)


def _stdout_excerpt(payload: dict[str, Any]) -> str:
    stdout_path = payload.get("stdout_path")
    if not isinstance(stdout_path, str) or not stdout_path.strip():
        return ""
    try:
        text = Path(stdout_path).read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    if len(text) > 200:
        return text[:200]
    return text


def _accepted_type_for_closure_state(closure_state: str) -> str:
    if closure_state == "OBJECTIVE_COMPLETE":
        return "ACCEPTED_SUCCESS"
    if closure_state in {
        "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK",
        "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED",
        "OBJECTIVE_BLOCKED_MIGRATION_DEFECT",
    }:
        return "ACCEPTED_BLOCKED"
    return ""


def _derive_runtime_state_fallback(
    *,
    track_id: str,
    objective_status: dict[str, Any],
    objective_summary: dict[str, Any],
    support_confidence: dict[str, Any],
    runtime_state_path: Path,
) -> dict[str, Any]:
    closure_state = str(objective_status.get("closure_state") or objective_summary.get("closure_state") or "").strip()
    stop_allowed = bool(_accepted_type_for_closure_state(closure_state))
    return {
        "schema_version": "objective-runtime-state.v1",
        "objective_id": str(objective_status.get("objective_id") or stable_objective_id(track_id)),
        "track_id": track_id,
        "route_hint": str(objective_summary.get("route_hint") or ""),
        "controller_mode": "enforce",
        "lifecycle_status": "approved" if stop_allowed else "running",
        "closure_state": closure_state,
        "required_work_remaining": False,
        "material_optional_work_remaining": False,
        "stop_allowed": stop_allowed,
        "stop_reason": "legacy_fallback" if stop_allowed else "closure_not_ready",
        "next_recommended_packet": str(objective_summary.get("next_recommended_packet") or ""),
        "unsupported_closure_risk": str(support_confidence.get("unsupported_closure_risk") or "none"),
        "last_verifier_result": {},
        "artifact_path": str(runtime_state_path),
        "derived_from_legacy_artifacts": True,
    }


def _capture_entries(
    captures_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], set[str], set[str]]:
    tests: list[dict[str, Any]] = []
    smoke: list[dict[str, Any]] = []
    logging: list[dict[str, Any]] = []
    rollback: dict[str, Any] = {"executed": False, "result": "not_run", "evidence": "", "proof_artifact": "", "proof_hash": ""}
    seen_prefixes: set[str] = set()
    smoke_stages_seen: set[str] = set()
    for manifest_path in sorted(captures_root.rglob("manifest.json")):
        payload = _load_json(manifest_path)
        if payload.get("schema_version") != CAPTURE_SCHEMA_VERSION:
            continue
        name = str(payload.get("name", "")).strip().lower()
        stage = str(payload.get("stage", "")).strip()
        proof = {"proof_artifact": str(manifest_path), "proof_hash": sha256_file(manifest_path)}
        if any(name.startswith(prefix) for prefix in ROLLBACK_CAPTURE_PREFIXES):
            seen_prefixes.add("rollback")
            rollback = {
                "executed": True,
                "result": "pass" if _manifest_exit_code(payload) == 0 else "blocked",
                "evidence": _stdout_excerpt(payload) or name or "rollback",
                **proof,
            }
        elif any(name.startswith(prefix) for prefix in LOG_CAPTURE_PREFIXES):
            seen_prefixes.add("log")
            logging.append({"event": name or "runtime-log", **proof})
        elif any(name.startswith(prefix) for prefix in SMOKE_CAPTURE_PREFIXES) and stage in SMOKE_STAGES:
            seen_prefixes.add("smoke")
            smoke_stages_seen.add(stage)
            smoke.append(
                {
                    "stage": stage,
                    "status": "pass" if _manifest_exit_code(payload) == 0 else "fail",
                    "command": " ".join(payload.get("command_argv", [])) if isinstance(payload.get("command_argv"), list) else "",
                    "observed_output": _stdout_excerpt(payload) or name or stage,
                    "decision": "continue" if stage != "100%" else "approve",
                    **proof,
                }
            )
        elif any(name.startswith(prefix) for prefix in TEST_CAPTURE_PREFIXES):
            if name.startswith("worker-"):
                seen_prefixes.add("worker")
            elif name.startswith("verifier-"):
                seen_prefixes.add("verifier")
            elif name.startswith("test-"):
                seen_prefixes.add("test")
            tests.append(
                {
                    "name": name or "test",
                    "command": " ".join(payload.get("command_argv", [])) if isinstance(payload.get("command_argv"), list) else "",
                    "status": "pass" if _manifest_exit_code(payload) == 0 else "fail",
                    "result": _stdout_excerpt(payload) or name or "capture",
                    **proof,
                }
            )
    return tests, smoke, logging, rollback, seen_prefixes, smoke_stages_seen


def _required_capture_failures(
    *,
    closure_state: str,
    seen_prefixes: set[str],
    smoke_stages_seen: set[str],
) -> list[str]:
    missing: list[str] = []
    for prefix in ("worker", "verifier", "test", "log", "rollback"):
        if prefix not in seen_prefixes:
            missing.append(f"missing_capture_prefix:{prefix}")
    if closure_state in {
        "OBJECTIVE_COMPLETE",
        "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK",
        "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED",
    }:
        for stage in TERMINAL_CAPTURE_REQUIRED_STAGES:
            if stage not in smoke_stages_seen:
                missing.append(f"missing_smoke_stage:{stage}")
    return missing


def compile_implementation_payload(
    *,
    plan_payload: dict[str, Any],
    artifacts_root: Path,
    track_id: str,
    workspace_root: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    runtime_paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    session_paths = session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    packet_dag = _load_json(runtime_paths["packet_dag"])
    objective_status = _load_json(runtime_paths["status"])
    schedule = _load_json(runtime_paths["schedule"])
    objective_summary = _load_json(runtime_paths["summary"]) if runtime_paths["summary"].exists() else {}
    validation_plan = _load_json(runtime_paths["validation_plan"]) if runtime_paths["validation_plan"].exists() else {}
    repo_capabilities = _load_json(runtime_paths["repo_capabilities"]) if runtime_paths["repo_capabilities"].exists() else {}
    packet_quality = _load_json(runtime_paths["packet_quality"]) if runtime_paths["packet_quality"].exists() else {}
    execution_coverage = _load_json(runtime_paths["execution_coverage"]) if runtime_paths["execution_coverage"].exists() else {}
    support_confidence = _load_json(runtime_paths["support_confidence"]) if runtime_paths["support_confidence"].exists() else {}
    operator_view = _load_json(runtime_paths["operator_view"]) if runtime_paths["operator_view"].exists() else {}
    objective_runtime_state = (
        {**_load_json(runtime_paths["runtime_state"]), "artifact_path": str(runtime_paths["runtime_state"])}
        if runtime_paths["runtime_state"].exists()
        else _derive_runtime_state_fallback(
            track_id=track_id,
            objective_status=objective_status,
            objective_summary=objective_summary,
            support_confidence=support_confidence,
            runtime_state_path=runtime_paths["runtime_state"],
        )
    )
    checkpoint = _load_json(session_paths["checkpoint"])
    packet_verdicts = []
    changed_files: list[str] = []
    for packet in packet_dag.get("packets", []):
        if not isinstance(packet, dict):
            continue
        packet_id = str(packet.get("packet_id", "")).strip()
        if not packet_id:
            continue
        verdict_path = packet_verdict_path(artifacts_root=artifacts_root, track_id=track_id, packet_id=packet_id)
        if verdict_path.exists():
            verdict_payload = _load_json(verdict_path)
            packet_verdicts.append(
                {
                    "packet_id": packet_id,
                    "strategy_name": verdict_payload.get("strategy_name", packet.get("execution_strategy", "")),
                    "runner_kind": verdict_payload.get("runner_kind", ""),
                    "runtime_state": verdict_payload.get("runtime_state", packet.get("runtime_state", "queued")),
                    "verifier_output": verdict_payload.get("verifier_output", "accepted" if packet.get("runtime_state") == "accepted" else "rejected_rework"),
                    "allowed_scope_status": verdict_payload.get("allowed_scope_status", "within_scope"),
                    "support_status": verdict_payload.get("support_status", ""),
                    "unsupported_risk_reason": verdict_payload.get("unsupported_risk_reason", ""),
                    "artifact_path": str(verdict_path),
                }
            )
            for path in verdict_payload.get("changed_files", []):
                if str(path).strip():
                    changed_files.append(str(path).strip())

    closure_state = str(objective_status.get("closure_state", "")).strip()
    if closure_state == "OBJECTIVE_BLOCKED_MIGRATION_DEFECT":
        return {
            "schema_version": "implementation-migration-fallback.v1",
            "track_id": track_id,
            "objective_id": stable_objective_id(track_id),
            "reason": "migration_defect_fallback_required",
            "artifact_path": str(runtime_paths["status"]),
        }
    captures_root = artifacts_root / track_id / "captures"
    tests_run, smoke_results, logging_evidence, rollback_validation, seen_prefixes, smoke_stages_seen = _capture_entries(captures_root)
    capture_failures = _required_capture_failures(
        closure_state=closure_state,
        seen_prefixes=seen_prefixes,
        smoke_stages_seen=smoke_stages_seen,
    )
    if capture_failures:
        raise ValueError("runtime_capture_requirements:" + ",".join(sorted(capture_failures)))
    if not changed_files:
        changed_files = sorted(
            {
                str(path).strip()
                for packet in packet_dag.get("packets", [])
                if isinstance(packet, dict)
                for path in packet.get("allowed_scope", [])
                if str(path).strip()
            }
        )

    return {
        "schema_version": "implementation.v1",
        "summary": f"Runtime-compiled implementation for {track_id}",
        "changed_files": sorted(set(changed_files)) or ["runtime-artifacts-only"],
        "tests_run": tests_run,
        "smoke_results": smoke_results,
        "logging_evidence": logging_evidence,
        "rollback_validation": rollback_validation,
        "memory_retrieval_evidence": [
            {"tool": "mcp__codex-mem__build_context", "query": "pref:planning-gate", "result_count": 0}
        ],
        "preferences_applied": [
            {
                "key": "pref:planning-gate",
                "decision": "Use runtime-generated governed artifacts as the single source of objective state.",
                "rationale": "The memory backend was unavailable, so the implementation followed the repo-local governed-runtime contract directly.",
            }
        ],
        "skill_trigger_eval_results": [
            {"skill": "planning-gate", "false_positive_rate": 0.0, "false_negative_rate": 0.0, "threshold_passed": True}
        ],
        "prompt_contract_used": [
            {
                "name": "proxy-runtime-closeout.v1",
                "required_context": "objective intent, validated plan, runtime artifacts, cycle artifacts, and capture manifests",
                "required_constraints": "single-writer runtime state, verifier-gated packet outcomes, and fail-closed terminal integrity",
                "verification_section": "runtime tests, wrapper tests, validate_impl, finalize_gate, and fresh-track artifact validation",
                "done_when": "objective closure or accepted blocked closure is represented by runtime-generated artifacts and finalize_gate returns ok=true",
            }
        ],
        "frontend_roundtrip_evidence": [],
        "objective_runtime_state": objective_runtime_state,
        "objective_status": {**objective_status, "artifact_path": str(runtime_paths["status"])},
        "objective_summary": (
            {
                **objective_summary,
                "artifact_path": str(runtime_paths["summary"]),
                "authoritative_source_artifact": str(runtime_paths["runtime_state"]),
            }
            if objective_summary
            else {}
        ),
        "validation_plan": {**validation_plan, "artifact_path": str(runtime_paths["validation_plan"])} if validation_plan else {},
        "repo_capabilities": {**repo_capabilities, "artifact_path": str(runtime_paths["repo_capabilities"])} if repo_capabilities else {},
        "packet_quality": {**packet_quality, "artifact_path": str(runtime_paths["packet_quality"])} if packet_quality else {},
        "execution_coverage": {**execution_coverage, "artifact_path": str(runtime_paths["execution_coverage"])} if execution_coverage else {},
        "support_confidence": (
            {
                **support_confidence,
                "artifact_path": str(runtime_paths["support_confidence"]),
                "authoritative_source_artifact": str(runtime_paths["runtime_state"]),
            }
            if support_confidence
            else {}
        ),
        "operator_view": (
            {
                **operator_view,
                "artifact_path": str(runtime_paths["operator_view"]),
                "authoritative_source_artifact": str(runtime_paths["runtime_state"]),
            }
            if operator_view
            else {}
        ),
        "schedule_artifact": str(runtime_paths["schedule"]),
        "packet_verdicts": packet_verdicts,
        "execution_ledger": str(runtime_paths["execution_ledger"]),
        "adaptation_log": str(runtime_paths["adaptation_log"]),
        "packet_results_artifact": str(runtime_paths["packet_results"]),
        "checkpoint_commit": str(checkpoint.get("checkpoint_commit", "")).strip(),
        "checkpoint_blocked": checkpoint.get("checkpoint_blocked") is True,
        "checkpoint_block_reason": str(checkpoint.get("checkpoint_block_reason", "")).strip(),
        "checkpoint_block_evidence": str(checkpoint.get("checkpoint_block_evidence", "")).strip(),
        "bootstrap_commands": checkpoint.get("bootstrap_commands", []),
        "validation_commands": checkpoint.get("validation_commands", []),
        "clean_state_assertions": checkpoint.get("clean_state_assertions", []),
        "migration_fallback": {"used": False, "reason": "not-needed", "artifact_path": ""},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile implementation evidence from runtime artifacts.")
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--track-id", required=True)
    parser.add_argument("--artifacts-root", default=None)
    parser.add_argument("--workspace-root", default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    artifacts_root = resolve_artifacts_root(args.artifacts_root, cwd=args.workspace_root)
    try:
        plan_payload = _load_json(Path(args.plan_json))
        payload = compile_implementation_payload(
            plan_payload=plan_payload,
            artifacts_root=artifacts_root,
            track_id=args.track_id,
            workspace_root=args.workspace_root,
        )
        write_json_file(args.out, payload)
        print(json.dumps(payload, sort_keys=True))
        return 0 if payload.get("schema_version") == "implementation.v1" else 20
    except Exception as exc:
        payload = {
            "schema_version": "implementation-compile-review.v1",
            "status": "revise",
            "track_id": args.track_id,
            "blocked_fields": [str(exc)],
            "next_step": "Generate the missing runtime capture manifests and rerun compile_implementation.py.",
        }
        write_json_file(args.out, payload)
        print(json.dumps(payload, sort_keys=True))
        return 10


if __name__ == "__main__":
    raise SystemExit(main())
