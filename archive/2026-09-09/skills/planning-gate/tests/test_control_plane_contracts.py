#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from compile_intent import compile_intent_payload
from compile_plan import compile_plan_payload
from initialize_session import initialize_session_payload
from common import (
    build_plan_frontloaded_artifacts,
    build_repo_validation_plan,
    build_packet_quality_report,
    discover_repo_capabilities,
    runtime_artifact_paths,
    session_artifact_paths,
    sha256_file,
    stable_objective_id,
    validate_impl_contract,
    validate_plan_contract,
)
from objective_runtime import bootstrap_runtime
from objective_scheduler import build_schedule, compute_runnable_set
from verify_plan import verify_plan_payload

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _capture_manifest(path: Path, *, track_id: str, stage: str = "100%", exit_code: int = 0) -> tuple[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path = path.parent / "stdout.redacted.txt"
    stderr_path = path.parent / "stderr.redacted.txt"
    stdout_path.write_text("ok\n", encoding="utf-8")
    stderr_path.write_text("\n", encoding="utf-8")
    payload = {
        "schema_version": "run-cmd-capture.v1",
        "producer": "run_cmd_capture.v1",
        "track_id": track_id,
        "stage": stage,
        "name": path.stem.replace(".manifest", ""),
        "command_argv": ["echo", "ok"],
        "exit_code": exit_code,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }
    _write_json(path, payload)
    return str(path), sha256_file(path)


def _definition_of_done() -> list[dict[str, str]]:
    return [
        {
            "id": "dod-correctness",
            "category": "correctness",
            "criterion": "Contract and scheduler validations pass deterministically.",
            "verification": "python3.11 -m pytest ~/.claude/skills/planning-gate/tests/test_control_plane_contracts.py -k plan",
        },
        {
            "id": "dod-tests",
            "category": "tests",
            "criterion": "Automated tests cover packet, scheduler, and closure edge cases.",
            "verification": "python3.11 -m pytest ~/.claude/skills/planning-gate/tests/test_control_plane_contracts.py",
        },
        {
            "id": "dod-security",
            "category": "security",
            "criterion": "Validators reject out-of-policy control-plane contracts and artifact drift.",
            "verification": "python3.11 -m pytest ~/.claude/skills/planning-gate/tests/test_control_plane_contracts.py -k security",
        },
        {
            "id": "dod-observability",
            "category": "observability",
            "criterion": "Schedule and objective artifacts are emitted and replayable.",
            "verification": "Inspect objective.status.json and objective.schedule.json artifacts under planning_artifacts.",
        },
        {
            "id": "dod-rollback",
            "category": "rollback",
            "criterion": "Migration fallback stays explicit and compat rollback is verified.",
            "verification": "python3.11 -m pytest ~/.claude/skills/planning-gate/tests/test_control_plane_contracts.py -k migration",
        },
    ]


def _prepare_plan_artifacts(plan: dict, artifacts_root: Path, track_id: str) -> None:
    compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=artifacts_root)
    initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=artifacts_root)
    compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=artifacts_root)
    verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=artifacts_root)
    bootstrap_runtime(plan=plan, track_id=track_id, artifacts_root=artifacts_root)


def _scheduler_policy() -> dict:
    return {
        "max_parallel_packets": 2,
        "parallelism_policy": "bounded_parallel",
        "admission_rule": [
            "packet validity",
            "autonomy readiness",
            "dependency readiness",
            "conflict check",
            "retry-budget check",
        ],
        "recompute_triggers": [
            "verifier_verdict",
            "packet_cancellation",
            "repacketization",
            "escalation_decision",
            "boundary_shrink_decision",
        ],
        "terminal_stop_conditions": [
            "objective_closed",
            "escalation_required_no_runnable_packets",
            "migration_defect_fallback_invoked",
            "unrecoverable_graph_invalidity",
        ],
    }


def _packets() -> list[dict]:
    return json.loads(json.dumps(_plan_payload()["packets"]))


def _plan_payload() -> dict:
    return json.loads((FIXTURES / "plan_valid.json").read_text(encoding="utf-8"))


