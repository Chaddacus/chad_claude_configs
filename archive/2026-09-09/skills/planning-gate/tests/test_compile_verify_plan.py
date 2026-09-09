#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import ExecutionPlanCompileError, canonical_python_argv  # noqa: E402
from compile_intent import compile_intent_payload  # noqa: E402
from compile_plan import compile_plan_payload  # noqa: E402
from initialize_session import initialize_session_payload  # noqa: E402
from verify_plan import verify_plan_payload  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
COMPILE_INTENT_SCRIPT = SCRIPT_DIR / "compile_intent.py"


class CompileVerifyPlanTests(unittest.TestCase):
    def _fixture(self) -> dict:
        return json.loads((FIXTURES / "plan_valid.json").read_text(encoding="utf-8"))

    def _write_intent_model_stub(self, root: Path, *, plan: dict | None = None) -> Path:
        fixture_plan = plan or self._fixture()
        objective = json.dumps(fixture_plan["objective"])
        success_criteria = json.dumps(fixture_plan["intent_contract"]["success_criteria"])
        scope_boundaries = json.dumps(fixture_plan["scope_boundaries"])
        authority = json.dumps(fixture_plan["intent_contract"]["authority_sensitive_decisions"])
        non_goals = json.dumps(fixture_plan["non_goals"])
        stub = root / "codex-real-stub"
        stub_template = """#!/usr/bin/env python3
import json
import sys

prompt = sys.argv[2] if len(sys.argv) > 2 else ""
if "blocked request" in prompt:
    payload = {
        "objective": "Blocked objective",
        "success_criteria": ["none"],
        "audience": ["maintainers"],
        "scope_boundaries": {"in_scope": [], "out_of_scope": []},
        "authority_sensitive_decisions": ["security approval required"],
        "known_unknowns": [],
        "discoverable_unknowns": [],
        "discoverable_resolution_log": [],
        "clarification_questions": [],
        "clarification_batch_count": 0,
        "objective_shape_status": "blocked",
        "normalization_source": "model_raw_request",
    }
elif "revise request" in prompt:
    payload = {
        "objective": "",
        "success_criteria": [],
        "audience": ["maintainers"],
        "scope_boundaries": {"in_scope": [], "out_of_scope": []},
        "authority_sensitive_decisions": [],
        "known_unknowns": [],
        "discoverable_unknowns": [],
        "discoverable_resolution_log": [],
        "clarification_questions": ["What exact behavior should close this objective?"],
        "clarification_batch_count": 0,
        "objective_shape_status": "revise_required",
        "normalization_source": "model_raw_request",
    }
elif "needs clarification" in prompt and "platform-team" not in prompt:
    payload = {
        "objective": __OBJECTIVE__,
        "success_criteria": __SUCCESS_CRITERIA__,
        "non_goals": __NON_GOALS__,
        "audience": ["maintainers"],
        "scope_boundaries": __SCOPE_BOUNDARIES__,
        "authority_sensitive_decisions": __AUTHORITY__,
        "known_unknowns": [],
        "discoverable_unknowns": ["current owner team"],
        "discoverable_resolution_log": [{"question": "Who owns the runtime?", "resolution": "Could not infer owner from the raw request.", "source": "model"}],
        "clarification_questions": ["Who owns the runtime boundary: platform-team or infra-team?"],
        "clarification_batch_count": 0,
        "objective_shape_status": "accepted_rewritten",
        "normalization_source": "model_raw_request",
    }
else:
    batch_count = 1 if "platform-team" in prompt else 0
    payload = {
        "objective": __OBJECTIVE__,
        "success_criteria": __SUCCESS_CRITERIA__,
        "non_goals": __NON_GOALS__,
        "audience": ["maintainers"],
        "scope_boundaries": __SCOPE_BOUNDARIES__,
        "authority_sensitive_decisions": __AUTHORITY__,
        "known_unknowns": [],
        "discoverable_unknowns": [],
        "discoverable_resolution_log": [{"question": "Where does the scheduler live?", "resolution": "Inside the proxy runtime loop.", "source": "model"}],
        "clarification_questions": [],
        "clarification_batch_count": batch_count,
        "objective_shape_status": "accepted_rewritten",
        "normalization_source": "model_raw_request",
        }
print(json.dumps(payload))
"""
        stub.write_text(
            stub_template
            .replace("__OBJECTIVE__", objective)
            .replace("__SUCCESS_CRITERIA__", success_criteria)
            .replace("__NON_GOALS__", non_goals)
            .replace("__SCOPE_BOUNDARIES__", scope_boundaries)
            .replace("__AUTHORITY__", authority),
            encoding="utf-8",
        )
        stub.chmod(0o755)
        return stub

    def test_compile_intent_emits_intent_and_readiness_artifacts(self) -> None:
        plan = self._fixture()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = compile_intent_payload(plan_payload=plan, track_id="intent-case", artifacts_root=root)
            self.assertEqual(result["status"], "approve")
            objective_intent = root / "intent-case" / "objective.intent.json"
            intent = root / "intent-case" / "plan.intent.json"
            readiness = root / "intent-case" / "plan.readiness.json"
            self.assertTrue(objective_intent.exists())
            self.assertTrue(intent.exists())
            self.assertTrue(readiness.exists())
            objective_intent_payload = json.loads(objective_intent.read_text(encoding="utf-8"))
            intent_payload = json.loads(intent.read_text(encoding="utf-8"))
            self.assertEqual(objective_intent_payload["objective_shape_status"], "accepted_as_given")
            self.assertEqual(intent_payload["intent_contract"]["objective_shape_status"], "accepted_as_given")

    def test_compile_intent_respects_objective_shape_status(self) -> None:
        plan = self._fixture()
        plan["intent_contract"]["objective_shape_status"] = "revise_required"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = compile_intent_payload(plan_payload=plan, track_id="intent-revise", artifacts_root=root)
            self.assertEqual(result["status"], "revise")

    def test_compile_plan_writes_compiler_and_coverage_artifacts(self) -> None:
        plan = self._fixture()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compile_intent_payload(plan_payload=plan, track_id="compile-case", artifacts_root=root)
            objective_intent = json.loads((root / "compile-case" / "objective.intent.json").read_text(encoding="utf-8"))
            initialize_session_payload(objective_intent=objective_intent, track_id="compile-case", artifacts_root=root)
            result = compile_plan_payload(
                plan_payload=plan,
                track_id="compile-case",
                artifacts_root=root,
                objective_intent=objective_intent,
            )
            self.assertEqual(result["status"], "approve")
            compiler = root / "compile-case" / "plan.compiler.json"
            coverage = root / "compile-case" / "plan.coverage.json"
            self.assertTrue(compiler.exists())
            self.assertTrue(coverage.exists())
            compiler_payload = json.loads(compiler.read_text(encoding="utf-8"))
            self.assertEqual(compiler_payload["schema_version"], "compiled-contract.v2.7")
            self.assertEqual(sorted(compiler_payload["packet_ids"]), sorted(plan["required_packets"]))

    def test_compile_plan_accepts_matching_raw_request_intent(self) -> None:
        plan = self._fixture()
        request = {
            "raw_request": "Turn the proxy into the real orchestrator for governed packet execution.",
            "objective": plan["objective"],
            "success_criteria": plan["intent_contract"]["success_criteria"],
            "audience": ["maintainers"],
            "authority_sensitive_decisions": plan["intent_contract"]["authority_sensitive_decisions"],
            "scope_boundaries": plan["scope_boundaries"],
            "objective_shape_status": "accepted_rewritten",
            "non_goals": plan["non_goals"],
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compile_intent_payload(request_payload=request, track_id="raw-intent", artifacts_root=root)
            objective_intent = json.loads((root / "raw-intent" / "objective.intent.json").read_text(encoding="utf-8"))
            initialize_session_payload(objective_intent=objective_intent, track_id="raw-intent", artifacts_root=root)
            compile_result = compile_plan_payload(
                plan_payload=plan,
                track_id="raw-intent",
                artifacts_root=root,
                objective_intent=objective_intent,
            )
            verify_result = verify_plan_payload(plan_payload=plan, track_id="raw-intent", artifacts_root=root)
            self.assertEqual(compile_result["status"], "approve")
            self.assertEqual(verify_result["status"], "approve")

    def test_compile_plan_accepts_matching_accepted_rewritten_intent(self) -> None:
        plan = self._fixture()
        request = {
            "raw_request": "Rewrite this into an executable proxy runtime objective.",
            "objective": plan["objective"],
            "success_criteria": plan["intent_contract"]["success_criteria"],
            "audience": ["maintainers"],
            "authority_sensitive_decisions": plan["intent_contract"]["authority_sensitive_decisions"],
            "scope_boundaries": plan["scope_boundaries"],
            "objective_shape_status": "accepted_rewritten",
            "normalization_source": "model_raw_request",
            "non_goals": plan["non_goals"],
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compile_intent_payload(request_payload=request, track_id="rewritten-intent", artifacts_root=root)
            objective_intent = json.loads((root / "rewritten-intent" / "objective.intent.json").read_text(encoding="utf-8"))
            initialize_session_payload(objective_intent=objective_intent, track_id="rewritten-intent", artifacts_root=root)
            compile_result = compile_plan_payload(
                plan_payload=plan,
                track_id="rewritten-intent",
                artifacts_root=root,
                objective_intent=objective_intent,
            )
            self.assertEqual(compile_result["status"], "approve")

    def test_compile_intent_request_stdin_emits_accepted_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stub = self._write_intent_model_stub(root)
            review_path = root / "intent.review.json"
            completed = subprocess.run(
                canonical_python_argv(
                    str(COMPILE_INTENT_SCRIPT),
                    "--request-stdin",
                    "--review-json-out",
                    str(review_path),
                    "--track-id",
                    "stdin-accept",
                    "--artifacts-root",
                    str(root),
                ),
                input="Turn this messy request into a governed runtime objective.",
                text=True,
                capture_output=True,
                env={**os.environ, "CODEX_INTENT_MODEL_BIN": str(stub)},
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            review = json.loads(review_path.read_text(encoding="utf-8"))
            objective_intent = json.loads((root / "stdin-accept" / "objective.intent.json").read_text(encoding="utf-8"))
            self.assertEqual(review["status"], "approve")
            self.assertEqual(objective_intent["objective_shape_status"], "accepted_rewritten")
            self.assertEqual(objective_intent["normalization_source"], "model_raw_request")
            self.assertEqual(objective_intent["clarification_batch_count"], 0)
            self.assertTrue(objective_intent["discoverable_resolution_log"])
            self.assertEqual(objective_intent["intent_contract"]["non_goals"], self._fixture()["non_goals"])

    def test_compile_plan_accepts_model_generated_intent(self) -> None:
        plan = self._fixture()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stub = self._write_intent_model_stub(root, plan=plan)
            review_path = root / "intent.review.json"
            completed = subprocess.run(
                canonical_python_argv(
                    str(COMPILE_INTENT_SCRIPT),
                    "--request-stdin",
                    "--review-json-out",
                    str(review_path),
                    "--track-id",
                    "stdin-plan-match",
                    "--artifacts-root",
                    str(root),
                ),
                input="Turn the proxy into the real orchestrator for governed packet execution.",
                text=True,
                capture_output=True,
                env={**os.environ, "CODEX_INTENT_MODEL_BIN": str(stub)},
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            objective_intent = json.loads((root / "stdin-plan-match" / "objective.intent.json").read_text(encoding="utf-8"))
            initialize_session_payload(objective_intent=objective_intent, track_id="stdin-plan-match", artifacts_root=root)
            compile_result = compile_plan_payload(
                plan_payload=plan,
                track_id="stdin-plan-match",
                artifacts_root=root,
                objective_intent=objective_intent,
            )
            self.assertEqual(compile_result["status"], "approve")

    def test_compile_plan_rejects_model_generated_intent_missing_non_goals(self) -> None:
        plan = self._fixture()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stub = self._write_intent_model_stub(root, plan=plan)
            review_path = root / "intent.review.json"
            completed = subprocess.run(
                canonical_python_argv(
                    str(COMPILE_INTENT_SCRIPT),
                    "--request-stdin",
                    "--review-json-out",
                    str(review_path),
                    "--track-id",
                    "stdin-plan-mismatch",
                    "--artifacts-root",
                    str(root),
                ),
                input="Turn the proxy into the real orchestrator for governed packet execution.",
                text=True,
                capture_output=True,
                env={**os.environ, "CODEX_INTENT_MODEL_BIN": str(stub)},
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            objective_intent_path = root / "stdin-plan-mismatch" / "objective.intent.json"
            objective_intent = json.loads(objective_intent_path.read_text(encoding="utf-8"))
            objective_intent["intent_contract"]["non_goals"] = []
            objective_intent_path.write_text(json.dumps(objective_intent, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            initialize_session_payload(objective_intent=objective_intent, track_id="stdin-plan-mismatch", artifacts_root=root)
            with self.assertRaises(ValueError):
                compile_plan_payload(
                    plan_payload=plan,
                    track_id="stdin-plan-mismatch",
                    artifacts_root=root,
                    objective_intent=objective_intent,
                )

    def test_compile_plan_rejects_model_generated_intent_without_non_goals(self) -> None:
        plan = self._fixture()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stub = root / "codex-real-stub"
            stub.write_text(
                f"""#!/usr/bin/env python3
import json

payload = {{
    "objective": {json.dumps(plan["objective"])},
    "success_criteria": {json.dumps(plan["intent_contract"]["success_criteria"])},
    "audience": ["maintainers"],
    "scope_boundaries": {json.dumps(plan["scope_boundaries"])},
    "authority_sensitive_decisions": {json.dumps(plan["intent_contract"]["authority_sensitive_decisions"])},
    "known_unknowns": [],
    "discoverable_unknowns": [],
    "discoverable_resolution_log": [{{"question": "Where does the runtime live?", "resolution": "Inside the governed proxy runtime.", "source": "model"}}],
    "clarification_questions": [],
    "clarification_batch_count": 0,
    "objective_shape_status": "accepted_rewritten",
    "normalization_source": "model_raw_request",
}}
print(json.dumps(payload))
""",
                encoding="utf-8",
            )
            stub.chmod(0o755)
            review_path = root / "intent.review.json"
            completed = subprocess.run(
                canonical_python_argv(
                    str(COMPILE_INTENT_SCRIPT),
                    "--request-stdin",
                    "--review-json-out",
                    str(review_path),
                    "--track-id",
                    "stdin-plan-match-no-nongoals",
                    "--artifacts-root",
                    str(root),
                ),
                input="Turn the proxy into the real orchestrator for governed packet execution.",
                text=True,
                capture_output=True,
                env={**os.environ, "CODEX_INTENT_MODEL_BIN": str(stub)},
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            objective_intent = json.loads(
                (root / "stdin-plan-match-no-nongoals" / "objective.intent.json").read_text(encoding="utf-8")
            )
            self.assertEqual(objective_intent["intent_contract"]["non_goals"], [])
            initialize_session_payload(
                objective_intent=objective_intent,
                track_id="stdin-plan-match-no-nongoals",
                artifacts_root=root,
            )
            with self.assertRaises(ValueError):
                compile_plan_payload(
                    plan_payload=plan,
                    track_id="stdin-plan-match-no-nongoals",
                    artifacts_root=root,
                    objective_intent=objective_intent,
                )

    def test_compile_intent_request_stdin_supports_single_clarification_batch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stub = self._write_intent_model_stub(root)
            review_path = root / "clarify.review.json"
            first = subprocess.run(
                canonical_python_argv(
                    str(COMPILE_INTENT_SCRIPT),
                    "--request-stdin",
                    "--review-json-out",
                    str(review_path),
                    "--track-id",
                    "stdin-clarify",
                    "--artifacts-root",
                    str(root),
                ),
                input="needs clarification",
                text=True,
                capture_output=True,
                env={**os.environ, "CODEX_INTENT_MODEL_BIN": str(stub)},
                check=False,
            )
            self.assertNotEqual(first.returncode, 0)
            objective_intent = json.loads((root / "stdin-clarify" / "objective.intent.json").read_text(encoding="utf-8"))
            self.assertEqual(objective_intent["objective_shape_status"], "accepted_rewritten")
            self.assertTrue(objective_intent["clarification_questions"])

            clarification_path = root / "clarification.json"
            clarification_path.write_text(json.dumps({"authority_sensitive_decisions": ["platform-team"]}), encoding="utf-8")
            second = subprocess.run(
                canonical_python_argv(
                    str(COMPILE_INTENT_SCRIPT),
                    "--request-stdin",
                    "--clarification-json",
                    str(clarification_path),
                    "--review-json-out",
                    str(review_path),
                    "--track-id",
                    "stdin-clarify",
                    "--artifacts-root",
                    str(root),
                ),
                input="needs clarification",
                text=True,
                capture_output=True,
                env={**os.environ, "CODEX_INTENT_MODEL_BIN": str(stub)},
                check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            objective_intent = json.loads((root / "stdin-clarify" / "objective.intent.json").read_text(encoding="utf-8"))
            self.assertEqual(objective_intent["clarification_batch_count"], 1)
            self.assertFalse(objective_intent["clarification_questions"])
            self.assertFalse(objective_intent["clarification_needed"])

    def test_compile_intent_request_stdin_can_block_or_revise(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stub = self._write_intent_model_stub(root)
            review_path = root / "status.review.json"
            blocked = subprocess.run(
                canonical_python_argv(
                    str(COMPILE_INTENT_SCRIPT),
                    "--request-stdin",
                    "--review-json-out",
                    str(review_path),
                    "--track-id",
                    "stdin-blocked",
                    "--artifacts-root",
                    str(root),
                ),
                input="blocked request",
                text=True,
                capture_output=True,
                env={**os.environ, "CODEX_INTENT_MODEL_BIN": str(stub)},
                check=False,
            )
            self.assertNotEqual(blocked.returncode, 0)
            blocked_review = json.loads(review_path.read_text(encoding="utf-8"))
            self.assertEqual(blocked_review["status"], "blocked")

            revise = subprocess.run(
                canonical_python_argv(
                    str(COMPILE_INTENT_SCRIPT),
                    "--request-stdin",
                    "--review-json-out",
                    str(review_path),
                    "--track-id",
                    "stdin-revise",
                    "--artifacts-root",
                    str(root),
                ),
                input="revise request",
                text=True,
                capture_output=True,
                env={**os.environ, "CODEX_INTENT_MODEL_BIN": str(stub)},
                check=False,
            )
            self.assertNotEqual(revise.returncode, 0)
            revise_review = json.loads(review_path.read_text(encoding="utf-8"))
            self.assertEqual(revise_review["status"], "revise")

    def test_verify_plan_rejects_unresolved_gaps(self) -> None:
        plan = self._fixture()
        plan["plan_gap_report"]["gaps_unresolved"] = ["authority issue remains"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compile_intent_payload(plan_payload=plan, track_id="gap-case", artifacts_root=root)
            objective_intent = json.loads((root / "gap-case" / "objective.intent.json").read_text(encoding="utf-8"))
            initialize_session_payload(objective_intent=objective_intent, track_id="gap-case", artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id="gap-case", artifacts_root=root, objective_intent=objective_intent)
            result = verify_plan_payload(plan_payload=plan, track_id="gap-case", artifacts_root=root)
            self.assertEqual(result["status"], "revise")

    def test_verify_plan_rejects_non_execution_ready_status(self) -> None:
        plan = self._fixture()
        plan["plan_status"] = "execution_ready_candidate"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compile_intent_payload(plan_payload=plan, track_id="status-case", artifacts_root=root)
            objective_intent = json.loads((root / "status-case" / "objective.intent.json").read_text(encoding="utf-8"))
            initialize_session_payload(objective_intent=objective_intent, track_id="status-case", artifacts_root=root)
            compile_plan_payload(plan_payload=plan, track_id="status-case", artifacts_root=root, objective_intent=objective_intent)
            result = verify_plan_payload(plan_payload=plan, track_id="status-case", artifacts_root=root)
            self.assertEqual(result["status"], "revise")

    def test_verify_plan_rejects_packet_scope_mismatch(self) -> None:
        plan = self._fixture()
        modified = deepcopy(plan)
        modified["packets"][0]["definition_of_done"]["allowed_scope"] = ["mismatch.py"]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            compile_intent_payload(plan_payload=modified, track_id="scope-case", artifacts_root=root)
            objective_intent = json.loads((root / "scope-case" / "objective.intent.json").read_text(encoding="utf-8"))
            initialize_session_payload(objective_intent=objective_intent, track_id="scope-case", artifacts_root=root)
            compile_plan_payload(plan_payload=modified, track_id="scope-case", artifacts_root=root, objective_intent=objective_intent)
            with self.assertRaisesRegex(ExecutionPlanCompileError, r"scope_drift:packet-compiler"):
                verify_plan_payload(plan_payload=modified, track_id="scope-case", artifacts_root=root)

    def test_compile_intent_request_stdin_uses_model_backed_normalization(self) -> None:
        script = SCRIPT_DIR / "compile_intent.py"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stub = root / "codex.real"
            stub.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "print(json.dumps({"
                "'objective':'Normalize the proxy runtime front door',"
                "'success_criteria':['Emit objective.intent.json from raw text'],"
                "'audience':['maintainers'],"
                "'scope_boundaries':{'in_scope':['compile_intent.py'],'out_of_scope':['deploy']},"
                "'authority_sensitive_decisions':[],"
                "'known_unknowns':[],"
                "'discoverable_unknowns':[],"
                "'discoverable_resolution_log':[{'question':'repo layout','resolution':'derived from prompt','source':'model'}],"
                "'clarification_questions':[],"
                "'clarification_batch_count':0,"
                "'objective_shape_status':'accepted_rewritten',"
                "'normalization_source':'model_raw_request'"
                "}))\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
            review_path = root / "review.json"
            completed = subprocess.run(
                canonical_python_argv(
                    str(script),
                    "--request-stdin",
                    "--review-json-out",
                    str(review_path),
                    "--track-id",
                    "stdin-intent",
                    "--artifacts-root",
                    str(root),
                ),
                input="Turn this messy request into a governed runtime objective.",
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "CODEX_INTENT_MODEL_BIN": str(stub)},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            objective_intent = json.loads((root / "stdin-intent" / "objective.intent.json").read_text(encoding="utf-8"))
            self.assertEqual(objective_intent["objective_shape_status"], "accepted_rewritten")
            self.assertEqual(objective_intent["normalization_source"], "model_raw_request")
            self.assertEqual(objective_intent["clarification_batch_count"], 0)

    def test_compile_intent_clarification_json_turns_revise_into_approve(self) -> None:
        script = SCRIPT_DIR / "compile_intent.py"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stub = root / "codex.real"
            stub.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "prompt = sys.argv[-1]\n"
                "has_clarification = 'ship PR-ready output' in prompt\n"
                "payload = {"
                "'objective':'Compile a raw request into governed intent',"
                "'success_criteria':(['ship PR-ready output'] if has_clarification else []),"
                "'audience':['maintainers'],"
                "'scope_boundaries':{'in_scope':['compile_intent.py'],'out_of_scope':['deployment']},"
                "'authority_sensitive_decisions':[],"
                "'known_unknowns':[],"
                "'discoverable_unknowns':[],"
                "'discoverable_resolution_log':[],"
                "'clarification_questions':([] if has_clarification else ['What does done look like?']),"
                "'clarification_batch_count':(1 if has_clarification else 0),"
                "'objective_shape_status':('accepted_rewritten' if has_clarification else 'revise_required'),"
                "'normalization_source':'model_raw_request'"
                "}\n"
                "print(json.dumps(payload))\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
            review_path = root / 'review.json'
            first = subprocess.run(
                canonical_python_argv(
                    str(script),
                    "--request-stdin",
                    "--review-json-out",
                    str(review_path),
                    "--track-id",
                    "clarify-intent",
                    "--artifacts-root",
                    str(root),
                ),
                input="Make the proxy front door handle natural language requests.",
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "CODEX_INTENT_MODEL_BIN": str(stub)},
            )
            self.assertNotEqual(first.returncode, 0)
            clarification_path = root / "clarification.json"
            clarification_path.write_text(json.dumps({"success_criteria": ["ship PR-ready output"]}), encoding="utf-8")
            second = subprocess.run(
                canonical_python_argv(
                    str(script),
                    "--request-stdin",
                    "--clarification-json",
                    str(clarification_path),
                    "--review-json-out",
                    str(review_path),
                    "--track-id",
                    "clarify-intent",
                    "--artifacts-root",
                    str(root),
                ),
                input="Make the proxy front door handle natural language requests.",
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "CODEX_INTENT_MODEL_BIN": str(stub)},
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            objective_intent = json.loads((root / "clarify-intent" / "objective.intent.json").read_text(encoding="utf-8"))
            self.assertEqual(objective_intent["clarification_batch_count"], 1)
            self.assertEqual(objective_intent["objective_shape_status"], "accepted_rewritten")


if __name__ == "__main__":
    unittest.main()
