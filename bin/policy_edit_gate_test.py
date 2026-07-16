"""Tests for policy_edit_gate's --review-queue operator CLI (audit M6).

Run from /Users/chadsimon/.claude/bin/:
    python3 -m unittest policy_edit_gate_test

Covers the review finding from the 2026-07-16 branch review: review_queue()
shipped without tests. Pins all four status branches (APPLIED / NOT-APPLIED
/ SUPERSEDED / MISSING), Write-tool status, archive semantics (all proposals
moved + summary written + queue emptied), and the per-run summary filename
(a same-day second archive must not overwrite the first run's record).
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import policy_edit_gate as peg


class ReviewQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        # Redirect the module's state surface into the sandbox.
        self._orig = (peg.STATE_DIR, peg.QUEUE_DIR, peg.LOG_FILE)
        peg.STATE_DIR = root / "policy-edit-gate"
        peg.QUEUE_DIR = peg.STATE_DIR / "queue"
        peg.LOG_FILE = peg.STATE_DIR / "gate.log"
        peg.QUEUE_DIR.mkdir(parents=True)
        # A watched-file stand-in the proposals point at.
        self.target = root / "watched.md"
        self.target.write_text("alpha\nNEW CONTENT\ngamma\n")

    def tearDown(self) -> None:
        peg.STATE_DIR, peg.QUEUE_DIR, peg.LOG_FILE = self._orig
        self._tmp.cleanup()

    def _enqueue(self, name: str, **payload) -> Path:
        p = peg.QUEUE_DIR / f"proposal-{name}.json"
        p.write_text(json.dumps({
            "ts": "2026-07-16T00:00:00+00:00",
            "target": str(payload.pop("target", self.target)),
            "tool_name": payload.pop("tool_name", "Edit"),
            "tool_input": payload.pop("tool_input", {}),
        }))
        return p

    def _run(self, archive: bool = False) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = peg.review_queue(archive=archive)
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_status_branches(self):
        self._enqueue("applied", tool_input={"old_string": "OLD", "new_string": "NEW CONTENT"})
        self._enqueue("notapplied", tool_input={"old_string": "alpha", "new_string": "never landed"})
        self._enqueue("superseded", tool_input={"old_string": "OLD", "new_string": "also gone"})
        self._enqueue("missing", target=self.target.parent / "no-such-file.md",
                      tool_input={"old_string": "x", "new_string": "y"})
        out = self._run()
        self.assertIn("4 proposal(s)", out)
        for status, name in [("APPLIED", "applied"), ("NOT-APPLIED", "notapplied"),
                             ("SUPERSEDED", "superseded"), ("MISSING", "missing")]:
            line = next(l for l in out.splitlines() if f"proposal-{name}.json" in l)
            self.assertIn(status, line)

    def test_write_tool_applied_vs_superseded(self):
        self._enqueue("w-applied", tool_name="Write",
                      tool_input={"content": self.target.read_text()})
        self._enqueue("w-superseded", tool_name="Write",
                      tool_input={"content": "something else entirely"})
        out = self._run()
        self.assertIn("APPLIED", next(l for l in out.splitlines() if "w-applied" in l))
        self.assertIn("SUPERSEDED", next(l for l in out.splitlines() if "w-superseded" in l))

    def test_archive_moves_all_and_empties_queue(self):
        self._enqueue("one", tool_input={"old_string": "OLD", "new_string": "NEW CONTENT"})
        self._enqueue("two", tool_input={"old_string": "alpha", "new_string": "z"})
        self._run(archive=True)
        self.assertEqual(list(peg.QUEUE_DIR.glob("proposal-*.json")), [])
        reviewed = list((peg.STATE_DIR / "reviewed").rglob("proposal-*.json"))
        self.assertEqual(len(reviewed), 2)
        summaries = list((peg.STATE_DIR / "reviewed").rglob("review-summary-*.json"))
        self.assertEqual(len(summaries), 1)
        entries = json.loads(summaries[0].read_text())["entries"]
        self.assertEqual(len(entries), 2)
        # Empty-queue re-run is a safe no-op.
        out = self._run(archive=True)
        self.assertIn("empty", out)

    def test_same_day_double_archive_keeps_both_summaries(self):
        # Review finding (LOW, 2026-07-16): a fixed summary filename lost the
        # morning run's record on a same-day second archive.
        self._enqueue("morning", tool_input={"old_string": "OLD", "new_string": "NEW CONTENT"})
        self._run(archive=True)
        self._enqueue("afternoon", tool_input={"old_string": "alpha", "new_string": "z"})
        self._run(archive=True)
        summaries = list((peg.STATE_DIR / "reviewed").rglob("review-summary-*.json"))
        self.assertEqual(len(summaries), 2,
                         "second same-day archive overwrote the first summary")
        proposals = list((peg.STATE_DIR / "reviewed").rglob("proposal-*.json"))
        self.assertEqual(len(proposals), 2)

    def test_unreadable_proposal_reported_not_crashed(self):
        bad = peg.QUEUE_DIR / "proposal-bad.json"
        bad.write_text("{not json")
        out = self._run()
        self.assertIn("UNREADABLE", out)


if __name__ == "__main__":
    unittest.main()