def _implementation_payload(artifacts_root: Path, track_id: str) -> tuple[dict, dict]:
    plan = _plan_payload()
    packet_ids = list(plan["required_packets"])
    packets = {packet["packet_id"]: packet for packet in _packets()}
    _prepare_plan_artifacts(plan, artifacts_root, track_id)
    test_artifact, test_hash = _capture_manifest(artifacts_root / "captures" / "test-unit.manifest.json", track_id=track_id, stage="25%")
    smoke25_artifact, smoke25_hash = _capture_manifest(artifacts_root / "captures" / "smoke-25.manifest.json", track_id=track_id, stage="25%")
    smoke50_artifact, smoke50_hash = _capture_manifest(artifacts_root / "captures" / "smoke-50.manifest.json", track_id=track_id, stage="50%")
    smoke75_artifact, smoke75_hash = _capture_manifest(artifacts_root / "captures" / "smoke-75.manifest.json", track_id=track_id, stage="75%")
    smoke100_artifact, smoke100_hash = _capture_manifest(artifacts_root / "captures" / "smoke-100.manifest.json", track_id=track_id, stage="100%")
    log_artifact, log_hash = _capture_manifest(artifacts_root / "captures" / "log-runtime-cycle-001.manifest.json", track_id=track_id, stage="50%")
    rollback_artifact, rollback_hash = _capture_manifest(artifacts_root / "captures" / "rollback-checkpoint.manifest.json", track_id=track_id, stage="75%")
    budget_artifact, budget_hash = _capture_manifest(artifacts_root / "captures" / "budget-diffstat.manifest.json", track_id=track_id, stage="75%")

    runtime_paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    objective_status_artifact = runtime_paths["status"]
    objective_status_payload = {
        "objective_id": stable_objective_id(track_id),
        "closure_state": "OBJECTIVE_COMPLETE",
        "completed_packets": packet_ids,
        "pending_packets": [],
        "blocked_packets": [],
        "deferred_packets": [],
        "boundary_shrunk_remainder": [],
    }
    _write_json(objective_status_artifact, objective_status_payload)

    schedule_artifact = runtime_paths["schedule"]
    schedule_payload = build_schedule(
        objective_id=stable_objective_id(track_id),
        packets=packets,
        accepted_packets=set(packet_ids),
        active_packets=set(),
        retry_counters={},
        max_parallel_packets=2,
        parallelism_policy="bounded_parallel",
    )
    _write_json(schedule_artifact, schedule_payload)
    summary_artifact = runtime_paths["summary"]
    _write_json(
        summary_artifact,
        {
            "schema_version": "objective-summary.v1",
            "objective_id": stable_objective_id(track_id),
            "track_id": track_id,
            "route_hint": "R3",
            "closure_state": "OBJECTIVE_COMPLETE",
            "current_frontier": [],
            "blocked_reasons": [],
            "accepted_packet_count": len(packet_ids),
            "next_action": "finalize",
            "updated_at": "2026-03-08T00:01:00+00:00",
        },
    )
    validation_plan_artifact = runtime_paths["validation_plan"]
    _write_json(
        validation_plan_artifact,
        {
            "schema_version": "objective-validation-plan.v1",
            "objective_id": stable_objective_id(track_id),
            "track_id": track_id,
            "route_hint": "R3",
            "workspace_root": str(artifacts_root),
            "changed_files": ["compile_plan.py", "verify_plan.py"],
            "required_packets": packet_ids,
            "lanes": [
                {"lane": "tests", "required": True, "reasons": ["code path touched"], "paths": ["compile_plan.py"], "commands": ["pytest"]},
                {"lane": "types_build", "required": True, "reasons": ["code path touched"], "paths": ["compile_plan.py"], "commands": ["pytest"]},
            ],
            "escalated_review_required": False,
        },
    )
    execution_ledger_artifact = runtime_paths["execution_ledger"]
    _write_json(
        execution_ledger_artifact,
        {
            "schema_version": "objective-execution-ledger.v1",
            "objective_id": stable_objective_id(track_id),
            "track_id": track_id,
            "packets": [
                {
                    "packet_id": packet_id,
                    "strategy_name": "command_capture",
                    "fallback_used": False,
                    "evidence_destination": f"planning_artifacts/{track_id}/packets/{packet_id}.verdict.json",
                    "runtime_state": "accepted",
                    "verifier_output": "accepted",
                    "retry_counters": {},
                    "latest_result_artifact": f"{packet_id}.result.json",
                }
                for packet_id in packet_ids
            ],
        },
    )
    execution_coverage_artifact = runtime_paths["execution_coverage"]
    _write_json(
        execution_coverage_artifact,
        {
            "schema_version": "objective-execution-coverage.v1",
            "route_hint": "R3",
            "packet_count": len(packet_ids),
            "deterministic_packet_count": len(packet_ids),
            "fallback_packet_count": 0,
            "review_packet_count": 0,
            "deterministic_ratio": 1.0,
            "non_review_deterministic_ratio": 1.0,
            "fallback_packet_ids": [],
            "fallback_reasons": {},
            "thresholds": {"deterministic_ratio_min": 0.90, "non_review_deterministic_ratio_min": 0.90},
            "status": "pass",
        },
    )
    support_confidence_artifact = runtime_paths["support_confidence"]
    _write_json(
        support_confidence_artifact,
        {
            "schema_version": "objective-support-confidence.v1",
            "objective_id": stable_objective_id(track_id),
            "track_id": track_id,
            "mode": "enforce",
            "packet_support": [
                {
                    "packet_id": packet_id,
                    "runtime_state": "accepted",
                    "strategy_name": "command_capture",
                    "support_status": "supported",
                    "unsupported_risk_reason": "",
                    "artifact_path": str(artifacts_root / "packets" / f"{packet_id}.verdict.json"),
                }
                for packet_id in packet_ids
            ],
            "objective_support_status": "supported",
            "unsupported_closure_risk": "none",
            "support_gap_reasons": [],
            "support_remediation_available": False,
            "support_backed_closure": True,
            "external_support_coverage": {
                "validation_gap_present": False,
                "required_lane_gap_count": 0,
                "checkpoint_ready": True,
                "fallback_support_packet_ids": [],
                "deterministic_coverage_status": "pass",
            },
            "final_gate_recommendation": "allow_closure",
        },
    )
    packet_results_artifact = runtime_paths["packet_results"]
    packet_results_artifact.parent.mkdir(parents=True, exist_ok=True)
    packet_results_artifact.write_text(
        "\n".join(
            json.dumps(
                {
                    "packet_id": packet_id,
                    "strategy_name": "command_capture",
                    "runner_kind": "command",
                    "exit_code": 0,
                    "verifier_output": "accepted",
                    "allowed_scope_status": "within_scope",
                    "summary": "ok",
                    "changed_files": [],
                    "evidence_refs": [f"capture://{packet_id}"],
                    "result_artifact_path": f"{packet_id}.result.json",
                    "captured_commands": [{"command": "echo ok", "exit_code": 0}],
                    "produced_artifacts": [],
                    "blocked_reason": "",
                    "retry_mode": "same_method",
                    "fallback_used": False,
                },
                sort_keys=True,
            )
            for packet_id in packet_ids
        )
        + "\n",
        encoding="utf-8",
    )

    verdict_a = artifacts_root / "packets" / f"{packet_ids[0]}.verdict.json"
    verdict_b = artifacts_root / "packets" / f"{packet_ids[1]}.verdict.json"
    _write_json(verdict_a, {"packet_id": packet_ids[0], "status": "accepted", "strategy_name": "command_capture", "runner_kind": "command"})
    _write_json(verdict_b, {"packet_id": packet_ids[1], "status": "accepted", "strategy_name": "validation_command", "runner_kind": "command"})
    session_paths = session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    feature_payload = json.loads(session_paths["feature_list"].read_text(encoding="utf-8"))
    for item in feature_payload["features"]:
        item["status"] = "verified"
        item["evidence_refs"] = [str(verdict_a), str(verdict_b)]
    _write_json(session_paths["feature_list"], feature_payload)
    checkpoint_payload = json.loads(session_paths["checkpoint"].read_text(encoding="utf-8"))
    checkpoint_payload.update(
        {
            "last_verified_packet_ids": packet_ids,
            "current_frontier": [],
            "next_recommended_packet": "",
            "checkpoint_strategy": "git_checkpoint_required",
            "checkpoint_attempted_at": "2026-03-08T00:00:30+00:00",
            "checkpoint_commit": f"checkpoint-{track_id}",
            "checkpoint_blocked": False,
            "checkpoint_block_reason": "",
            "checkpoint_block_evidence": "",
            "rollback_validation_ref": rollback_artifact,
        }
    )
    _write_json(session_paths["checkpoint"], checkpoint_payload)
    progress_events = [
        {
            "schema_version": "objective-progress-event.v1",
            "event_type": "session_initialized",
            "timestamp": "2026-03-08T00:00:00+00:00",
            "objective_id": checkpoint_payload["objective_id"],
            "track_id": track_id,
            "checkpoint_id": checkpoint_payload["checkpoint_id"],
            "current_frontier": list(packets.keys()),
            "feature_status_summary": {
                item["requirement_id"]: item["status"] for item in feature_payload["features"]
            },
        },
        {
            "schema_version": "objective-progress-event.v1",
            "event_type": "checkpoint",
            "timestamp": "2026-03-08T00:01:00+00:00",
            "objective_id": checkpoint_payload["objective_id"],
            "track_id": track_id,
            "checkpoint_id": checkpoint_payload["checkpoint_id"],
            "last_verified_packet_ids": packet_ids,
            "current_frontier": [],
            "next_recommended_packet": "",
        },
    ]
    session_paths["progress"].write_text(
        "\n".join(json.dumps(item, sort_keys=True) for item in progress_events) + "\n",
        encoding="utf-8",
    )
    runtime_state_artifact = runtime_paths["runtime_state"]
    runtime_state_artifact.write_text(
        json.dumps(
            {
                "schema_version": "objective-runtime-state.v1",
                "objective_id": stable_objective_id(track_id),
                "track_id": track_id,
                "route_hint": "R3",
                "controller_mode": "enforce",
                "lifecycle_status": "approved",
                "closure_state": "OBJECTIVE_COMPLETE",
                "current_cycle_id": "cycle-001",
                "current_frontier": [],
                "current_packet": "finalize",
                "safe_momentum_available": False,
                "required_work_remaining": False,
                "required_work_reasons": [],
                "material_optional_work_remaining": False,
                "material_optional_work_reasons": [],
                "stop_allowed": True,
                "stop_reason": "all_policy_backed_work_satisfied",
                "next_recommended_packet": "",
                "unsupported_closure_risk": "none",
                "last_verifier_result": {"status": "approve", "reason": "fixture"},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    impl = {
        "schema_version": "implementation.v1",
        "summary": "Implemented governed control-plane scheduler contracts.",
        "changed_files": ["scripts/common.py", "scripts/objective_scheduler.py"],
        "tests_run": [
            {
                "name": "contracts",
                "command": "python3.11 -m pytest ~/.claude/skills/planning-gate/tests/test_control_plane_contracts.py",
                "status": "pass",
                "result": "control-plane contract tests passed",
                "proof_artifact": test_artifact,
                "proof_hash": test_hash,
            }
        ],
        "smoke_results": [
            {
                "stage": "25%",
                "status": "pass",
                "command": "python3.11 -m pytest -k plan_valid",
                "observed_output": "plan valid",
                "decision": "continue",
                "proof_artifact": smoke25_artifact,
                "proof_hash": smoke25_hash,
            },
            {
                "stage": "50%",
                "status": "pass",
                "command": "python3.11 -m py_compile scripts/objective_scheduler.py",
                "observed_output": "py_compile passed",
                "decision": "continue",
                "proof_artifact": smoke50_artifact,
                "proof_hash": smoke50_hash,
            },
            {
                "stage": "75%",
                "status": "pass",
                "command": "python3.11 -m pytest ~/.claude/skills/planning-gate/tests/test_control_plane_contracts.py -k runnable",
                "observed_output": "runnable set tests passed",
                "decision": "continue",
                "proof_artifact": smoke75_artifact,
                "proof_hash": smoke75_hash,
            },
            {
                "stage": "100%",
                "status": "pass",
                "command": "python3.11 -m pytest ~/.claude/skills/planning-gate/tests/test_control_plane_contracts.py -k impl_valid",
                "observed_output": "implementation validation passed",
                "decision": "approve",
                "proof_artifact": smoke100_artifact,
                "proof_hash": smoke100_hash,
            },
        ],
        "logging_evidence": [{"event": "scheduler-ready", "proof_artifact": log_artifact, "proof_hash": log_hash}],
        "rollback_validation": {
            "executed": True,
            "result": "pass",
            "evidence": "compat rollback remains available",
            "proof_artifact": rollback_artifact,
            "proof_hash": rollback_hash,
        },
        "memory_retrieval_evidence": [{"tool": "mcp__codex-mem__build_context", "query": "control plane", "result_count": 1}],
        "preferences_applied": [{"key": "pref:control-plane", "decision": "deterministic scheduler", "rationale": "Matches global governance preference for hard evidence and bounded autonomy."}],
        "skill_trigger_eval_results": [{"skill": "planning-gate", "false_positive_rate": 0.0, "false_negative_rate": 0.0, "threshold_passed": True}],
        "prompt_contract_used": [{"name": "feature-contract.v1", "required_context": "Global control-plane surfaces and validator contracts.", "required_constraints": "Keep public compatibility surfaces stable.", "verification_section": "pytest and py_compile evidence plus artifact checks.", "done_when": "Artifacts validate and closure state is accepted."}],
        "frontend_roundtrip_evidence": [],
        "objective_runtime_state": {
            **json.loads(runtime_state_artifact.read_text(encoding="utf-8")),
            "artifact_path": str(runtime_state_artifact),
        },
        "objective_status": {
            **objective_status_payload,
            "artifact_path": str(objective_status_artifact),
        },
        "objective_summary": {"artifact_path": str(summary_artifact)},
        "validation_plan": {"artifact_path": str(validation_plan_artifact)},
        "execution_coverage": {"artifact_path": str(execution_coverage_artifact)},
        "support_confidence": {
            **json.loads(support_confidence_artifact.read_text(encoding="utf-8")),
            "artifact_path": str(support_confidence_artifact),
        },
        "schedule_artifact": str(schedule_artifact),
        "packet_verdicts": [
            {
                "packet_id": packet_ids[0],
                "strategy_name": "command_capture",
                "runner_kind": "command",
                "runtime_state": "accepted",
                "verifier_output": "accepted",
                "allowed_scope_status": "within_scope",
                "support_status": "supported",
                "unsupported_risk_reason": "",
                "artifact_path": str(verdict_a),
            },
            {
                "packet_id": packet_ids[1],
                "strategy_name": "validation_command",
                "runner_kind": "command",
                "runtime_state": "accepted",
                "verifier_output": "accepted",
                "allowed_scope_status": "within_scope",
                "support_status": "supported",
                "unsupported_risk_reason": "",
                "artifact_path": str(verdict_b),
            },
        ],
        "execution_ledger": str(execution_ledger_artifact),
        "packet_results_artifact": str(packet_results_artifact),
        "checkpoint_commit": checkpoint_payload["checkpoint_commit"],
        "checkpoint_blocked": checkpoint_payload["checkpoint_blocked"],
        "checkpoint_block_reason": checkpoint_payload["checkpoint_block_reason"],
        "checkpoint_block_evidence": checkpoint_payload["checkpoint_block_evidence"],
        "bootstrap_commands": checkpoint_payload["bootstrap_commands"],
        "validation_commands": checkpoint_payload["validation_commands"],
        "clean_state_assertions": checkpoint_payload["clean_state_assertions"],
        "migration_fallback": {"used": False, "reason": "not-needed", "artifact_path": ""},
        "closure_drift_report": {
            "unexpected_modules": [],
            "unexpected_public_apis": [],
            "unexpected_persisted_paths": [],
            "unexpected_enum_values": [],
            "unexpected_state_surfaces": [],
            "repair_boundary_violations": [],
            "read_only_boundary_violations": [],
            "overengineering_tripwires_triggered": []
        },
        "budget_outcome": {
            "planned_files_touched": plan["estimated_files_touched"],
            "planned_loc": plan["estimated_loc"],
            "actual_files_touched": 2,
            "actual_loc": 240,
            "exception_used": False,
            "exception_justification": "not-needed",
            "proof_artifact": budget_artifact,
            "proof_hash": budget_hash,
        },
    }
    return impl, objective_status_payload


def test_plan_contract_accepts_valid_control_plane_payload() -> None:
    result = validate_plan_contract(_plan_payload())
    assert result.missing == []
    assert result.blocked == []


def test_frontloaded_artifacts_preserve_pre_delivery_gap_review() -> None:
    payload = _plan_payload()
    artifacts = build_plan_frontloaded_artifacts(payload, track_id="gap-review-artifacts")
    assert artifacts["gaps"]["pre_delivery_gap_review"] == payload["pre_delivery_gap_review"]
    assert artifacts["sufficiency"]["pre_delivery_gap_review"] == payload["pre_delivery_gap_review"]


def test_plan_contract_requires_solution_ladder_for_r3() -> None:
    payload = _plan_payload()
    payload.pop("solution_ladder", None)
    payload.pop("chosen_layer", None)
    payload.pop("layer_justification", None)
    payload.pop("why_not_lower", None)
    payload.pop("why_not_higher", None)
    payload.pop("future_reuse_gain", None)
    result = validate_plan_contract(payload)
    assert "solution_ladder" in result.missing
    assert "chosen_layer" in result.missing
    assert "future_reuse_gain" in result.missing


def test_plan_contract_requires_execution_shape_for_r3() -> None:
    payload = _plan_payload()
    payload.pop("execution_shape", None)
    result = validate_plan_contract(payload)
    assert "execution_shape" in result.missing


def test_plan_contract_blocks_bounded_swarm_without_lane_policy() -> None:
    payload = _plan_payload()
    payload["execution_shape"] = "bounded_swarm"
    payload["scheduler_policy"].pop("lane_caps", None)
    result = validate_plan_contract(payload)
    assert "scheduler_policy:lane_caps" in result.missing


def test_plan_contract_blocks_underreaching_l1_choice() -> None:
    payload = _plan_payload()
    payload["chosen_layer"] = "L1_patch"
    result = validate_plan_contract(payload)
    assert any(item.startswith("solution_ladder:chosen_layer_below_useful") for item in result.blocked)


def test_plan_contract_allows_r2_without_solution_ladder() -> None:
    payload = _plan_payload()
    payload["session_harness"]["route_hint"] = "R2"
    payload["execution_shape"] = "single_lane"
    payload.pop("solution_ladder", None)
    payload.pop("chosen_layer", None)
    payload.pop("layer_justification", None)
    payload.pop("why_not_lower", None)
    payload.pop("why_not_higher", None)
    payload.pop("future_reuse_gain", None)
    result = validate_plan_contract(payload)
    assert "solution_ladder" not in result.missing
    assert "chosen_layer" not in result.missing


def test_plan_contract_blocks_packet_cycle() -> None:
    payload = _plan_payload()
    packet_a = payload["packets"][0]
    packet_b = payload["packets"][1]
    packet_a["dependency_mode"] = "accepted_upstream"
    packet_a["dependencies"] = [packet_b["packet_id"]]
    packet_a.pop("stub_dependencies", None)
    packet_b["dependency_mode"] = "accepted_upstream"
    packet_b["dependencies"] = [packet_a["packet_id"]]
    packet_b.pop("stub_dependencies", None)
    blocked = validate_plan_contract(payload).blocked
    assert "packets:dependency_cycle" in blocked


def test_plan_contract_blocks_unknown_execution_strategy() -> None:
    payload = _plan_payload()
    payload["packets"][0]["execution_strategy"] = "mystery_strategy"
    blocked = validate_plan_contract(payload).blocked
    assert any("strategy_unknown" in item for item in blocked)


def test_compute_runnable_set_blocks_overlapping_scope() -> None:
    packets = {packet["packet_id"]: packet for packet in _packets()}
    second_packet_id = sorted(packets)[1]
    first_packet_id = sorted(packets)[0]
    packets[second_packet_id]["dependencies"] = []
    packets[second_packet_id]["classification"] = "ready"
    packets[second_packet_id]["allowed_scope"] = list(packets[first_packet_id]["allowed_scope"])
    runnable = compute_runnable_set(
        packets=packets,
        accepted_packets=set(),
        active_packets={first_packet_id},
        retry_counters={},
        max_parallel_packets=2,
        parallelism_policy="bounded_parallel",
    )
    assert runnable == []


def test_compute_runnable_set_blocks_shared_surface_conflict() -> None:
    packets = {packet["packet_id"]: packet for packet in _packets()}
    second_packet_id = sorted(packets)[1]
    first_packet_id = sorted(packets)[0]
    packets[second_packet_id]["dependencies"] = []
    packets[second_packet_id]["classification"] = "ready"
    packets[second_packet_id]["allowed_scope"] = ["verify_plan.py"]
    packets[second_packet_id]["shared_surface_categories"] = ["planning-gate-core"]
    runnable = compute_runnable_set(
        packets=packets,
        accepted_packets=set(),
        active_packets={first_packet_id},
        retry_counters={},
        max_parallel_packets=2,
        parallelism_policy="bounded_parallel",
    )
    assert runnable == []


def test_compute_runnable_set_respects_lane_caps() -> None:
    packets = {packet["packet_id"]: packet for packet in _packets()}
    for packet in packets.values():
        packet["dependencies"] = []
        packet["dependency_mode"] = "explicit_stub"
        packet["stub_dependencies"] = ["runtime"]
        packet["execution_mode"] = "parallel_safe"
        packet["parallelism_class"] = "isolated"
        packet["swarm_eligible"] = True
        packet["packet_lane"] = "validator"
        packet["allowed_scope"] = [f"{packet['packet_id']}.py"]
        packet["shared_surface_categories"] = []
    runnable = compute_runnable_set(
        packets=packets,
        accepted_packets=set(),
        active_packets=set(),
        retry_counters={},
        max_parallel_packets=2,
        parallelism_policy="bounded_parallel",
        execution_shape="bounded_swarm",
        lane_caps={"validator": 1},
        route_swarm_cap=2,
        frontier_dispatch_order=["validator", "worker", "reviewer"],
        reviewer_barrier_points=["closure"],
    )
    assert len(runnable) == 1


def test_compute_runnable_set_holds_reviewer_until_convergence() -> None:
    packets = {packet["packet_id"]: packet for packet in _packets()}
    packet_ids = sorted(packets)
    packets[packet_ids[0]]["dependencies"] = []
    packets[packet_ids[0]]["dependency_mode"] = "explicit_stub"
    packets[packet_ids[0]]["stub_dependencies"] = ["runtime"]
    packets[packet_ids[0]]["execution_mode"] = "parallel_safe"
    packets[packet_ids[0]]["parallelism_class"] = "isolated"
    packets[packet_ids[0]]["swarm_eligible"] = True
    packets[packet_ids[0]]["packet_lane"] = "worker"
    packets[packet_ids[0]]["allowed_scope"] = ["worker.py"]
    packets[packet_ids[0]]["shared_surface_categories"] = []
    packets[packet_ids[1]]["dependencies"] = []
    packets[packet_ids[1]]["dependency_mode"] = "explicit_stub"
    packets[packet_ids[1]]["stub_dependencies"] = ["runtime"]
    packets[packet_ids[1]]["execution_mode"] = "parallel_safe"
    packets[packet_ids[1]]["parallelism_class"] = "bounded"
    packets[packet_ids[1]]["swarm_eligible"] = True
    packets[packet_ids[1]]["packet_lane"] = "reviewer"
    packets[packet_ids[1]]["allowed_scope"] = ["reviewer.json"]
    packets[packet_ids[1]]["shared_surface_categories"] = []
    runnable = compute_runnable_set(
        packets=packets,
        accepted_packets=set(),
        active_packets=set(),
        retry_counters={},
        max_parallel_packets=2,
        parallelism_policy="bounded_parallel",
        execution_shape="bounded_swarm",
        lane_caps={"worker": 2, "reviewer": 1},
        route_swarm_cap=2,
        frontier_dispatch_order=["worker", "reviewer"],
        reviewer_barrier_points=["closure"],
    )
    assert packet_ids[0] in runnable
    assert packet_ids[1] not in runnable


def test_plan_contract_blocks_fallback_packet_without_reason() -> None:
    payload = _plan_payload()
    payload["packets"][0]["execution_strategy"] = "codex_prompt_worker"
    payload["packets"][0]["strategy_inputs"] = {
        "worker_goal": "Do the work.",
        "prompt_contract_ref": "proxy-runtime-closeout.v1",
        "expected_artifacts": ["packet-compiler.review.json"],
    }
    payload["packets"][0].pop("fallback_reason", None)
    result = validate_plan_contract(payload)
    assert any("fallback_reason" in item for item in result.missing)


def test_repo_capability_discovery_reads_package_json(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "test": "vitest",
                    "lint": "eslint .",
                    "typecheck": "tsc --noEmit",
                    "build": "vite build",
                    "test:e2e": "playwright test",
                }
            }
        ),
        encoding="utf-8",
    )
    payload = discover_repo_capabilities(cwd=str(tmp_path))
    assert payload["capabilities"]["tests"] is True
    assert payload["capabilities"]["smoke_e2e"] is True
    assert "npm run lint" in payload["lane_commands"]["lint"]


