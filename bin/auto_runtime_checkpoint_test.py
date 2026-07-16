"""Tests for _git_checkpoint_on_acceptance's owned-scope discipline.

Run from /Users/chadsimon/.claude/bin/:
    python3 -m unittest auto_runtime_checkpoint_test

Regression for the 2026-07-16 sweep incident: a slice accepted with no
owned_scope auto-committed the ENTIRE dirty working tree (owned_scope
defaulted to [cwd] -> `git add <repo-root>`, plus an `add -u` fallback),
capturing 11 unrelated in-flight user files into a feature branch. The
contract now: an auto-checkpoint may only commit what the slice DECLARED
it owns; no scope -> no commit; the repo root is never a scope; relative
scopes resolve against the repo cwd.
"""
from __future__ import annotations

import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from auto_runtime_common import _git_checkpoint_on_acceptance


def _git(cwd: Path, *args) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return p.stdout.strip()


class CheckpointScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ckpt-"))
        _git(self.tmp, "init", "-q", "-b", "work")
        _git(self.tmp, "config", "user.email", "t@t")
        _git(self.tmp, "config", "user.name", "t")
        (self.tmp / "owned.py").write_text("x = 1\n")
        (self.tmp / "unrelated.py").write_text("y = 1\n")
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-q", "-m", "seed")
        # Dirty BOTH files — the checkpoint must only ever take the owned one.
        (self.tmp / "owned.py").write_text("x = 2\n")
        (self.tmp / "unrelated.py").write_text("y = 2\n")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _head(self) -> str:
        return _git(self.tmp, "rev-parse", "HEAD")

    def test_no_owned_scope_skips_and_never_sweeps(self) -> None:
        head = self._head()
        res = _git_checkpoint_on_acceptance(
            "t", "slice-1", {"title": "t", "owned_scope": []}, str(self.tmp))
        self.assertEqual(res["status"], "skipped")
        self.assertEqual(res["reason"], "no_owned_scope")
        self.assertEqual(self._head(), head)                    # no commit
        self.assertIn("unrelated.py", _git(self.tmp, "status", "--short"))

    def test_repo_root_scope_is_not_a_scope(self) -> None:
        # The exact shape that caused the sweep: owned_scope = [cwd].
        head = self._head()
        res = _git_checkpoint_on_acceptance(
            "t", "slice-1", {"title": "t", "owned_scope": [str(self.tmp)]}, str(self.tmp))
        self.assertEqual(res["reason"], "no_owned_scope")
        self.assertEqual(self._head(), head)

    def test_owned_scope_commits_only_owned_files(self) -> None:
        res = _git_checkpoint_on_acceptance(
            "t", "slice-1", {"title": "t", "owned_scope": ["owned.py"]}, str(self.tmp))
        self.assertEqual(res["status"], "committed", res)
        committed = _git(self.tmp, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(committed, ["owned.py"])
        # The unrelated dirt survives, uncommitted.
        self.assertIn("unrelated.py", _git(self.tmp, "status", "--short"))

    def test_outside_repo_scope_ignored(self) -> None:
        outside = self.tmp.parent / f"outside-{self.tmp.name}.txt"
        outside.write_text("z\n")
        try:
            res = _git_checkpoint_on_acceptance(
                "t", "slice-1", {"title": "t", "owned_scope": [str(outside)]}, str(self.tmp))
            self.assertEqual(res["reason"], "no_owned_scope")
        finally:
            outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
