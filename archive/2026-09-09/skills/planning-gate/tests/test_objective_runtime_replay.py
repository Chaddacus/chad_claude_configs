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

from common import canonical_python_argv, cycle_artifact_paths, runtime_artifact_paths, write_json_file  # noqa: E402
from compile_intent import compile_intent_payload  # noqa: E402
from compile_plan import compile_plan_payload  # noqa: E402
from initialize_session import initialize_session_payload  # noqa: E402
from objective_runtime import bootstrap_runtime, run_runtime, step  # noqa: E402
from objective_runtime_replay import load_runtime_replay_payload, render_runtime_replay_text  # noqa: E402
from verify_plan import verify_plan_payload  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPLAY_SCRIPT = SCRIPT_DIR / "objective_runtime_replay.py"


class ObjectiveRuntimeReplayTests(unittest.TestCase):
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

    def test_load_replay_payload_reconstructs_success_timeline(self) -> None:
        plan = self._fixture()
        track_id = "replay-success"
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

            replay = load_runtime_replay_payload(track_id=track_id, artifacts_root=root)
            self.assertEqual(replay["schema_version"], "objective-runtime-replay.v1")
            self.assertTrue(replay["terminal"]["terminal"])
            self.assertEqual(replay["terminal"]["halt_reason"], "accepted_success")
            self.assertIn(replay["transaction"]["state"], {"committed", "recovered"})
            self.assertGreaterEqual(replay["kernel_summary"]["step_count"], 1)
            self.assertTrue(any(step["verification_count"] >= 1 for step in replay["steps"]))
            timeline_text = render_runtime_replay_text(replay, selected_view="timeline")
            self.assertIn("guard=", timeline_text)
            self.assertIn("verification", timeline_text)
            summary_text = render_runtime_replay_text(replay, selected_view="summary")
            self.assertIn("transaction:", summary_text)

    def test_replay_surfaces_invalid_transition_trap(self) -> None:
        plan = self._fixture()
        track_id = "replay-trap"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_bootstrap(root=root, track_id=track_id, plan=plan)
            bootstrap_runtime(plan=plan, track_id=track_id, artifacts_root=root)
            runtime_paths = runtime_artifact_paths(artifacts_root=root, track_id=track_id)
            kernel_state = json.loads(runtime_paths["kernel_runtime_state"].read_text(encoding="utf-8"))
            corrupted = copy.deepcopy(kernel_state)
            corrupted["state"] = "verifying"
            corrupted["evidence_refs"] = []
            corrupted["last_action"] = {"kind": "edit", "unit_id": "packet-compiler", "step_id": "bad-step"}
            write_json_file(runtime_paths["kernel_runtime_state"], corrupted)

            payload = step(
                plan_payload=plan,
                artifacts_root=root,
                track_id=track_id,
                cwd=None,
                codex_home=None,
                controller_mode="enforce",
            )
            self.assertFalse(payload["valid"])
            self.assertEqual(payload["runtime_payload"]["status"], "blocked")

            replay = load_runtime_replay_payload(track_id=track_id, artifacts_root=root)
            self.assertTrue(replay["trap"]["detected"])
            self.assertEqual(replay["trap"]["step_id"], "step-preflight")
            self.assertEqual(replay["terminal"]["halt_reason"], "invalid_transition")
            trap_text = render_runtime_replay_text(replay, selected_view="trap")
            self.assertIn("errors:", trap_text)
            self.assertIn("trap step: step-preflight", trap_text)

    def test_replay_prefers_aborted_transaction_over_orphaned_cycle_evidence(self) -> None:
        plan = self._fixture()
        track_id = "replay-orphaned-evidence"
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
            transaction_state = json.loads(
                runtime_artifact_paths(artifacts_root=root, track_id=track_id)["transaction_state"].read_text(encoding="utf-8")
            )
            staged_kernel = next(
                target["staged_path"]
                for target in transaction_state["targets"]
                if target["artifact_key"] == "kernel_runtime_state"
            )
            Path(staged_kernel).unlink()
            cycle_id = sorted(path.name for path in (root / track_id / "cycles").iterdir() if path.is_dir())[0]
            cycle_paths = cycle_artifact_paths(artifacts_root=root, track_id=track_id, cycle_id=cycle_id)
            self.assertTrue(cycle_paths["result"].exists())
            self.assertTrue(cycle_paths["review"].exists())
            rc, payload = run_runtime(
                plan=plan,
                track_id=track_id,
                artifacts_root=root,
                workspace_root=str(root),
                codex_home=None,
                command="run",
            )
            self.assertEqual(rc, 20)
            self.assertEqual(payload["reason_code"], "TRANSACTION_INTEGRITY_FAILURE")
            replay = load_runtime_replay_payload(track_id=track_id, artifacts_root=root)
            self.assertEqual(replay["transaction"]["state"], "aborted")
            self.assertEqual(replay["terminal"]["halt_reason"], "invalid_transition")
            self.assertTrue(cycle_paths["result"].exists())
            self.assertTrue(cycle_paths["review"].exists())

    def test_replay_cli_renders_timeline_and_json(self) -> None:
        plan = self._fixture()
        track_id = "replay-cli"
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
            timeline = subprocess.run(
                canonical_python_argv(
                    str(REPLAY_SCRIPT),
                    "--track-id",
                    track_id,
                    "--artifacts-root",
                    str(root),
                    "--view",
                    "timeline",
                ),
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertIn("timeline:", timeline.stdout)
            self.assertIn("verification", timeline.stdout)

            payload = subprocess.run(
                canonical_python_argv(
                    str(REPLAY_SCRIPT),
                    "--track-id",
                    track_id,
                    "--artifacts-root",
                    str(root),
                    "--json",
                ),
                text=True,
                capture_output=True,
                check=True,
            )
            replay = json.loads(payload.stdout)
            self.assertEqual(replay["track_id"], track_id)
            self.assertIn("steps", replay)
            self.assertIn("terminal", replay)


if __name__ == "__main__":
    unittest.main()