def test_repo_capability_discovery_reads_python_project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.pytest.ini_options]
addopts = "-q"

[tool.mypy]
python_version = "3.11"

[tool.ruff]
line-length = 100
""".strip()
        + "\n",
        encoding="utf-8",
    )
    payload = discover_repo_capabilities(cwd=str(tmp_path))
    assert payload["capabilities"]["tests"] is True
    assert payload["capabilities"]["typecheck"] is True
    assert payload["capabilities"]["lint"] is True
    assert "python -m pytest" in payload["lane_commands"]["tests"]


def test_repo_capability_discovery_reads_python_aux_configs_and_workflow_yaml(tmp_path: Path) -> None:
    (tmp_path / "pyrightconfig.json").write_text('{"typeCheckingMode":"strict"}\n', encoding="utf-8")
    (tmp_path / "noxfile.py").write_text("import nox\n@nox.session\ndef tests(session):\n    session.run('pytest')\n", encoding="utf-8")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "main.yaml").write_text(
        """
jobs:
  checks:
    steps:
      - run: make lint
      - run: pyright
""".strip()
        + "\n",
        encoding="utf-8",
    )
    payload = discover_repo_capabilities(cwd=str(tmp_path))
    assert payload["capabilities"]["tests"] is True
    assert payload["capabilities"]["typecheck"] is True
    assert payload["capabilities"]["lint"] is True
    assert any("main.yaml" in item for item in payload["source_refs"]["lint"])


def test_repo_capability_discovery_reads_ci_and_migration_layout(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text(
        """
