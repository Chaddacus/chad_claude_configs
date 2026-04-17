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

from common import canonical_python_argv, cycle_artifact_paths, runtime_artifact_paths, session_artifact_paths  # noqa: E402
from compile_implementation import compile_implementation_payload  # noqa: E402
from compile_intent import compile_intent_payload  # noqa: E402
from compile_plan import compile_plan_payload  # noqa: E402
from initialize_session import initialize_session_payload  # noqa: E402
from objective_runtime import _controller_verdict, _recover_runtime_transaction, bootstrap_runtime, governed_runtime, run_runtime, step  # noqa: E402
from validate_impl import validate_impl_payload  # noqa: E402
from verify_plan import verify_plan_payload  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
COMPILE_IMPLEMENTATION_SCRIPT = SCRIPT_DIR / "compile_implementation.py"


class ObjectiveRuntimeTests(unittest.TestCase):
    def _create_fake_real_codex(self, path: Path, prompt_log: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"""#!/usr/bin/env python3
import sys
from pathlib import Path
log = Path({str(prompt_log)!r})
argv = sys.argv[1:]
if argv and argv[0] == 'exec':
    prompt = argv[1] if len(argv) > 1 else ''
    with log.open('a', encoding='utf-8') as fh:
        fh.write(prompt.replace('\\n', '\\\\n') + '\\n')
    sys.exit(0)
sys.exit(0)
""",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _create_fake_ralph(self, codex_home: Path, mode: str) -> None:
        script = codex_home / "bin" / "ralph_done_loop.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            """#!/usr/bin/env python3
import argparse, json, sys
p = argparse.ArgumentParser()
p.add_argument('--loop-index', type=int, default=1)
p.add_argument('--max-loops', type=int, default=3)
p.add_argument('--route-task-id', required=False)
p.add_argument('--route-class', required=False)
p.add_argument('--track-id', required=False)
p.add_argument('--plan-json', required=False)
p.add_argument('--impl-json', required=False)
p.add_argument('--review-json', required=False)
p.add_argument('--workspace-root', required=False)
p.add_argument('--codex-home', required=False)
p.add_argument('--mode', required=False)
p.add_argument('--artifacts-root', required=False)
p.add_argument('--timeout-sec', required=False)
p.add_argument('--external-remediation-loop', action='store_true')
args = p.parse_args()
base = {
  'schema_version':'postflight_result.v1',
  'route_task_id':args.route_task_id or 'test',
  'route_class':args.route_class or 'R3',
  'track_id':args.track_id or 't',
  'loop_count':args.loop_index,
  'gate_results':[],
  'missing_fields':['dod.evidence'],
  'blocked_fields':[],
  'next_action_prompt':'Fix missing DoD evidence from validator output.'
}
if '""" + mode + """' == 'revise_then_approve':
    if args.loop_index == 1:
        base.update({'ok':False,'status':'revise','reason_code':'FINALIZE_REVISE_REQUIRED','reason':'revise','exit_code':10})
        print(json.dumps(base, sort_keys=True))
        raise SystemExit(10)
    base.update({'ok':True,'status':'approve','reason_code':'APPROVED','reason':'approve','exit_code':0})
    print(json.dumps(base, sort_keys=True))
    raise SystemExit(0)
base.update({'ok':True,'status':'approve','reason_code':'APPROVED','reason':'approve','exit_code':0})
print(json.dumps(base, sort_keys=True))
raise SystemExit(0)
""",
            encoding="utf-8",
        )
        script.chmod(0o755)

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
        scheduler_policy = plan.setdefault("scheduler_policy", {})
        scheduler_policy["execution_shape"] = execution_shape
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
        review_output: str = "accepted",
        evidence_refs: list[str] | None = None,
        produced_artifacts: list[str] | None = None,
        support_expectations: dict | None = None,
        external_support_required: bool | None = None,
        support_remediation_mode: str | None = None,
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
        packet["evidence_destination"] = f"planning_artifacts/<track-id>/packets/{packet_id}.verdict.json"
        packet["definition_of_done"]["allowed_scope"] = allowed_scope
        packet["definition_of_done"]["objective_linkage"] = f"req-{packet_id}"
        if execution_strategy:
            packet["execution_strategy"] = execution_strategy
        if dependency_mode == "accepted_upstream":
            packet["stub_dependencies"] = []
        if support_expectations is not None:
            packet["support_expectations"] = support_expectations
        if external_support_required is not None:
            packet["external_support_required"] = external_support_required
        if support_remediation_mode is not None:
            packet["support_remediation_mode"] = support_remediation_mode
        simulation_attempt = {
            "worker_exit_code": 0,
            "stdout": f"{packet_id} complete",
            "review_output": review_output,
            "allowed_scope_status": "within_scope",
            "changed_files": allowed_scope,
            "evidence_refs": evidence_refs or [f"capture://{packet_id}"],
            "captured_commands": [{"command": f"echo {packet_id}", "exit_code": 0}],
            "result_artifact_path": f"planning_artifacts/<track-id>/cycles/{packet_id}.json",
        }
        if file_write_content is not None and allowed_scope:
            simulation_attempt["file_writes"] = [{"path": allowed_scope[0], "content": file_write_content}]
        if produced_artifacts is not None:
            simulation_attempt["produced_artifacts"] = produced_artifacts
        packet["simulation"] = {"attempts": [simulation_attempt]}
        return packet

    def _r2_parallel_fixture(self) -> dict:
        plan = self._runtime_plan(route_hint="R2", execution_shape="single_lane")
        scheduler_policy = plan["scheduler_policy"]
        scheduler_policy["parallelism_policy"] = "bounded_parallel"
        scheduler_policy["max_parallel_packets"] = 2
        worker = self._make_packet(
            base_packet=plan["packets"][0],
            packet_id="packet-worker",
            lane="worker",
            parallelism_class="bounded",
            allowed_scope=["compile_plan.py"],
            execution_strategy="command_capture",
            file_write_content="packet-worker-updated\n",
        )
        validator = self._make_packet(
            base_packet=plan["packets"][1],
            packet_id="packet-validator",
            lane="validator",
            parallelism_class="isolated",
            allowed_scope=["verify_plan.py"],
            execution_strategy="validation_command",
            file_write_content="packet-validator-updated\n",
        )
        plan["packets"] = [worker, validator]
        plan["required_packets"] = [packet["packet_id"] for packet in plan["packets"]]
        return plan

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

    def _r4_reviewer_swarm_fixture(self) -> dict:
        plan = self._runtime_plan(route_hint="R4", execution_shape="bounded_swarm")
        scheduler_policy = plan["scheduler_policy"]
        scheduler_policy["route_swarm_cap"] = 2
        scheduler_policy["lane_caps"] = {"worker": 1, "validator": 1, "reviewer": 1}
        worker = self._make_packet(
            base_packet=plan["packets"][0],
            packet_id="packet-worker",
            lane="worker",
            parallelism_class="bounded",
            allowed_scope=["compile_plan.py"],
            execution_strategy="command_capture",
            file_write_content="packet-worker-updated\n",
        )
        validator = self._make_packet(
            base_packet=plan["packets"][1],
            packet_id="packet-validator",
            lane="validator",
            parallelism_class="isolated",
            allowed_scope=["verify_plan.py"],
            execution_strategy="validation_command",
            file_write_content="packet-validator-updated\n",
        )
        reviewer = self._make_packet(
            base_packet=plan["packets"][0],
            packet_id="packet-reviewer",
            lane="reviewer",
            parallelism_class="serial",
            allowed_scope=["validate_plan.py"],
            execution_strategy="command_capture",
            execution_mode="sequence_required",
            dependencies=["packet-worker", "packet-validator"],
            dependency_mode="accepted_upstream",
            file_write_content="packet-reviewer-updated\n",
        )
        reviewer["acceptance_checks"] = ["review evidence emitted"]
        reviewer["definition_of_done"]["acceptance_checks"] = ["review evidence emitted"]
        reviewer["definition_of_done"]["verifier_acceptance_condition"] = "Review evidence emitted."
        plan["packets"] = [worker, validator, reviewer]
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

    def _load_transition_rows(self, *, root: Path, track_id: str) -> list[dict]:
        return [
            json.loads(line)
            for line in runtime_artifact_paths(artifacts_root=root, track_id=track_id)["transition_history"].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _load_verification_rows(self, *, root: Path, track_id: str) -> list[dict]:
        return [
            json.loads(line)
            for line in runtime_artifact_paths(artifacts_root=root, track_id=track_id)["verification_results"].read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _load_kernel_runtime_state(self, *, root: Path, track_id: str) -> dict:
        return json.loads(runtime_artifact_paths(artifacts_root=root, track_id=track_id)["kernel_runtime_state"].read_text(encoding="utf-8"))

    def _load_transaction_state(self, *, root: Path, track_id: str) -> dict:
        return json.loads(runtime_artifact_paths(artifacts_root=root, track_id=track_id)["transaction_state"].read_text(encoding="utf-8"))

    def _load_transaction_rows(self, *, root: Path, track_id: str) -> list[dict]:
        path = runtime_artifact_paths(artifacts_root=root, track_id=track_id)["transaction_log"]
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_bootstrap_seeds_runtime_and_packet_aware_session_artifacts(self) -> None:
        plan = self._fixture()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compile_intent_payload(plan_payload=plan, track_id="runtime-bootstrap", artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id="runtime-bootstrap", artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id="runtime-bootstrap", artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id="runtime-bootstrap", artifacts_root=root)
            bootstrap_runtime(plan=plan, track_id="runtime-bootstrap", artifacts_root=root)
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id="runtime-bootstrap")
            session_paths = session_artifact_paths(artifacts_root=root, track_id="runtime-bootstrap")
            optional_runtime_artifacts = {"objective.benchmark.json", "objective.canary.json"}
            for path in runtime_paths.values():
                if path.name.startswith("."):
                    continue
                if path.name in optional_runtime_artifacts:
                    continue
                self.assertTrue(path.exists(), path.name)
            self.assertTrue(session_paths["feature_list"].exists())
            self.assertTrue(session_paths["momentum"].exists())
            self.assertTrue(session_paths["blockers"].exists())
            summary = json.loads(runtime_paths["summary"].read_text(encoding="utf-8"))
            runtime_state = json.loads(runtime_paths["runtime_state"].read_text(encoding="utf-8"))
            kernel_runtime_state = json.loads(runtime_paths["kernel_runtime_state"].read_text(encoding="utf-8"))
            execution_plan = json.loads(runtime_paths["execution_plan"].read_text(encoding="utf-8"))
            validation_plan = json.loads(runtime_paths["validation_plan"].read_text(encoding="utf-8"))
            repo_capabilities = json.loads(runtime_paths["repo_capabilities"].read_text(encoding="utf-8"))
            packet_quality = json.loads(runtime_paths["packet_quality"].read_text(encoding="utf-8"))
            execution_coverage = json.loads(runtime_paths["execution_coverage"].read_text(encoding="utf-8"))
            transition_history = [
                json.loads(line)
                for line in runtime_paths["transition_history"].read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            transaction_state = json.loads(runtime_paths["transaction_state"].read_text(encoding="utf-8"))
            transaction_rows = [json.loads(line) for line in runtime_paths["transaction_log"].read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(summary["route_hint"], "R3")
            self.assertEqual(runtime_state["route_hint"], "R3")
            self.assertEqual(runtime_state["lifecycle_status"], "revise")
            self.assertTrue(summary["required_work_remaining"])
            self.assertTrue(runtime_state["required_work_remaining"])
            self.assertFalse(summary["stop_allowed"])
            self.assertFalse(runtime_state["stop_allowed"])
            self.assertEqual(execution_plan["schema_version"], "execution-plan.v1")
            self.assertTrue(execution_plan["units"])
            self.assertEqual(kernel_runtime_state["schema_version"], "runtime-state.v1")
            self.assertEqual(kernel_runtime_state["state"], "ready")
            self.assertEqual(len(transition_history), 2)
            self.assertEqual(transaction_state["state"], "committed")
            self.assertEqual(transaction_rows[0]["state"], "prepared")
            self.assertEqual(transaction_rows[-1]["state"], "committed")
            self.assertEqual(transition_history[0]["to"], "planning_complete")
            self.assertEqual(transition_history[1]["to"], "ready")
            self.assertTrue(validation_plan["lanes"])
            self.assertTrue(validation_plan["generated_packets"])
            self.assertIn("tests", repo_capabilities["capabilities"])
            self.assertEqual(packet_quality["schema_version"], "objective-packet-quality.v1")
            self.assertEqual(execution_coverage["schema_version"], "objective-execution-coverage.v1")

    def test_run_completes_and_compiles_valid_implementation(self) -> None:
        plan = self._fixture()
        track_id = "runtime-complete"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
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
            summary = json.loads(runtime_artifact_paths(artifacts_root=root, track_id=track_id)["summary"].read_text(encoding="utf-8"))
            runtime_state = json.loads(runtime_artifact_paths(artifacts_root=root, track_id=track_id)["runtime_state"].read_text(encoding="utf-8"))
            kernel_runtime_state = json.loads(runtime_artifact_paths(artifacts_root=root, track_id=track_id)["kernel_runtime_state"].read_text(encoding="utf-8"))
            verification_rows = [
                json.loads(line)
                for line in runtime_artifact_paths(artifacts_root=root, track_id=track_id)["verification_results"].read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            transition_rows = [
                json.loads(line)
                for line in runtime_artifact_paths(artifacts_root=root, track_id=track_id)["transition_history"].read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            execution_coverage = json.loads(runtime_artifact_paths(artifacts_root=root, track_id=track_id)["execution_coverage"].read_text(encoding="utf-8"))
            self.assertTrue({"test_command", "typecheck_command", "lint_command"} & set(summary["strategy_mix"]))
            self.assertFalse(summary["required_work_remaining"])
            self.assertFalse(summary["material_optional_work_remaining"])
            self.assertTrue(summary["stop_allowed"])
            self.assertEqual(summary["stop_reason"], "all_policy_backed_work_satisfied")
            self.assertEqual(runtime_state["lifecycle_status"], "approved")
            self.assertTrue(runtime_state["stop_allowed"])
            self.assertEqual(runtime_state["current_packet"], "finalize")
            self.assertEqual(kernel_runtime_state["state"], "success")
            self.assertTrue(kernel_runtime_state["halt"]["terminal"])
            self.assertTrue(verification_rows)
            self.assertIn(verification_rows[-1]["status"], {"pass", "soft_fail", "hard_fail"})
            self.assertGreaterEqual(len(transition_rows), 4)
            self.assertTrue((runtime_artifact_paths(artifacts_root=root, track_id=track_id)["execution_ledger"]).exists())
            self.assertTrue((runtime_artifact_paths(artifacts_root=root, track_id=track_id)["packet_results"]).exists())
            self.assertGreaterEqual(execution_coverage["deterministic_ratio"], 0.90)
            checkpoint = json.loads(
                session_artifact_paths(artifacts_root=root, track_id=track_id)["checkpoint"].read_text(encoding="utf-8")
            )
            self.assertFalse(checkpoint["checkpoint_blocked"])
            self.assertTrue(checkpoint["checkpoint_commit"])
            self.assertTrue(checkpoint["rollback_validation_ref"])
            impl = compile_implementation_payload(
                plan_payload=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
            )
            self.assertIn("objective_runtime_state", impl)
            self.assertTrue(impl["objective_runtime_state"]["stop_allowed"])
            self.assertEqual(impl["objective_runtime_state"]["artifact_path"], str(runtime_artifact_paths(artifacts_root=root, track_id=track_id)["runtime_state"]))
            self.assertTrue(impl["objective_summary"])
            self.assertTrue(impl["validation_plan"])

    def test_verifying_soft_fail_transitions_to_repair_pending_with_failure_record(self) -> None:
        plan = self._fixture()
        track_id = "runtime-repair-pending"
        plan["packets"][0]["simulation"]["attempts"][0]["review_output"] = "rejected_rework"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            bootstrap_runtime(plan=plan, track_id=track_id, artifacts_root=root)
            before_state = self._load_kernel_runtime_state(root=root, track_id=track_id)
            result = step(
                plan_payload=plan,
                artifacts_root=root,
                track_id=track_id,
                cwd=str(root),
                codex_home=None,
                controller_mode="enforce",
            )
            self.assertTrue(result["valid"])
            kernel_state = self._load_kernel_runtime_state(root=root, track_id=track_id)
            verification_rows = self._load_verification_rows(root=root, track_id=track_id)
            transition_rows = self._load_transition_rows(root=root, track_id=track_id)
            transaction_state = self._load_transaction_state(root=root, track_id=track_id)
            transaction_rows = self._load_transaction_rows(root=root, track_id=track_id)
            self.assertTrue(result["transaction_id"])
            self.assertEqual(result["transaction_state"], "committed")
            self.assertFalse(result["recovered"])
            self.assertGreater(result["committed_artifact_count"], 0)
            self.assertEqual(transaction_state["state"], "committed")
            self.assertEqual(transaction_rows[-1]["state"], "committed")
            self.assertEqual(kernel_state["state"], "repair_pending")
            self.assertEqual(kernel_state["halt"]["reason"], "none")
            self.assertEqual(verification_rows[-1]["status"], "soft_fail")
            self.assertEqual(verification_rows[-1]["suggested_transition"], "repair_pending")
            self.assertTrue(kernel_state["failed_attempts"])
            self.assertEqual(kernel_state["failed_attempts"][-1]["failure_class"], "verification_soft_fail")
            self.assertEqual(
                kernel_state["budget"]["remaining_retries"],
                before_state["budget"]["remaining_retries"] - 1,
            )
            self.assertEqual(transition_rows[-1]["to"], "repair_pending")
            self.assertEqual(
                transition_rows[-1]["guard"],
                "verification.status == soft_fail && verification.repairability in ['local_patch','retryable']",
            )
            self.assertEqual(transition_rows[-1]["trigger"], "review_applied")

    def test_verifying_pass_transitions_through_finalize_pending_to_success(self) -> None:
        plan = self._fixture()
        track_id = "runtime-finalize-success"
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
            kernel_state = self._load_kernel_runtime_state(root=root, track_id=track_id)
            transition_rows = self._load_transition_rows(root=root, track_id=track_id)
            self.assertEqual(kernel_state["state"], "success")
            self.assertEqual(kernel_state["halt"]["reason"], "accepted_success")
            self.assertEqual(transition_rows[-2]["to"], "finalize_pending")
            self.assertEqual(
                transition_rows[-2]["guard"],
                "verification.status == pass && all_required_acceptance_checks_satisfied",
            )
            self.assertEqual(transition_rows[-2]["trigger"], "review_applied")
            self.assertEqual(transition_rows[-1]["from"], "finalize_pending")
            self.assertEqual(transition_rows[-1]["to"], "success")
            self.assertEqual(transition_rows[-1]["trigger"], "closure_adjudicated")

    def test_boundary_shrink_transitions_finalize_pending_to_partial(self) -> None:
        plan = self._fixture()
        track_id = "runtime-finalize-partial"
        plan["packets"][0]["simulation"]["attempts"][0]["review_output"] = "accepted"
        plan["packets"][0]["simulation"]["attempts"][0]["evidence_refs"] = ["capture://packet-compiler"]
        plan["packets"][1]["simulation"]["attempts"][0]["review_output"] = "blocked_boundary"
        plan["packets"][1]["simulation"]["attempts"][0]["blocked_reason"] = "external_evidence"
        plan["packets"][1]["simulation"]["attempts"][0]["evidence_refs"] = ["capture://packet-verifier-blocked"]
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
            self.assertEqual(payload["closure_state"], "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK")
            kernel_state = self._load_kernel_runtime_state(root=root, track_id=track_id)
            transition_rows = self._load_transition_rows(root=root, track_id=track_id)
            self.assertEqual(kernel_state["state"], "partial")
            self.assertEqual(kernel_state["halt"]["reason"], "accepted_partial")
            self.assertEqual(transition_rows[-2]["to"], "finalize_pending")
            self.assertEqual(transition_rows[-1]["from"], "finalize_pending")
            self.assertEqual(transition_rows[-1]["to"], "partial")
            self.assertEqual(transition_rows[-1]["trigger"], "closure_adjudicated")

    def test_blocked_closure_transitions_verifying_to_blocked_then_closed_blocked(self) -> None:
        plan = self._fixture()
        track_id = "runtime-finalize-blocked"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "unrelated.txt").write_text("dirty\n", encoding="utf-8")
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            rc, payload = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="run",
            )
            self.assertEqual(rc, 20)
            self.assertEqual(payload["closure_state"], "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED")
            kernel_state = self._load_kernel_runtime_state(root=root, track_id=track_id)
            transition_rows = self._load_transition_rows(root=root, track_id=track_id)
            self.assertEqual(kernel_state["state"], "closed_blocked")
            self.assertEqual(kernel_state["halt"]["reason"], "accepted_blocked")
            self.assertEqual(transition_rows[-2]["to"], "blocked")
            self.assertEqual(
                transition_rows[-2]["guard"],
                "verification.repairability == blocked || verification.scope == environment",
            )
            self.assertEqual(transition_rows[-2]["trigger"], "review_applied")
            self.assertEqual(transition_rows[-1]["from"], "blocked")
            self.assertEqual(transition_rows[-1]["to"], "closed_blocked")
            self.assertEqual(transition_rows[-1]["guard"], "external_blocker_evidenced && no_authorized_path_forward")

    def test_finalize_pending_invalid_state_traps_to_unsafe(self) -> None:
        plan = self._fixture()
        track_id = "runtime-finalize-unsafe"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            bootstrap_runtime(plan=plan, track_id=track_id, artifacts_root=root)
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
            kernel_state = self._load_kernel_runtime_state(root=root, track_id=track_id)
            kernel_state["state"] = "finalize_pending"
            kernel_state["halt"] = {"terminal": True, "reason": "accepted_success"}
            runtime_paths["kernel_runtime_state"].write_text(json.dumps(kernel_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            result = step(
                plan_payload=plan,
                artifacts_root=root,
                track_id=track_id,
                cwd=str(root),
                codex_home=None,
                controller_mode="enforce",
            )
            trapped_state = self._load_kernel_runtime_state(root=root, track_id=track_id)
            invalid_transition = json.loads(runtime_paths["invalid_transition"].read_text(encoding="utf-8"))
            transition_rows = self._load_transition_rows(root=root, track_id=track_id)
            self.assertFalse(result["valid"])
            self.assertEqual(trapped_state["state"], "unsafe")
            self.assertEqual(trapped_state["halt"]["reason"], "invalid_transition")
            self.assertEqual(invalid_transition["step_id"], "step-preflight")
            self.assertTrue(invalid_transition["errors"])
            self.assertEqual(transition_rows[-1]["from"], "finalize_pending")
            self.assertEqual(transition_rows[-1]["to"], "unsafe")
            self.assertEqual(transition_rows[-1]["guard"], "invalid_transition_detected")

    def test_prepare_only_bootstrap_transaction_recovers_by_finishing_commit(self) -> None:
        plan = self._fixture()
        track_id = "runtime-tx-recover-prepare"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            prior = os.environ.get("CODEX_OBJECTIVE_TX_PREPARE_ONLY")
            try:
                os.environ["CODEX_OBJECTIVE_TX_PREPARE_ONLY"] = "1"
                with self.assertRaises(RuntimeError):
                    bootstrap_runtime(plan=plan, track_id=track_id, artifacts_root=root)
            finally:
                if prior is None:
                    os.environ.pop("CODEX_OBJECTIVE_TX_PREPARE_ONLY", None)
                else:
                    os.environ["CODEX_OBJECTIVE_TX_PREPARE_ONLY"] = prior
            prepared_state = self._load_transaction_state(root=root, track_id=track_id)
            self.assertEqual(prepared_state["state"], "prepared")
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
            recovered_state = self._load_transaction_state(root=root, track_id=track_id)
            transaction_rows = self._load_transaction_rows(root=root, track_id=track_id)
            self.assertEqual(recovered_state["state"], "committed")
            self.assertIn("recovered", [row["state"] for row in transaction_rows])
            self.assertTrue(any(row.get("recovered") is True for row in transaction_rows))

    def test_partial_commit_bootstrap_transaction_recovers_by_finishing_commit(self) -> None:
        plan = self._fixture()
        track_id = "runtime-tx-recover-partial"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            prior = os.environ.get("CODEX_OBJECTIVE_TX_FAIL_AFTER")
            try:
                os.environ["CODEX_OBJECTIVE_TX_FAIL_AFTER"] = "1"
                with self.assertRaises(RuntimeError):
                    bootstrap_runtime(plan=plan, track_id=track_id, artifacts_root=root)
            finally:
                if prior is None:
                    os.environ.pop("CODEX_OBJECTIVE_TX_FAIL_AFTER", None)
                else:
                    os.environ["CODEX_OBJECTIVE_TX_FAIL_AFTER"] = prior
            prepared_state = self._load_transaction_state(root=root, track_id=track_id)
            self.assertEqual(prepared_state["state"], "committing")
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
            recovered_state = self._load_transaction_state(root=root, track_id=track_id)
            transaction_rows = self._load_transaction_rows(root=root, track_id=track_id)
            self.assertEqual(recovered_state["state"], "committed")
            self.assertIn("recovered", [row["state"] for row in transaction_rows])
            self.assertTrue(any(row.get("recovered") is True for row in transaction_rows))

    def test_corrupted_staged_transaction_traps_fail_closed(self) -> None:
        plan = self._fixture()
        track_id = "runtime-tx-corrupt"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            prior = os.environ.get("CODEX_OBJECTIVE_TX_PREPARE_ONLY")
            try:
                os.environ["CODEX_OBJECTIVE_TX_PREPARE_ONLY"] = "1"
                with self.assertRaises(RuntimeError):
                    bootstrap_runtime(plan=plan, track_id=track_id, artifacts_root=root)
            finally:
                if prior is None:
                    os.environ.pop("CODEX_OBJECTIVE_TX_PREPARE_ONLY", None)
                else:
                    os.environ["CODEX_OBJECTIVE_TX_PREPARE_ONLY"] = prior
            transaction_state = self._load_transaction_state(root=root, track_id=track_id)
            staged_target = next(
                target for target in transaction_state["targets"] if target["artifact_key"] == "kernel_runtime_state"
            )
            Path(staged_target["staged_path"]).unlink()
            rc, payload = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="run",
            )
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
            trapped_state = self._load_kernel_runtime_state(root=root, track_id=track_id)
            invalid_transition = json.loads(runtime_paths["invalid_transition"].read_text(encoding="utf-8"))
            aborted_state = self._load_transaction_state(root=root, track_id=track_id)
            self.assertEqual(rc, 20)
            self.assertEqual(payload["reason_code"], "TRANSACTION_INTEGRITY_FAILURE")
            self.assertEqual(trapped_state["state"], "unsafe")
            self.assertEqual(trapped_state["halt"]["reason"], "invalid_transition")
            self.assertTrue(invalid_transition["errors"])
            self.assertEqual(aborted_state["state"], "aborted")
            self.assertEqual(aborted_state["recovery_outcome"], "integrity_failure")

    def test_pretransaction_cycle_request_recovers_without_duplicate_cycle(self) -> None:
        plan = self._fixture()
        track_id = "runtime-cycle-recovery"
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
            cycle_ids_before = sorted(path.name for path in cycles_root.iterdir() if path.is_dir())
            self.assertEqual(len(cycle_ids_before), 1)
            cycle_paths = cycle_artifact_paths(artifacts_root=root, track_id=track_id, cycle_id=cycle_ids_before[0])
            self.assertTrue(cycle_paths["request"].exists())
            self.assertTrue(cycle_paths["result"].exists())
            self.assertTrue(cycle_paths["review"].exists())
            self.assertEqual(json.loads(cycle_paths["state"].read_text(encoding="utf-8"))["phase"], "requested")
            recovery = _recover_runtime_transaction(artifacts_root=root, track_id=track_id)
            self.assertIsNotNone(recovery)
            assert recovery is not None
            self.assertEqual(recovery["transaction_state"], "recovered")
            cycle_ids_after = sorted(path.name for path in cycles_root.iterdir() if path.is_dir())
            self.assertEqual(cycle_ids_after, cycle_ids_before)
            self.assertEqual(json.loads(cycle_paths["state"].read_text(encoding="utf-8"))["phase"], "applied")
            transaction_rows = self._load_transaction_rows(root=root, track_id=track_id)
            self.assertIn("recovered", [row["state"] for row in transaction_rows])

    def test_step_traps_invalid_kernel_state_to_unsafe(self) -> None:
        plan = self._fixture()
        track_id = "runtime-invalid-kernel"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            bootstrap_runtime(plan=plan, track_id=track_id, artifacts_root=root)
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
            kernel_runtime_state = json.loads(runtime_paths["kernel_runtime_state"].read_text(encoding="utf-8"))
            kernel_runtime_state["state"] = "verifying"
            kernel_runtime_state["evidence_refs"] = []
            runtime_paths["kernel_runtime_state"].write_text(json.dumps(kernel_runtime_state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            result = step(
                plan_payload=plan,
                artifacts_root=root,
                track_id=track_id,
                cwd=str(root),
                codex_home=None,
                controller_mode="enforce",
            )
            trapped_state = json.loads(runtime_paths["kernel_runtime_state"].read_text(encoding="utf-8"))
            invalid_transition = json.loads(runtime_paths["invalid_transition"].read_text(encoding="utf-8"))
            self.assertFalse(result["valid"])
            self.assertEqual(result["runtime_payload"]["status"], "blocked")
            self.assertEqual(trapped_state["state"], "unsafe")
            self.assertTrue(trapped_state["halt"]["terminal"])
            self.assertEqual(trapped_state["halt"]["reason"], "invalid_transition")
            self.assertTrue(invalid_transition["errors"])

    def test_reviewed_cycle_is_applied_once_on_resume(self) -> None:
        plan = self._fixture()
        track_id = "runtime-resume"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            bootstrap_runtime(plan=plan, track_id=track_id, artifacts_root=root)
            rc, payload = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="step",
            )
            self.assertIn(rc, {0, 10})
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
            packet_dag_before = json.loads(runtime_paths["packet_dag"].read_text(encoding="utf-8"))
            accepted_before = [
                packet["packet_id"]
                for packet in packet_dag_before["packets"]
                if packet.get("runtime_state") == "accepted"
            ]
            rc, payload = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="resume",
            )
            packet_dag_after = json.loads(runtime_paths["packet_dag"].read_text(encoding="utf-8"))
            accepted_after = [
                packet["packet_id"]
                for packet in packet_dag_after["packets"]
                if packet.get("runtime_state") == "accepted"
            ]
            self.assertGreaterEqual(len(accepted_after), len(accepted_before))

    def test_rejected_packet_pivots_to_alternate_strategy_once(self) -> None:
        plan = self._fixture()
        track_id = "runtime-adaptation"
        plan["packets"][0]["alternate_strategies"] = ["multi_command_pipeline"]
        plan["packets"][0]["adaptation_policy"] = "bounded_retry_then_alternate"
        plan["packets"][0]["max_adaptations"] = 1
        plan["packets"][0]["simulation"]["attempts"] = [
            {
                "worker_exit_code": 1,
                "stdout": "",
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
            compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            rc, payload = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="step",
            )
            rc, payload = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="step",
            )
            self.assertIn(rc, {0, 10, 20})
            packet_dag = json.loads(runtime_artifact_paths(artifacts_root=root, track_id=track_id)["packet_dag"].read_text(encoding="utf-8"))
            compiler_packet = next(packet for packet in packet_dag["packets"] if packet["packet_id"] == "packet-compiler")
            self.assertEqual(compiler_packet["execution_strategy"], "multi_command_pipeline")
            adaptation_events = runtime_artifact_paths(artifacts_root=root, track_id=track_id)["adaptation_log"].read_text(encoding="utf-8").strip().splitlines()
            self.assertTrue(adaptation_events)

    def test_compile_implementation_revises_when_required_capture_is_missing(self) -> None:
        plan = self._fixture()
        track_id = "runtime-missing-capture"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            rc, _ = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="run",
            )
            self.assertEqual(rc, 0)
            (root / track_id / "captures" / "smoke-100" / "manifest.json").unlink()
            out_path = root / "implementation.review.json"
            completed = subprocess.run(
                canonical_python_argv(
                    str(COMPILE_IMPLEMENTATION_SCRIPT),
                    "--plan-json",
                    str(FIXTURES / "plan_valid.json"),
                    "--track-id",
                    track_id,
                    "--artifacts-root",
                    str(root),
                    "--workspace-root",
                    str(root),
                    "--out",
                    str(out_path),
                ),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 10, completed.stderr)
            review = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(review["status"], "revise")
            self.assertIn("runtime_capture_requirements:missing_smoke_stage:100%", review["blocked_fields"][0])

    def test_unrelated_dirty_repo_blocks_checkpoint_and_closure(self) -> None:
        plan = self._fixture()
        track_id = "runtime-dirty"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            (root / "unrelated.txt").write_text("dirty\n", encoding="utf-8")
            compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            rc, payload = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="run",
            )
            self.assertEqual(rc, 20)
            self.assertEqual(payload["closure_state"], "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED")
            checkpoint = json.loads(
                session_artifact_paths(artifacts_root=root, track_id=track_id)["checkpoint"].read_text(encoding="utf-8")
            )
            self.assertTrue(checkpoint["checkpoint_blocked"])
            self.assertEqual(checkpoint["checkpoint_block_reason"], "unrelated_dirty_state")

    def test_external_evidence_boundary_shrink_is_accepted_blocked_closure(self) -> None:
        plan = self._fixture()
        track_id = "runtime-boundary-shrink"
        plan["packets"][0]["simulation"]["attempts"][0]["review_output"] = "accepted"
        plan["packets"][0]["simulation"]["attempts"][0]["evidence_refs"] = ["capture://packet-compiler"]
        plan["packets"][1]["simulation"]["attempts"][0]["review_output"] = "blocked_boundary"
        plan["packets"][1]["simulation"]["attempts"][0]["blocked_reason"] = "external_evidence"
        plan["packets"][1]["simulation"]["attempts"][0]["evidence_refs"] = ["capture://packet-verifier-blocked"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            rc, payload = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="run",
            )
            self.assertEqual(rc, 0)
            self.assertEqual(payload["closure_state"], "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK")
            status = json.loads(runtime_artifact_paths(artifacts_root=root, track_id=track_id)["status"].read_text(encoding="utf-8"))
            self.assertEqual(status["boundary_shrunk_remainder"], ["packet-verifier"])

    def test_missing_accepted_evidence_blocks_cycle_review(self) -> None:
        plan = self._fixture()
        track_id = "runtime-missing-evidence"
        for packet in plan["packets"]:
            packet["simulation"]["attempts"][0]["review_output"] = "accepted"
            packet["simulation"]["attempts"][0]["evidence_refs"] = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            rc, payload = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="step",
            )
            self.assertEqual(rc, 20)
            self.assertEqual(payload["reason_code"], "CYCLE_REVIEW_BLOCKED")
            self.assertTrue(any("accepted_requires_evidence" in item for item in payload["blocked_fields"]))

    def test_r2_bounded_parallel_runtime_dispatches_two_safe_packets(self) -> None:
        plan = self._r2_parallel_fixture()
        track_id = "runtime-r2-bounded-parallel"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            rc, payload = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="step",
            )
            self.assertEqual(rc, 10)
            self.assertEqual(payload["status"], "revise")
            self.assertEqual(payload["reason_code"], "REQUIRED_WORK_REMAINING")
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
            schedule = json.loads(runtime_paths["schedule"].read_text(encoding="utf-8"))
            summary = json.loads(runtime_paths["summary"].read_text(encoding="utf-8"))
            operator_view = json.loads(runtime_paths["operator_view"].read_text(encoding="utf-8"))
            self.assertEqual(schedule["parallelism_policy"], "bounded_parallel")
            self.assertEqual(schedule["max_parallel_packets"], 2)
            self.assertEqual(schedule["execution_shape"], "single_lane")
            self.assertEqual(sorted(schedule["cycle_log"][-1]["packet_ids"]), ["packet-validator", "packet-worker"])
            self.assertEqual(summary["route_hint"], "R2")
            self.assertEqual(summary["execution_shape"], "single_lane")
            self.assertTrue(summary["required_work_remaining"])
            self.assertFalse(summary["stop_allowed"])
            self.assertEqual(operator_view["swarm_status"], "single_lane")
            self.assertEqual(operator_view["active_packets_by_lane"]["validator"], [])
            self.assertEqual(operator_view["active_packets_by_lane"]["worker"], [])

    def test_r3_bounded_swarm_happy_path_dispatches_parallel_then_holds_reviewer(self) -> None:
        plan = self._r3_swarm_fixture()
        track_id = "runtime-r3-bounded-swarm"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            rc1, _ = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="step",
            )
            self.assertEqual(rc1, 10)
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
            schedule_step1 = json.loads(runtime_paths["schedule"].read_text(encoding="utf-8"))
            self.assertEqual(sorted(schedule_step1["cycle_log"][-1]["packet_ids"]), ["packet-explorer", "packet-validator"])
            self.assertEqual(schedule_step1["current_frontier"], ["packet-worker"])
            rc2, _ = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="step",
            )
            self.assertEqual(rc2, 10)
            schedule_step2 = json.loads(runtime_paths["schedule"].read_text(encoding="utf-8"))
            operator_view = json.loads(runtime_paths["operator_view"].read_text(encoding="utf-8"))
            self.assertEqual(schedule_step2["cycle_log"][-1]["packet_ids"], ["packet-worker"])
            self.assertEqual(schedule_step2["current_frontier"], ["packet-reviewer"])
            self.assertEqual(schedule_step2["convergence_status"], "reviewer_barrier")
            self.assertEqual(operator_view["swarm_status"], "bounded_swarm")
            self.assertEqual(operator_view["execution_shape"], "bounded_swarm")
            self.assertEqual(operator_view["convergence_status"], "reviewer_barrier")
            self.assertEqual(operator_view["current_frontier"], ["packet-reviewer"])
            rc3, payload3 = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="step",
            )
            self.assertEqual(rc3, 10)
            self.assertEqual(payload3["status"], "revise")
            self.assertEqual(payload3["reason_code"], "REQUIRED_WORK_REMAINING")
            schedule_step3 = json.loads(runtime_paths["schedule"].read_text(encoding="utf-8"))
            self.assertEqual(schedule_step3["cycle_log"][-1]["packet_ids"], ["packet-reviewer"])
            self.assertEqual(schedule_step3["current_frontier"], ["packet-validation-lint"])

    def test_r3_swarm_serializes_overlapping_write_scopes(self) -> None:
        plan = self._r3_swarm_fixture(conflicting_workers=True)
        track_id = "runtime-r3-scope-conflict"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            rc1, _ = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="step",
            )
            self.assertEqual(rc1, 10)
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
            schedule_step1 = json.loads(runtime_paths["schedule"].read_text(encoding="utf-8"))
            operator_view_step1 = json.loads(runtime_paths["operator_view"].read_text(encoding="utf-8"))
            self.assertEqual(sorted(schedule_step1["cycle_log"][-1]["packet_ids"]), ["packet-explorer", "packet-validator"])
            self.assertEqual(schedule_step1["runnable_but_not_dispatched"], ["packet-worker-b"])
            self.assertEqual(schedule_step1["dispatch_block_reasons"]["packet-worker-b"], ["allowed_scope_conflict"])
            self.assertEqual(operator_view_step1["dispatch_block_reasons"].get("packet-worker-b"), ["allowed_scope_conflict"])
            rc2, _ = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="step",
            )
            self.assertEqual(rc2, 10)
            schedule_step2 = json.loads(runtime_paths["schedule"].read_text(encoding="utf-8"))
            self.assertEqual(schedule_step2["cycle_log"][-1]["packet_ids"], ["packet-worker"])
            self.assertEqual(schedule_step2["current_frontier"], ["packet-worker-b"])

    def test_r4_reviewer_centered_swarm_holds_closure_until_reviewer(self) -> None:
        plan = self._r4_reviewer_swarm_fixture()
        track_id = "runtime-r4-reviewer-barrier"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            rc1, _ = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="step",
            )
            self.assertEqual(rc1, 10)
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
            schedule_step1 = json.loads(runtime_paths["schedule"].read_text(encoding="utf-8"))
            self.assertEqual(sorted(schedule_step1["cycle_log"][-1]["packet_ids"]), ["packet-validator", "packet-worker"])
            self.assertEqual(schedule_step1["current_frontier"], ["packet-reviewer"])
            self.assertEqual(schedule_step1["convergence_status"], "reviewer_barrier")
            rc2, payload2 = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="step",
            )
            self.assertEqual(rc2, 10)
            self.assertEqual(payload2["status"], "revise")
            self.assertEqual(payload2["reason_code"], "REQUIRED_WORK_REMAINING")
            schedule_step2 = json.loads(runtime_paths["schedule"].read_text(encoding="utf-8"))
            operator_view = json.loads(runtime_paths["operator_view"].read_text(encoding="utf-8"))
            self.assertEqual(schedule_step2["cycle_log"][-1]["packet_ids"], ["packet-reviewer"])
            self.assertEqual(schedule_step2["current_frontier"], ["packet-validation-lint"])
            self.assertEqual(operator_view["route_hint"], "R4")
            self.assertEqual(operator_view["swarm_status"], "bounded_swarm")
            self.assertEqual(operator_view["convergence_status"], "dispatching")

    def test_swarm_mode_respects_support_confidence_and_blocks_false_completion(self) -> None:
        plan = self._r3_swarm_fixture()
        track_id = "runtime-r3-support-confidence"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._init_git_repo(root)
            compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            rc = None
            payload = None
            for _ in range(3):
                rc, payload = run_runtime(
                    plan=plan,
                    track_id=track_id,
                    artifacts_root=root,
                    workspace_root=str(root),
                    codex_home=None,
                    command="step",
                )
            self.assertEqual(rc, 10)
            self.assertEqual(payload["status"], "revise")
            self.assertEqual(payload["reason_code"], "REQUIRED_WORK_REMAINING")
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
            support_confidence = json.loads(runtime_paths["support_confidence"].read_text(encoding="utf-8"))
            operator_view = json.loads(runtime_paths["operator_view"].read_text(encoding="utf-8"))
            self.assertEqual(support_confidence["objective_support_status"], "unsupported")
            self.assertEqual(support_confidence["unsupported_closure_risk"], "objective_claim_ahead_of_external_support")
            self.assertIn("validation_claim_ahead_of_lane_coverage", support_confidence["support_gap_reasons"])
            self.assertEqual(operator_view["unsupported_closure_risk"], "objective_claim_ahead_of_external_support")
            self.assertTrue(operator_view["required_work_remaining"])
            self.assertIn("unsupported_closure_risk:objective_claim_ahead_of_external_support", operator_view["required_work_reasons"])
            self.assertFalse(operator_view["stop_allowed"])
            self.assertTrue(operator_view["support_remediation_available"])

    def test_controller_verdict_blocks_when_required_work_has_no_safe_momentum(self) -> None:
        plan = self._fixture()
        track_id = "runtime-controller-blocked"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            bootstrap_runtime(plan=plan, track_id=track_id, artifacts_root=root, controller_mode="enforce")
            verdict = _controller_verdict(artifacts_root=root, track_id=track_id, controller_mode="enforce")
            self.assertEqual(verdict["status"], "revise")
            self.assertEqual(verdict["reason_code"], "REQUIRED_WORK_REMAINING")

    def test_controller_verdict_revises_for_material_optional_work_in_enforce_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            track_id = "runtime-optional-enforce"
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
            runtime_paths["runtime_state"].parent.mkdir(parents=True, exist_ok=True)
            runtime_paths["runtime_state"].write_text(
                json.dumps(
                    {
                        "schema_version": "objective-runtime-state.v1",
                        "closure_state": "OBJECTIVE_COMPLETE",
                        "safe_momentum_available": True,
                        "controller_mode": "enforce",
                        "required_work_remaining": False,
                        "required_work_reasons": [],
                        "material_optional_work_remaining": True,
                        "material_optional_work_reasons": ["policy_backed_finishing_packet_available"],
                        "stop_allowed": False,
                        "stop_reason": "policy_backed_finishing_packet_available",
                        "next_recommended_packet": "packet-polish",
                        "unsupported_closure_risk": "none",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            verdict = _controller_verdict(artifacts_root=root, track_id=track_id, controller_mode="enforce")
            self.assertEqual(verdict["status"], "revise")
            self.assertEqual(verdict["reason_code"], "MATERIAL_FINISHING_WORK_REMAINING")

    def test_controller_verdict_approves_optional_work_in_audit_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            track_id = "runtime-optional-audit"
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
            runtime_paths["runtime_state"].parent.mkdir(parents=True, exist_ok=True)
            runtime_paths["runtime_state"].write_text(
                json.dumps(
                    {
                        "schema_version": "objective-runtime-state.v1",
                        "closure_state": "OBJECTIVE_COMPLETE",
                        "safe_momentum_available": True,
                        "controller_mode": "audit",
                        "required_work_remaining": False,
                        "required_work_reasons": [],
                        "material_optional_work_remaining": True,
                        "material_optional_work_reasons": ["policy_backed_finishing_packet_available"],
                        "stop_allowed": False,
                        "stop_reason": "policy_backed_finishing_packet_available",
                        "next_recommended_packet": "packet-polish",
                        "unsupported_closure_risk": "none",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            verdict = _controller_verdict(artifacts_root=root, track_id=track_id, controller_mode="audit")
            self.assertEqual(verdict["status"], "approve")
            self.assertEqual(verdict["reason_code"], "MATERIAL_FINISHING_WORK_REMAINING")
            self.assertTrue(verdict["advisory_only"])

    def test_governed_runtime_uses_ralph_as_final_verifier_plugin(self) -> None:
        plan = self._fixture()
        track_id = "runtime-governed-finalize"
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td)
            root = temp_root / "workspace"
            root.mkdir(parents=True, exist_ok=True)
            codex_home = temp_root / "codex-home"
            prompt_log = temp_root / "prompt.log"
            real_bin = temp_root / "bin" / "codex-real"
            plan_json = temp_root / "plan.json"
            impl_json = temp_root / "implementation.json"
            review_json = temp_root / "review.impl.json"

            self._init_git_repo(root)
            self._create_fake_real_codex(real_bin, prompt_log)
            self._create_fake_ralph(codex_home, "revise_then_approve")
            plan_json.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
            impl_json.write_text(json.dumps({"schema_version": "implementation.v1"}) + "\n", encoding="utf-8")
            review_json.write_text(json.dumps({"type": "planning_gate_review", "status": "approve"}) + "\n", encoding="utf-8")
            compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
            verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)

            prior_real_bin = os.environ.get("CODEX_REAL_BIN")
            prior_runtime_real_bin = os.environ.get("CODEX_RUNTIME_REAL_BIN")
            try:
                os.environ["CODEX_REAL_BIN"] = str(real_bin)
                os.environ["CODEX_RUNTIME_REAL_BIN"] = str(real_bin)
                rc, payload = governed_runtime(
                    plan_payload=plan,
                    plan_json_path=str(plan_json),
                    artifacts_root=root,
                    track_id=track_id,
                    workspace_root=str(root),
                    codex_home=str(codex_home),
                    controller_mode="enforce",
                    finalize_attempt=True,
                    route_class="R3",
                    route_task_id="runtime-finalize-task",
                    impl_json=str(impl_json),
                    review_json=str(review_json),
                    verifier_mode="enforce",
                    verifier_max_loops=3,
                    verifier_base_prompt="base prompt",
                )
            finally:
                if prior_real_bin is None:
                    os.environ.pop("CODEX_REAL_BIN", None)
                else:
                    os.environ["CODEX_REAL_BIN"] = prior_real_bin
                if prior_runtime_real_bin is None:
                    os.environ.pop("CODEX_RUNTIME_REAL_BIN", None)
                else:
                    os.environ["CODEX_RUNTIME_REAL_BIN"] = prior_runtime_real_bin

            self.assertEqual(rc, 0)
            self.assertEqual(payload["status"], "approve")
            self.assertEqual(payload["final_verifier"], "ralph")
            prompts = prompt_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(prompts), 1)
            self.assertIn("Ralph Remediation Loop 1", prompts[0])
            runtime_state = json.loads(runtime_artifact_paths(artifacts_root=root, track_id=track_id)["runtime_state"].read_text(encoding="utf-8"))
            self.assertEqual(runtime_state["lifecycle_status"], "approved")
            self.assertEqual(runtime_state["last_verifier_result"]["status"], "approve")


if __name__ == "__main__":
    unittest.main()
