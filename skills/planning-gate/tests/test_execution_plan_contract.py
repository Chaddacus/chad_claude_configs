#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import (  # noqa: E402
    EXECUTION_PLAN_REQUIRED_FIELDS,
    EXECUTION_PLAN_UNIT_REQUIRED_FIELDS,
    ExecutionPlanCompileError,
    build_execution_plan,
    build_repo_validation_plan,
    discover_repo_capabilities,
)
from compile_intent import compile_intent_payload  # noqa: E402
from compile_plan import compile_plan_payload  # noqa: E402
from initialize_session import initialize_session_payload  # noqa: E402
from objective_runtime import bootstrap_runtime  # noqa: E402
from verify_plan import verify_plan_payload  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ExecutionPlanContractTests(unittest.TestCase):
    def _fixture(self) -> dict:
        return json.loads((FIXTURES / "plan_valid.json").read_text(encoding="utf-8"))

    def _prepare_plan_artifacts(self, *, plan: dict, root: Path, track_id: str) -> None:
        compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
        initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
        compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
        verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)

    def test_build_execution_plan_is_deterministic_for_same_plan_and_track(self) -> None:
        plan = self._fixture()
        left = build_execution_plan(plan=deepcopy(plan), track_id="compile-deterministic")
        right = build_execution_plan(plan=deepcopy(plan), track_id="compile-deterministic")

        self.assertEqual(
            json.dumps(left, sort_keys=True),
            json.dumps(right, sort_keys=True),
        )

    def test_build_execution_plan_preserves_authored_packet_ids_and_required_fields(self) -> None:
        plan = self._fixture()
        payload = build_execution_plan(plan=plan, track_id="compile-identity")

        self.assertTrue(all(field in payload for field in EXECUTION_PLAN_REQUIRED_FIELDS))
        authored_packet_ids = [packet["packet_id"] for packet in plan["packets"]]
        compiled_unit_ids = [unit["unit_id"] for unit in payload["units"]]
        self.assertEqual(compiled_unit_ids, authored_packet_ids)
        for unit, packet in zip(payload["units"], plan["packets"], strict=True):
            self.assertTrue(all(field in unit for field in EXECUTION_PLAN_UNIT_REQUIRED_FIELDS))
            self.assertEqual(unit["unit_id"], packet["packet_id"])
            self.assertEqual(unit["candidate_files"], packet["allowed_scope"])
            self.assertEqual(unit["allowed_scope"], packet["allowed_scope"])

    def test_build_execution_plan_rejects_invalid_plan_schema(self) -> None:
        plan = self._fixture()
        plan["schema_version"] = "plan.v0"

        with self.assertRaisesRegex(ExecutionPlanCompileError, r"invalid_plan_schema:plan\.v0"):
            build_execution_plan(plan=plan, track_id="compile-bad-schema")

    def test_build_execution_plan_rejects_scope_drift_between_packet_and_definition_of_done(self) -> None:
        plan = self._fixture()
        plan["packets"][0]["definition_of_done"]["allowed_scope"] = ["mismatch.py"]

        with self.assertRaisesRegex(ExecutionPlanCompileError, r"scope_drift:packet-compiler"):
            build_execution_plan(plan=plan, track_id="compile-scope-drift")

    def test_generated_validation_packet_ids_are_distinct_from_authored_ids(self) -> None:
        plan = self._fixture()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo_capabilities = discover_repo_capabilities(cwd=str(root))
            validation_plan = build_repo_validation_plan(
                plan,
                track_id="compile-generated-ids",
                cwd=str(root),
                repo_capabilities=repo_capabilities,
            )

        authored_packet_ids = {packet["packet_id"] for packet in plan["packets"]}
        generated_packet_ids = {
            packet["packet_id"]
            for packet in validation_plan["generated_packets"]
            if packet["packet_id"]
        }

        self.assertTrue(generated_packet_ids)
        self.assertTrue(all(packet_id.startswith("packet-validation-") for packet_id in generated_packet_ids))
        self.assertTrue(authored_packet_ids.isdisjoint(generated_packet_ids))

    def test_compile_failure_blocks_runtime_bootstrap_start(self) -> None:
        plan = self._fixture()
        plan["packets"][0]["definition_of_done"]["allowed_scope"] = ["mismatch.py"]

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaisesRegex(ExecutionPlanCompileError, r"scope_drift:packet-compiler"):
                bootstrap_runtime(plan=plan, track_id="compile-bootstrap-blocked", artifacts_root=root)
            self.assertFalse((root / "compile-bootstrap-blocked" / "objective.execution-plan.json").exists())


if __name__ == "__main__":
    unittest.main()