jobs:
  test:
    steps:
      - run: python -m pytest
      - run: python -m ruff check .
      - run: python manage.py makemigrations --check --dry-run
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "migrations").mkdir()
    payload = discover_repo_capabilities(cwd=str(tmp_path))
    assert payload["capabilities"]["tests"] is True
    assert payload["capabilities"]["lint"] is True
    assert payload["capabilities"]["schema_check"] is True
    assert any("pytest" in command for command in payload["lane_commands"]["tests"])


def test_repo_capability_discovery_reads_ui_config_without_package_scripts(tmp_path: Path) -> None:
    (tmp_path / "vitest.config.ts").write_text("export default {}\n", encoding="utf-8")
    (tmp_path / "vite.config.ts").write_text("export default {}\n", encoding="utf-8")
    (tmp_path / "playwright.config.ts").write_text("export default {}\n", encoding="utf-8")
    payload = discover_repo_capabilities(cwd=str(tmp_path))
    assert payload["capabilities"]["tests"] is True
    assert payload["capabilities"]["build"] is True
    assert payload["capabilities"]["smoke_e2e"] is True
    assert "npx vitest run" in payload["lane_commands"]["tests"]
    assert "npx vite build" in payload["lane_commands"]["build"]
    assert "npx playwright test" in payload["lane_commands"]["smoke_e2e"]


