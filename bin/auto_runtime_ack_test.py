"""Tests for the S7 reviewer-ack dispatch gate (auto_runtime_common).

Run from /Users/chadsimon/.claude/bin/:
    python3 -m unittest auto_runtime_ack_test

The gate: R3/R4 tracks may not dispatch any slice until a reviewer ack is
recorded (record_reviewer_ack / `auto_runtime.py record-ack`). R1/R2 exempt.
The gate never mutates node state, so recording the ack is the full unblock.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import auto_runtime_common as rt


def _make_track(tmp: Path, task: str, route: str) -> str:
    """Init a track and give slice-1 enough acceptance criteria for R3/R4."""
    r = rt.initialize_track(task=task, cwd=str(tmp), route_override=route,
                            include_memory=False)
    tid = r["track_id"]
    sd = rt.track_dir(tid)
    st = json.loads((sd / "objective.state.json").read_text())
    node = st["views"]["graph"]["nodes"]["slice-1"]
    node.setdefault("slice_contract", {})["acceptance_criteria"] = [
        "criterion one", "criterion two", "criterion three",
    ]
    (sd / "objective.state.json").write_text(json.dumps(st))
    return tid


class ReviewerAckGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ackgate-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_r3_dispatch_blocks_without_ack_and_node_stays_ready(self) -> None:
        tid = _make_track(self.tmp, "ack gate r3", "R3")
        d = rt.dispatch_track(tid)
        self.assertEqual(d["status"], "blocked", d)
        self.assertEqual(d["reason"], "missing_reviewer_ack")
        self.assertIn("record-ack", d.get("unblock", ""))
        # The node must NOT be marked blocked — the gate is recoverable.
        st = json.loads((rt.track_dir(tid) / "objective.state.json").read_text())
        self.assertNotEqual(st["views"]["graph"]["nodes"]["slice-1"]["state"], "blocked")

    def test_record_ack_unblocks_r3_dispatch(self) -> None:
        tid = _make_track(self.tmp, "ack gate r3 unblock", "R3")
        self.assertEqual(rt.dispatch_track(tid)["reason"], "missing_reviewer_ack")
        res = rt.record_reviewer_ack(tid, acked_by="reviewer", ref="criteria-hash-abc")
        self.assertEqual(res["reviewer_ack"]["acked_by"], "reviewer")
        d = rt.dispatch_track(tid)
        self.assertEqual(d["status"], "dispatched", d)

    def test_r2_dispatch_needs_no_ack(self) -> None:
        tid = _make_track(self.tmp, "ack gate r2 exempt", "R2")
        d = rt.dispatch_track(tid)
        self.assertEqual(d["status"], "dispatched", d)

    def test_validate_reviewer_ack_predicate_shapes(self) -> None:
        self.assertTrue(rt.validate_reviewer_ack({}, "R2")["valid"])
        self.assertFalse(rt.validate_reviewer_ack({}, "R3")["valid"])
        good = {"views": {"governance": {"reviewer_ack": {
            "acked_by": "reviewer", "ref": "x", "at": "2026-07-15T00:00:00Z"}}}}
        self.assertTrue(rt.validate_reviewer_ack(good, "R4")["valid"])
        # Malformed ack (missing acked_by) does not pass.
        bad = {"views": {"governance": {"reviewer_ack": {"at": "t"}}}}
        self.assertFalse(rt.validate_reviewer_ack(bad, "R3")["valid"])

    def test_content_free_ack_rejected(self) -> None:
        # Reviewer finding (2026-07-16): an ack row with empty ref must NOT
        # satisfy the gate — it enforces "a reviewer reviewed THIS".
        empty_ref = {"views": {"governance": {"reviewer_ack": {
            "acked_by": "reviewer", "ref": "   ", "at": "2026-07-16T00:00:00Z"}}}}
        self.assertFalse(rt.validate_reviewer_ack(empty_ref, "R3")["valid"])
        tid = _make_track(self.tmp, "ack gate empty ref", "R3")
        with self.assertRaises(ValueError):
            rt.record_reviewer_ack(tid, acked_by="reviewer", ref="  ")
        # Still blocked after the rejected record attempt.
        self.assertEqual(rt.dispatch_track(tid)["reason"], "missing_reviewer_ack")


if __name__ == "__main__":
    unittest.main()
