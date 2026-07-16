"""Tests for web_budget's budget/circuit policy and its RMW-race fix.

Run from /Users/chadsimon/.claude/bin/:
    python3 -m unittest web_budget_test

The load-bearing test is the concurrency one: check() used to do an
unlocked load→mutate→save, so parallel hook invocations (teammate fan-outs
— the exact scenario the breaker guards) lost increments and the budget
undercounted. _locked() serializes the RMW; 30 simultaneous processes must
land exactly 30 counts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).parent


def _env(home: str, **extra: str) -> dict:
    env = dict(os.environ)
    env["CLAUDE_HOME"] = home
    env.update(extra)
    return env


def _run_check(home: str, sid: str = "s1", **extra: str) -> str:
    """Invoke check() in a fresh interpreter (faithful to real hook calls)."""
    code = (
        f"import sys; sys.path.insert(0, {str(BIN)!r});"
        "import web_budget; print(web_budget.check("
        f"{sid!r})[0])"
    )
    out = subprocess.run([sys.executable, "-c", code], env=_env(home, **extra),
                         capture_output=True, text=True, timeout=60)
    return out.stdout.strip()


class ConcurrencyTest(unittest.TestCase):
    def test_parallel_increments_not_lost(self):
        # 30 simultaneous check() calls -> count must be exactly 30.
        with tempfile.TemporaryDirectory() as home:
            code = (
                f"import sys; sys.path.insert(0, {str(BIN)!r});"
                "import web_budget; web_budget.check('swarm')"
            )
            procs = [
                subprocess.Popen([sys.executable, "-c", code], env=_env(home))
                for _ in range(30)
            ]
            for p in procs:
                self.assertEqual(p.wait(timeout=60), 0)
            state = json.load(open(Path(home) / "state" / "web_search_breaker.json"))
            self.assertEqual(state["swarm"]["count"], 30,
                             "increments were lost — RMW race is back")


class PolicyTest(unittest.TestCase):
    def test_budget_denies_over_cap(self):
        with tempfile.TemporaryDirectory() as home:
            results = [
                _run_check(home, sid="cap", WEB_BREAKER_MAX_CALLS="3")
                for _ in range(4)
            ]
            self.assertEqual(results, ["True", "True", "True", "False"])

    def test_failure_trip_opens_circuit(self):
        with tempfile.TemporaryDirectory() as home:
            code = (
                f"import sys; sys.path.insert(0, {str(BIN)!r});"
                "import web_budget as wb;"
                "wb.record('trip', False); wb.record('trip', False);"
                "allowed, reason = wb.check('trip');"
                "print(allowed); print(reason[:30])"
            )
            out = subprocess.run(
                [sys.executable, "-c", code],
                env=_env(home, WEB_BREAKER_MAX_FAILS="2"),
                capture_output=True, text=True, timeout=60)
            lines = out.stdout.strip().splitlines()
            self.assertEqual(lines[0], "False")
            self.assertIn("circuit OPEN", lines[1])

    def test_fail_open_on_corrupt_state(self):
        with tempfile.TemporaryDirectory() as home:
            state_dir = Path(home) / "state"
            state_dir.mkdir(parents=True)
            (state_dir / "web_search_breaker.json").write_text("{corrupt json")
            self.assertEqual(_run_check(home), "True")


if __name__ == "__main__":
    unittest.main()
