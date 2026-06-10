"""Tests for apply_rehearsal.apply_with_rehearsal.

Run from /Users/chadsimon/.claude/bin/:
    python3 -m unittest apply_rehearsal_test
"""
from __future__ import annotations

import errno
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import apply_rehearsal
from apply_rehearsal import apply_with_rehearsal


def _run(cmd, cwd, **kwargs):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=True, **kwargs)


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], cwd=path)
    _run(["git", "config", "user.email", "test@local"], cwd=path)
    _run(["git", "config", "user.name", "Test"], cwd=path)
    _run(["git", "config", "commit.gpgsign", "false"], cwd=path)
    (path / "f").write_text("A\n")
    _run(["git", "add", "f"], cwd=path)
    _run(["git", "commit", "-q", "-m", "init"], cwd=path)
    return _run(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()


def _make_diff_for_f(repo: Path, new_content: str) -> bytes:
    """Generate a unified diff for editing f, by staging in a throwaway worktree."""
    tmp = repo.parent / f"_difftmp-{os.urandom(4).hex()}"
    _run(["git", "worktree", "add", "--detach", str(tmp), "HEAD"], cwd=repo)
    try:
        (tmp / "f").write_text(new_content)
        _run(["git", "add", "f"], cwd=tmp)
        proc = subprocess.run(
            ["git", "diff", "--cached", "--binary"],
            cwd=str(tmp),
            capture_output=True,
            check=True,
        )
        return proc.stdout
    finally:
        _run(["git", "worktree", "remove", "--force", str(tmp)], cwd=repo)


def _make_diff_add_file(repo: Path, relpath: str, content: str) -> bytes:
    tmp = repo.parent / f"_difftmp-{os.urandom(4).hex()}"
    _run(["git", "worktree", "add", "--detach", str(tmp), "HEAD"], cwd=repo)
    try:
        target = tmp / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        _run(["git", "add", relpath], cwd=tmp)
        proc = subprocess.run(
            ["git", "diff", "--cached", "--binary"],
            cwd=str(tmp),
            capture_output=True,
            check=True,
        )
        return proc.stdout
    finally:
        _run(["git", "worktree", "remove", "--force", str(tmp)], cwd=repo)


def _make_diff_delete_file(repo: Path, relpath: str) -> bytes:
    tmp = repo.parent / f"_difftmp-{os.urandom(4).hex()}"
    _run(["git", "worktree", "add", "--detach", str(tmp), "HEAD"], cwd=repo)
    try:
        (tmp / relpath).unlink()
        _run(["git", "add", "-A", relpath], cwd=tmp)
        proc = subprocess.run(
            ["git", "diff", "--cached", "--binary"],
            cwd=str(tmp),
            capture_output=True,
            check=True,
        )
        return proc.stdout
    finally:
        _run(["git", "worktree", "remove", "--force", str(tmp)], cwd=repo)


class ApplyRehearsalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cp2-"))
        self.repo = self.tmp / "main"
        self.base_sha = _init_repo(self.repo)
        self.rehearsal_parent = self.tmp / "rehearsals"

    def tearDown(self) -> None:
        # Clean any straggler worktree registrations.
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(self.repo) if self.repo.exists() else self.tmp,
            capture_output=True,
        )
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- happy path ---------------------------------------------------

    def test_clean_apply_happy_path(self) -> None:
        diff = _make_diff_for_f(self.repo, "B\n")
        result = apply_with_rehearsal(
            main_repo=self.repo,
            diff_bytes=diff,
            base_sha=self.base_sha,
            commit_message="apply: f -> B",
            rehearsal_parent=self.rehearsal_parent,
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.stage, "done")
        self.assertIsNotNone(result.new_head_sha)
        self.assertNotEqual(result.new_head_sha, self.base_sha)
        self.assertEqual((self.repo / "f").read_text(), "B\n")
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, result.new_head_sha)
        # Sibling cleaned up.
        self.assertFalse(Path(result.rehearsal_path).exists())
        self.assertFalse(result.cleanup_failed)

    def test_add_file_diff(self) -> None:
        diff = _make_diff_add_file(self.repo, "new/x.txt", "hello\n")
        result = apply_with_rehearsal(
            main_repo=self.repo,
            diff_bytes=diff,
            base_sha=self.base_sha,
            commit_message="add new/x.txt",
            rehearsal_parent=self.rehearsal_parent,
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual((self.repo / "new" / "x.txt").read_text(), "hello\n")

    def test_delete_file_diff(self) -> None:
        diff = _make_diff_delete_file(self.repo, "f")
        result = apply_with_rehearsal(
            main_repo=self.repo,
            diff_bytes=diff,
            base_sha=self.base_sha,
            commit_message="delete f",
            rehearsal_parent=self.rehearsal_parent,
        )
        self.assertTrue(result.ok, result.error)
        self.assertFalse((self.repo / "f").exists())

    def test_commit_author_identity(self) -> None:
        diff = _make_diff_for_f(self.repo, "B\n")
        result = apply_with_rehearsal(
            main_repo=self.repo,
            diff_bytes=diff,
            base_sha=self.base_sha,
            commit_message="auth test",
            author_name="Outer Loop",
            author_email="loop@example.com",
            rehearsal_parent=self.rehearsal_parent,
        )
        self.assertTrue(result.ok, result.error)
        author = _run(
            ["git", "log", "-1", "--format=%an <%ae>"], cwd=self.repo
        ).stdout.strip()
        self.assertEqual(author, "Outer Loop <loop@example.com>")

    # ---- empty / no-op diffs -----------------------------------------

    def test_empty_diff_rejected(self) -> None:
        result = apply_with_rehearsal(
            main_repo=self.repo,
            diff_bytes=b"",
            base_sha=self.base_sha,
            commit_message="should not happen",
            rehearsal_parent=self.rehearsal_parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "empty")
        # Main untouched.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, self.base_sha)
        self.assertEqual(result.main_state_hash_before, result.main_state_hash_after)

    def test_noop_diff_rejected(self) -> None:
        # Diff that has hunks but produces zero staged change: apply same content.
        # Easiest way: a diff that just has a no-op header. We construct one by
        # diffing identical content from a sibling worktree (will be empty bytes
        # — covered above) so instead inject a hand-crafted whitespace-only diff.
        # Use a context-only diff that touches no lines.
        diff = (
            b"diff --git a/f b/f\n"
            b"--- a/f\n"
            b"+++ b/f\n"
            b"@@ -1 +1 @@\n"
            b"-A\n"
            b"+A\n"
        )
        # git apply will fail this because -A +A on same line produces no change
        # actually it produces an identical line — apply may succeed but stage
        # nothing. We just need apply to succeed with no staged delta; if git
        # rejects, that exercises the "rehearsal" stage instead. Both are valid
        # rejections.
        result = apply_with_rehearsal(
            main_repo=self.repo,
            diff_bytes=diff,
            base_sha=self.base_sha,
            commit_message="noop",
            rehearsal_parent=self.rehearsal_parent,
        )
        self.assertFalse(result.ok)
        self.assertIn(result.stage, {"empty", "rehearsal"})
        # Main untouched.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, self.base_sha)

    # ---- precheck failures -------------------------------------------

    def test_precheck_dirty_tracked(self) -> None:
        (self.repo / "f").write_text("dirty\n")
        diff = b"dummy"
        result = apply_with_rehearsal(
            main_repo=self.repo,
            diff_bytes=diff,
            base_sha=self.base_sha,
            commit_message="x",
            rehearsal_parent=self.rehearsal_parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "precheck")
        self.assertIn("dirty", result.error.lower())
        # Main untouched (still dirty with same content).
        self.assertEqual((self.repo / "f").read_text(), "dirty\n")

    def test_precheck_untracked_file_allowed(self) -> None:
        (self.repo / "stray.log").write_text("log\n")
        diff = _make_diff_for_f(self.repo, "B\n")
        result = apply_with_rehearsal(
            main_repo=self.repo,
            diff_bytes=diff,
            base_sha=self.base_sha,
            commit_message="apply with untracked stray",
            rehearsal_parent=self.rehearsal_parent,
        )
        self.assertTrue(result.ok, result.error)
        # Stray file preserved.
        self.assertEqual((self.repo / "stray.log").read_text(), "log\n")
        self.assertEqual((self.repo / "f").read_text(), "B\n")

    def test_precheck_wrong_base_sha(self) -> None:
        # Advance main one commit so HEAD != base_sha.
        (self.repo / "f").write_text("intermediate\n")
        _run(["git", "add", "f"], cwd=self.repo)
        _run(["git", "commit", "-q", "-m", "intermediate"], cwd=self.repo)
        diff = b"dummy"
        result = apply_with_rehearsal(
            main_repo=self.repo,
            diff_bytes=diff,
            base_sha=self.base_sha,  # stale
            commit_message="x",
            rehearsal_parent=self.rehearsal_parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "precheck")
        self.assertIn("base_sha", result.error)

    # ---- rehearsal failure -------------------------------------------

    def test_rehearsal_apply_failure_leaves_main_untouched(self) -> None:
        # Diff that expects f=A -> B, but we'll bump main first so the diff
        # generated below uses the new base_sha. Then we hand the rehearsal an
        # incompatible diff (one that expects different starting content).
        bad_diff = (
            b"diff --git a/f b/f\n"
            b"--- a/f\n"
            b"+++ b/f\n"
            b"@@ -1 +1 @@\n"
            b"-NOT_IN_FILE\n"
            b"+B\n"
        )
        result = apply_with_rehearsal(
            main_repo=self.repo,
            diff_bytes=bad_diff,
            base_sha=self.base_sha,
            commit_message="should fail",
            rehearsal_parent=self.rehearsal_parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "rehearsal")
        # Main untouched.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, self.base_sha)
        self.assertEqual((self.repo / "f").read_text(), "A\n")
        # Sibling cleaned up.
        self.assertFalse(Path(result.rehearsal_path).exists())
        # Fingerprints match.
        self.assertEqual(result.main_state_hash_before, result.main_state_hash_after)

    # ---- postcheck drift via monkeypatch -----------------------------

    def test_postcheck_detects_drift_between_rehearsal_and_merge(self) -> None:
        """Simulate concurrent modification of main during rehearsal.

        We monkey-patch _main_repo_state so the FIRST call (precheck) returns
        the true state, and the SECOND call (postcheck) returns a different
        fingerprint. apply_with_rehearsal must abort at stage="postcheck" and
        leave main untouched.
        """
        diff = _make_diff_for_f(self.repo, "B\n")
        true_state = apply_rehearsal._main_repo_state(self.repo)

        call_log = {"n": 0}
        real_fn = apply_rehearsal._main_repo_state

        def fake_state(repo):
            call_log["n"] += 1
            if call_log["n"] == 1:
                return true_state
            if call_log["n"] == 2:
                # Pretend main drifted: same HEAD, different hash.
                return (true_state[0], "FAKE_DRIFT_HASH", true_state[2])
            return real_fn(repo)

        with mock.patch.object(apply_rehearsal, "_main_repo_state", side_effect=fake_state):
            result = apply_with_rehearsal(
                main_repo=self.repo,
                diff_bytes=diff,
                base_sha=self.base_sha,
                commit_message="should abort at postcheck",
                rehearsal_parent=self.rehearsal_parent,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "postcheck")
        self.assertIn("drift", result.error.lower())
        # Main genuinely untouched.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, self.base_sha)
        self.assertEqual((self.repo / "f").read_text(), "A\n")
        # Sibling cleaned up.
        self.assertFalse(Path(result.rehearsal_path).exists())

    # ---- ff-merge refusal via concurrent advance ---------------------

    def test_ff_merge_refused_when_main_advances_pre_merge(self) -> None:
        """Force the ff-only merge to fail by advancing main past the rehearsal
        commit between postcheck and the merge call.

        We monkey-patch subprocess.run so that the `git merge --ff-only` call
        is preceded by an out-of-band commit on main. ff-only must refuse.
        """
        diff = _make_diff_for_f(self.repo, "B\n")
        real_run = subprocess.run
        repo = str(self.repo)

        def racing_run(cmd, *args, **kwargs):
            # Right before the ff-only merge, advance main one commit.
            if (
                isinstance(cmd, list)
                and "merge" in cmd
                and "--ff-only" in cmd
                and kwargs.get("cwd") == repo
            ):
                race_file = self.repo / "race.txt"
                race_file.write_text("race\n")
                real_run(["git", "add", "race.txt"], cwd=repo, capture_output=True, check=True)
                real_run(
                    ["git", "commit", "-q", "-m", "race", "--no-gpg-sign"],
                    cwd=repo,
                    capture_output=True,
                    check=True,
                )
            return real_run(cmd, *args, **kwargs)

        with mock.patch.object(apply_rehearsal.subprocess, "run", side_effect=racing_run):
            result = apply_with_rehearsal(
                main_repo=self.repo,
                diff_bytes=diff,
                base_sha=self.base_sha,
                commit_message="should be refused",
                rehearsal_parent=self.rehearsal_parent,
            )

        # The post-rehearsal fingerprint and the actual ff merge happen in
        # back-to-back subprocess calls. The race advance happens immediately
        # before the merge call. Postcheck already passed (read before race),
        # so we expect stage="main_merge".
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "main_merge")
        # Main reflects the race commit, not the rehearsal commit.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertNotEqual(head, result.new_head_sha)
        self.assertEqual((self.repo / "f").read_text(), "A\n")

    # ---- non-git target ----------------------------------------------

    def test_non_git_main_fails_precheck(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "f").write_text("A\n")
        result = apply_with_rehearsal(
            main_repo=plain,
            diff_bytes=b"dummy",
            base_sha="0" * 40,
            commit_message="x",
            rehearsal_parent=self.rehearsal_parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "precheck")

    # ---- main-untouched invariant on every failure path --------------

    def test_main_untouched_invariant_on_failure(self) -> None:
        """For every failure stage, main HEAD + working tree must match
        precheck state. We check this across a battery of failure-inducing
        inputs."""
        scenarios = []

        # Empty diff
        scenarios.append(("empty", b""))

        # Bad diff (rehearsal fails)
        scenarios.append(
            (
                "rehearsal",
                b"diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-NOPE\n+X\n",
            )
        )

        for name, diff in scenarios:
            with self.subTest(scenario=name):
                head_before = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
                content_before = (self.repo / "f").read_text()
                result = apply_with_rehearsal(
                    main_repo=self.repo,
                    diff_bytes=diff,
                    base_sha=self.base_sha,
                    commit_message="x",
                    rehearsal_parent=self.rehearsal_parent,
                )
                self.assertFalse(result.ok)
                head_after = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
                content_after = (self.repo / "f").read_text()
                self.assertEqual(head_before, head_after)
                self.assertEqual(content_before, content_after)

    # ---- cleanup on exception ----------------------------------------

    def test_cleanup_runs_even_on_unexpected_exception(self) -> None:
        """If something raises mid-flow, the sibling worktree is still removed."""
        diff = _make_diff_for_f(self.repo, "B\n")
        real_run = subprocess.run
        sibling_seen = {"path": None}

        def crashing_run(cmd, *args, **kwargs):
            # Capture sibling path from worktree-add.
            if (
                isinstance(cmd, list)
                and "worktree" in cmd
                and "add" in cmd
            ):
                sibling_seen["path"] = cmd[-2]  # --detach <path> <base>
            # Crash on the apply call so the finally cleanup must run.
            if isinstance(cmd, list) and "apply" in cmd and "--index" in cmd:
                raise RuntimeError("injected failure during apply")
            return real_run(cmd, *args, **kwargs)

        with mock.patch.object(apply_rehearsal.subprocess, "run", side_effect=crashing_run):
            with self.assertRaises(RuntimeError):
                apply_with_rehearsal(
                    main_repo=self.repo,
                    diff_bytes=diff,
                    base_sha=self.base_sha,
                    commit_message="should crash",
                    rehearsal_parent=self.rehearsal_parent,
                )

        self.assertIsNotNone(sibling_seen["path"])
        self.assertFalse(Path(sibling_seen["path"]).exists())
        # Main untouched.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, self.base_sha)
        self.assertEqual((self.repo / "f").read_text(), "A\n")


    # ---- R1 fixes: hooks disabled, post-merge verification ------------

    def test_post_merge_hook_cannot_dirty_main(self) -> None:
        """A malicious post-merge hook in main must not dirty main.

        With hooks disabled via core.hooksPath, the hook does not run and
        main lands clean. ok=True.
        """
        hooks_dir = self.repo / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / "post-merge"
        hook.write_text(
            "#!/bin/sh\n"
            "echo HOOK_RAN > hook-marker.txt\n"
            "echo dirty >> f\n"
        )
        hook.chmod(0o755)

        diff = _make_diff_add_file(self.repo, "g", "g\n")
        result = apply_with_rehearsal(
            main_repo=self.repo,
            diff_bytes=diff,
            base_sha=self.base_sha,
            commit_message="hook test",
            rehearsal_parent=self.rehearsal_parent,
        )
        self.assertTrue(result.ok, f"{result.stage}: {result.error}")
        self.assertEqual(result.stage, "done")
        # Hook did not run (hooks disabled).
        self.assertFalse((self.repo / "hook-marker.txt").exists())
        # Main is clean.
        porc = _run(["git", "status", "--porcelain"], cwd=self.repo).stdout
        self.assertEqual(porc.strip(), "")

    def test_post_merge_verification_catches_race_dirty(self) -> None:
        """If something dirties a tracked file between merge and verify,
        post_merge_verify must catch it and report applied_to_main=True."""
        diff = _make_diff_for_f(self.repo, "B\n")

        # Patch _porcelain_z so the post-merge call returns a synthetic
        # porcelain with a dirty tracked entry. Precheck call (1) and
        # post-rehearsal call (2) return real state; post-merge call (3)
        # injects dirt.
        real_pz = apply_rehearsal._porcelain_z
        call_count = {"n": 0}

        def fake_pz(repo_path):
            call_count["n"] += 1
            if call_count["n"] == 3:
                return ([b" M unrelated.txt"], set())
            return real_pz(repo_path)

        with mock.patch.object(apply_rehearsal, "_porcelain_z", side_effect=fake_pz):
            result = apply_with_rehearsal(
                main_repo=self.repo,
                diff_bytes=diff,
                base_sha=self.base_sha,
                commit_message="race test",
                rehearsal_parent=self.rehearsal_parent,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "post_merge_verify")
        self.assertTrue(result.applied_to_main)
        self.assertIn("dirty tracked", result.error)

    def test_post_merge_new_untracked_detected(self) -> None:
        """A new untracked file appearing post-merge is flagged."""
        diff = _make_diff_for_f(self.repo, "B\n")
        real_pz = apply_rehearsal._porcelain_z
        call_count = {"n": 0}

        def fake_pz(repo_path):
            call_count["n"] += 1
            if call_count["n"] == 3:
                return ([], {b"new-stray.txt"})
            return real_pz(repo_path)

        with mock.patch.object(apply_rehearsal, "_porcelain_z", side_effect=fake_pz):
            result = apply_with_rehearsal(
                main_repo=self.repo,
                diff_bytes=diff,
                base_sha=self.base_sha,
                commit_message="untracked race",
                rehearsal_parent=self.rehearsal_parent,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "post_merge_verify")
        self.assertTrue(result.applied_to_main)
        self.assertIn("new untracked", result.error)

    def test_cleanup_failure_forces_ok_false_after_merge(self) -> None:
        """If sibling cleanup fails after a successful merge, ok must be
        False and stage='cleanup', with applied_to_main=True."""
        diff = _make_diff_for_f(self.repo, "B\n")
        real_run = subprocess.run
        repo = str(self.repo)

        def cleanup_breaking_run(cmd, *args, **kwargs):
            # Fail worktree remove. Run real prune so dangling refs go away
            # but the sibling dir remains and rmtree will also fail because
            # we'll lock it.
            if (
                isinstance(cmd, list)
                and "worktree" in cmd
                and "remove" in cmd
            ):
                # Return a fake CompletedProcess with rc=1.
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="injected failure"
                )
            return real_run(cmd, *args, **kwargs)

        # Also patch shutil.rmtree to fail so the sibling actually leaks.
        def broken_rmtree(*args, **kwargs):
            raise OSError(errno.EACCES, "injected rmtree failure")

        with mock.patch.object(apply_rehearsal.subprocess, "run", side_effect=cleanup_breaking_run), \
             mock.patch.object(apply_rehearsal.shutil, "rmtree", side_effect=broken_rmtree):
            result = apply_with_rehearsal(
                main_repo=self.repo,
                diff_bytes=diff,
                base_sha=self.base_sha,
                commit_message="cleanup race",
                rehearsal_parent=self.rehearsal_parent,
            )

        # Merge landed — applied_to_main True.
        self.assertTrue(result.applied_to_main)
        # But ok=False because cleanup failed.
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "cleanup")
        self.assertTrue(result.cleanup_failed)
        self.assertIn("injected", result.cleanup_output)
        # Main is at the new SHA.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, result.new_head_sha)
        # Cleanup the leaked sibling so tearDown doesn't choke.
        subprocess.run(
            ["git", "worktree", "remove", "--force", result.rehearsal_path],
            cwd=str(self.repo), capture_output=True,
        )
        if Path(result.rehearsal_path).exists():
            shutil.rmtree(result.rehearsal_path, ignore_errors=True)

    def test_lock_contention_returns_lock_stage(self) -> None:
        """Concurrent apply_with_rehearsal against the same main repo
        returns stage='lock' for the second caller."""
        import fcntl
        common = self.repo / ".git"
        lock_path = common / "apply-rehearsal.lock"
        # Hold the lock externally.
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            diff = _make_diff_for_f(self.repo, "B\n")
            result = apply_with_rehearsal(
                main_repo=self.repo,
                diff_bytes=diff,
                base_sha=self.base_sha,
                commit_message="should be locked out",
                rehearsal_parent=self.rehearsal_parent,
            )
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "lock")
        # Main untouched.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, self.base_sha)

    # ---- R2 fixes: post-checkout hook, untracked content drift -------

    def test_post_checkout_hook_in_sibling_does_not_contaminate_commit(self) -> None:
        """A post-checkout hook installed in the shared .git/hooks dir
        fires when `git worktree add` checks out the sibling. If it stages
        a tracked sibling change, that change would otherwise ride along
        the sibling commit and ff-merge into main.

        With hooks disabled via core.hooksPath, the hook does not run.
        The merged commit contains only the diff's changes.
        """
        # Track a second file 'h' so the hook has something tracked to bash.
        (self.repo / "h").write_text("h-initial\n")
        _run(["git", "add", "h"], cwd=self.repo)
        _run(["git", "commit", "-q", "-m", "add h"], cwd=self.repo)
        new_base = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()

        # Generate the diff BEFORE installing the hook, otherwise our
        # diff-generation helper's worktree add would fire the hook and
        # contaminate the fixture.
        diff = _make_diff_for_f(self.repo, "B\n")

        hooks_dir = self.repo / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / "post-checkout"
        hook.write_text(
            "#!/bin/sh\n"
            "echo HOOK_RAN > hook-checkout-marker.txt\n"
            "echo hooked > h\n"
            "git add h\n"
        )
        hook.chmod(0o755)

        result = apply_with_rehearsal(
            main_repo=self.repo,
            diff_bytes=diff,
            base_sha=new_base,
            commit_message="post-checkout hook test",
            rehearsal_parent=self.rehearsal_parent,
        )
        self.assertTrue(result.ok, f"{result.stage}: {result.error}")
        self.assertEqual(result.stage, "done")
        # Hook did not run (no marker file).
        self.assertFalse((self.repo / "hook-checkout-marker.txt").exists())
        # h is unchanged from base.
        self.assertEqual((self.repo / "h").read_text(), "h-initial\n")
        # The commit contains only f, not h.
        name_status = _run(
            ["git", "show", "--name-only", "--format=", "HEAD"], cwd=self.repo
        ).stdout.strip().splitlines()
        self.assertEqual(name_status, ["f"])

    def test_existing_untracked_content_drift_caught(self) -> None:
        """An existing untracked file whose CONTENT changes between
        postcheck and post-merge verify must be detected.

        Path-set check alone misses content-only drift, so this exercises
        the untracked-content fingerprint added in R2.
        """
        stray = self.repo / "stray.log"
        stray.write_text("original\n")

        diff = _make_diff_for_f(self.repo, "B\n")
        real_state = apply_rehearsal._main_repo_state
        real_fp = apply_rehearsal._fingerprint_untracked
        call_count = {"state": 0}

        # The verifier reads fingerprints from disk POST-merge. Inject a
        # rewrite of stray.log right between the merge and the
        # post-merge fingerprint call. Easiest hook: patch
        # _fingerprint_untracked so the SECOND invocation (post-merge)
        # sees the modified content. Modify the file on disk before
        # calling the real fingerprint helper.
        fp_call = {"n": 0}

        def fake_fp(repo, paths):
            fp_call["n"] += 1
            if fp_call["n"] == 2:
                # Simulate race: rewrite stray.log just before reading.
                # paths is Set[bytes] now.
                if b"stray.log" in paths:
                    (repo / "stray.log").write_text("RACED\n")
            return real_fp(repo, paths)

        with mock.patch.object(apply_rehearsal, "_fingerprint_untracked", side_effect=fake_fp):
            result = apply_with_rehearsal(
                main_repo=self.repo,
                diff_bytes=diff,
                base_sha=self.base_sha,
                commit_message="untracked content drift",
                rehearsal_parent=self.rehearsal_parent,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "post_merge_verify")
        self.assertTrue(result.applied_to_main)
        self.assertIn("untracked content drift", result.error)

    def test_existing_untracked_deletion_caught(self) -> None:
        """An existing untracked file deleted between postcheck and
        post-merge verify must be detected."""
        stray = self.repo / "stray.log"
        stray.write_text("important\n")

        diff = _make_diff_for_f(self.repo, "B\n")
        real_state = apply_rehearsal._main_repo_state
        state_call = {"n": 0}

        def fake_state(repo_path):
            state_call["n"] += 1
            # Calls: 1=precheck, 2=postcheck, 3=post-merge verify.
            # Between 2 and 3, simulate a race that deletes stray.log.
            # We do the deletion on call 3's entry.
            if state_call["n"] == 3:
                try:
                    (repo_path / "stray.log").unlink()
                except OSError:
                    pass
            return real_state(repo_path)

        with mock.patch.object(apply_rehearsal, "_main_repo_state", side_effect=fake_state):
            result = apply_with_rehearsal(
                main_repo=self.repo,
                diff_bytes=diff,
                base_sha=self.base_sha,
                commit_message="untracked deletion",
                rehearsal_parent=self.rehearsal_parent,
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "post_merge_verify")
        self.assertTrue(result.applied_to_main)
        self.assertIn("deleted", result.error)

    def test_lock_setup_failure_returns_lock_setup_stage(self) -> None:
        """If we cannot create/open the lock file, fail closed with
        stage='lock_setup' rather than silently running unlocked."""
        # Patch os.open to raise on the lock path.
        real_open = os.open
        lock_target = "apply-rehearsal.lock"

        def failing_open(path, *args, **kwargs):
            if isinstance(path, (str, bytes)) and lock_target in str(path):
                raise OSError(errno.EACCES, "injected lock denial")
            return real_open(path, *args, **kwargs)

        diff = _make_diff_for_f(self.repo, "B\n")
        with mock.patch.object(apply_rehearsal.os, "open", side_effect=failing_open):
            result = apply_with_rehearsal(
                main_repo=self.repo,
                diff_bytes=diff,
                base_sha=self.base_sha,
                commit_message="x",
                rehearsal_parent=self.rehearsal_parent,
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "lock_setup")
        # Main untouched.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, self.base_sha)

    # ---- R3 fixes: private hooks dir, -z porcelain parsing -----------

    def test_pre_seeded_hooks_dir_does_not_contaminate(self) -> None:
        """An attacker that pre-seeds a shared hook directory must not
        be able to contaminate the merged commit. We use a per-call
        private mkdtemp dir; pre-seeding a stale shared path has no
        effect because we never use it.

        Sanity: even creating a directory at the OLD shared location
        with hostile content does nothing now.
        """
        # Plant a hostile post-checkout at the OLD shared location, just
        # to prove we don't read from there anymore.
        stale_shared = Path(tempfile.gettempdir()) / "apply-rehearsal-empty-hooks"
        stale_shared.mkdir(parents=True, exist_ok=True)
        hook = stale_shared / "post-checkout"
        hook.write_text(
            "#!/bin/sh\n"
            "echo hooked > h\n"
            "git add h\n"
        )
        hook.chmod(0o755)
        self.addCleanup(lambda: shutil.rmtree(stale_shared, ignore_errors=True))

        # Track h so the hook has something to target.
        (self.repo / "h").write_text("h-initial\n")
        _run(["git", "add", "h"], cwd=self.repo)
        _run(["git", "commit", "-q", "-m", "add h"], cwd=self.repo)
        new_base = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()

        diff = _make_diff_for_f(self.repo, "B\n")
        result = apply_with_rehearsal(
            main_repo=self.repo,
            diff_bytes=diff,
            base_sha=new_base,
            commit_message="stale shared hooks dir",
            rehearsal_parent=self.rehearsal_parent,
        )
        self.assertTrue(result.ok, f"{result.stage}: {result.error}")
        # h unchanged.
        self.assertEqual((self.repo / "h").read_text(), "h-initial\n")
        # Merged commit only touched f.
        names = _run(
            ["git", "show", "--name-only", "--format=", "HEAD"], cwd=self.repo
        ).stdout.strip().splitlines()
        self.assertEqual(names, ["f"])

    def test_private_hooks_dir_cleaned_up_after_run(self) -> None:
        """The per-call private hooks dir is removed after the call."""
        seen_dirs = []
        real_mkdir = apply_rehearsal._make_private_hooks_dir

        def tracking_mkdir():
            d = real_mkdir()
            seen_dirs.append(d)
            return d

        diff = _make_diff_for_f(self.repo, "B\n")
        with mock.patch.object(apply_rehearsal, "_make_private_hooks_dir", side_effect=tracking_mkdir):
            result = apply_with_rehearsal(
                main_repo=self.repo,
                diff_bytes=diff,
                base_sha=self.base_sha,
                commit_message="cleanup hooks dir",
                rehearsal_parent=self.rehearsal_parent,
            )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(seen_dirs), 1)
        self.assertFalse(seen_dirs[0].exists())

    def test_newline_in_untracked_path_handled_safely(self) -> None:
        """An untracked path containing a newline must not be split into
        ghost entries by the porcelain parser. The pre-existing untracked
        file is preserved across a clean apply."""
        weird = self.repo / "weird\nname.log"
        try:
            weird.write_text("preserved\n")
        except OSError:
            self.skipTest("filesystem rejects newline in filename")

        diff = _make_diff_for_f(self.repo, "B\n")
        result = apply_with_rehearsal(
            main_repo=self.repo,
            diff_bytes=diff,
            base_sha=self.base_sha,
            commit_message="newline path",
            rehearsal_parent=self.rehearsal_parent,
        )
        self.assertTrue(result.ok, f"{result.stage}: {result.error}")
        # Newline-containing untracked file still present.
        self.assertEqual(weird.read_text(), "preserved\n")

    def test_porcelain_z_raises_on_status_failure(self) -> None:
        """_porcelain_z fails closed: status failure raises SandboxError
        instead of synthesizing a clean ([],set()) result.

        This catches the R4 fail-open hole — a broken `git status`
        invocation must not be mistaken for clean main state.
        """
        from apply_rehearsal import SandboxError
        # Point at a non-git directory.
        plain = self.tmp / "not-a-repo"
        plain.mkdir()
        with self.assertRaises(SandboxError):
            apply_rehearsal._porcelain_z(plain)

    def test_porcelain_z_failure_at_precheck_is_fail_closed(self) -> None:
        """If _porcelain_z raises during precheck, the call returns
        stage='precheck' with main untouched, NOT ok=True."""
        from apply_rehearsal import SandboxError
        diff = _make_diff_for_f(self.repo, "B\n")

        def boom(repo):
            raise SandboxError("simulated status failure")

        with mock.patch.object(apply_rehearsal, "_porcelain_z", side_effect=boom):
            result = apply_with_rehearsal(
                main_repo=self.repo,
                diff_bytes=diff,
                base_sha=self.base_sha,
                commit_message="should fail closed",
                rehearsal_parent=self.rehearsal_parent,
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "precheck")
        self.assertFalse(result.applied_to_main)
        # Main untouched.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, self.base_sha)

    def test_porcelain_z_failure_post_merge_marks_verify(self) -> None:
        """If _porcelain_z raises during the post-merge verification
        call, the result is stage='post_merge_verify' with
        applied_to_main=True (merge already landed)."""
        from apply_rehearsal import SandboxError
        diff = _make_diff_for_f(self.repo, "B\n")
        real_pz = apply_rehearsal._porcelain_z
        call_count = {"n": 0}

        def maybe_boom(repo):
            call_count["n"] += 1
            # Calls 1=precheck, 2=postcheck/pre-merge untracked, 3=post-merge.
            if call_count["n"] == 3:
                raise SandboxError("simulated post-merge status failure")
            return real_pz(repo)

        with mock.patch.object(apply_rehearsal, "_porcelain_z", side_effect=maybe_boom):
            result = apply_with_rehearsal(
                main_repo=self.repo,
                diff_bytes=diff,
                base_sha=self.base_sha,
                commit_message="status fail post-merge",
                rehearsal_parent=self.rehearsal_parent,
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "post_merge_verify")
        self.assertTrue(result.applied_to_main)

    def test_porcelain_z_parses_newline_path(self) -> None:
        """Direct test of _porcelain_z: a newline-containing untracked
        path is returned as a single bytes entry, not split."""
        weird = self.repo / "newline\nin\nname.log"
        try:
            weird.write_text("x\n")
        except OSError:
            self.skipTest("filesystem rejects newline in filename")
        tracked, untracked = apply_rehearsal._porcelain_z(self.repo)
        self.assertEqual(tracked, [])
        self.assertIn(b"newline\nin\nname.log", untracked)

    def test_rehearsal_setup_failure_returns_structured_result(self) -> None:
        """If the rehearsal parent cannot be created (path is a file),
        we return stage='rehearsal_setup' rather than raising."""
        bad_parent_file = self.tmp / "not-a-dir"
        bad_parent_file.write_text("blocking file\n")
        bad_parent = bad_parent_file / "child"

        diff = _make_diff_for_f(self.repo, "B\n")
        result = apply_with_rehearsal(
            main_repo=self.repo,
            diff_bytes=diff,
            base_sha=self.base_sha,
            commit_message="should fail at setup",
            rehearsal_parent=bad_parent,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "rehearsal_setup")
        self.assertFalse(result.applied_to_main)
        # Main untouched.
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        self.assertEqual(head, self.base_sha)


if __name__ == "__main__":
    unittest.main()
