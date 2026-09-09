#!/usr/bin/env python3
"""Tests for worker_sandbox.py (CP1).

All tests use fake worker scripts (no live Claude). The fake worker is a
Python one-liner whose argv is constructed per-test to perform the exact
behavior under test.

Run:
    python3 -m unittest bin/worker_sandbox_test.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

# Make the bin dir importable.
sys.path.insert(0, str(Path(__file__).parent))

from worker_sandbox import (  # noqa: E402
    SandboxError,
    WorkerRunResult,
    run_worker_in_sandbox,
    _is_clean,
)


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    ).stdout


def _make_repo() -> Path:
    """Create a temp git repo with one commit. Returns the path."""
    tmp = Path(tempfile.mkdtemp(prefix="cp1-repo-"))
    _git(["init", "-q", "-b", "main"], cwd=tmp)
    _git(["config", "user.email", "test@example.invalid"], cwd=tmp)
    _git(["config", "user.name", "Test"], cwd=tmp)
    (tmp / "README.md").write_text("seed\n")
    _git(["add", "."], cwd=tmp)
    _git(["commit", "-q", "-m", "seed"], cwd=tmp)
    return tmp


def _write_fake_worker(script_dir: Path, name: str, body: str) -> Path:
    """Write a fake worker script. `body` is Python source; argv ends with
    the prompt (which worker can read or ignore)."""
    script_dir.mkdir(parents=True, exist_ok=True)
    path = script_dir / name
    path.write_text(textwrap.dedent(body))
    path.chmod(0o755)
    return path


class WorkerSandboxTest(unittest.TestCase):
    def setUp(self):
        self.repo = _make_repo()
        self.scripts = Path(tempfile.mkdtemp(prefix="cp1-scripts-"))
        self.head_before = _git(["rev-parse", "HEAD"], cwd=self.repo).strip()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.scripts, ignore_errors=True)

    # ------------------------------------------------------------------
    # Property 1: main repo HEAD invariant holds across all paths.
    # ------------------------------------------------------------------

    def test_clean_run_main_head_unchanged(self):
        """Worker makes an edit and commits; main HEAD stays at base."""
        worker = _write_fake_worker(self.scripts, "edit_and_commit.py", """
            import subprocess, pathlib, sys
            wt = pathlib.Path.cwd()
            (wt / "added.txt").write_text("hello from worker\\n")
            subprocess.run(["git", "config", "user.email", "w@x"], check=True)
            subprocess.run(["git", "config", "user.name", "W"], check=True)
            subprocess.run(["git", "add", "."], check=True)
            subprocess.run(["git", "commit", "-q", "-m", "worker change"], check=True)
            sys.exit(0)
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="add a file",
            branch_name="cp1/edit-and-commit",
            worker_command=["python3", str(worker)],
        )
        self.assertTrue(result.ok, msg=f"result={result}")
        self.assertEqual(result.exit_code, 0)
        self.assertNotEqual(result.tip_sha, result.base_sha, "worker should have committed")
        self.assertIn("added.txt", result.changed_files)
        self.assertIn("hello from worker", result.diff)
        # Critical: main repo HEAD is unchanged.
        self.assertEqual(result.main_head_after, self.head_before)
        self.assertEqual(_git(["rev-parse", "HEAD"], cwd=self.repo).strip(), self.head_before)
        # Sandbox cleaned up on success.
        self.assertFalse(result.sandbox_retained)
        self.assertIsNone(result.sandbox_path)
        # Branch should be cleaned up too.
        branches = _git(["branch", "--list", "cp1/edit-and-commit"], cwd=self.repo)
        self.assertEqual(branches.strip(), "")

    def test_worker_nonzero_exit_keeps_sandbox_main_intact(self):
        """Worker exits 1 after editing; sandbox retained, main HEAD unchanged."""
        worker = _write_fake_worker(self.scripts, "fail_after_edit.py", """
            import pathlib, sys
            wt = pathlib.Path.cwd()
            (wt / "partial.txt").write_text("half-done\\n")
            sys.exit(1)
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="fail",
            branch_name="cp1/fail-after-edit",
            worker_command=["python3", str(worker)],
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 1)
        # Worker did edit; we captured the uncommitted change.
        self.assertTrue(result.working_tree_dirty)
        self.assertIn("partial.txt", result.changed_files)
        # Sandbox retained for inspection.
        self.assertTrue(result.sandbox_retained)
        self.assertIsNotNone(result.sandbox_path)
        self.assertTrue(result.sandbox_path.exists())
        # Main repo HEAD unchanged.
        self.assertEqual(_git(["rev-parse", "HEAD"], cwd=self.repo).strip(), self.head_before)
        # Cleanup the retained sandbox so tearDown doesn't choke.
        _git(["worktree", "remove", "--force", str(result.sandbox_path)], cwd=self.repo)

    def test_worker_timeout_kills_and_keeps_sandbox(self):
        """Worker sleeps past timeout; subprocess group killed, main HEAD unchanged."""
        worker = _write_fake_worker(self.scripts, "sleeper.py", """
            import time, sys
            time.sleep(60)
            sys.exit(0)
        """)
        t0 = time.monotonic()
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="hang",
            branch_name="cp1/sleeper",
            worker_command=["python3", str(worker)],
            timeout_s=2,
        )
        elapsed = time.monotonic() - t0
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)
        self.assertNotEqual(result.exit_code, 0)
        # Killed promptly, not 60s later.
        self.assertLess(elapsed, 15, f"timeout enforcement took {elapsed}s")
        # Main HEAD unchanged.
        self.assertEqual(_git(["rev-parse", "HEAD"], cwd=self.repo).strip(), self.head_before)
        # Sandbox retained.
        self.assertTrue(result.sandbox_retained)
        if result.sandbox_path and result.sandbox_path.exists():
            _git(["worktree", "remove", "--force", str(result.sandbox_path)], cwd=self.repo)

    def test_worker_no_changes_yields_empty_diff(self):
        """Worker exits 0 without editing; diff and changed_files are empty."""
        worker = _write_fake_worker(self.scripts, "noop.py", """
            import sys
            sys.exit(0)
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="do nothing",
            branch_name="cp1/noop",
            worker_command=["python3", str(worker)],
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.diff, "")
        self.assertEqual(result.changed_files, [])
        self.assertFalse(result.working_tree_dirty)
        self.assertEqual(result.base_sha, result.tip_sha)
        # Main HEAD unchanged.
        self.assertEqual(_git(["rev-parse", "HEAD"], cwd=self.repo).strip(), self.head_before)

    def test_worker_commits_captures_committed_diff(self):
        """Committed and uncommitted changes both appear in the captured diff."""
        worker = _write_fake_worker(self.scripts, "mixed.py", """
            import subprocess, pathlib, sys
            wt = pathlib.Path.cwd()
            (wt / "committed.txt").write_text("c\\n")
            subprocess.run(["git", "config", "user.email", "w@x"], check=True)
            subprocess.run(["git", "config", "user.name", "W"], check=True)
            subprocess.run(["git", "add", "committed.txt"], check=True)
            subprocess.run(["git", "commit", "-q", "-m", "c"], check=True)
            # Now leave an uncommitted edit too.
            (wt / "uncommitted.txt").write_text("u\\n")
            sys.exit(0)
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="mixed",
            branch_name="cp1/mixed",
            worker_command=["python3", str(worker)],
        )
        # Worker exited 0 but left uncommitted edits — sandbox itself still
        # reports ok=True because the *subprocess* succeeded. The commit
        # contract is enforced in CP4 (parent-side post-worker check), not
        # here. CP1 just captures faithfully.
        self.assertTrue(result.ok)
        self.assertNotEqual(result.tip_sha, result.base_sha)
        self.assertTrue(result.working_tree_dirty)
        self.assertIn("committed.txt", result.changed_files)
        self.assertIn("uncommitted.txt", result.changed_files)
        # Main HEAD unchanged.
        self.assertEqual(_git(["rev-parse", "HEAD"], cwd=self.repo).strip(), self.head_before)
        # On success, sandbox cleaned up.
        self.assertFalse(result.sandbox_retained)

    # ------------------------------------------------------------------
    # Property 2: invariant holds even with dirty main repo at start.
    # ------------------------------------------------------------------

    def test_main_dirty_at_start_does_not_crash(self):
        """Main repo dirty before run; sandbox still operates, invariant held.

        CP1 does not refuse on dirty main — that's CP2's concern. CP1 just
        reports the state and ensures the worker can't make it worse.
        """
        (self.repo / "dirty.txt").write_text("uncommitted\n")
        self.assertFalse(_is_clean(self.repo))
        worker = _write_fake_worker(self.scripts, "edit.py", """
            import pathlib, sys
            (pathlib.Path.cwd() / "added.txt").write_text("x\\n")
            sys.exit(0)
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="x",
            branch_name="cp1/main-dirty",
            worker_command=["python3", str(worker)],
        )
        self.assertTrue(result.ok, msg=f"result.error={result.error}")
        # Main porcelain content was captured before AND after; the worker
        # didn't touch main, so the porcelain output is identical.
        self.assertNotEqual(result.main_porcelain_before.strip(), "",
                            "main should report dirty in porcelain before")
        self.assertEqual(result.main_porcelain_before, result.main_porcelain_after,
                         "main porcelain must be identical before/after")
        # HEAD unchanged.
        self.assertEqual(_git(["rev-parse", "HEAD"], cwd=self.repo).strip(), self.head_before)
        # The original dirty file is still there, untouched.
        self.assertTrue((self.repo / "dirty.txt").exists())
        self.assertEqual((self.repo / "dirty.txt").read_text(), "uncommitted\n")

    # ------------------------------------------------------------------
    # Property 3: sandbox refuses on non-git repo (precondition).
    # ------------------------------------------------------------------

    def test_non_git_repo_raises_sandbox_error(self):
        tmp = Path(tempfile.mkdtemp(prefix="cp1-nongit-"))
        try:
            with self.assertRaises(SandboxError):
                run_worker_in_sandbox(
                    main_repo=tmp,
                    prompt="x",
                    branch_name="cp1/nongit",
                    worker_command=["true"],
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    # Property 4: stdout/stderr captured and bounded.
    # ------------------------------------------------------------------

    def test_stdout_and_stderr_captured(self):
        worker = _write_fake_worker(self.scripts, "noisy.py", """
            import sys
            print("out line")
            print("err line", file=sys.stderr)
            sys.exit(0)
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="noise",
            branch_name="cp1/noisy",
            worker_command=["python3", str(worker)],
        )
        self.assertTrue(result.ok)
        self.assertIn("out line", result.stdout)
        self.assertIn("err line", result.stderr)

    # ------------------------------------------------------------------
    # Property 5: concurrent sandboxes don't collide.
    # ------------------------------------------------------------------

    def test_concurrent_sandboxes_distinct_worktrees(self):
        """Two sandboxes on the same repo, run sequentially with the same
        branch name pattern, should not collide (the timestamp suffix gives
        each one a distinct path)."""
        worker = _write_fake_worker(self.scripts, "tag.py", """
            import pathlib, sys, os
            (pathlib.Path.cwd() / "tag.txt").write_text(f"pid={os.getpid()}\\n")
            sys.exit(0)
        """)
        r1 = run_worker_in_sandbox(
            main_repo=self.repo, prompt="t",
            branch_name="cp1/concurrent", worker_command=["python3", str(worker)],
        )
        r2 = run_worker_in_sandbox(
            main_repo=self.repo, prompt="t",
            branch_name="cp1/concurrent", worker_command=["python3", str(worker)],
        )
        self.assertTrue(r1.ok)
        self.assertTrue(r2.ok)
        self.assertEqual(_git(["rev-parse", "HEAD"], cwd=self.repo).strip(), self.head_before)

    # ------------------------------------------------------------------
    # Property 6: prompt is passed as last positional arg to worker.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Property 7: drift detection — main HEAD mutation by misbehaving worker.
    # ------------------------------------------------------------------

    def test_worker_mutating_main_head_is_detected(self):
        """Worker actively reaches into main_repo and moves HEAD.

        Per CP1's honest framing (cwd-isolation, not process-jail), we
        DETECT but don't prevent. result.ok must be False and the error
        must call out the drift. Main repo will be left in the mutated
        state — that's the documented limitation.
        """
        worker = _write_fake_worker(self.scripts, "evil.py", f"""
            import subprocess, sys
            # Reach into main repo and mutate it.
            subprocess.run(
                ["git", "-C", "{self.repo}", "commit", "--allow-empty", "-m", "evil"],
                env={{"GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@y",
                     "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@y"}},
                check=True,
            )
            sys.exit(0)
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="evil",
            branch_name="cp1/evil",
            worker_command=["python3", str(worker)],
        )
        self.assertFalse(result.ok, "drift must mark ok=False")
        self.assertIsNotNone(result.error)
        self.assertIn("DRIFT DETECTED", result.error)
        self.assertNotEqual(result.main_head_before, result.main_head_after)
        # Main repo IS in fact mutated — this is the documented limitation.
        self.assertNotEqual(_git(["rev-parse", "HEAD"], cwd=self.repo).strip(),
                            self.head_before)

    def test_worker_writing_to_main_workdir_is_detected(self):
        """Worker writes a file into main repo's worktree (not its sandbox).

        Detected via porcelain comparison even when HEAD didn't move.
        """
        worker = _write_fake_worker(self.scripts, "rogue_write.py", f"""
            import pathlib, sys
            # Write directly into main repo (not the sandbox).
            (pathlib.Path("{self.repo}") / "rogue.txt").write_text("rogue\\n")
            sys.exit(0)
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="rogue",
            branch_name="cp1/rogue",
            worker_command=["python3", str(worker)],
        )
        self.assertFalse(result.ok)
        self.assertIn("DRIFT DETECTED", result.error or "")
        self.assertNotEqual(result.main_porcelain_before, result.main_porcelain_after)
        # Cleanup the rogue file.
        (self.repo / "rogue.txt").unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Property 8: launch failure paths return a result (don't propagate).
    # ------------------------------------------------------------------

    def test_nonexistent_worker_executable_returns_result(self):
        """worker_command points at a binary that doesn't exist.

        Per CP1 contract: launch failures return a structured result,
        not raise.
        """
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="x",
            branch_name="cp1/no-exec",
            worker_command=["/nonexistent/path/to/binary"],
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 127)
        self.assertIsNotNone(result.error)
        self.assertIn("worker launch failed", result.error)
        # Main repo invariant still checked.
        self.assertEqual(result.main_head_before, result.main_head_after)

    def test_worker_deletes_own_cwd_returns_result(self):
        """Worker deletes its own sandbox cwd before exit.

        State capture handles the missing directory and returns a
        structured result with an error.
        """
        worker = _write_fake_worker(self.scripts, "rm_cwd.py", """
            import os, shutil, sys
            cwd = os.getcwd()
            os.chdir("/")
            shutil.rmtree(cwd, ignore_errors=True)
            sys.exit(0)
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="rm",
            branch_name="cp1/rm-cwd",
            worker_command=["python3", str(worker)],
        )
        # Worker exit was 0 but sandbox is gone.
        self.assertFalse(result.ok, msg=f"result={result}")
        self.assertIsNotNone(result.error)
        # The worker is a "successful" exit, but the sandbox state could
        # not be captured. ok=False.
        # Main HEAD unchanged.
        self.assertEqual(result.main_head_before, result.main_head_after)

    # ------------------------------------------------------------------
    # Property 8b: dirty-content drift (porcelain text identical, bytes differ).
    # ------------------------------------------------------------------

    def test_dirty_path_content_mutation_is_detected(self):
        """Main starts with `tracked.txt` modified; worker overwrites the
        SAME main file with different bytes. `git status --porcelain` is
        identical before and after, but content-sensitive fingerprint catches
        it.

        This is the exact hole codex flagged in CP1 R2. Must fail with
        DRIFT DETECTED.
        """
        # Make tracked.txt part of the initial commit, then dirty it.
        (self.repo / "tracked.txt").write_text("clean\n")
        _git(["add", "tracked.txt"], cwd=self.repo)
        _git(["commit", "-q", "-m", "add tracked"], cwd=self.repo)
        new_head = _git(["rev-parse", "HEAD"], cwd=self.repo).strip()
        # Dirty it.
        (self.repo / "tracked.txt").write_text("dirty before run\n")
        # Worker overwrites the same main file with different bytes.
        worker = _write_fake_worker(self.scripts, "subtle_mutator.py", f"""
            import pathlib, sys
            (pathlib.Path("{self.repo}") / "tracked.txt").write_text("dirty AFTER run\\n")
            sys.exit(0)
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="subtle",
            branch_name="cp1/subtle",
            worker_command=["python3", str(worker)],
        )
        # Porcelain text might be identical (` M tracked.txt` both ways)
        # but the fingerprint hashes content; drift detected.
        self.assertFalse(result.ok, msg=f"hidden drift not caught; result={result}")
        self.assertIn("DRIFT DETECTED", result.error or "")
        self.assertNotEqual(result.main_state_hash_before, result.main_state_hash_after)
        # No cleanup needed; tearDown rmtrees the temp repo.

    def test_post_state_fingerprint_failure_is_treated_as_drift(self):
        """Worker corrupts main `.git/index` after exit (or before, doesn't
        matter). Post-run state fingerprint raises CalledProcessError;
        CP1 treats that as drift, ok=False, error mentions the failure.
        """
        worker = _write_fake_worker(self.scripts, "corrupt_index.py", f"""
            import pathlib, sys
            idx = pathlib.Path("{self.repo}") / ".git" / "index"
            idx.write_bytes(b"\\x00\\x00\\x00\\x00invalid")
            sys.exit(0)
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="corrupt",
            branch_name="cp1/corrupt-idx",
            worker_command=["python3", str(worker)],
        )
        self.assertFalse(result.ok, msg=f"corrupted index not caught; result={result}")
        self.assertIsNotNone(result.error)
        # Either drift detected via fingerprint failure or porcelain failure.
        self.assertTrue(
            "DRIFT DETECTED" in result.error or "fingerprint failed" in result.error,
            f"unexpected error: {result.error}",
        )
        # Repair: reset index so tearDown can proceed.
        subprocess.run(["git", "reset"], cwd=str(self.repo), check=False,
                       capture_output=True)
        subprocess.run(["git", "read-tree", "HEAD"], cwd=str(self.repo),
                       check=False, capture_output=True)

    # ------------------------------------------------------------------
    # Property 8c: drift in a large dirty file past any byte cap is detected.
    # ------------------------------------------------------------------

    def test_large_dirty_file_content_mutation_is_detected(self):
        """Main has a dirty file > 4MB; worker mutates bytes far past 4MB.

        Codex R3 hole #1: previously the fingerprint capped at 4MB per file
        so a mutation past the cap was invisible. The fix removes the cap;
        this test enforces it stays gone.

        Test uses 1MB + a small tail (not 5MB) to keep the test fast while
        still exercising the "mutate near end of file" path.
        """
        big = self.repo / "big.bin"
        size = 1024 * 1024 + 64  # 1MB + a small tail
        big.write_bytes(b"A" * size)
        _git(["add", "big.bin"], cwd=self.repo)
        _git(["commit", "-q", "-m", "add big"], cwd=self.repo)
        # Dirty the file (overwrite with mostly-As + sentinel near end).
        before = bytearray(b"A" * size)
        before[size - 10] = ord("B")
        big.write_bytes(bytes(before))
        # Worker overwrites the same file with a different byte near end.
        worker = _write_fake_worker(self.scripts, "big_mutator.py", f"""
            import pathlib
            p = pathlib.Path("{big}")
            data = bytearray(p.read_bytes())
            data[len(data) - 5] = ord("C")
            p.write_bytes(bytes(data))
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="big",
            branch_name="cp1/big-mut",
            worker_command=["python3", str(worker)],
        )
        self.assertFalse(result.ok, msg=f"large-file drift not caught; result.error={result.error}")
        self.assertIn("DRIFT DETECTED", result.error or "")
        self.assertNotEqual(result.main_state_hash_before, result.main_state_hash_after)

    # ------------------------------------------------------------------
    # Property 8d: symlink target drift is detected.
    # ------------------------------------------------------------------

    def test_symlink_target_mutation_is_detected(self):
        """Main has a tracked symlink, dirty before run; worker changes
        the symlink target.

        Codex R3 hole #2: previously symlinks weren't hashed at all; the
        fix uses lstat + os.readlink. Enforce the fingerprint catches
        target swaps.
        """
        link = self.repo / "link"
        target0 = self.repo / "target0"
        target0.write_text("t0\n")
        os.symlink("target0", link)
        _git(["add", "target0", "link"], cwd=self.repo)
        _git(["commit", "-q", "-m", "add link"], cwd=self.repo)
        # Dirty the link to point at target1 (target1 doesn't have to exist).
        link.unlink()
        os.symlink("target1", link)
        # Worker re-points it at target2.
        worker = _write_fake_worker(self.scripts, "symlink_swap.py", f"""
            import os, pathlib
            p = pathlib.Path("{link}")
            p.unlink()
            os.symlink("target2", "{link}")
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="sym",
            branch_name="cp1/sym-swap",
            worker_command=["python3", str(worker)],
        )
        self.assertFalse(result.ok, msg=f"symlink drift not caught; result.error={result.error}")
        self.assertIn("DRIFT DETECTED", result.error or "")
        self.assertNotEqual(result.main_state_hash_before, result.main_state_hash_after)

    # ------------------------------------------------------------------
    # Property 8e: dirty submodule checkout drift is detected.
    # ------------------------------------------------------------------

    def test_submodule_head_swap_is_detected(self):
        """Main repo has a submodule that's already dirty (porcelain " M sub")
        because its HEAD is at commit B but the gitlink expects C. Worker
        swaps the submodule's HEAD to A. Superproject porcelain stays
        identical between before and after, but the submodule's HEAD moved.

        Codex R4 hole: previously the fingerprint recorded only "DIR" for
        submodule paths, missing the inner HEAD swap. The fix recurses
        into submodules. Enforce the fingerprint catches it.
        """
        # Create an "upstream" submodule repo with three commits A, B, C.
        upstream = Path(tempfile.mkdtemp(prefix="cp1-sub-up-"))
        try:
            _git(["init", "-q", "-b", "main"], cwd=upstream)
            _git(["config", "user.email", "u@x"], cwd=upstream)
            _git(["config", "user.name", "U"], cwd=upstream)
            (upstream / "f").write_text("A\n")
            _git(["add", "."], cwd=upstream)
            _git(["commit", "-q", "-m", "A"], cwd=upstream)
            sha_a = _git(["rev-parse", "HEAD"], cwd=upstream).strip()
            (upstream / "f").write_text("B\n")
            _git(["commit", "-qam", "B"], cwd=upstream)
            sha_b = _git(["rev-parse", "HEAD"], cwd=upstream).strip()
            (upstream / "f").write_text("C\n")
            _git(["commit", "-qam", "C"], cwd=upstream)
            sha_c = _git(["rev-parse", "HEAD"], cwd=upstream).strip()

            # Add submodule to main repo, pointing at C.
            # `-c protocol.file.allow=always` works around the file:// CVE block.
            subprocess.run(
                ["git", "-c", "protocol.file.allow=always", "submodule", "add",
                 "-q", str(upstream), "sub"],
                cwd=str(self.repo), check=True, capture_output=True,
            )
            _git(["commit", "-q", "-m", "add sub"], cwd=self.repo)
            self.head_before = _git(["rev-parse", "HEAD"], cwd=self.repo).strip()

            # Dirty the submodule: check it out at B (gitlink still points at C).
            sub_path = self.repo / "sub"
            subprocess.run(["git", "checkout", "-q", sha_b],
                           cwd=str(sub_path), check=True, capture_output=True)
            # Confirm superproject sees " M sub".
            porc = subprocess.run(["git", "status", "--porcelain"],
                                  cwd=str(self.repo), capture_output=True, text=True).stdout
            self.assertIn("sub", porc)

            # Worker swaps the submodule to A (porcelain stays " M sub").
            worker = _write_fake_worker(self.scripts, "sub_swap.py", f"""
                import subprocess
                subprocess.run(["git", "checkout", "-q", "{sha_a}"],
                               cwd="{sub_path}", check=True)
            """)
            result = run_worker_in_sandbox(
                main_repo=self.repo,
                prompt="sub",
                branch_name="cp1/sub-swap",
                worker_command=["python3", str(worker)],
            )
            self.assertFalse(result.ok, msg=f"submodule drift not caught; result.error={result.error}")
            self.assertIn("DRIFT DETECTED", result.error or "")
            self.assertNotEqual(result.main_state_hash_before, result.main_state_hash_after)
            # Superproject porcelain is identical (this is the trick); fingerprint hashes differ.
            self.assertEqual(result.main_porcelain_before, result.main_porcelain_after)
        finally:
            shutil.rmtree(upstream, ignore_errors=True)

    # ------------------------------------------------------------------
    # Property 8f: submodule whose .git pointer is removed mid-run is detected.
    # ------------------------------------------------------------------

    def test_submodule_gitdir_removal_with_content_mutation_is_detected(self):
        """Codex R5 repro shape: clean superproject with a tracked submodule.
        Worker deletes `sub/.git` AND mutates `sub/f`. Superproject porcelain
        is clean before and after (nothing to report); the prior fingerprint
        never visited the path. The fix walks tracked gitlinks from
        `git ls-files --stage` independently of porcelain.
        """
        # Build upstream submodule repo.
        upstream = Path(tempfile.mkdtemp(prefix="cp1-sub-up2-"))
        try:
            _git(["init", "-q", "-b", "main"], cwd=upstream)
            _git(["config", "user.email", "u@x"], cwd=upstream)
            _git(["config", "user.name", "U"], cwd=upstream)
            (upstream / "f").write_text("seed\n")
            _git(["add", "."], cwd=upstream)
            _git(["commit", "-q", "-m", "seed"], cwd=upstream)

            # Add as submodule and commit cleanly.
            subprocess.run(
                ["git", "-c", "protocol.file.allow=always", "submodule", "add",
                 "-q", str(upstream), "sub"],
                cwd=str(self.repo), check=True, capture_output=True,
            )
            _git(["commit", "-q", "-m", "add sub"], cwd=self.repo)

            # Confirm clean state.
            porc = subprocess.run(["git", "status", "--porcelain"],
                                  cwd=str(self.repo), capture_output=True, text=True).stdout
            self.assertEqual(porc.strip(), "", "superproject must be clean before run")

            # Worker deletes sub/.git AND rewrites sub/f.
            sub_path = self.repo / "sub"
            worker = _write_fake_worker(self.scripts, "gitdir_remove.py", f"""
                import pathlib, shutil
                sub = pathlib.Path("{sub_path}")
                gitfile = sub / ".git"
                if gitfile.is_file():
                    gitfile.unlink()
                else:
                    shutil.rmtree(gitfile)
                (sub / "f").write_text("changed-after-gitdir-delete\\n")
            """)
            result = run_worker_in_sandbox(
                main_repo=self.repo,
                prompt="gitdir-remove",
                branch_name="cp1/gitdir-rm",
                worker_command=["python3", str(worker)],
            )
            self.assertFalse(result.ok, msg=f"submodule .git removal not caught; result.error={result.error}")
            self.assertIn("DRIFT DETECTED", result.error or "")
            self.assertNotEqual(result.main_state_hash_before, result.main_state_hash_after)
        finally:
            shutil.rmtree(upstream, ignore_errors=True)

    # ------------------------------------------------------------------
    # Property 8g: index-only drift (worktree unchanged, staged blob differs).
    # ------------------------------------------------------------------

    def test_index_only_drift_is_detected(self):
        """Codex R6 repro: file has MM porcelain (staged AND unstaged
        changes). Worker replaces ONLY the staged blob via update-index
        --cacheinfo. Worktree bytes unchanged. Porcelain text identical.
        But the index fingerprint differs.
        """
        f = self.repo / "f"
        f.write_text("A\n")
        _git(["add", "f"], cwd=self.repo)
        _git(["commit", "-q", "-m", "seed f"], cwd=self.repo)
        # Stage B
        f.write_text("B staged before\n")
        _git(["add", "f"], cwd=self.repo)
        # Worktree C (unstaged)
        f.write_text("C worktree before and after\n")
        # Confirm MM state.
        porc = subprocess.run(["git", "status", "--porcelain"],
                              cwd=str(self.repo), capture_output=True, text=True).stdout
        self.assertIn("MM f", porc)

        # Worker replaces ONLY the staged blob.
        worker = _write_fake_worker(self.scripts, "stage_swap.py", f"""
            import subprocess
            blob = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd="{self.repo}", input=b"D staged after\\n",
                capture_output=True, check=True,
            ).stdout.decode().strip()
            subprocess.run(
                ["git", "update-index", "--cacheinfo", "100644", blob, "f"],
                cwd="{self.repo}", check=True,
            )
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="index",
            branch_name="cp1/index-only",
            worker_command=["python3", str(worker)],
        )
        self.assertFalse(result.ok, msg=f"index-only drift not caught; result={result}")
        self.assertIn("DRIFT DETECTED", result.error or "")
        self.assertNotEqual(result.main_state_hash_before, result.main_state_hash_after)
        # Porcelain text is identical — proving the index hash caught it, not porcelain.
        self.assertEqual(result.main_porcelain_before, result.main_porcelain_after)
        # Worktree bytes still "C worktree before and after\n".
        self.assertEqual(f.read_text(), "C worktree before and after\n")

    # ------------------------------------------------------------------
    # Property 8h: over-cap dir walk fails closed, not silently truncates.
    # ------------------------------------------------------------------

    def test_over_cap_dir_walk_fails_closed(self):
        """Codex R6 NEEDS-ATTENTION: if _hash_dir_walk hits its cap, the
        fallback fingerprint must NOT silently emit a stable sentinel that
        compares equal across drift past the cap. Fix: raise → caller treats
        as unmeasurable → drift signal.

        Test: monkey-patch the cap to 1, create a submodule with broken .git
        (forcing the fallback walk), 2 files. Worker mutates the second file.
        Walking past entry 1 → raises → drift detected.
        """
        import worker_sandbox as ws
        # Build a submodule with broken .git so the dir-walk fallback fires.
        upstream = Path(tempfile.mkdtemp(prefix="cp1-cap-up-"))
        try:
            _git(["init", "-q", "-b", "main"], cwd=upstream)
            _git(["config", "user.email", "u@x"], cwd=upstream)
            _git(["config", "user.name", "U"], cwd=upstream)
            (upstream / "a.txt").write_text("a\n")
            (upstream / "b.txt").write_text("b\n")
            _git(["add", "."], cwd=upstream)
            _git(["commit", "-q", "-m", "seed"], cwd=upstream)

            subprocess.run(
                ["git", "-c", "protocol.file.allow=always", "submodule", "add",
                 "-q", str(upstream), "sub"],
                cwd=str(self.repo), check=True, capture_output=True,
            )
            _git(["commit", "-q", "-m", "add sub"], cwd=self.repo)
            sub_path = self.repo / "sub"
            # Break the submodule .git pointer so fallback walk fires.
            gitfile = sub_path / ".git"
            if gitfile.is_file():
                gitfile.unlink()
            else:
                shutil.rmtree(gitfile)

            # Monkey-patch the cap to 1 so the walk fails closed.
            original_cap = ws._DIR_WALK_FILE_CAP
            ws._DIR_WALK_FILE_CAP = 1
            try:
                worker = _write_fake_worker(self.scripts, "mutate_b.py", f"""
                    import pathlib
                    p = pathlib.Path("{sub_path}") / "b.txt"
                    p.write_text("b-after\\n")
                """)
                # Over-cap on the PRE-run fingerprint → SandboxError (correct:
                # we cannot establish a baseline). If the over-cap happened
                # post-run instead, result.ok=False with drift in error.
                # Either path is fail-closed; this scenario hits the pre-run path.
                with self.assertRaises(SandboxError) as cm:
                    run_worker_in_sandbox(
                        main_repo=self.repo,
                        prompt="cap",
                        branch_name="cp1/over-cap",
                        worker_command=["python3", str(worker)],
                    )
                self.assertIn("cap of 1 files", str(cm.exception))
                self.assertIn("unmeasurable", str(cm.exception))
            finally:
                ws._DIR_WALK_FILE_CAP = original_cap
        finally:
            shutil.rmtree(upstream, ignore_errors=True)

    # ------------------------------------------------------------------
    # Property 9: bounded output streams don't OOM the parent.
    # ------------------------------------------------------------------

    def test_oversized_stdout_is_truncated(self):
        """Worker produces > CAPTURE_CAP_BYTES of stdout.

        Parent should cap the stored stdout and set stdout_truncated=True.
        Test uses a small cap to make the test fast.
        """
        from worker_sandbox import CAPTURE_CAP_BYTES
        # Worker prints ~10MB of bytes to stdout (chunked to keep test fast).
        worker = _write_fake_worker(self.scripts, "noisy.py", f"""
            import sys
            chunk = "x" * 4096
            for _ in range({CAPTURE_CAP_BYTES // 4096 + 100}):
                sys.stdout.write(chunk)
            sys.stdout.flush()
            sys.exit(0)
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="noise",
            branch_name="cp1/noisy",
            worker_command=["python3", str(worker)],
        )
        self.assertTrue(result.ok, msg=f"result.error={result.error}")
        self.assertTrue(result.stdout_truncated)
        # Stored stdout is approximately CAP_BYTES + the truncation marker.
        # Allow some slack for the chunk boundary.
        self.assertLessEqual(len(result.stdout), CAPTURE_CAP_BYTES + 200)
        self.assertIn("[truncated at", result.stdout)

    # ------------------------------------------------------------------
    # Property 10: prompt arg passing (was test #6 originally).
    # ------------------------------------------------------------------

    def test_prompt_reaches_worker_as_last_arg(self):
        worker = _write_fake_worker(self.scripts, "echo_prompt.py", """
            import pathlib, sys
            prompt = sys.argv[-1]
            (pathlib.Path.cwd() / "prompt.txt").write_text(prompt + "\\n")
            sys.exit(0)
        """)
        result = run_worker_in_sandbox(
            main_repo=self.repo,
            prompt="HELLO-PROMPT-MARKER",
            branch_name="cp1/echo",
            worker_command=["python3", str(worker)],
        )
        self.assertTrue(result.ok)
        self.assertIn("HELLO-PROMPT-MARKER", result.diff)


if __name__ == "__main__":
    unittest.main()
