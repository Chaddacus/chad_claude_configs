#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
import sys

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import build_objective_intent_payload, session_artifact_paths, stable_objective_id  # noqa: E402
from initialize_session import initialize_session_payload  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class InitializeSessionTests(unittest.TestCase):
    def _fixture(self) -> dict:
        return json.loads((FIXTURES / "plan_valid.json").read_text(encoding="utf-8"))

    def test_initializer_writes_all_harness_artifacts(self) -> None:
        plan = self._fixture()
        objective_intent = build_objective_intent_payload(track_id="init-case", plan=plan)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            review = initialize_session_payload(objective_intent=objective_intent, track_id="init-case", artifacts_root=root)
            self.assertEqual(review["status"], "approve")
            paths = session_artifact_paths(artifacts_root=root, track_id="init-case")
            for key in ("session", "progress", "checkpoint", "context_index"):
                self.assertTrue(paths[key].exists(), f"missing {paths[key].name}")
            for key in ("feature_list", "momentum", "blockers"):
                self.assertFalse(paths[key].exists(), f"unexpected {paths[key].name}")
            session_payload = json.loads(paths["session"].read_text(encoding="utf-8"))
            self.assertEqual(session_payload["objective_id"], stable_objective_id("init-case"))
            self.assertEqual(session_payload["packet_ids"], [])

    def test_initializer_prefers_repo_local_context_index(self) -> None:
        plan = self._fixture()
        objective_intent = build_objective_intent_payload(track_id="context-case", plan=plan)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo = Path(td) / "repo"
            docs = repo / "docs"
            docs.mkdir(parents=True)
            (docs / "context.index.json").write_text(
                json.dumps(
                    {
                        "categories": {
                            "architecture_docs": [str(docs / "architecture.md")],
                            "design_docs": [],
                            "execution_docs": [],
                            "schema_contract_docs": [],
                            "test_runbook_docs": [],
                            "security_policy_docs": [],
                            "active_objective_docs": [],
                        }
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            review = initialize_session_payload(
                objective_intent=objective_intent,
                track_id="context-case",
                artifacts_root=root,
                cwd=str(repo),
            )
            self.assertEqual(review["status"], "approve")
            context_payload = json.loads(
                session_artifact_paths(artifacts_root=root, track_id="context-case")["context_index"].read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(context_payload["source"], "repo_local")
            self.assertTrue(context_payload["source_path"].endswith("docs/context.index.json"))


if __name__ == "__main__":
    unittest.main()
