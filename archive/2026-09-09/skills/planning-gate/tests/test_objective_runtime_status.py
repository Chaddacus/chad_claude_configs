#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
import sys

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import runtime_artifact_paths, session_artifact_paths, write_json_file  # noqa: E402
from compile_intent import compile_intent_payload  # noqa: E402
from compile_plan import compile_plan_payload  # noqa: E402
from initialize_session import initialize_session_payload  # noqa: E402
from objective_runtime import _sync_operator_view, bootstrap_runtime, run_runtime, step  # noqa: E402
from objective_runtime_status import load_operator_view_payload, render_operator_view_text  # noqa: E402
from swarm_evaluation import _init_repo as init_swarm_repo, build_archetype_plan, run_benchmark_archetype, run_live_canary  # noqa: E402
from verify_plan import verify_plan_payload  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STATUS_SCRIPT = SCRIPT_DIR / "objective_runtime_status.py"


class ObjectiveRuntimeStatusTests(unittest.TestCase):
    def _fixture(self) -> dict:
        plan = json.loads((FIXTURES / "plan_valid.json").read_text(encoding="utf-8"))
        plan["tests"]["unit"] = ['python3.11 -c "print(\'unit-ok\')"']
        plan["tests"]["integration"] = []
        plan["tests"]["regression"] = []
        plan["session_harness"]["validation_commands"] = ['python3.11 -c "print(\'validate-ok\')"']
        for gate in plan["tests"]["smoke_gates"]:
            stage = str(gate["stage"]).replace("%", "")
            gate["commands"] = [f'python3.11 -c "print(\'smoke-{stage}\')"']
        initial_content = {
            "packet-compiler": [{"path": "compile_plan.py", "content": "compiler-updated\n"}],
            "packet-verifier": [{"path": "verify_plan.py", "content": "verifier-updated\n"}],
        }
        for packet in plan["packets"]:
            changed_files = [str(path) for path in packet.get("allowed_scope", []) if str(path).strip()]
            packet["simulation"] = {
                "attempts": [
                    {
                        "worker_exit_code": 0,
                        "stdout": f"{packet['packet_id']} complete",
                        "review_output": "accepted",
                        "allowed_scope_status": "within_scope",
                        "changed_files": changed_files,
                        "evidence_refs": [f"capture://{packet['packet_id']}"],
                        "captured_commands": [{"command": "echo ok", "exit_code": 0}],
                        "result_artifact_path": f"planning_artifacts/<track-id>/cycles/{packet['packet_id']}.json",
                        "file_writes": initial_content.get(packet["packet_id"], []),
                    }
                ]
            }
        return plan

    def _runtime_plan(self, *, route_hint: str = "R3", execution_shape: str = "bounded_swarm") -> dict:
        plan = self._fixture()
        plan["route_hint"] = route_hint
        plan["session_harness"]["route_hint"] = route_hint
        plan["execution_shape"] = execution_shape
        plan.setdefault("scheduler_policy", {})["execution_shape"] = execution_shape
        return plan

    def _make_packet(
        self,
        *,
        base_packet: dict,
        packet_id: str,
        lane: str,
        parallelism_class: str,
        allowed_scope: list[str],
        execution_strategy: str | None = None,
        execution_mode: str = "parallel_safe",
        dependencies: list[str] | None = None,
        dependency_mode: str = "explicit_stub",
        file_write_content: str | None = None,
    ) -> dict:
        packet = copy.deepcopy(base_packet)
        packet["packet_id"] = packet_id
        packet["packet_lane"] = lane
        packet["parallelism_class"] = parallelism_class
        packet["execution_mode"] = execution_mode
        packet["allowed_scope"] = allowed_scope
        packet["dependencies"] = dependencies or []
        packet["dependency_mode"] = dependency_mode
        packet["shared_surface_categories"] = [packet_id]
        if dependency_mode == "accepted_upstream":
            packet["stub_dependencies"] = []
        if execution_strategy:
            packet["execution_strategy"] = execution_strategy
        packet["evidence_destination"] = f"planning_artifacts/<track-id>/packets/{packet_id}.verdict.json"
        packet["definition_of_done"]["allowed_scope"] = allowed_scope
        packet["definition_of_done"]["objective_linkage"] = f"req-{packet_id}"
        packet["simulation"] = {
            "attempts": [
                {
                    "worker_exit_code": 0,
                    "stdout": f"{packet_id} complete",
                    "review_output": "accepted",
                    "allowed_scope_status": "within_scope",
                    "changed_files": allowed_scope,
                    "evidence_refs": [f"capture://{packet_id}"],
                    "captured_commands": [{"command": f"echo {packet_id}", "exit_code": 0}],
                    "result_artifact_path": f"planning_artifacts/<track-id>/cycles/{packet_id}.json",
                    "file_writes": (
                        [{"path": allowed_scope[0], "content": file_write_content}]
                        if file_write_content is not None and allowed_scope
                        else []
                    ),
                }
            ]
        }
        packet["classification"] = "ready"
        packet["product_meaning_resolved"] = True
        packet["automatable_acceptance"] = True
        packet["prohibited_action_required"] = False
        packet["maintainable_completion_path"] = True
        return packet

    def _r3_swarm_fixture(self, *, conflicting_workers: bool = False) -> dict:
        plan = self._runtime_plan(route_hint="R3", execution_shape="bounded_swarm")
        validator = self._make_packet(
            base_packet=plan["packets"][1],
            packet_id="packet-validator",
            lane="validator",
            parallelism_class="isolated",
            allowed_scope=["verify_plan.py"],
            execution_strategy="validation_command",
            file_write_content="packet-validator-updated\n",
        )
        explorer = self._make_packet(
            base_packet=plan["packets"][0],
            packet_id="packet-explorer",
            lane="explorer",
            parallelism_class="isolated",
            allowed_scope=["common.py"],
            execution_strategy="command_capture",
            file_write_content="packet-explorer-updated\n",
        )
        worker = self._make_packet(
            base_packet=plan["packets"][0],
            packet_id="packet-worker",
            lane="worker",
            parallelism_class="bounded",
            allowed_scope=["compile_plan.py"],
            execution_strategy="command_capture",
            dependencies=["packet-validator", "packet-explorer"],
            dependency_mode="accepted_upstream",
            file_write_content="packet-worker-updated\n",
        )
        reviewer = self._make_packet(
            base_packet=plan["packets"][0],
            packet_id="packet-reviewer",
            lane="reviewer",
            parallelism_class="serial",
            allowed_scope=["validate_plan.py"],
            execution_strategy="command_capture",
            execution_mode="sequence_required",
            dependencies=["packet-worker"],
            dependency_mode="accepted_upstream",
            file_write_content="packet-reviewer-updated\n",
        )
        reviewer["acceptance_checks"] = ["review evidence emitted"]
        reviewer["definition_of_done"]["acceptance_checks"] = ["review evidence emitted"]
        reviewer["definition_of_done"]["verifier_acceptance_condition"] = "Review evidence emitted."
        packets = [validator, explorer, worker, reviewer]
        if conflicting_workers:
            worker_b = self._make_packet(
                base_packet=plan["packets"][0],
                packet_id="packet-worker-b",
                lane="worker",
                parallelism_class="bounded",
                allowed_scope=["compile_plan.py"],
                execution_strategy="command_capture",
                dependencies=["packet-validator", "packet-explorer"],
                dependency_mode="accepted_upstream",
                file_write_content="packet-worker-b-updated\n",
            )
            packets = [validator, explorer, worker, worker_b]
        plan["packets"] = packets
        plan["required_packets"] = [packet["packet_id"] for packet in plan["packets"]]
        return plan

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Codex Tests"], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "codex-tests@example.invalid"], cwd=root, check=True, capture_output=True, text=True)
        for path in ("compile_plan.py", "common.py", "verify_plan.py", "validate_plan.py"):
            (root / path).write_text(f"baseline:{path}\n", encoding="utf-8")
        subprocess.run(["git", "add", "--", "."], cwd=root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True, text=True)

    def _prepare_bootstrap(self, *, root: Path, track_id: str, plan: dict) -> None:
        compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
        initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
        compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
        verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)

    def test_bootstrap_writes_operator_view(self) -> None:
        plan = self._fixture()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_bootstrap(root=root, track_id="operator-bootstrap", plan=plan)
            bootstrap_runtime(plan=plan, track_id="operator-bootstrap", artifacts_root=root)
            operator_view_path = runtime_artifact_paths(artifacts_root=root, track_id="operator-bootstrap")["operator_view"]
            self.assertTrue(operator_view_path.exists())
            operator_view = json.loads(operator_view_path.read_text(encoding="utf-8"))
            self.assertEqual(operator_view["schema_version"], "objective-operator-view.v1")
            self.assertIn("health_signals", operator_view)
            self.assertIn("repo_capabilities_summary", operator_view)
            self.assertIn("packet_quality_summary", operator_view)
            self.assertEqual(operator_view["transaction"]["state"], "committed")
            self.assertIn("transaction_state", operator_view["artifacts"])

    def test_clean_success_snapshot_is_strong_and_renderable(self) -> None:
        plan = self._fixture()
        track_id = "operator-success"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            rc, payload = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="run",
            )
            self.assertEqual(rc, 0)
            self.assertEqual(payload["closure_state"], "OBJECTIVE_COMPLETE")
            operator_view = load_operator_view_payload(track_id=track_id, artifacts_root=root)
            self.assertTrue(operator_view["health_signals"]["closure_claim_is_strong"])
            text = render_operator_view_text(operator_view, selected_view="summary")
            self.assertIn("closure: OBJECTIVE_COMPLETE", text)
            self.assertIn("why next:", text)
            trust_text = render_operator_view_text(operator_view, selected_view="trust")
            self.assertIn("closure strength: strong", trust_text)

    def test_summary_prefers_runtime_state_fields(self) -> None:
        plan = self._fixture()
        track_id = "operator-runtime-state-first"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            rc, _ = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="run",
            )
            self.assertEqual(rc, 0)
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
            runtime_state = json.loads(runtime_paths["runtime_state"].read_text(encoding="utf-8"))
            runtime_state["lifecycle_status"] = "approved_pending_verify"
            runtime_state["stop_reason"] = "runtime_state_override"
            runtime_state["next_recommended_packet"] = "packet-runtime"
            runtime_state["current_packet"] = "packet-runtime"
            runtime_state["last_verifier_result"] = {"status": "approve", "reason": "runtime-state-first"}
            runtime_paths["runtime_state"].write_text(json.dumps(runtime_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            operator_view = load_operator_view_payload(track_id=track_id, artifacts_root=root)
            summary_text = render_operator_view_text(operator_view, selected_view="summary")
            self.assertIn("lifecycle: approved_pending_verify", summary_text)
            self.assertIn("stop reason: runtime_state_override", summary_text)
            self.assertIn("transaction:", summary_text)

    def test_status_surfaces_prepared_transaction_with_orphaned_cycle_evidence(self) -> None:
        plan = self._fixture()
        track_id = "operator-transaction-prepared"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            bootstrap_runtime(plan=plan, track_id=track_id, artifacts_root=root)
            prior = os.environ.get("CODEX_OBJECTIVE_TX_PREPARE_ONLY")
            try:
                os.environ["CODEX_OBJECTIVE_TX_PREPARE_ONLY"] = "1"
                with self.assertRaises(RuntimeError):
                    step(
                        plan_payload=plan,
                        artifacts_root=root,
                        track_id=track_id,
                        cwd=str(root),
                        codex_home=None,
                        controller_mode="enforce",
                    )
            finally:
                if prior is None:
                    os.environ.pop("CODEX_OBJECTIVE_TX_PREPARE_ONLY", None)
                else:
                    os.environ["CODEX_OBJECTIVE_TX_PREPARE_ONLY"] = prior
            cycles_root = root / track_id / "cycles"
            cycle_dirs = sorted(path for path in cycles_root.iterdir() if path.is_dir())
            self.assertEqual(len(cycle_dirs), 1)
            self.assertTrue((cycle_dirs[0] / "cycle.result.json").exists())
            self.assertTrue((cycle_dirs[0] / "cycle.review.json").exists())
            operator_view = load_operator_view_payload(track_id=track_id, artifacts_root=root)
            self.assertEqual(operator_view["transaction"]["state"], "prepared")
            self.assertFalse(operator_view["transaction"]["recovered"])
            summary_text = render_operator_view_text(operator_view, selected_view="summary")
            self.assertIn("state=prepared", summary_text)
            self.assertIn("current packet: packet-compiler", summary_text)
            self.assertIn("verifier: (none)", summary_text)

    def test_dirty_repo_surface_explains_checkpoint_block(self) -> None:
        plan = self._fixture()
        track_id = "operator-dirty"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "unrelated.txt").write_text("dirty\n", encoding="utf-8")
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            rc, _ = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="run",
            )
            self.assertEqual(rc, 20)
            operator_view = load_operator_view_payload(track_id=track_id, artifacts_root=root)
            blocked_text = render_operator_view_text(operator_view, selected_view="why-blocked")
            self.assertIn("Checkpointing is blocked by unrelated dirty repo state.", blocked_text)

    def test_validation_gap_signal_is_derived_from_manual_only_lane(self) -> None:
        plan = self._fixture()
        track_id = "operator-validation-gap"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            bootstrap_runtime(plan=plan, track_id=track_id, artifacts_root=root)
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
            validation_plan = json.loads(runtime_paths["validation_plan"].read_text(encoding="utf-8"))
            validation_plan["lanes"].append(
                {
                    "lane": "smoke_e2e",
                    "required": True,
                    "reasons": ["ui surface touched"],
                    "paths": ["frontend/app.tsx"],
                    "commands": [],
                    "generated_packet_ids": [],
                    "manual_only_blocker": "required lane smoke_e2e has no deterministic validation command",
                }
            )
            write_json_file(runtime_paths["validation_plan"], validation_plan)
            _sync_operator_view(artifacts_root=root, track_id=track_id)
            operator_view = load_operator_view_payload(track_id=track_id, artifacts_root=root)
            self.assertTrue(operator_view["health_signals"]["validation_gap_present"])
            validation_text = render_operator_view_text(operator_view, selected_view="validation")
            self.assertIn("manual_blocked", validation_text)
            trust_text = render_operator_view_text(operator_view, selected_view="trust")
            self.assertIn("validation coverage: incomplete", trust_text)

    def test_low_confidence_capability_gap_is_distinct_from_missing_capability(self) -> None:
        plan = self._fixture()
        track_id = "operator-low-confidence"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            bootstrap_runtime(plan=plan, track_id=track_id, artifacts_root=root)
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
            validation_plan = json.loads(runtime_paths["validation_plan"].read_text(encoding="utf-8"))
            validation_plan["lanes"].append(
                {
                    "lane": "smoke_e2e",
                    "required": True,
                    "reasons": ["ui surface touched"],
                    "paths": ["frontend/app.tsx"],
                    "commands": [],
                    "generated_packet_ids": [],
                    "manual_only_blocker": "",
                    "missing_capability_reason": "No deterministic repo capability discovered for smoke_e2e.",
                    "capability_confidence": "low",
                }
            )
            write_json_file(runtime_paths["validation_plan"], validation_plan)
            repo_capabilities = json.loads(runtime_paths["repo_capabilities"].read_text(encoding="utf-8"))
            repo_capabilities["confidence_by_lane"]["smoke_e2e"] = "low"
            repo_capabilities["missing_capabilities"]["smoke_e2e"] = "No deterministic repo capability discovered for smoke_e2e."
            write_json_file(runtime_paths["repo_capabilities"], repo_capabilities)
            _sync_operator_view(artifacts_root=root, track_id=track_id)
            operator_view = load_operator_view_payload(track_id=track_id, artifacts_root=root)
            capability_text = render_operator_view_text(operator_view, selected_view="capabilities")
            trust_text = render_operator_view_text(operator_view, selected_view="trust")
            self.assertIn("low confidence lanes: smoke_e2e", capability_text)
            self.assertIn("low confidence lanes: smoke_e2e", trust_text)
            self.assertIn("capability_detection_low_confidence", trust_text)

    def test_fallback_burden_signal_trips_for_prompt_worker_heavy_objective(self) -> None:
        plan = self._fixture()
        plan["session_harness"]["route_hint"] = "R3"
        packet = plan["packets"][0]
        packet["execution_strategy"] = "codex_prompt_worker"
        packet["strategy_inputs"] = {
                "worker_goal": f"Execute {packet['packet_id']}",
                "prompt_contract_ref": "proxy-runtime-closeout.v1",
                "expected_artifacts": [f"{packet['packet_id']}.txt"],
            }
        packet["fallback_reason"] = "no_deterministic_runner"
        packet["support_expectations"] = {
            "expected_evidence_artifacts": [f"{packet['packet_id']}.txt"],
            "support_kind": "fallback_artifacts",
        }
        packet["external_support_required"] = True
        packet["support_remediation_mode"] = "fallback_rework"
        packet["simulation"]["attempts"][0]["produced_artifacts"] = [f"{packet['packet_id']}.txt"]
        packet["simulation"]["attempts"][0]["evidence_refs"] = [f"{packet['packet_id']}.txt"]
        track_id = "operator-fallback-heavy"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            rc, _ = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="run",
            )
            self.assertEqual(rc, 0)
            operator_view = load_operator_view_payload(track_id=track_id, artifacts_root=root)
            self.assertTrue(operator_view["health_signals"]["fallback_burden_high"])
            execution_text = render_operator_view_text(operator_view, selected_view="execution")
            self.assertIn("fallback burden high: True", execution_text)
            packet_quality_text = render_operator_view_text(operator_view, selected_view="packet-quality")
            self.assertIn("budget status:", packet_quality_text)

    def test_checkpoint_unhealthy_success_path_is_flagged(self) -> None:
        plan = self._fixture()
        track_id = "operator-checkpoint-unhealthy"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            rc, _ = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="run",
            )
            self.assertEqual(rc, 0)
            checkpoint_path = session_artifact_paths(artifacts_root=root, track_id=track_id)["checkpoint"]
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["checkpoint_blocked"] = True
            checkpoint["checkpoint_block_reason"] = "rollback_validation_failed"
            write_json_file(checkpoint_path, checkpoint)
            _sync_operator_view(artifacts_root=root, track_id=track_id)
            operator_view = load_operator_view_payload(track_id=track_id, artifacts_root=root)
            self.assertTrue(operator_view["health_signals"]["checkpoint_unhealthy"])
            checkpoint_text = render_operator_view_text(operator_view, selected_view="checkpoint")
            self.assertIn("rollback_validation_failed", checkpoint_text)

    def test_timeline_and_json_text_key_fields_stay_consistent(self) -> None:
        plan = self._fixture()
        track_id = "operator-timeline"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            rc, _ = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="run",
            )
            self.assertEqual(rc, 0)
            operator_view = load_operator_view_payload(track_id=track_id, artifacts_root=root)
            self.assertTrue(operator_view["timeline"])
            summary_text = render_operator_view_text(operator_view, selected_view="summary")
            timeline_text = render_operator_view_text(operator_view, selected_view="timeline")
            self.assertIn(operator_view["closure_state"], summary_text)
            self.assertIn(operator_view["timeline"][-1]["cycle_id"], timeline_text)

    def test_capability_and_adaptation_views_render_when_present(self) -> None:
        plan = self._fixture()
        track_id = "operator-capability-adaptation"
        plan["packets"][0]["alternate_strategies"] = ["multi_command_pipeline"]
        plan["packets"][0]["adaptation_policy"] = "bounded_retry_then_alternate"
        plan["packets"][0]["max_adaptations"] = 1
        plan["packets"][0]["simulation"]["attempts"] = [
            {
                "worker_exit_code": 1,
                "stderr": "first strategy failed",
                "review_output": "rejected_rework",
                "allowed_scope_status": "within_scope",
                "changed_files": [],
                "evidence_refs": ["capture://packet-compiler-failure"],
                "result_artifact_path": "packet-compiler.failure.json",
            }
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="step",
            )
            operator_view = load_operator_view_payload(track_id=track_id, artifacts_root=root)
            capabilities_text = render_operator_view_text(operator_view, selected_view="capabilities")
            adaptation_text = render_operator_view_text(operator_view, selected_view="adaptation")
            self.assertIn("enabled lanes:", capabilities_text)
            self.assertIn("detectors:", capabilities_text)
            self.assertIn("adaptation events:", adaptation_text)

    def test_status_cli_renders_trust_view_and_json(self) -> None:
        plan = self._fixture()
        track_id = "operator-cli"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            rc, _ = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="run",
            )
            self.assertEqual(rc, 0)
            text = subprocess.run(
                ["python3.11", str(STATUS_SCRIPT), "--track-id", track_id, "--artifacts-root", str(root), "--view", "trust"],
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertIn("closure strength:", text.stdout)
            self.assertIn("deterministic coverage:", text.stdout)
            payload = subprocess.run(
                ["python3.11", str(STATUS_SCRIPT), "--track-id", track_id, "--artifacts-root", str(root), "--json"],
                text=True,
                capture_output=True,
                check=True,
            )
            operator_view = json.loads(payload.stdout)
            self.assertEqual(operator_view["track_id"], track_id)
            self.assertIn("trust_report", operator_view)
            self.assertIn("execution_coverage", operator_view)

    def test_benchmark_canary_and_evaluation_views_render_from_real_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            benchmark = run_benchmark_archetype(archetype="ui", artifacts_root=root)
            benchmark_track = benchmark["runs"][0]["track_id"]
            benchmark_view = load_operator_view_payload(track_id=benchmark_track, artifacts_root=root)
            benchmark_text = render_operator_view_text(benchmark_view, selected_view="benchmark")
            evaluation_text = render_operator_view_text(benchmark_view, selected_view="evaluation")
            self.assertIn("recommended:", benchmark_text)
            self.assertIn("swarm helped:", benchmark_text)
            self.assertIn("has benchmark: True", evaluation_text)
            self.assertIn("recommended mode:", evaluation_text)

            workspace = root / "canary-workspace"
            workspace.mkdir()
            init_swarm_repo(workspace, archetype="mixed")
            plan = build_archetype_plan(archetype="mixed", mode="bounded_swarm")
            plan_path = root / "mixed-plan.json"
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            canary = run_live_canary(
                plan_json=plan_path,
                workspace_root=workspace,
                artifacts_root=root,
                track_id="operator-canary-real",
                route_hint="R3",
                execution_shape="bounded_swarm",
            )
            canary_view = load_operator_view_payload(track_id="operator-canary-real", artifacts_root=root)
            canary_text = render_operator_view_text(canary_view, selected_view="canary")
            evaluation_canary_text = render_operator_view_text(canary_view, selected_view="evaluation")
            self.assertIn("safe to run: True", canary_text)
            self.assertIn("isolation mode:", canary_text)
            self.assertIn("has canary: True", evaluation_canary_text)
            self.assertIn("canary safety mode:", evaluation_canary_text)
            self.assertFalse(canary["refused"])

    def test_swarm_views_render_from_real_runtime_artifact(self) -> None:
        plan = self._r3_swarm_fixture()
        track_id = "operator-swarm-runtime"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            rc, _ = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="step",
            )
            self.assertEqual(rc, 10)
            operator_view = load_operator_view_payload(track_id=track_id, artifacts_root=root)
            swarm_text = render_operator_view_text(operator_view, selected_view="swarm")
            lanes_text = render_operator_view_text(operator_view, selected_view="lanes")
            frontier_why_text = render_operator_view_text(operator_view, selected_view="frontier-why")
            convergence_text = render_operator_view_text(operator_view, selected_view="convergence")
            self.assertIn("swarm status: bounded_swarm", swarm_text)
            self.assertIn("execution shape: bounded_swarm", swarm_text)
            self.assertIn("lanes:", lanes_text)
            self.assertIn("- validator:", lanes_text)
            self.assertIn("runnable but not dispatched:", frontier_why_text)
            self.assertIn("convergence status:", convergence_text)

    def test_frontier_why_view_explains_scope_conflict_in_swarm_mode(self) -> None:
        plan = self._r3_swarm_fixture(conflicting_workers=True)
        track_id = "operator-frontier-why-conflict"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            rc, _ = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="step",
            )
            self.assertEqual(rc, 10)
            operator_view = load_operator_view_payload(track_id=track_id, artifacts_root=root)
            frontier_why_text = render_operator_view_text(operator_view, selected_view="frontier-why")
            lanes_text = render_operator_view_text(operator_view, selected_view="lanes")
            self.assertIn("packet-worker-b", frontier_why_text)
            self.assertIn("allowed_scope_conflict", frontier_why_text)
            self.assertIn("- worker:", lanes_text)


if __name__ == "__main__":
    unittest.main()