def test_repo_capability_discovery_reads_workspace_package_manifests(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"packageManager": "pnpm@9.0.0", "workspaces": ["packages/*"]}),
        encoding="utf-8",
    )
    package_dir = tmp_path / "packages" / "web"
    package_dir.mkdir(parents=True)
    (package_dir / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "test": "vitest",
                    "lint": "eslint .",
                    "typecheck": "tsc --noEmit",
                    "build": "vite build",
                    "test:e2e": "playwright test",
                }
            }
        ),
        encoding="utf-8",
    )
    payload = discover_repo_capabilities(cwd=str(tmp_path))
    assert payload["capabilities"]["tests"] is True
    assert payload["capabilities"]["lint"] is True
    assert payload["capabilities"]["typecheck"] is True
    assert payload["capabilities"]["build"] is True
    assert payload["capabilities"]["smoke_e2e"] is True
    assert any(command.startswith("pnpm --filter ./packages/web run test") for command in payload["lane_commands"]["tests"])
    assert any("packages/web/package.json" in ref for ref in payload["source_refs"]["tests"])


def test_validation_plan_uses_pipeline_for_multi_command_lane(tmp_path: Path) -> None:
    plan = _plan_payload()
    plan["tests"]["unit"] = ["python3.11 -m pytest tests/unit"]
    plan["tests"]["integration"] = ["python3.11 -m pytest tests/integration"]
    plan["tests"]["regression"] = []
    repo_capabilities = discover_repo_capabilities(cwd=str(tmp_path))
    payload = build_repo_validation_plan(
        plan,
        track_id="track-pipeline",
        cwd=str(tmp_path),
        repo_capabilities=repo_capabilities,
    )
    tests_packet = next(packet for packet in payload["generated_packets"] if packet["packet_id"] == "packet-validation-tests")
    assert tests_packet["execution_strategy"] == "multi_command_pipeline"
    assert tests_packet["strategy_inputs"]["commands"] == [
        "python3.11 -m pytest tests/unit",
        "python3.11 -m pytest tests/integration",
    ]


