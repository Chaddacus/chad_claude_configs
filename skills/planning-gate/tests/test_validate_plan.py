#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
import sys

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from compile_intent import compile_intent_payload  # noqa: E402
from compile_plan import compile_plan_payload  # noqa: E402
from initialize_session import initialize_session_payload  # noqa: E402
from validate_plan import validate_plan_payload  # noqa: E402
from verify_plan import verify_plan_payload  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ValidatePlanTests(unittest.TestCase):
    def _fixture(self, name: str) -> dict:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def _prepare_plan_artifacts(self, plan: dict, root: Path, track_id: str) -> None:
        compile_intent_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
        initialize_session_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
        compile_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)
        verify_plan_payload(plan_payload=plan, track_id=track_id, artifacts_root=root)

    def test_missing_required_fields_revise(self) -> None:
        plan = self._fixture("plan_missing_field.json")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(plan, root, "case-missing")
            review = validate_plan_payload(
                plan_payload=plan,
                track_id="case-missing",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "revise")
        self.assertIn("objective", review["missing_fields"])

    def test_wrong_schema_is_blocked(self) -> None:
        plan = self._fixture("plan_bad_schema.json")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(plan, root, "case-bad-schema")
            review = validate_plan_payload(
                plan_payload=plan,
                track_id="case-bad-schema",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "blocked")
        self.assertTrue(any(item.startswith("schema_version:") for item in review["blocked_fields"]))

    def test_missing_smoke_stage_revise(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified["tests"]["smoke_gates"] = modified["tests"]["smoke_gates"][:-1]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-smoke-missing")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-smoke-missing",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "revise")
        self.assertIn("tests.smoke_gates:missing_stage:100%", review["missing_fields"])

    def test_missing_definition_of_done_revise(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified.pop("definition_of_done", None)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-missing-dod")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-missing-dod",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "revise")
        self.assertIn("definition_of_done", review["missing_fields"])

    def test_definition_of_done_missing_category_revise(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified["definition_of_done"] = [
            item for item in modified["definition_of_done"] if item["category"] != "security"
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-missing-dod-category")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-missing-dod-category",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "revise")
        self.assertIn("definition_of_done:missing_category:security", review["missing_fields"])

    def test_definition_of_done_duplicate_id_revise(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified["definition_of_done"][1]["id"] = modified["definition_of_done"][0]["id"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-dup-dod-id")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-dup-dod-id",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "revise")
        self.assertIn("definition_of_done:2:id_duplicate", review["missing_fields"])

    def test_definition_of_done_non_string_field_revise(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified["definition_of_done"][0]["verification"] = {"cmd": "pytest -q"}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-non-string-dod")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-non-string-dod",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "revise")
        self.assertIn("definition_of_done:1:verification_not_string", review["missing_fields"])

    def test_definition_of_done_generic_verification_revise(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified["definition_of_done"][0]["verification"] = "manual review"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-generic-dod-verification")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-generic-dod-verification",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "revise")
        self.assertIn("definition_of_done:1:verification_too_generic", review["missing_fields"])

    def test_top_level_smoke_gates_location_revise(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified["smoke_gates"] = modified["tests"]["smoke_gates"]
        modified["tests"].pop("smoke_gates", None)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-top-level-smoke")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-top-level-smoke",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "revise")
        self.assertIn("tests.smoke_gates:preferred_location", review["missing_fields"])

    def test_valid_plan_approves(self) -> None:
        plan = self._fixture("plan_valid.json")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(plan, root, "case-plan-ok")
            review = validate_plan_payload(
                plan_payload=plan,
                track_id="case-plan-ok",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "approve")

    def test_missing_contract_closure_revise(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified.pop("contract_closure", None)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-missing-contract-closure")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-missing-contract-closure",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "revise")
        self.assertIn("contract_closure", review["missing_fields"])

    def test_missing_overengineering_guardrails_revise(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified.pop("overengineering_guardrails", None)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-missing-overengineering")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-missing-overengineering",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "revise")
        self.assertIn("overengineering_guardrails", review["missing_fields"])

    def test_contract_closure_mutator_without_reject_behavior_blocks(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified["contract_closure"]["mutator_contracts"] = {
            "devsup.objectives.create": {
                "preconditions": "Canonical repo root is resolved before mutation.",
                "write_set": ["objective state"],
                "tx_shape": "single create transaction",
                "quarantine_allowed": False,
            }
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-mutator-reject-missing")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-mutator-reject-missing",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "blocked")
        self.assertIn(
            "contract_closure:mutator_contracts:devsup.objectives.create:reject_behavior_required",
            review["blocked_fields"],
        )

    def test_surface_budget_exceeded_blocks(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified["overengineering_guardrails"]["surface_budget"]["new_modules"] = 1
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-surface-budget")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-surface-budget",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "blocked")
        self.assertIn(
            "overengineering_guardrails:surface_budget_exceeded:new_modules",
            review["blocked_fields"],
        )

    def test_missing_pre_delivery_gap_review_revise(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified.pop("pre_delivery_gap_review", None)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-gap-review-missing")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-gap-review-missing",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "revise")
        self.assertIn("pre_delivery_gap_review", review["missing_fields"])

    def test_pre_delivery_gap_review_with_remaining_issues_blocks(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified["pre_delivery_gap_review"]["issues_remaining"] = [
            "Still missing an explicit default for rollback evidence."
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-gap-review-remaining")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-gap-review-remaining",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "blocked")
        self.assertIn("pre_delivery_gap_review:issues_remaining", review["blocked_fields"])

    def test_pre_delivery_gap_review_not_ready_blocks(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified["pre_delivery_gap_review"]["ready_to_present"] = False
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-gap-review-not-ready")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-gap-review-not-ready",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "blocked")
        self.assertIn("pre_delivery_gap_review:ready_to_present_required", review["blocked_fields"])

    def test_missing_solution_ladder_revise_for_r3(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        for key in (
            "solution_ladder",
            "chosen_layer",
            "layer_justification",
            "why_not_lower",
            "why_not_higher",
            "future_reuse_gain",
        ):
            modified.pop(key, None)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-solution-ladder-missing")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-solution-ladder-missing",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "revise")
        self.assertIn("solution_ladder", review["missing_fields"])

    def test_missing_reuse_first_fields_revise(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified.pop("existing_primitives_considered", None)
        modified.pop("reuse_first_decision", None)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-reuse-first-missing")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-reuse-first-missing",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "revise")
        self.assertIn("existing_primitives_considered", review["missing_fields"])
        self.assertIn("reuse_first_decision", review["missing_fields"])

    def test_bounded_swarm_requires_swarm_justification(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified.pop("swarm_justification", None)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-swarm-justification")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-swarm-justification",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "blocked")
        self.assertIn("execution_shape:bounded_swarm_requires_swarm_justification", review["blocked_fields"])

    def test_underreaching_l1_choice_blocks(self) -> None:
        plan = self._fixture("plan_valid.json")
        modified = deepcopy(plan)
        modified["chosen_layer"] = "L1_patch"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(modified, root, "case-layer-underreach")
            review = validate_plan_payload(
                plan_payload=modified,
                track_id="case-layer-underreach",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )
        self.assertEqual(review["status"], "blocked")
        self.assertTrue(
            any(item.startswith("solution_ladder:chosen_layer_below_useful") for item in review["blocked_fields"])
        )

    def test_progression_stall_flag_triggers(self) -> None:
        plan = self._fixture("plan_valid.json")
        plan["tests"]["smoke_gates"][-1]["status"] = "not_run"

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._prepare_plan_artifacts(plan, root, "stall-case")
            for _ in range(2):
                validate_plan_payload(
                    plan_payload=plan,
                    track_id="stall-case",
                    artifacts_root=root,
                    stall_limit=2,
                    ttl_hours=24,
                )
            review = validate_plan_payload(
                plan_payload=plan,
                track_id="stall-case",
                artifacts_root=root,
                stall_limit=2,
                ttl_hours=24,
            )

        self.assertEqual(review["status"], "revise")
        self.assertIn("progression:smoke_quality_stalled", review["missing_fields"])


if __name__ == "__main__":
    unittest.main()
