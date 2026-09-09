#!/usr/bin/env python3
"""Reusable benchmark and canary harness for governed swarm evaluation."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from common import (
    load_json_file,
    now_iso,
    resolve_artifacts_root,
    runtime_artifact_paths,
    session_artifact_paths,
    write_json_file,
)
from compile_intent import compile_intent_payload
from compile_plan import compile_plan_payload
from initialize_session import initialize_session_payload
from objective_runtime import _sync_operator_view, bootstrap_runtime, run_runtime
from verify_plan import verify_plan_payload


SCRIPT_DIR = Path(__file__).resolve().parent
TESTS_DIR = SCRIPT_DIR.parent / "tests"
FIXTURE_PLAN_PATH = TESTS_DIR / "fixtures" / "plan_valid.json"

BENCHMARK_SCHEMA_VERSION = "swarm-benchmark-report.v1"
CANARY_SCHEMA_VERSION = "swarm-canary-result.v1"
BENCHMARK_MODES = ("serial_only", "bounded_parallel", "bounded_swarm")
ARCHETYPES = ("service", "ui", "migration", "mixed", "rubik_3d")
UNSAFE_CANARY_PATTERNS = ("auth", "billing", "secret", "token", "credential", "payment", "migration", "rollback")


def _benchmark_contract(archetype: str) -> dict[str, Any]:
    base = {
        "schema_version": "swarm-benchmark-contract.v1",
        "benchmark_id": archetype,
        "task_class": "governed_runtime_benchmark",
    }
    if archetype == "rubik_3d":
        return {
            **base,
            "benchmark_id": "rubik_3d_self_solve",
            "title": "3D Rubik cube self-solving application",
            "goal": "Build a browser-based 3D Rubik cube app with deterministic scramble, self-solve, solve animation, and reset.",
            "required_features": [
                "interactive 3D cube viewport",
                "deterministic scramble action",
                "self-solve from current cube state",
                "visible solve animation",
                "reset to solved state",
                "unit-test evidence for cube state and solver behavior",
                "browser smoke evidence for app load, scramble, solve, and reset",
            ],
            "non_goals": [
                "backend or persistence",
                "multiplayer collaboration",
                "speedcubing analytics",
                "account system",
                "deployment work",
                "open-ended visual polish",
            ],
            "required_evidence": [
                "solver/state validation evidence",
                "browser/smoke interaction evidence",
                "bounded implementation artifacts within declared scope",
            ],
            "pressure_dimensions": [
                "ui_logic_solver_integration",
                "incremental_verification_pressure",
                "scope_boundary_discipline",
                "repair_after_validation_failure",
                "closure_without_overbuilding",
            ],
            "scoring_dimensions": [
                "closure_correctness",
                "evidence_quality",
                "cycles_to_closure",
                "repair_discipline",
                "support_confidence_cleanliness",
                "wall_clock_time",
            ],
        }
    return {
        **base,
        "benchmark_id": archetype,
        "title": f"{archetype} governed swarm benchmark",
        "goal": f"Exercise planning-gate runtime behavior for the {archetype} archetype.",
        "required_features": [],
        "non_goals": [],
        "required_evidence": [],
        "pressure_dimensions": [],
        "scoring_dimensions": [
            "closure_correctness",
            "evidence_quality",
            "cycles_to_closure",
            "wall_clock_time",
        ],
    }


def _base_plan_fixture() -> dict[str, Any]:
    plan = json.loads(FIXTURE_PLAN_PATH.read_text(encoding="utf-8"))
    plan["tests"]["unit"] = []
    plan["tests"]["integration"] = []
    plan["tests"]["regression"] = []
    plan["session_harness"]["validation_commands"] = []
    for gate in plan["tests"]["smoke_gates"]:
        gate["commands"] = []
    return plan


def _make_packet(
    *,
    base_packet: dict[str, Any],
    packet_id: str,
    lane: str,
    parallelism_class: str,
    allowed_scope: list[str],
    execution_strategy: str,
    execution_mode: str = "parallel_safe",
    dependencies: list[str] | None = None,
    dependency_mode: str = "explicit_stub",
    file_write_content: str | None = None,
    review_output: str = "accepted",
    evidence_refs: list[str] | None = None,
    produced_artifacts: list[str] | None = None,
) -> dict[str, Any]:
    packet = copy.deepcopy(base_packet)
    packet["packet_id"] = packet_id
    packet["packet_lane"] = lane
    packet["parallelism_class"] = parallelism_class
    packet["execution_mode"] = execution_mode
    packet["allowed_scope"] = allowed_scope
    packet["dependencies"] = dependencies or []
    packet["dependency_mode"] = dependency_mode
    packet["shared_surface_categories"] = [packet_id]
    packet["execution_strategy"] = execution_strategy
    packet["evidence_destination"] = f"planning_artifacts/<track-id>/packets/{packet_id}.verdict.json"
    packet["definition_of_done"]["allowed_scope"] = allowed_scope
    packet["definition_of_done"]["objective_linkage"] = f"req-{packet_id}"
    packet["classification"] = "ready"
    packet["product_meaning_resolved"] = True
    packet["automatable_acceptance"] = True
    packet["prohibited_action_required"] = False
    packet["maintainable_completion_path"] = True
    if dependency_mode == "accepted_upstream":
        packet["stub_dependencies"] = []

    packet_class = "implementation"
    acceptance_checks = ["packet artifacts prove the required work completed"]
    failure_signals = [f"{packet_id} failed"]
    constraints = ["Stay within declared scope."]
    fallback_or_rollback = "Stop and preserve evidence."
    support_expectations: dict[str, Any] = {
        "expected_evidence_artifacts": [f"{packet_id}.evidence.json"],
        "support_kind": "packet_evidence",
    }
    external_support_required = False
    support_remediation_mode = ""
    strategy_inputs: dict[str, Any]

    if lane == "validator":
        packet_class = "validation"
        acceptance_checks = [f"{packet_id} capture manifests prove the validation lane passes"]
        failure_signals = [f"{packet_id} validation failed"]
        constraints = ["Validation only; no source edits."]
        fallback_or_rollback = "Stop on failed validation and preserve evidence."
        support_expectations = {
            "expected_evidence_artifacts": ["validation capture manifests"],
            "support_kind": "validation_lane",
        }
        external_support_required = True
        support_remediation_mode = "validation_packet"
    elif lane == "reviewer":
        packet_class = "review"
        acceptance_checks = ["review evidence emitted"]
        failure_signals = ["review evidence missing"]
        constraints = ["Review and evidence only; no source edits."]
        fallback_or_rollback = "Escalate with explicit blocker evidence."
        support_expectations = {
            "expected_evidence_artifacts": [f"{packet_id}.review.json"],
            "support_kind": "review_evidence",
        }
        external_support_required = True
        support_remediation_mode = "review_evidence_packet"
    elif execution_strategy == "multi_command_pipeline":
        packet_class = "validation" if lane == "validator" else "implementation"
        acceptance_checks = [f"{packet_id} step evidence proves the pipeline completed successfully"]
        failure_signals = [f"{packet_id} pipeline failed"]
        constraints = ["Preserve per-step evidence for each command."]
        fallback_or_rollback = "Stop on failed pipeline step and preserve evidence."
        support_expectations = {
            "required_step_evidence": [f"command://{packet_id}:pipeline:{idx}" for idx in range(1, 3)],
            "support_kind": "validation_pipeline" if lane == "validator" else "pipeline_execution",
        }
        external_support_required = True
        support_remediation_mode = "validation_packet" if lane == "validator" else "packet_rework"
    if execution_strategy == "schema_check_command" or any(str(path).endswith(".sql") for path in allowed_scope):
        fallback_or_rollback = "Stop on failed schema validation, preserve evidence, and require rollback evidence before closure."
    if execution_strategy == "multi_command_pipeline":
        support_expectations = {
            **support_expectations,
            "required_step_evidence": [f"command://{packet_id}:pipeline:{idx}" for idx in range(1, 3)],
            "support_kind": "validation_pipeline" if lane == "validator" else "pipeline_execution",
        }

    if execution_strategy == "multi_command_pipeline":
        strategy_inputs = {
            "commands": [
                f'python3.11 -c "print(\'{packet_id}-step1\')"',
                f'python3.11 -c "print(\'{packet_id}-step2\')"',
            ],
            "cwd": str(Path.cwd()),
        }
    elif execution_strategy == "review_evidence_packet":
        strategy_inputs = {
            "review_focus": f"review {packet_id}",
            "expected_artifacts": [f"{packet_id}.review.json"],
        }
    elif execution_strategy == "validation_command":
        strategy_inputs = {
            "command": f'python3.11 -c "print(\'{packet_id}-validation\')"',
            "commands": [f'python3.11 -c "print(\'{packet_id}-validation\')"'],
            "validation_lane": packet_id.replace("packet-", ""),
            "cwd": str(Path.cwd()),
        }
    elif execution_strategy in {"lint_command", "typecheck_command", "build_command", "smoke_command", "schema_check_command", "test_command"}:
        strategy_inputs = {
            "command": f'python3.11 -c "print(\'{packet_id}-run\')"',
            "commands": [f'python3.11 -c "print(\'{packet_id}-run\')"'],
            "validation_lane": packet_id.replace("packet-", ""),
            "test_lane": packet_id.replace("packet-", "") if execution_strategy == "test_command" else "",
            "cwd": str(Path.cwd()),
        }
    else:
        strategy_inputs = {
            "command": f'python3.11 -c "print(\'{packet_id}-run\')"',
            "cwd": str(Path.cwd()),
        }

    packet["packet_class"] = packet_class
    packet["acceptance_checks"] = acceptance_checks
    packet["failure_signals"] = failure_signals
    packet["constraints"] = constraints
    packet["fallback_or_rollback"] = fallback_or_rollback
    packet["support_expectations"] = support_expectations
    packet["external_support_required"] = external_support_required
    packet["support_remediation_mode"] = support_remediation_mode
    packet["strategy_inputs"] = strategy_inputs
    packet["definition_of_done"]["acceptance_checks"] = acceptance_checks
    packet["definition_of_done"]["evidence_requirements"] = support_expectations.get("expected_evidence_artifacts") or support_expectations.get("required_step_evidence") or ["packet evidence"]
    packet["definition_of_done"]["rollback_or_fallback"] = fallback_or_rollback
    packet["definition_of_done"]["verifier_acceptance_condition"] = acceptance_checks[0]
    attempt: dict[str, Any] = {
        "worker_exit_code": 0,
        "stdout": f"{packet_id} complete",
        "review_output": review_output,
        "allowed_scope_status": "within_scope",
        "changed_files": allowed_scope,
        "evidence_refs": evidence_refs or [f"capture://{packet_id}"],
        "captured_commands": [{"command": f"echo {packet_id}", "exit_code": 0}],
        "result_artifact_path": f"planning_artifacts/<track-id>/cycles/{packet_id}.json",
    }
    if produced_artifacts is not None:
        attempt["produced_artifacts"] = produced_artifacts
    if file_write_content is not None and allowed_scope:
        attempt["file_writes"] = [{"path": allowed_scope[0], "content": file_write_content}]
    if execution_strategy == "multi_command_pipeline":
        attempt["step_results"] = [
            {"command": f"echo {packet_id}-step1", "exit_code": 0, "evidence_ref": f"capture://{packet_id}-step1"},
            {"command": f"echo {packet_id}-step2", "exit_code": 0, "evidence_ref": f"capture://{packet_id}-step2"},
        ]
        attempt["captured_commands"] = [
            {"command": f"echo {packet_id}-step1", "exit_code": 0},
            {"command": f"echo {packet_id}-step2", "exit_code": 0},
        ]
    if execution_strategy == "review_evidence_packet":
        attempt["produced_artifacts"] = [f"{packet_id}.review.json"]
        attempt["evidence_refs"] = [f"artifact://{packet_id}.review.json"]
    packet["simulation"] = {"attempts": [attempt]}
    return packet


def _configure_mode(plan: dict[str, Any], *, archetype: str, mode: str) -> None:
    scheduler_policy = plan.setdefault("scheduler_policy", {})
    if archetype == "migration":
        plan["route_hint"] = "R4"
        plan["session_harness"]["route_hint"] = "R4"
    else:
        plan["route_hint"] = "R3"
        plan["session_harness"]["route_hint"] = "R3"
    if mode == "serial_only":
        plan["execution_shape"] = "single_lane"
        scheduler_policy["execution_shape"] = "single_lane"
        scheduler_policy["parallelism_policy"] = "serial_only"
        scheduler_policy["max_parallel_packets"] = 1
    elif mode == "bounded_parallel":
        plan["execution_shape"] = "single_lane"
        scheduler_policy["execution_shape"] = "single_lane"
        scheduler_policy["parallelism_policy"] = "bounded_parallel"
        scheduler_policy["max_parallel_packets"] = 2
    else:
        plan["execution_shape"] = "bounded_swarm"
        scheduler_policy["execution_shape"] = "bounded_swarm"
        scheduler_policy["parallelism_policy"] = "bounded_parallel"
        scheduler_policy["max_parallel_packets"] = 4 if archetype not in {"migration", "rubik_3d"} else 2 if archetype == "migration" else 3


def build_archetype_plan(*, archetype: str, mode: str) -> dict[str, Any]:
    if archetype not in ARCHETYPES:
        raise ValueError(f"unknown_archetype:{archetype}")
    if mode not in BENCHMARK_MODES:
        raise ValueError(f"unknown_benchmark_mode:{mode}")

    plan = _base_plan_fixture()
    _configure_mode(plan, archetype=archetype, mode=mode)
    base_worker = plan["packets"][0]
    base_validator = plan["packets"][1]

    if archetype == "service":
        packets = [
            _make_packet(
                base_packet=base_validator,
                packet_id="packet-lint",
                lane="validator",
                parallelism_class="isolated",
                allowed_scope=["service.py"],
                execution_strategy="lint_command",
                file_write_content="service-lint-pass\n",
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-worker",
                lane="worker",
                parallelism_class="bounded",
                allowed_scope=["compile_plan.py"],
                execution_strategy="command_capture",
                dependencies=["packet-lint"],
                dependency_mode="accepted_upstream",
                file_write_content="service-worker-updated\n",
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-reviewer",
                lane="reviewer",
                parallelism_class="serial",
                allowed_scope=["validate_plan.py"],
                execution_strategy="review_evidence_packet",
                execution_mode="sequence_required",
                dependencies=["packet-worker"],
                dependency_mode="accepted_upstream",
            ),
        ]
    elif archetype == "ui":
        packets = [
            _make_packet(
                base_packet=base_validator,
                packet_id="packet-lint",
                lane="validator",
                parallelism_class="isolated",
                allowed_scope=["ui.tsx"],
                execution_strategy="lint_command",
                file_write_content="ui-lint-pass\n",
            ),
            _make_packet(
                base_packet=base_validator,
                packet_id="packet-smoke",
                lane="validator",
                parallelism_class="bounded",
                allowed_scope=["smoke.spec.ts"],
                execution_strategy="multi_command_pipeline",
                file_write_content="ui-smoke-pass\n",
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-explorer",
                lane="explorer",
                parallelism_class="isolated",
                allowed_scope=["common.py"],
                execution_strategy="command_capture",
                file_write_content="ui-explorer-updated\n",
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-worker",
                lane="worker",
                parallelism_class="bounded",
                allowed_scope=["compile_plan.py"],
                execution_strategy="command_capture",
                dependencies=["packet-lint", "packet-explorer"],
                dependency_mode="accepted_upstream",
                file_write_content="ui-worker-updated\n",
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-reviewer",
                lane="reviewer",
                parallelism_class="serial",
                allowed_scope=["validate_plan.py"],
                execution_strategy="review_evidence_packet",
                execution_mode="sequence_required",
                dependencies=["packet-worker", "packet-smoke"],
                dependency_mode="accepted_upstream",
            ),
        ]
    elif archetype == "migration":
        packets = [
            _make_packet(
                base_packet=base_validator,
                packet_id="packet-schema",
                lane="validator",
                parallelism_class="serial",
                allowed_scope=["schema.sql"],
                execution_strategy="schema_check_command",
                file_write_content="schema-validated\n",
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-worker-a",
                lane="worker",
                parallelism_class="serial",
                allowed_scope=["compile_plan.py"],
                execution_strategy="command_capture",
                dependencies=["packet-schema"],
                dependency_mode="accepted_upstream",
                file_write_content="migration-worker-a\n",
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-worker-b",
                lane="worker",
                parallelism_class="serial",
                allowed_scope=["compile_plan.py"],
                execution_strategy="command_capture",
                dependencies=["packet-worker-a"],
                dependency_mode="accepted_upstream",
                file_write_content="migration-worker-b\n",
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-reviewer",
                lane="reviewer",
                parallelism_class="serial",
                allowed_scope=["validate_plan.py"],
                execution_strategy="review_evidence_packet",
                execution_mode="sequence_required",
                dependencies=["packet-worker-b"],
                dependency_mode="accepted_upstream",
            ),
        ]
    elif archetype == "mixed":
        packets = [
            _make_packet(
                base_packet=base_validator,
                packet_id="packet-validator",
                lane="validator",
                parallelism_class="isolated",
                allowed_scope=["verify_plan.py"],
                execution_strategy="validation_command",
                file_write_content="mixed-validator-pass\n",
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-explorer",
                lane="explorer",
                parallelism_class="isolated",
                allowed_scope=["common.py"],
                execution_strategy="command_capture",
                file_write_content="mixed-explorer-pass\n",
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-worker-a",
                lane="worker",
                parallelism_class="bounded",
                allowed_scope=["compile_plan.py"],
                execution_strategy="command_capture",
                dependencies=["packet-validator"],
                dependency_mode="accepted_upstream",
                file_write_content="mixed-worker-a\n",
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-worker-b",
                lane="worker",
                parallelism_class="bounded",
                allowed_scope=["service.py"],
                execution_strategy="command_capture",
                dependencies=["packet-explorer"],
                dependency_mode="accepted_upstream",
                file_write_content="mixed-worker-b\n",
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-reviewer",
                lane="reviewer",
                parallelism_class="serial",
                allowed_scope=["validate_plan.py"],
                execution_strategy="review_evidence_packet",
                execution_mode="sequence_required",
                dependencies=["packet-worker-a", "packet-worker-b"],
                dependency_mode="accepted_upstream",
            ),
        ]
    else:
        packets = [
            _make_packet(
                base_packet=base_validator,
                packet_id="packet-lint",
                lane="validator",
                parallelism_class="isolated",
                allowed_scope=["rubik_scene.tsx"],
                execution_strategy="lint_command",
                file_write_content="rubik-lint-pass\n",
            ),
            _make_packet(
                base_packet=base_validator,
                packet_id="packet-solver-tests",
                lane="validator",
                parallelism_class="isolated",
                allowed_scope=["cube_solver.ts"],
                execution_strategy="test_command",
                file_write_content="rubik-solver-tests-pass\n",
                evidence_refs=["capture://packet-solver-tests", "artifact://cube-solver-tests.json"],
                produced_artifacts=["cube-solver-tests.json"],
            ),
            _make_packet(
                base_packet=base_validator,
                packet_id="packet-ui-smoke",
                lane="validator",
                parallelism_class="bounded",
                allowed_scope=["rubik_smoke.spec.ts"],
                execution_strategy="multi_command_pipeline",
                file_write_content="rubik-ui-smoke-pass\n",
                evidence_refs=[
                    "capture://packet-ui-smoke-step1",
                    "capture://packet-ui-smoke-step2",
                    "artifact://rubik-ui-smoke.json",
                ],
                produced_artifacts=["rubik-ui-smoke.json"],
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-explorer",
                lane="explorer",
                parallelism_class="isolated",
                allowed_scope=["common.py"],
                execution_strategy="command_capture",
                file_write_content="rubik-explorer-updated\n",
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-solver",
                lane="worker",
                parallelism_class="bounded",
                allowed_scope=["cube_state.ts", "cube_solver.ts"],
                execution_strategy="command_capture",
                dependencies=["packet-solver-tests", "packet-explorer"],
                dependency_mode="accepted_upstream",
                file_write_content="rubik-solver-updated\n",
                evidence_refs=["capture://packet-solver", "artifact://cube-solver-proof.json"],
                produced_artifacts=["cube-solver-proof.json"],
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-ui",
                lane="worker",
                parallelism_class="bounded",
                allowed_scope=["rubik_scene.tsx", "ui.tsx"],
                execution_strategy="command_capture",
                dependencies=["packet-lint", "packet-explorer"],
                dependency_mode="accepted_upstream",
                file_write_content="rubik-ui-updated\n",
                evidence_refs=["capture://packet-ui", "artifact://rubik-ui-proof.json"],
                produced_artifacts=["rubik-ui-proof.json"],
            ),
            _make_packet(
                base_packet=base_worker,
                packet_id="packet-reviewer",
                lane="reviewer",
                parallelism_class="serial",
                allowed_scope=["validate_plan.py"],
                execution_strategy="review_evidence_packet",
                execution_mode="sequence_required",
                dependencies=["packet-solver", "packet-ui", "packet-ui-smoke"],
                dependency_mode="accepted_upstream",
            ),
        ]
    plan["packets"] = packets
    plan["required_packets"] = []
    contract = _benchmark_contract(archetype)
    plan["objective"] = contract["goal"]
    plan["benchmark_contract"] = contract
    return plan


def _package_json_for_archetype(archetype: str) -> dict[str, Any]:
    scripts: dict[str, str] = {
        "test": 'python3.11 -c "print(\'test-ok\')"',
        "lint": 'python3.11 -c "print(\'lint-ok\')"',
        "typecheck": 'python3.11 -c "print(\'typecheck-ok\')"',
        "build": 'python3.11 -c "print(\'build-ok\')"',
    }
    if archetype in {"ui", "rubik_3d"}:
        scripts["smoke"] = 'python3.11 -c "print(\'smoke-ok\')"'
    return {
        "name": f"swarm-{archetype}-fixture",
        "private": True,
        "packageManager": "npm@11.9.0",
        "scripts": scripts,
    }


def _write_archetype_configs(root: Path, *, archetype: str) -> None:
    (root / "package.json").write_text(
        json.dumps(_package_json_for_archetype(archetype), indent=2) + "\n",
        encoding="utf-8",
    )
    if archetype in {"ui", "rubik_3d"}:
        (root / "playwright.config.ts").write_text(
            "export default { testDir: '.', retries: 0 };\n",
            encoding="utf-8",
        )
    if archetype == "migration":
        (root / "Makefile").write_text(
            "schema-check:\n\tpython3.11 -c \"print('schema-check-ok')\"\n",
            encoding="utf-8",
        )
        workflow_dir = root / ".github" / "workflows"
        workflow_dir.mkdir(parents=True, exist_ok=True)
        (workflow_dir / "ci.yml").write_text(
            "\n".join(
                [
                    "name: CI",
                    "on: [push]",
                    "jobs:",
                    "  validate:",
                    "    runs-on: ubuntu-latest",
                    "    steps:",
                    "      - run: npm run test",
                    "      - run: npm run lint",
                    "      - run: npm run typecheck",
                    "      - run: npm run build",
                    "      - run: make schema-check",
                ]
            )
            + "\n",
            encoding="utf-8",
        )


def _init_repo(root: Path, *, archetype: str) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Codex Tests"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "codex-tests@example.invalid"], cwd=root, check=True, capture_output=True, text=True)
    baseline_files = {
        "compile_plan.py": "baseline:compile_plan.py\n",
        "common.py": "baseline:common.py\n",
        "verify_plan.py": "baseline:verify_plan.py\n",
        "validate_plan.py": "baseline:validate_plan.py\n",
        "service.py": "def service():\n    return 'ok'\n",
        "ui.tsx": "export const UI = () => null;\n",
        "smoke.spec.ts": "console.log('smoke');\n",
        "cube_state.ts": "export const solvedState = 'solved';\n",
        "cube_solver.ts": "export const solveCube = () => ['R', 'U', 'R'];\n",
        "rubik_scene.tsx": "export const RubikScene = () => null;\n",
        "rubik_smoke.spec.ts": "console.log('rubik-smoke');\n",
        "schema.sql": "create table items(id integer primary key);\n",
    }
    for path, content in baseline_files.items():
        (root / path).write_text(content, encoding="utf-8")
    _write_archetype_configs(root, archetype=archetype)
    subprocess.run(["git", "add", "--", "."], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True, text=True)


def _prepare_bootstrap(*, root: Path, track_id: str, plan: dict[str, Any]) -> None:
    compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
    initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
    compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
    verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)


def _load_runtime_json(path: Path) -> dict[str, Any]:
    payload = load_json_file(path)
    return payload if isinstance(payload, dict) else {}


def _quality_preserving(*, operator_view: dict[str, Any], metrics: dict[str, Any]) -> bool:
    trust = operator_view.get("trust_report", {}) if isinstance(operator_view.get("trust_report"), dict) else {}
    support = operator_view.get("support_confidence", {}) if isinstance(operator_view.get("support_confidence"), dict) else {}
    return (
        metrics.get("final_closure_state") == "OBJECTIVE_COMPLETE"
        and metrics.get("stop_allowed") is True
        and support.get("unsupported_closure_risk") in {None, "", "none"}
        and trust.get("closure_strength") in {"strong", "verified"}
    )


def _benchmark_score(*, metrics: dict[str, Any]) -> float:
    if metrics.get("quality_preserving") is not True:
        return 0.0
    score = 100.0
    score -= min(float(metrics.get("wall_clock_seconds", 0.0) or 0.0), 30.0)
    score -= float(int(metrics.get("cycles_to_closure", 0) or 0)) * 4.0
    score -= float(int(metrics.get("support_confidence_failures", 0) or 0)) * 6.0
    score -= float(int(metrics.get("blocked_dispatch_count", 0) or 0))
    score -= float(int(metrics.get("reviewer_barrier_wait_count", 0) or 0)) * 0.5
    score -= float(metrics.get("fallback_ratio", 0.0) or 0.0) * 25.0
    return round(max(score, 0.0), 3)


def _run_stepwise_runtime(
    *,
    plan: dict[str, Any],
    artifacts_root: Path,
    track_id: str,
    workspace_root: Path,
    codex_home: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _prepare_bootstrap(root=artifacts_root, track_id=track_id, plan=plan)
    bootstrap_runtime(plan_payload=plan, artifacts_root=artifacts_root, track_id=track_id, cwd=str(workspace_root))

    runtime_paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    frontier_widths: list[int] = []
    blocked_dispatch_total = 0
    reviewer_barrier_wait_count = 0
    support_confidence_failures = 0

    started = time.perf_counter()
    while True:
        rc, payload = run_runtime(
            plan_payload=plan,
            artifacts_root=artifacts_root,
            track_id=track_id,
            workspace_root=str(workspace_root),
            codex_home=codex_home,
            command="step",
        )
        schedule = _load_runtime_json(runtime_paths["schedule"])
        support = _load_runtime_json(runtime_paths["support_confidence"])
        frontier_widths.append(len(schedule.get("current_frontier", [])) if isinstance(schedule.get("current_frontier"), list) else 0)
        blocked_dispatch_total += len(schedule.get("dispatch_block_reasons", {})) if isinstance(schedule.get("dispatch_block_reasons"), dict) else 0
        if isinstance(schedule.get("awaiting_reviewer_barrier"), list) and schedule.get("awaiting_reviewer_barrier"):
            reviewer_barrier_wait_count += 1
        if str(support.get("unsupported_closure_risk") or "none").strip() not in {"", "none"}:
            support_confidence_failures += 1
        if payload.get("status") != "revise":
            break
        if rc not in {10}:
            break
    finished = time.perf_counter()

    schedule = _load_runtime_json(runtime_paths["schedule"])
    runtime_state = _load_runtime_json(runtime_paths["runtime_state"])
    operator_view = _load_runtime_json(runtime_paths["operator_view"])
    execution_coverage = _load_runtime_json(runtime_paths["execution_coverage"])
    cycles = schedule.get("cycle_log") if isinstance(schedule.get("cycle_log"), list) else []
    dispatch_history = schedule.get("dispatch_history") if isinstance(schedule.get("dispatch_history"), list) else []
    packets_completed = int(schedule.get("accepted_packet_count", 0) or 0)
    fallback_ratio = float(execution_coverage.get("fallback_ratio", 0.0) or operator_view.get("health_signals", {}).get("fallback_ratio", 0.0) or 0.0)

    metrics = {
        "mode": str(schedule.get("parallelism_policy") or plan.get("scheduler_policy", {}).get("parallelism_policy") or ""),
        "route_hint": str(plan.get("route_hint") or ""),
        "execution_shape": str(plan.get("execution_shape") or ""),
        "wall_clock_seconds": round(finished - started, 6),
        "cycles_to_closure": len(cycles),
        "packets_completed": packets_completed,
        "average_frontier_width": round(sum(frontier_widths) / len(frontier_widths), 3) if frontier_widths else 0.0,
        "max_frontier_width": max(frontier_widths) if frontier_widths else 0,
        "blocked_dispatch_count": blocked_dispatch_total,
        "reviewer_barrier_wait_count": reviewer_barrier_wait_count,
        "support_confidence_failures": support_confidence_failures,
        "fallback_ratio": round(fallback_ratio, 4),
        "final_closure_state": str(runtime_state.get("closure_state") or payload.get("closure_state") or ""),
        "lifecycle_status": str(runtime_state.get("lifecycle_status") or payload.get("status") or ""),
        "stop_allowed": runtime_state.get("stop_allowed") is True,
        "dispatch_cycles": len(dispatch_history),
        "track_id": track_id,
    }
    metrics["quality_preserving"] = _quality_preserving(operator_view=operator_view, metrics=metrics)
    metrics["benchmark_score"] = _benchmark_score(metrics=metrics)
    return metrics, operator_view


def _select_recommended_mode(runs: list[dict[str, Any]]) -> tuple[str, bool, bool, str]:
    quality_runs = [run for run in runs if run.get("quality_preserving") is True]
    if not quality_runs:
        return "serial_only", False, True, "No execution mode preserved closure quality; keeping serial as the conservative baseline."
    ranked = sorted(
        quality_runs,
        key=lambda run: (
            int(run.get("cycles_to_closure", 0) or 0),
            float(run.get("wall_clock_seconds", 0.0) or 0.0),
            float(run.get("fallback_ratio", 0.0) or 0.0),
        ),
    )
    recommended = str(ranked[0]["mode"])
    serial_run = next((run for run in runs if run.get("mode") == "serial_only"), None)
    swarm_run = next((run for run in runs if run.get("mode") == "bounded_swarm"), None)
    swarm_outperformed_serial = bool(
        swarm_run
        and serial_run
        and swarm_run.get("quality_preserving") is True
        and (
            int(swarm_run.get("cycles_to_closure", 0) or 0) < int(serial_run.get("cycles_to_closure", 0) or 0)
            or float(swarm_run.get("wall_clock_seconds", 0.0) or 0.0) < float(serial_run.get("wall_clock_seconds", 0.0) or 0.0)
        )
    )
    serial_better = recommended == "serial_only"
    if recommended == "bounded_swarm":
        reason = "Bounded swarm reduced governed cycles or wall-clock time without weakening closure quality."
    elif recommended == "bounded_parallel":
        reason = "Bounded parallelism improved throughput without requiring full swarm coordination."
    else:
        reason = "Serial execution remained the best quality-preserving path for this objective shape."
    return recommended, swarm_outperformed_serial, serial_better, reason


def run_benchmark_archetype(
    *,
    archetype: str,
    artifacts_root: str | Path | None = None,
    codex_home: str | None = None,
    track_prefix: str = "benchmark",
) -> dict[str, Any]:
    if archetype not in ARCHETYPES:
        raise ValueError(f"unknown_archetype:{archetype}")
    root = resolve_artifacts_root(str(artifacts_root) if artifacts_root is not None else None)
    runs: list[dict[str, Any]] = []
    for mode in BENCHMARK_MODES:
        track_id = f"{track_prefix}-{archetype}-{mode}"
        workspace_root = root / f"{track_id}-workspace"
        workspace_root.mkdir(parents=True, exist_ok=True)
        _init_repo(workspace_root, archetype=archetype)
        plan = build_archetype_plan(archetype=archetype, mode=mode)
        metrics, operator_view = _run_stepwise_runtime(
            plan=plan,
            artifacts_root=root,
            track_id=track_id,
            workspace_root=workspace_root,
            codex_home=codex_home,
        )
        metrics["mode"] = mode
        metrics["operator_track"] = operator_view.get("track_id")
        runs.append(metrics)
    recommended_mode, swarm_outperformed_serial, serial_better, reason = _select_recommended_mode(runs)
    contract = _benchmark_contract(archetype)
    report = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "archetype": archetype,
        "benchmark_contract": contract,
        "baseline_mode": "serial_only",
        "recommended_mode": recommended_mode,
        "swarm_outperformed_serial": swarm_outperformed_serial,
        "serial_better": serial_better,
        "reason": reason,
        "runs": runs,
        "comparison_fields": [
            "quality_preserving",
            "benchmark_score",
            "cycles_to_closure",
            "wall_clock_seconds",
            "fallback_ratio",
            "support_confidence_failures",
        ],
    }
    for run in runs:
        path = runtime_artifact_paths(artifacts_root=root, track_id=str(run["track_id"]))["benchmark"]
        write_json_file(path, report)
        _sync_operator_view(artifacts_root=root, track_id=str(run["track_id"]))
    benchmarks_root = root / "benchmarks"
    benchmarks_root.mkdir(parents=True, exist_ok=True)
    write_json_file(benchmarks_root / f"{archetype}.benchmark.json", report)
    return report


def run_benchmark_corpus(
    *,
    artifacts_root: str | Path | None = None,
    codex_home: str | None = None,
    archetypes: list[str] | None = None,
) -> dict[str, Any]:
    selected = archetypes or list(ARCHETYPES)
    root = resolve_artifacts_root(str(artifacts_root) if artifacts_root is not None else None)
    reports = [run_benchmark_archetype(archetype=archetype, artifacts_root=root, codex_home=codex_home) for archetype in selected]
    payload = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "reports": reports,
    }
    benchmarks_root = root / "benchmarks"
    benchmarks_root.mkdir(parents=True, exist_ok=True)
    write_json_file(benchmarks_root / "swarm-report.json", payload)
    return payload


def _is_git_workspace(path: Path) -> bool:
    return (path / ".git").exists()


def _create_isolated_workspace(*, workspace_root: Path, destination: Path) -> tuple[Path, str]:
    if _is_git_workspace(workspace_root):
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(destination), "HEAD"],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return destination, "git_worktree"
    shutil.copytree(workspace_root, destination)
    return destination, "copytree"


def _canary_refusal_reason(*, plan: dict[str, Any], route_hint: str, execution_shape: str) -> str:
    if route_hint not in {"R2", "R3"}:
        return "route_not_allowed_in_first_wave"
    if execution_shape == "bounded_swarm" and route_hint != "R3":
        return "bounded_swarm_canary_limited_to_r3"
    scope_terms = " ".join(
        [str(plan.get("objective_id") or "")]
        + [str(path) for packet in plan.get("packets", []) if isinstance(packet, dict) for path in packet.get("allowed_scope", []) if isinstance(path, str)]
    ).lower()
    for pattern in UNSAFE_CANARY_PATTERNS:
        if pattern in scope_terms:
            return f"unsafe_scope_pattern:{pattern}"
    return ""


def run_live_canary(
    *,
    plan_json: str | Path,
    workspace_root: str | Path,
    artifacts_root: str | Path | None = None,
    track_id: str = "swarm-canary",
    route_hint: str | None = None,
    execution_shape: str | None = None,
    codex_home: str | None = None,
    safety_mode: str = "bounded",
) -> dict[str, Any]:
    root = resolve_artifacts_root(str(artifacts_root) if artifacts_root is not None else None, cwd=str(workspace_root))
    workspace = Path(workspace_root).resolve()
    plan = load_json_file(plan_json)
    plan["route_hint"] = route_hint or str(plan.get("route_hint") or "R3")
    plan["session_harness"]["route_hint"] = plan["route_hint"]
    if execution_shape is not None:
        plan["execution_shape"] = execution_shape
        plan.setdefault("scheduler_policy", {})["execution_shape"] = execution_shape
    route = str(plan.get("route_hint") or "")
    shape = str(plan.get("execution_shape") or "single_lane")
    refusal_reason = _canary_refusal_reason(plan=plan, route_hint=route, execution_shape=shape)
    isolated_root = root / f"{track_id}-workspace"
    isolated_root.parent.mkdir(parents=True, exist_ok=True)
    isolated_workspace, isolation_mode = _create_isolated_workspace(workspace_root=workspace, destination=isolated_root)

    canary_payload: dict[str, Any] = {
        "schema_version": CANARY_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "workspace_root": str(workspace),
        "isolated_workspace_root": str(isolated_workspace),
        "isolation_mode": isolation_mode,
        "route_hint": route,
        "execution_shape": shape,
        "safety_mode": safety_mode,
        "safe_to_run": refusal_reason == "",
        "refused": refusal_reason != "",
        "refusal_reason": refusal_reason,
    }
    runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
    if refusal_reason:
        _prepare_bootstrap(root=root, track_id=track_id, plan=plan)
        bootstrap_runtime(plan_payload=plan, artifacts_root=root, track_id=track_id, cwd=str(isolated_workspace))
        write_json_file(runtime_paths["canary"], canary_payload)
        _sync_operator_view(artifacts_root=root, track_id=track_id)
        return canary_payload

    metrics, _ = _run_stepwise_runtime(
        plan=plan,
        artifacts_root=root,
        track_id=track_id,
        workspace_root=isolated_workspace,
        codex_home=codex_home,
    )
    canary_payload["metrics"] = metrics
    write_json_file(runtime_paths["canary"], canary_payload)
    _sync_operator_view(artifacts_root=root, track_id=track_id)
    return canary_payload