def test_packet_quality_budget_flags_fallback_overuse() -> None:
    packets = _packets()
    packets[0]["execution_strategy"] = "codex_prompt_worker"
    packets[0]["fallback_reason"] = "no_deterministic_runner"
    packets[0]["support_expectations"] = {
        "expected_evidence_artifacts": ["packet-compiler.review.json"],
        "support_kind": "fallback_artifacts",
    }
    packets[0]["external_support_required"] = True
    packets[0]["support_remediation_mode"] = "fallback_rework"
    report = build_packet_quality_report(plan=_plan_payload(), route_hint="R4", packets=packets)
    assert report["budget"]["status"] == "hard_fail"


def test_packet_quality_requires_adaptation_metadata_when_alternates_exist() -> None:
    packets = _packets()
    packets[0]["alternate_strategies"] = ["multi_command_pipeline"]
    packets[0].pop("adaptation_policy", None)
    packets[0].pop("max_adaptations", None)
    report = build_packet_quality_report(plan=_plan_payload(), route_hint="R3", packets=packets)
    row = next(item for item in report["rows"] if item["packet_id"] == packets[0]["packet_id"])
    assert "adaptation_policy_missing" in row["hard_fail_checks"]
    assert "max_adaptations_missing" in row["hard_fail_checks"]


