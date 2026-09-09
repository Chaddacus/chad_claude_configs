#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
import sys

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from swarm_evaluation import (  # noqa: E402
    BENCHMARK_SCHEMA_VERSION,
    CANARY_SCHEMA_VERSION,
    _init_repo,
    build_archetype_plan,
    run_benchmark_archetype,
    run_benchmark_corpus,
    run_live_canary,
)
from common import canonical_python_argv, runtime_artifact_paths  # noqa: E402

BENCHMARK_RUNNER = SCRIPT_DIR / "run_swarm_benchmarks.py"
CANARY_RUNNER = SCRIPT_DIR / "run_swarm_canary.py"


class SwarmEvaluationTests(unittest.TestCase):
    def test_benchmark_archetype_produces_quality_preserving_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report = run_benchmark_archetype(archetype="ui", artifacts_root=root)
            self.assertEqual(report["schema_version"], BENCHMARK_SCHEMA_VERSION)
            self.assertEqual(report["archetype"], "ui")
            self.assertEqual(len(report["runs"]), 3)
            self.assertTrue(all(run["quality_preserving"] for run in report["runs"]))
            self.assertTrue(all("benchmark_score" in run for run in report["runs"]))
            self.assertIn(report["recommended_mode"], {"serial_only", "bounded_parallel", "bounded_swarm"})
            report_path = root / "benchmarks" / "ui.benchmark.json"
            self.assertTrue(report_path.exists())
            written = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(written["recommended_mode"], report["recommended_mode"])
            self.assertIn("benchmark_contract", written)

    def test_benchmark_corpus_includes_swarm_win_and_non_win_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = run_benchmark_corpus(artifacts_root=root)
            self.assertEqual(payload["schema_version"], BENCHMARK_SCHEMA_VERSION)
            reports = payload["reports"]
            self.assertGreaterEqual(len(reports), 5)
            self.assertTrue(any(report["swarm_outperformed_serial"] for report in reports))
            self.assertTrue(any((not report["swarm_outperformed_serial"]) or report["recommended_mode"] != "bounded_swarm" for report in reports))
            self.assertTrue(all(all(run["quality_preserving"] for run in report["runs"]) for report in reports))
            self.assertTrue((root / "benchmarks" / "swarm-report.json").exists())

    def test_rubik_benchmark_archetype_emits_contract_and_scores(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            report = run_benchmark_archetype(archetype="rubik_3d", artifacts_root=root)
            self.assertEqual(report["archetype"], "rubik_3d")
            self.assertEqual(report["benchmark_contract"]["benchmark_id"], "rubik_3d_self_solve")
            self.assertIn("interactive 3D cube viewport", report["benchmark_contract"]["required_features"])
            self.assertIn("benchmark_score", report["runs"][0])
            self.assertIn("comparison_fields", report)
            self.assertIn("benchmark_score", report["comparison_fields"])
            report_path = root / "benchmarks" / "rubik_3d.benchmark.json"
            self.assertTrue(report_path.exists())

    def test_build_rubik_plan_includes_benchmark_contract_and_split_packets(self) -> None:
        plan = build_archetype_plan(archetype="rubik_3d", mode="bounded_swarm")
        packet_ids = [packet["packet_id"] for packet in plan["packets"]]
        self.assertEqual(plan["benchmark_contract"]["benchmark_id"], "rubik_3d_self_solve")
        self.assertIn("packet-solver", packet_ids)
        self.assertIn("packet-ui", packet_ids)
        self.assertIn("packet-ui-smoke", packet_ids)
        self.assertEqual(plan["route_hint"], "R3")

    def test_canary_refuses_unsafe_route_and_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspace"
            workspace.mkdir()
            _init_repo(workspace, archetype="service")
            plan = build_archetype_plan(archetype="migration", mode="bounded_swarm")
            plan_path = root / "migration-plan.json"
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            result = run_live_canary(
                plan_json=plan_path,
                workspace_root=workspace,
                artifacts_root=root,
                track_id="unsafe-canary",
                route_hint="R4",
                execution_shape="bounded_swarm",
            )
            self.assertEqual(result["schema_version"], CANARY_SCHEMA_VERSION)
            self.assertTrue(result["refused"])
            self.assertFalse(result["safe_to_run"])
            self.assertEqual(result["refusal_reason"], "route_not_allowed_in_first_wave")
            self.assertTrue(runtime_artifact_paths(artifacts_root=root, track_id="unsafe-canary")["canary"].exists())

    def test_canary_runs_in_isolated_workspace_for_safe_objective(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspace"
            workspace.mkdir()
            _init_repo(workspace, archetype="mixed")
            plan = build_archetype_plan(archetype="mixed", mode="bounded_swarm")
            plan_path = root / "mixed-plan.json"
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            before_status = subprocess.run(
                ["git", "status", "--short"],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            result = run_live_canary(
                plan_json=plan_path,
                workspace_root=workspace,
                artifacts_root=root,
                track_id="safe-canary",
                route_hint="R3",
                execution_shape="bounded_swarm",
            )
            self.assertFalse(result["refused"])
            self.assertTrue(result["safe_to_run"])
            self.assertEqual(result["route_hint"], "R3")
            self.assertEqual(result["execution_shape"], "bounded_swarm")
            self.assertEqual(result["isolation_mode"], "git_worktree")
            self.assertNotEqual(result["workspace_root"], result["isolated_workspace_root"])
            self.assertIn("metrics", result)
            self.assertTrue(result["metrics"]["quality_preserving"])
            self.assertTrue(result["metrics"]["stop_allowed"])
            self.assertEqual(result["metrics"]["final_closure_state"], "OBJECTIVE_COMPLETE")
            after_status = subprocess.run(
                ["git", "status", "--short"],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            self.assertEqual(after_status, before_status)
            self.assertTrue(runtime_artifact_paths(artifacts_root=root, track_id="safe-canary")["canary"].exists())

    def test_benchmark_runner_cli_emits_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            out_path = root / "ui-report.json"
            completed = subprocess.run(
                canonical_python_argv(
                    str(BENCHMARK_RUNNER),
                    "--archetype",
                    "ui",
                    "--artifacts-root",
                    str(root),
                    "--output",
                    str(out_path),
                ),
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertTrue(out_path.exists(), completed.stdout)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], BENCHMARK_SCHEMA_VERSION)
            self.assertEqual(payload["archetype"], "ui")

    def test_canary_runner_cli_emits_result(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "workspace"
            workspace.mkdir()
            _init_repo(workspace, archetype="service")
            plan = build_archetype_plan(archetype="service", mode="bounded_parallel")
            plan_path = root / "service-plan.json"
            plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
            out_path = root / "service-canary.json"
            subprocess.run(
                canonical_python_argv(
                    str(CANARY_RUNNER),
                    "--plan-json",
                    str(plan_path),
                    "--workspace-root",
                    str(workspace),
                    "--artifacts-root",
                    str(root),
                    "--track-id",
                    "service-canary-cli",
                    "--route",
                    "R2",
                    "--execution-shape",
                    "single_lane",
                    "--output",
                    str(out_path),
                ),
                text=True,
                capture_output=True,
                check=True,
            )
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], CANARY_SCHEMA_VERSION)
            self.assertEqual(payload["route_hint"], "R2")
            self.assertFalse(payload["refused"])
            self.assertTrue(payload["safe_to_run"])


if __name__ == "__main__":
    unittest.main()
