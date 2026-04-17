#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
import sys

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import CAPTURE_PRODUCER, CAPTURE_SCHEMA_VERSION, runtime_artifact_paths, session_artifact_paths, sha256_file, stable_objective_id  # noqa: E402
from compile_intent import compile_intent_payload  # noqa: E402
from compile_plan import compile_plan_payload  # noqa: E402
from finalize_gate import finalize_decision  # noqa: E402
from initialize_session import initialize_session_payload  # noqa: E402
from objective_runtime import bootstrap_runtime  # noqa: E402
from objective_scheduler import build_schedule  # noqa: E402
from score_progress import update_progress  # noqa: E402
from verify_plan import verify_plan_payload  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _manifest(root: Path, track_id: str, name: str, *, stage: str = "100%") -> tuple[str, str]:
    path = root / track_id / "captures" / name
    path.mkdir(parents=True, exist_ok=True)
    stdout = path / "stdout.redacted.txt"
    stderr = path / "stderr.redacted.txt"
    stdout.write_text("ok\n", encoding="utf-8")
    stderr.write_text("\n", encoding="utf-8")
    manifest = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "producer": CAPTURE_PRODUCER,
        "track_id": track_id,
        "stage": stage,
        "name": name,
        "command_argv": ["echo", "ok"],
        "exit_code": 0,
        "stdout_path": str(stdout),
        "stderr_path": str(stderr),
    }
    manifest_path = path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(manifest_path), sha256_file(manifest_path)