def test_bootstrap_runtime_writes_repo_capability_and_quality_artifacts(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "planning_artifacts"
    plan = _plan_payload()
    _prepare_plan_artifacts(plan, artifacts_root, "track-bootstrap-artifacts")
    runtime_paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id="track-bootstrap-artifacts")
    assert runtime_paths["repo_capabilities"].exists()
    assert runtime_paths["packet_quality"].exists()
    assert runtime_paths["execution_coverage"].exists()
    assert runtime_paths["support_confidence"].exists()
    quality = json.loads(runtime_paths["packet_quality"].read_text(encoding="utf-8"))
    coverage = json.loads(runtime_paths["execution_coverage"].read_text(encoding="utf-8"))
    assert quality["schema_version"] == "objective-packet-quality.v1"
    assert coverage["schema_version"] == "objective-execution-coverage.v1"


def test_impl_contract_accepts_valid_objective_artifacts(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "planning_artifacts"
    plan = _plan_payload()
    impl, _ = _implementation_payload(artifacts_root, "track-1")
    result = validate_impl_contract(impl, artifacts_root=artifacts_root, track_id="track-1", plan=plan)
    assert result.missing == []
    assert result.blocked == []
    assert result.objective_closure_state == "OBJECTIVE_COMPLETE"
    assert result.accepted_type == "ACCEPTED_SUCCESS"


def test_impl_contract_requires_migration_fallback_for_migration_defect(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "planning_artifacts"
    plan = _plan_payload()
    impl, objective_status = _implementation_payload(artifacts_root, "track-2")
    impl["objective_status"]["closure_state"] = "OBJECTIVE_BLOCKED_MIGRATION_DEFECT"
    objective_status["closure_state"] = "OBJECTIVE_BLOCKED_MIGRATION_DEFECT"
    _write_json(Path(impl["objective_status"]["artifact_path"]), objective_status)
    runtime_state_path = Path(impl["objective_runtime_state"]["artifact_path"])
    runtime_state = json.loads(runtime_state_path.read_text(encoding="utf-8"))
    runtime_state["closure_state"] = "OBJECTIVE_BLOCKED_MIGRATION_DEFECT"
    runtime_state["stop_allowed"] = True
    runtime_state["lifecycle_status"] = "approved"
    _write_json(runtime_state_path, runtime_state)
    impl["objective_runtime_state"] = {**runtime_state, "artifact_path": str(runtime_state_path)}
    impl["migration_fallback"] = {"used": False, "reason": "not-recorded", "artifact_path": ""}

    result = validate_impl_contract(impl, artifacts_root=artifacts_root, track_id="track-2", plan=plan)
    assert "implementation:migration_fallback:required_for_blocked_migration_defect" in result.missing
