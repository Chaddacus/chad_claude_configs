"""Tests for slice_executor.execute_slice.

Run from /Users/chadsimon/.claude/bin/:
    python3 -m unittest slice_executor_test
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import slice_executor
from slice_executor import ExecutorResult, SliceSpec, execute_slice


def _run(cmd, cwd, **kwargs):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=True, **kwargs)


def _init_repo(path: Path, files: dict) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], cwd=path)
    _run(["git", "config", "user.email", "test@local"], cwd=path)
    _run(["git", "config", "user.name", "Test"], cwd=path)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=path)
    for relpath, content in files.items():
        target = path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        _run(["git", "add", relpath], cwd=path)
    _run(["git", "commit", "-q", "-m", "init"], cwd=path)
    return _run(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()


def _make_worker_script(path: Path, body: str) -> None:
    """Write a shell script that acts as a fake worker.

    The script is invoked with the prompt as $1 in worker_sandbox's cwd
    (a fresh git worktree). It must produce file changes; worker_sandbox
    captures them via git diff.
    """
    path.write_text("#!/bin/sh\nset -e\n" + body + "\n")
    path.chmod(0o755)


def _make_verifier_script(path: Path, body: str) -> None:
    """Write a shell script that acts as a fake verifier.

    The script is invoked with cwd set to the snapshot dir. Its stdout
    is parsed for CLAIM/CITE lines.
    """
    path.write_text("#!/bin/sh\nset -e\n" + body + "\n")
    path.chmod(0o755)


class ExecutorHappyPathTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cp5-"))
        self.repo = self.tmp / "main"
        self.base_sha = _init_repo(
            self.repo,
            {"app.py": "def foo():\n    return 1\n"},
        )
        # Worker: write a new function to app.py.
        self.worker_script = self.tmp / "worker.sh"
        _make_worker_script(
            self.worker_script,
            "cat > app.py <<EOF\n"
            "def foo():\n"
            "    return 2\n"
            "EOF",
        )
        # Verifier: emit one valid CLAIM/CITE.
        self.verifier_script = self.tmp / "verifier.sh"
        _make_verifier_script(
            self.verifier_script,
            'echo "CLAIM: foo updated to return 2"\n'
            'echo "CITE: app.py:1-2 \\"return 2\\""\n',
        )

    def tearDown(self) -> None:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(self.repo) if self.repo.exists() else self.tmp,
            capture_output=True,
        )
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spec(self, **overrides) -> SliceSpec:
        defaults = dict(
            prompt="update foo",
            commit_message="update: foo returns 2",
            worker_command=[str(self.worker_script)],
            verifier_command=[str(self.verifier_script)],
            worker_timeout_s=30,
            verifier_timeout_s=30,
        )
        defaults.update(overrides)
        return SliceSpec(**defaults)

    def test_happy_path_pipeline(self) -> None:
        result = execute_slice(main_repo=self.repo, spec=self._spec())
        self.assertTrue(result.ok, f"{result.stage}: {result.error}")
        self.assertEqual(result.stage, "done")
        self.assertIsNotNone(result.new_head_sha)
        self.assertNotEqual(result.new_head_sha, self.base_sha)
        # Main now has the updated content.
        self.assertEqual((self.repo / "app.py").read_text(), "def foo():\n    return 2\n")
        # Validation result has the parsed claim.
        self.assertEqual(len(result.validation_result.claims), 1)
        # Cleanup: snapshot and candidate are gone.
        if result.snapshot_path:
            self.assertFalse(Path(result.snapshot_path).exists())
        if result.candidate_path:
            self.assertFalse(Path(result.candidate_path).exists())


class ExecutorRejectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cp5-rej-"))
        self.repo = self.tmp / "main"
        self.base_sha = _init_repo(
            self.repo,
            {"app.py": "def foo():\n    return 1\n"},
        )

    def tearDown(self) -> None:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(self.repo) if self.repo.exists() else self.tmp,
            capture_output=True,
        )
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _good_verifier(self) -> Path:
        p = self.tmp / "good_verifier.sh"
        _make_verifier_script(
            p,
            'echo "CLAIM: ok"\n'
            'echo "CITE: app.py:1-2"\n',
        )
        return p

    def _empty_verifier(self) -> Path:
        p = self.tmp / "empty_verifier.sh"
        _make_verifier_script(p, 'echo "PASS"')
        return p

    def test_worker_failure(self) -> None:
        bad_worker = self.tmp / "bad.sh"
        bad_worker.write_text("#!/bin/sh\nexit 1\n")
        bad_worker.chmod(0o755)
        spec = SliceSpec(
            prompt="fail",
            commit_message="x",
            worker_command=[str(bad_worker)],
            verifier_command=[str(self._good_verifier())],
            worker_timeout_s=15,
            verifier_timeout_s=15,
        )
        result = execute_slice(main_repo=self.repo, spec=spec)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "worker")
        # Main untouched.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, self.base_sha)

    def test_empty_diff(self) -> None:
        noop_worker = self.tmp / "noop.sh"
        noop_worker.write_text("#!/bin/sh\nexit 0\n")
        noop_worker.chmod(0o755)
        spec = SliceSpec(
            prompt="noop",
            commit_message="x",
            worker_command=[str(noop_worker)],
            verifier_command=[str(self._good_verifier())],
            worker_timeout_s=15,
            verifier_timeout_s=15,
        )
        result = execute_slice(main_repo=self.repo, spec=spec)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "empty_diff")

    def test_static_gate_rejects_eval(self) -> None:
        # Worker introduces eval().
        worker = self.tmp / "evil.sh"
        _make_worker_script(
            worker,
            "cat > app.py <<EOF\n"
            "def foo(x):\n"
            "    return eval(x)\n"
            "EOF",
        )
        spec = SliceSpec(
            prompt="evil",
            commit_message="x",
            worker_command=[str(worker)],
            verifier_command=[str(self._good_verifier())],
            worker_timeout_s=15,
            verifier_timeout_s=15,
        )
        result = execute_slice(main_repo=self.repo, spec=spec)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "static_gate")
        # Main untouched.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, self.base_sha)

    def test_verifier_rejects_empty_output(self) -> None:
        good_worker = self.tmp / "good.sh"
        _make_worker_script(
            good_worker,
            "cat > app.py <<EOF\n"
            "def foo():\n"
            "    return 2\n"
            "EOF",
        )
        # Verifier emits no CLAIM lines.
        spec = SliceSpec(
            prompt="x",
            commit_message="x",
            worker_command=[str(good_worker)],
            verifier_command=[str(self._empty_verifier())],
            worker_timeout_s=15,
            verifier_timeout_s=15,
        )
        result = execute_slice(main_repo=self.repo, spec=spec)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "verify")
        self.assertIn("no CLAIM", result.error)
        # Main untouched.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, self.base_sha)

    def test_verifier_subprocess_failure(self) -> None:
        good_worker = self.tmp / "good.sh"
        _make_worker_script(
            good_worker,
            "cat > app.py <<EOF\n"
            "def foo():\n"
            "    return 2\n"
            "EOF",
        )
        bad_verifier = self.tmp / "bad_ver.sh"
        bad_verifier.write_text("#!/bin/sh\nexit 5\n")
        bad_verifier.chmod(0o755)
        spec = SliceSpec(
            prompt="x",
            commit_message="x",
            worker_command=[str(good_worker)],
            verifier_command=[str(bad_verifier)],
            worker_timeout_s=15,
            verifier_timeout_s=15,
        )
        result = execute_slice(main_repo=self.repo, spec=spec)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "verifier_subprocess")
        self.assertEqual(result.verifier_exit_code, 5)
        # Main untouched.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, self.base_sha)

    def test_verifier_timeout(self) -> None:
        good_worker = self.tmp / "good.sh"
        _make_worker_script(
            good_worker,
            "cat > app.py <<EOF\n"
            "def foo():\n"
            "    return 2\n"
            "EOF",
        )
        slow_verifier = self.tmp / "slow.sh"
        slow_verifier.write_text("#!/bin/sh\nsleep 10\n")
        slow_verifier.chmod(0o755)
        spec = SliceSpec(
            prompt="x",
            commit_message="x",
            worker_command=[str(good_worker)],
            verifier_command=[str(slow_verifier)],
            worker_timeout_s=15,
            verifier_timeout_s=1,
        )
        result = execute_slice(main_repo=self.repo, spec=spec)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "verifier_subprocess")
        self.assertTrue(result.verifier_timed_out)

    def test_verifier_bogus_citation(self) -> None:
        good_worker = self.tmp / "good.sh"
        _make_worker_script(
            good_worker,
            "cat > app.py <<EOF\n"
            "def foo():\n"
            "    return 2\n"
            "EOF",
        )
        # Verifier cites a non-existent file.
        ver = self.tmp / "ver_bogus.sh"
        _make_verifier_script(
            ver,
            'echo "CLAIM: I did the work"\n'
            'echo "CITE: nonexistent.py:1-5"\n',
        )
        spec = SliceSpec(
            prompt="x",
            commit_message="x",
            worker_command=[str(good_worker)],
            verifier_command=[str(ver)],
            worker_timeout_s=15,
            verifier_timeout_s=15,
        )
        result = execute_slice(main_repo=self.repo, spec=spec)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "verify")
        # Main untouched.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, self.base_sha)

    def test_main_untouched_invariant_across_stages(self) -> None:
        """Across multiple rejection-causing inputs, main HEAD must
        remain at base_sha and app.py must remain unchanged."""
        good_worker = self.tmp / "good.sh"
        _make_worker_script(
            good_worker,
            "cat > app.py <<EOF\n"
            "def foo():\n"
            "    return 2\n"
            "EOF",
        )

        scenarios = []

        bad_w = self.tmp / "bad_w.sh"
        bad_w.write_text("#!/bin/sh\nexit 7\n")
        bad_w.chmod(0o755)
        scenarios.append(("worker", SliceSpec(
            prompt="x", commit_message="x",
            worker_command=[str(bad_w)],
            verifier_command=[str(self._good_verifier())],
            worker_timeout_s=10, verifier_timeout_s=10,
        )))

        scenarios.append(("verify", SliceSpec(
            prompt="x", commit_message="x",
            worker_command=[str(good_worker)],
            verifier_command=[str(self._empty_verifier())],
            worker_timeout_s=10, verifier_timeout_s=10,
        )))

        for name, spec in scenarios:
            with self.subTest(scenario=name):
                head_before = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
                content_before = (self.repo / "app.py").read_text()
                result = execute_slice(main_repo=self.repo, spec=spec)
                self.assertFalse(result.ok)
                head_after = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
                content_after = (self.repo / "app.py").read_text()
                self.assertEqual(head_before, head_after, f"{name}: HEAD moved")
                self.assertEqual(content_before, content_after, f"{name}: content changed")


if __name__ == "__main__":
    unittest.main()