def _impl_valid(root: Path, track_id: str, plan: dict) -> dict:
    compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
    initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
    compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
    verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
    bootstrap_runtime(plan=plan, track_id=track_id, artifacts_root=root)
    packets = {packet["packet_id"]: packet for packet in plan["packets"]}
    packet_ids = list(plan["required_packets"])
    runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
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
    objective_status_artifact.write_text(json.dumps(objective_status_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
    schedule_artifact.write_text(json.dumps(schedule_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    runtime_paths["summary"].write_text(
        json.dumps(
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
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    runtime_paths["validation_plan"].write_text(
        json.dumps(
            {
                "schema_version": "objective-validation-plan.v1",
                "objective_id": stable_objective_id(track_id),
                "track_id": track_id,
                "route_hint": "R3",
                "workspace_root": str(root),
                "changed_files": ["compile_plan.py", "verify_plan.py"],
                "required_packets": packet_ids,
                "lanes": [
                    {
                        "lane": "tests",
                        "required": True,
                        "reasons": ["code path touched"],
                        "paths": ["compile_plan.py"],
                        "commands": ["pytest"],
                        "generated_packet_ids": [],
                        "manual_only_blocker": "",
                        "missing_capability_reason": "",
                        "risk_trigger": "",
                    }
                ],
                "generated_packets": [],
                "coverage": {
                    "required_lane_count": 1,
                    "generated_lane_count": 0,
                    "manual_blocker_count": 0,
                },
                "escalated_review_required": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    runtime_paths["execution_ledger"].write_text(
        json.dumps(
            {
                "schema_version": "objective-execution-ledger.v1",
                "objective_id": stable_objective_id(track_id),
                "track_id": track_id,
                "packets": [
                    {
                        "packet_id": packet_id,
                        "strategy_name": "command_capture" if packet_id != "packet-verifier" else "validation_command",
                        "fallback_used": False,
                        "evidence_destination": f"planning_artifacts/{track_id}/packets/{packet_id}.verdict.json",
                        "runtime_state": "accepted",
                        "verifier_output": "accepted",
                        "retry_counters": {},
                        "latest_result_artifact": f"{packet_id}.result.json",
                        "support_status": "supported",
                        "unsupported_risk_reason": "",
                    }
                    for packet_id in packet_ids
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    runtime_paths["execution_coverage"].write_text(
        json.dumps(
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
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    verdict_paths = []
    verdict_dir = root / "packets"
    verdict_dir.mkdir(parents=True, exist_ok=True)
    for packet_id in packet_ids:
        verdict_path = verdict_dir / f"{packet_id}.verdict.json"
        verdict_path.write_text(
            json.dumps(
                {
                    "packet_id": packet_id,
                    "status": "accepted",
                    "support_status": "supported",
                    "unsupported_risk_reason": "",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        verdict_paths.append((packet_id, verdict_path))
    runtime_paths["packet_results"].write_text(
        "\n".join(
            json.dumps(
                {
                    "packet_id": packet_id,
                    "strategy_name": "command_capture" if packet_id != "packet-verifier" else "validation_command",
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
    session_paths = session_artifact_paths(artifacts_root=root, track_id=track_id)
    feature_payload = json.loads(session_paths["feature_list"].read_text(encoding="utf-8"))
    for item in feature_payload["features"]:
        item["status"] = "verified"
        item["evidence_refs"] = [str(path) for _, path in verdict_paths]
    session_paths["feature_list"].write_text(
        json.dumps(feature_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
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
            "rollback_validation_ref": str(root / track_id / "captures" / "rollback-checkpoint" / "manifest.json"),
        }
    )
    session_paths["checkpoint"].write_text(
        json.dumps(checkpoint_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
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
    runtime_paths["support_confidence"].write_text(
        json.dumps(
            {
                "schema_version": "objective-support-confidence.v1",
                "objective_id": stable_objective_id(track_id),
                "track_id": track_id,
                "mode": "enforce",
                "packet_support": [
                    {
                        "packet_id": packet_id,
                        "support_status": "supported",
                        "unsupported_risk_reason": "",
                    }
                    for packet_id in packet_ids
                ],
                "objective_support_status": "supported",
                "unsupported_closure_risk": "none",
                "support_gap_reasons": [],
                "support_remediation_available": False,
                "support_backed_closure": True,
                "external_support_coverage": {
                    "required_validation_lanes": 0,
                    "satisfied_validation_lanes": 0,
                    "accepted_packets": len(packet_ids),
                    "supported_packets": len(packet_ids),
                },
                "final_gate_recommendation": "allow_closure",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    runtime_paths["runtime_state"].write_text(
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
        "summary": "ok",
        "changed_files": ["a", "b", "c"],
        "tests_run": [],
        "smoke_results": [],
        "logging_evidence": [],
        "rollback_validation": {},
        "memory_retrieval_evidence": [
            {"tool": "mcp__codex-mem__build_context", "query": "planning gate", "result_count": 1}
        ],
        "preferences_applied": [
            {"key": "pref:governed-control-plane", "decision": "deterministic scheduler", "rationale": "Matches policy-first execution rules."}
        ],
        "skill_trigger_eval_results": [
            {"skill": "planning-gate", "false_positive_rate": 0.0, "false_negative_rate": 0.0, "threshold_passed": True}
        ],
        "prompt_contract_used": [
            {
                "name": "control-plane-contract.v1",
                "required_context": "Global planning-gate and postflight surfaces.",
                "required_constraints": "Keep public compatibility surfaces stable.",
                "verification_section": "pytest and artifact-backed contract validation.",
                "done_when": "Objective closure is verifier-accepted with machine-readable artifacts.",
            }
        ],
        "frontend_roundtrip_evidence": [],
        "objective_runtime_state": {
            **json.loads(runtime_paths["runtime_state"].read_text(encoding="utf-8")),
            "artifact_path": str(runtime_paths["runtime_state"]),
        },
        "objective_status": {**objective_status_payload, "artifact_path": str(objective_status_artifact)},
        "objective_summary": {"artifact_path": str(runtime_paths["summary"])},
        "validation_plan": {"artifact_path": str(runtime_paths["validation_plan"])},
        "schedule_artifact": str(schedule_artifact),
        "support_confidence": {
            **json.loads(runtime_paths["support_confidence"].read_text(encoding="utf-8")),
            "artifact_path": str(runtime_paths["support_confidence"]),
        },
        "execution_ledger": str(runtime_paths["execution_ledger"]),
        "execution_coverage": {"artifact_path": str(runtime_paths["execution_coverage"])},
        "packet_results_artifact": str(runtime_paths["packet_results"]),
        "packet_verdicts": [
            {
                "packet_id": packet_id,
                "runtime_state": "accepted",
                "verifier_output": "accepted",
                "allowed_scope_status": "within_scope",
                "artifact_path": str(verdict_path),
                "strategy_name": "command_capture" if packet_id != "packet-verifier" else "validation_command",
                "runner_kind": "command",
                "support_status": "supported",
                "unsupported_risk_reason": "",
            }
            for packet_id, verdict_path in verdict_paths
        ],
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
            "overengineering_tripwires_triggered": [],
        },
        "budget_outcome": {
            "planned_files_touched": plan["estimated_files_touched"],
            "planned_loc": plan["estimated_loc"],
            "actual_files_touched": 3,
            "actual_loc": 240,
            "exception_used": False,
            "exception_justification": "not-needed",
            "proof_artifact": "",
            "proof_hash": "",
        },
    }

    for stage in ["25%", "50%", "75%", "100%"]:
        proof_path, proof_hash = _manifest(root, track_id, f"smoke-{stage.replace('%', '')}", stage=stage)
        impl["smoke_results"].append(
            {
                "stage": stage,
                "status": "pass",
                "command": "echo ok",
                "observed_output": "ok",
                "decision": "continue",
                "proof_artifact": proof_path,
                "proof_hash": proof_hash,
            }
        )

    for name, result in [("worker-runtime-cycle-001", "worker complete"), ("verifier-runtime-cycle-001", "verifier accepted"), ("test-unit", "all pass")]:
        proof_path, proof_hash = _manifest(root, track_id, name)
        impl["tests_run"].append(
            {
                "name": name,
                "command": "python -m unittest",
                "status": "pass",
                "result": result,
                "proof_artifact": proof_path,
                "proof_hash": proof_hash,
            }
        )

    proof_path, proof_hash = _manifest(root, track_id, "log-runtime-cycle-001")
    impl["logging_evidence"].append(
        {
            "event": "phase checkpoint",
            "proof_artifact": proof_path,
            "proof_hash": proof_hash,
        }
    )

    proof_path, proof_hash = _manifest(root, track_id, "rollback-checkpoint", stage="rollback")
    impl["rollback_validation"] = {
        "executed": True,
        "result": "pass",
        "evidence": "rollback verified",
        "proof_artifact": proof_path,
        "proof_hash": proof_hash,
    }
    proof_path, proof_hash = _manifest(root, track_id, "budget-diffstat", stage="75%")
    impl["budget_outcome"]["proof_artifact"] = proof_path
    impl["budget_outcome"]["proof_hash"] = proof_hash
    return impl


class FinalizeGateTests(unittest.TestCase):
    def test_forged_approve_review_is_rejected(self) -> None:
        plan = _fixture("plan_valid.json")
        review = _fixture("review_approve.json")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            impl = _impl_valid(root, "forged", plan)
            impl["smoke_results"][-1]["status"] = "fail"
            result = finalize_decision(
                plan_payload=plan,
                impl_payload=impl,
                review_payload=review,
                artifacts_root=root,
                track_id="forged",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["review_status_trusted"], False)

    def test_happy_path_returns_ok_true(self) -> None:
        plan = _fixture("plan_valid.json")
        review = _fixture("review_approve.json")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            impl = _impl_valid(root, "happy", plan)
            result = finalize_decision(
                plan_payload=plan,
                impl_payload=impl,
                review_payload=review,
                artifacts_root=root,
                track_id="happy",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "approve")

    def test_finalize_accepts_raw_track_id_with_normalization(self) -> None:
        plan = _fixture("plan_valid.json")
        review = _fixture("review_approve.json")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            impl = _impl_valid(root, "task-123", plan)
            result = finalize_decision(
                plan_payload=plan,
                impl_payload=impl,
                review_payload=review,
                artifacts_root=root,
                track_id="task 123",
            )

        self.assertTrue(result["ok"])

    def test_finalize_blocks_runtime_state_closure_mismatch(self) -> None:
        plan = _fixture("plan_valid.json")
        review = _fixture("review_approve.json")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            impl = _impl_valid(root, "runtime-mismatch", plan)
            runtime_state_path = runtime_artifact_paths(artifacts_root=root, track_id="runtime-mismatch")["runtime_state"]
            runtime_state = json.loads(runtime_state_path.read_text(encoding="utf-8"))
            runtime_state["closure_state"] = "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED"
            runtime_state_path.write_text(json.dumps(runtime_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            impl["objective_runtime_state"] = {**runtime_state, "artifact_path": str(runtime_state_path)}
            result = finalize_decision(
                plan_payload=plan,
                impl_payload=impl,
                review_payload=review,
                artifacts_root=root,
                track_id="runtime-mismatch",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("implementation:objective_runtime_state:closure_state_mismatch", result["blocked_fields"])

    def test_finalize_blocks_unapproved_budget_drift(self) -> None:
        plan = _fixture("plan_valid.json")
        review = _fixture("review_approve.json")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            impl = _impl_valid(root, "budget-drift", plan)
            impl["budget_outcome"]["actual_files_touched"] = 4
            impl["budget_outcome"]["actual_loc"] = 1200
            result = finalize_decision(
                plan_payload=plan,
                impl_payload=impl,
                review_payload=review,
                artifacts_root=root,
                track_id="budget-drift",
            )

        self.assertFalse(result["ok"])
        self.assertIn("implementation:budget_outcome:unapproved_budget_drift", result["blocked_fields"])

    def test_finalize_blocks_closure_drift(self) -> None:
        plan = _fixture("plan_valid.json")
        review = _fixture("review_approve.json")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            impl = _impl_valid(root, "closure-drift", plan)
            impl["closure_drift_report"]["unexpected_public_apis"] = ["devsup.hidden.repair"]
            result = finalize_decision(
                plan_payload=plan,
                impl_payload=impl,
                review_payload=review,
                artifacts_root=root,
                track_id="closure-drift",
            )

        self.assertFalse(result["ok"])
        self.assertIn(
            "implementation:closure_drift_report:unexpected_public_apis",
            result["blocked_fields"],
        )

    def test_finalize_blocks_when_runtime_state_stop_not_allowed(self) -> None:
        plan = _fixture("plan_valid.json")
        review = _fixture("review_approve.json")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            impl = _impl_valid(root, "runtime-stop-block", plan)
            runtime_state_path = runtime_artifact_paths(artifacts_root=root, track_id="runtime-stop-block")["runtime_state"]
            runtime_state = json.loads(runtime_state_path.read_text(encoding="utf-8"))
            runtime_state["stop_allowed"] = False
            runtime_state["required_work_remaining"] = True
            runtime_state["lifecycle_status"] = "revise"
            runtime_state["stop_reason"] = "required_validation_missing"
            runtime_state_path.write_text(json.dumps(runtime_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            impl["objective_runtime_state"] = {**runtime_state, "artifact_path": str(runtime_state_path)}
            result = finalize_decision(
                plan_payload=plan,
                impl_payload=impl,
                review_payload=review,
                artifacts_root=root,
                track_id="runtime-stop-block",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "implementation:objective_runtime_state:stop_not_allowed",
            result["blocked_fields"],
        )

    def test_concurrent_progress_updates_share_lock_safely(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            results = []
            errors = []

            def worker() -> None:
                try:
                    summary = update_progress(
                        artifacts_root=root,
                        track_id="concurrent-track",
                        kind="plan",
                        score=8,
                        passed_100=False,
                        stall_limit=2,
                        ttl_hours=24,
                    )
                    results.append(summary)
                except Exception as exc:  # pragma: no cover - assertion path
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(len(results), 6)
            max_loop = max(item["loop"] for item in results)
            self.assertEqual(max_loop, 6)

    def test_track_id_mismatch_is_blocked(self) -> None:
        plan = _fixture("plan_valid.json")
        review = _fixture("review_approve.json")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            impl = _impl_valid(root, "expected-track", plan)
            result = finalize_decision(
                plan_payload=plan,
                impl_payload=impl,
                review_payload=review,
                artifacts_root=root,
                track_id="different-track",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("proof_track_id_mismatch" in item for item in result["blocked_fields"]))


if __name__ == "__main__":
    unittest.main()
