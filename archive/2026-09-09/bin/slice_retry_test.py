#!/usr/bin/env python3
"""Unit tests for slice_retry (CP7). Run: python3 slice_retry_test.py

Uses a scripted fake `execute_fn` so the retry/backoff policy is exercised
deterministically without spawning real workers.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import slice_retry
from slice_executor import ExecutorResult, SliceSpec


def _spec() -> SliceSpec:
    return SliceSpec(prompt="p", commit_message="m", worker_command=["w"], verifier_command=["v"])


def _scripted(results):
    it = iter(results)

    def fn(*, main_repo, spec):
        return next(it)

    return fn


class TestSliceRetry(unittest.TestCase):
    def _run(self, results, **kw):
        return slice_retry.run_slice_with_retry(
            main_repo=Path("."), build_spec=lambda a, l: _spec(),
            execute_fn=_scripted(results), sleep=lambda *_: None, **kw,
        )

    def test_success_first_try(self):
        out = self._run([ExecutorResult(ok=True, stage="done", new_head_sha="s1")])
        self.assertTrue(out.ok)
        self.assertEqual(out.attempts, 1)
        self.assertIsNone(out.gave_up_reason)

    def test_transient_then_success(self):
        out = self._run([
            ExecutorResult(ok=False, stage="worker", error="boom"),
            ExecutorResult(ok=True, stage="done", new_head_sha="s2"),
        ])
        self.assertTrue(out.ok)
        self.assertEqual(out.attempts, 2)

    def test_transient_exhausts_at_max_attempts(self):
        out = self._run([ExecutorResult(ok=False, stage="worker", error="x")] * 5, max_attempts=3)
        self.assertFalse(out.ok)
        self.assertEqual(out.attempts, 3)
        self.assertEqual(out.gave_up_reason, "max_attempts")

    def test_hard_stage_gives_up_faster(self):
        out = self._run(
            [ExecutorResult(ok=False, stage="static_gate", error="banned")] * 5,
            max_attempts=3, hard_stage_max_attempts=2,
        )
        self.assertFalse(out.ok)
        self.assertEqual(out.attempts, 2)
        self.assertEqual(out.gave_up_reason, "hard_stage_limit")

    def test_rate_limit_backoff_then_success(self):
        sleeps = []
        out = slice_retry.run_slice_with_retry(
            main_repo=Path("."), build_spec=lambda a, l: _spec(),
            execute_fn=_scripted([
                ExecutorResult(ok=False, stage="worker", error="HTTP 429 rate limit hit"),
                ExecutorResult(ok=True, stage="done", new_head_sha="s3"),
            ]),
            sleep=lambda s: sleeps.append(s),
        )
        self.assertTrue(out.ok)
        self.assertEqual(out.attempts, 1)               # rate-limit did not burn an attempt
        self.assertEqual(out.rate_limit_retries, 1)
        self.assertEqual(len(sleeps), 1)

    def test_rate_limit_exhausts(self):
        out = self._run(
            [ExecutorResult(ok=False, stage="worker", error="overloaded")] * 6,
            max_rate_limit_retries=2,
        )
        self.assertFalse(out.ok)
        self.assertEqual(out.gave_up_reason, "rate_limit_exhausted")

    def test_build_spec_receives_prior_result(self):
        seen = []

        def build(attempt, last):
            seen.append((attempt, last.stage if last else None))
            return _spec()

        slice_retry.run_slice_with_retry(
            main_repo=Path("."), build_spec=build,
            execute_fn=_scripted([
                ExecutorResult(ok=False, stage="worker", error="e"),
                ExecutorResult(ok=True, stage="done", new_head_sha="s"),
            ]),
            sleep=lambda *_: None,
        )
        self.assertEqual(seen[0], (1, None))
        self.assertEqual(seen[1], (2, "worker"))       # prior failure fed to next attempt


if __name__ == "__main__":
    unittest.main(verbosity=2)
