"""
apply_rehearsal.py — rehearsed-apply pattern for the autonomous outer-loop driver.

Contract
--------
Given a unified diff (bytes), a main repo path, and a base SHA the diff was
generated against, this module:

  1. Acquires an advisory lock in the git common dir so concurrent CP2 calls
     against the same main repo serialize deterministically.
  2. Pre-checks that main is clean (tracked entries only) and at base_sha,
     and records the pre-existing untracked path set.
  3. Creates a disposable sibling worktree at base_sha (shares .git with main).
  4. Applies the diff in the sibling via `git apply --index --binary`.
  5. Commits in the sibling — capturing a new commit SHA that is already
     reachable from main's object DB because the sibling shares it.
  6. Post-checks that main has not drifted.
  7. Fast-forwards main to the new commit via `git merge --ff-only`, with
     repository hooks DISABLED (`-c core.hooksPath=<empty>`). Hooks can
     mutate main during merge and break the "main matches rehearsal" invariant.
  8. Post-merge verifies: HEAD == new_sha, no tracked dirtiness, untracked
     set unchanged from pre-merge.
  9. Cleans up the sibling worktree. Cleanup failure forces ok=False so
     callers must handle leaked worktrees.

Failure paths leave main exactly as it was. We never run destructive git
operations (reset --hard, checkout --) against the main worktree.

Driver-owned git invocations (commit, merge, anything that runs in main's
worktree or could trigger hooks) are run with `-c core.hooksPath=<empty>`
and `--no-gpg-sign`. Read-only or non-mutating calls (status, rev-parse,
worktree add at a SHA) leave default config alone.

Failure stages (returned in `ApplyResult.stage`):

  - "lock_setup"         — advisory lock could not be initialized.
  - "lock"               — advisory lock contended (another caller holds it).
  - "precheck"           — main dirty, wrong HEAD, or fingerprint failed.
  - "rehearsal_setup"    — sibling parent dir / worktree add failed.
  - "empty"              — diff is empty bytes, or stages nothing.
  - "rehearsal"          — diff didn't apply cleanly in the sibling.
  - "rehearsal_commit"   — sibling commit / rev-parse failed.
  - "postcheck"          — main drifted between rehearsal and merge.
  - "main_merge"         — ff-only merge refused (race, or main moved).
  - "post_merge_verify"  — main mutated by merge but not in the expected
                           clean state (post-merge hook, race, untracked
                           content drift, untracked deletion). applied_to_main
                           is True in this case.
  - "cleanup"            — merge succeeded but sibling cleanup failed.
                           applied_to_main is True; leaked worktree path
                           is in rehearsal_path.
  - "done"               — success.

Hook discipline
---------------
ALL driver-owned git operations (`worktree add`, `worktree remove`,
`worktree prune`, `apply`, `commit`, `merge`, etc.) run with
`-c core.hooksPath=<empty>`. This prevents repo-local hooks
(`post-checkout`, `post-merge`, `pre-commit`, etc.) from running during
driver operations. Hooks are a vector for staging hostile changes that
ride along the sibling commit or dirty main during merge.

`applied_to_main` distinguishes "main untouched" (False) from "main was
mutated by the merge" (True). Callers that need rollback semantics must
check this flag separately from `ok`.

Reuse
-----
Drift fingerprinting is shared with CP1's worker sandbox via the
`_main_repo_state` helper in worker_sandbox.py.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from worker_sandbox import _main_repo_state, SandboxError  # noqa: F401


@dataclass
class ApplyResult:
    ok: bool
    stage: str
    error: Optional[str] = None
    new_head_sha: Optional[str] = None
    base_sha: str = ""
    rehearsal_path: Optional[str] = None
    main_state_hash_before: str = ""
    main_state_hash_after: str = ""
    main_head_after: str = ""
    diff_bytes_len: int = 0
    rehearsal_output: str = ""
    main_output: str = ""
    cleanup_failed: bool = False
    cleanup_output: str = ""
    applied_to_main: bool = False


def _make_private_hooks_dir() -> Path:
    """Create a fresh private (mode 0700) empty directory for hook
    suppression, scoped to a single apply_with_rehearsal call.

    A shared persistent directory under tempdir would be a contamination
    vector: an attacker (or stale prior run) could plant an executable
    `post-checkout` script there, and every subsequent `git -c
    core.hooksPath=<that-dir>` invocation would happily execute it.

    `mkdtemp` creates with 0700 on POSIX, returns a freshly-unique path,
    and the directory is empty by construction. Caller is responsible
    for removing it.
    """
    return Path(tempfile.mkdtemp(prefix="apply-rehearsal-hooks-"))


def _safe_git(
    cwd: Path,
    extra_args: List[str],
    *,
    hooks_dir: Path,
    env=None,
    input=None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run a driver-owned git command with hooks + signing disabled.

    ALL driver-owned git operations — worktree add, worktree remove,
    worktree prune, commit, merge, apply — must go through this helper.
    `worktree add` in particular fires the `post-checkout` hook in the
    freshly-created worktree, which can stage hostile changes that ride
    along the sibling commit. The `-c core.hooksPath=<private-empty>`
    guard prevents that, but ONLY if the hooks dir is genuinely empty
    and not attacker-writable. Always pass a per-call private path from
    `_make_private_hooks_dir()`.
    """
    cmd = [
        "git",
        "-c",
        f"core.hooksPath={hooks_dir}",
        "-c",
        "commit.gpgsign=false",
    ] + extra_args
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=text, env=env, input=input,
    )


def _porcelain_z(repo: Path) -> Tuple[List[bytes], Set[bytes]]:
    """Parse `git status --porcelain=v1 -z --untracked-files=all` directly.

    Returns (tracked_dirty_entries_raw, untracked_paths_raw). Paths are
    returned as raw bytes (NO decoding) so newline-containing paths and
    non-UTF-8 path bytes are preserved.

    Raises SandboxError if the underlying `git status` invocation fails.
    Fail-closed: returning a synthetic ([], set()) on status failure
    would let callers infer a clean main state from a broken Git call,
    which is the safe-apply violation we are trying to prevent.

    Why raw bytes: `_main_repo_state` returns a display string with
    `\\x00` replaced by `\\n` and truncation at 4096 bytes. That string
    is fine for diagnostics but unsafe to parse — splitlines() on a
    newline-containing path produces ghost entries.
    """
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=str(repo),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise SandboxError(
            f"git status --porcelain=v1 -z failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    raw = proc.stdout
    if not raw:
        return [], set()

    tracked: List[bytes] = []
    untracked: Set[bytes] = set()
    entries = raw.split(b"\x00")
    if entries and entries[-1] == b"":
        entries.pop()

    i = 0
    while i < len(entries):
        entry = entries[i]
        if len(entry) < 3:
            i += 1
            continue
        status = entry[:2]
        # entry[2] is a space separator; entry[3:] is the path bytes.
        path = entry[3:]
        if status == b"??":
            untracked.add(path)
        else:
            tracked.append(entry)
            # Renames/copies emit the source path as the next \x00 entry.
            # Skip it so we don't mistake the source for a separate item.
            if status[:1] in (b"R", b"C") or status[1:2] in (b"R", b"C"):
                i += 1
        i += 1
    return tracked, untracked


def _fingerprint_untracked(repo: Path, paths: Set[bytes]) -> Dict[bytes, str]:
    """Snapshot content fingerprints for a set of untracked paths.

    Paths are raw bytes (from `_porcelain_z`). Used to detect content
    drift in pre-existing untracked files between pre-merge and
    post-merge. Path → sha256 of (mode | content).

    - Regular file: sha256 of bytes.
    - Symlink: sha256 of target.
    - Directory: marker.
    - Missing / unreadable: sentinel so we surface drift correctly.
    """
    out: Dict[bytes, str] = {}
    repo_bytes = os.fsencode(str(repo))
    for relpath in paths:
        full_bytes = repo_bytes + b"/" + relpath
        try:
            st = os.lstat(full_bytes)
        except OSError as exc:
            out[relpath] = f"missing:{exc.errno}"
            continue
        h = hashlib.sha256()
        h.update(f"{st.st_mode:o}|".encode())
        if stat.S_ISLNK(st.st_mode):
            try:
                target = os.readlink(full_bytes)
                if isinstance(target, str):
                    target = os.fsencode(target)
                h.update(b"L|" + target)
            except OSError as exc:
                h.update(f"L-err:{exc.errno}".encode())
        elif stat.S_ISREG(st.st_mode):
            try:
                with open(full_bytes, "rb") as fh:
                    while True:
                        chunk = fh.read(64 * 1024)
                        if not chunk:
                            break
                        h.update(chunk)
            except OSError as exc:
                h.update(f"R-err:{exc.errno}".encode())
        elif stat.S_ISDIR(st.st_mode):
            h.update(b"D|")
        else:
            h.update(f"O|{st.st_mode:o}".encode())
        out[relpath] = h.hexdigest()
    return out


def _git_common_dir(repo: Path) -> Optional[Path]:
    """Return the absolute path to repo's git common dir, or None if not a git repo."""
    proc = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = (repo / p).resolve()
    return p


def apply_with_rehearsal(
    *,
    main_repo: Path,
    diff_bytes: bytes,
    base_sha: str,
    commit_message: str,
    author_name: str = "outer-loop driver",
    author_email: str = "driver@local",
    rehearsal_parent: Optional[Path] = None,
) -> ApplyResult:
    """Rehearse, then fast-forward main. See module docstring for contract."""
    main_repo = Path(main_repo)
    diff_len = len(diff_bytes)

    # -- Lock --------------------------------------------------------------
    # Advisory lock under the git common dir serializes concurrent CP2
    # callers. Non-blocking by default.
    lock_path: Optional[Path] = None
    lock_fd: Optional[int] = None
    common = _git_common_dir(main_repo)
    if common is not None:
        try:
            common.mkdir(parents=True, exist_ok=True)
            lock_path = common / "apply-rehearsal.lock"
            lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(lock_fd)
                lock_fd = None
                return ApplyResult(
                    ok=False,
                    stage="lock",
                    error=f"apply-rehearsal lock busy at {lock_path}",
                    base_sha=base_sha,
                    diff_bytes_len=diff_len,
                )
        except OSError as exc:
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
                lock_fd = None
            return ApplyResult(
                ok=False,
                stage="lock_setup",
                error=f"failed to acquire apply-rehearsal lock: {exc}",
                base_sha=base_sha,
                diff_bytes_len=diff_len,
            )

    def _release_lock() -> None:
        nonlocal lock_fd
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass
            lock_fd = None

    # -- Private hooks dir -------------------------------------------------
    # Fresh mode-0700 empty dir for hook suppression. A shared persistent
    # dir is an attack vector (anyone could plant a post-checkout script).
    try:
        hooks_dir = _make_private_hooks_dir()
    except OSError as exc:
        _release_lock()
        return ApplyResult(
            ok=False,
            stage="lock_setup",
            error=f"failed to create private hooks dir: {exc}",
            base_sha=base_sha,
            diff_bytes_len=diff_len,
        )

    try:
        # -- Pre-check -----------------------------------------------------
        try:
            head_before, hash_before, porcelain_before = _main_repo_state(main_repo)
        except Exception as exc:
            return ApplyResult(
                ok=False,
                stage="precheck",
                error=f"failed to fingerprint main: {exc}",
                base_sha=base_sha,
                diff_bytes_len=diff_len,
            )

        if head_before != base_sha:
            return ApplyResult(
                ok=False,
                stage="precheck",
                error=f"main HEAD {head_before!r} != base_sha {base_sha!r}",
                base_sha=base_sha,
                main_state_hash_before=hash_before,
                main_state_hash_after=hash_before,
                main_head_after=head_before,
                diff_bytes_len=diff_len,
            )

        # Parse porcelain directly from `-z` raw bytes so paths with
        # newlines/non-UTF-8/etc. are handled safely. `_main_repo_state`
        # returns a display-only string that is unsafe to parse.
        try:
            tracked_dirty, _untracked_before = _porcelain_z(main_repo)
        except SandboxError as exc:
            return ApplyResult(
                ok=False,
                stage="precheck",
                error=f"porcelain parse failed: {exc}",
                base_sha=base_sha,
                main_state_hash_before=hash_before,
                main_state_hash_after=hash_before,
                main_head_after=head_before,
                diff_bytes_len=diff_len,
            )
        if tracked_dirty:
            return ApplyResult(
                ok=False,
                stage="precheck",
                error=f"main has dirty tracked entries: {tracked_dirty[:8]!r}",
                base_sha=base_sha,
                main_state_hash_before=hash_before,
                main_state_hash_after=hash_before,
                main_head_after=head_before,
                diff_bytes_len=diff_len,
            )

        if diff_len == 0:
            return ApplyResult(
                ok=False,
                stage="empty",
                error="diff is empty bytes",
                base_sha=base_sha,
                main_state_hash_before=hash_before,
                main_state_hash_after=hash_before,
                main_head_after=head_before,
                diff_bytes_len=0,
            )

        # -- Sibling parent setup -----------------------------------------
        try:
            parent = Path(rehearsal_parent) if rehearsal_parent else Path(tempfile.gettempdir()) / "apply-rehearsal"
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return ApplyResult(
                ok=False,
                stage="rehearsal_setup",
                error=f"failed to create rehearsal parent: {exc}",
                base_sha=base_sha,
                main_state_hash_before=hash_before,
                main_state_hash_after=hash_before,
                main_head_after=head_before,
                diff_bytes_len=diff_len,
            )

        sibling = parent / f"sib-{uuid.uuid4().hex[:12]}"

        result = ApplyResult(
            ok=False,
            stage="rehearsal",
            base_sha=base_sha,
            main_state_hash_before=hash_before,
            main_state_hash_after=hash_before,
            main_head_after=head_before,
            diff_bytes_len=diff_len,
            rehearsal_path=str(sibling),
        )

        sibling_created = False

        def _cleanup_sibling() -> Tuple[bool, str]:
            """Best-effort sibling removal. Returns (ok, combined_output).

            All worktree ops go through _safe_git so hooks cannot fire
            during cleanup either.
            """
            ok = True
            out_parts: List[str] = []
            if sibling_created:
                proc = _safe_git(
                    main_repo,
                    ["worktree", "remove", "--force", str(sibling)],
                    hooks_dir=hooks_dir,
                )
                if proc.returncode != 0:
                    ok = False
                    out_parts.append(f"worktree remove: {proc.stderr.strip()}")
            if sibling.exists():
                try:
                    shutil.rmtree(sibling)
                except OSError as exc:
                    ok = False
                    out_parts.append(f"rmtree: {exc}")
            # Prune dangling refs regardless.
            prune = _safe_git(main_repo, ["worktree", "prune"], hooks_dir=hooks_dir)
            if prune.returncode != 0:
                ok = False
                out_parts.append(f"prune: {prune.stderr.strip()}")
            return ok, "\n".join(out_parts)

        try:
            # `git worktree add` fires the post-checkout hook in the new
            # worktree. Route through _safe_git so the hook cannot stage
            # hostile changes that ride along the sibling commit.
            add_proc = _safe_git(
                main_repo,
                ["worktree", "add", "--detach", str(sibling), base_sha],
                hooks_dir=hooks_dir,
            )
            if add_proc.returncode != 0:
                result.stage = "rehearsal_setup"
                result.error = f"sibling worktree creation failed: {add_proc.stderr.strip()}"
                return result
            sibling_created = True

            # -- Rehearsal apply -----------------------------------------
            # `git apply` itself does not invoke hooks, but route through
            # the safe wrapper for consistency. text=False because diff
            # bytes may not be UTF-8.
            apply_proc = _safe_git(
                sibling,
                ["apply", "--index", "--binary", "-"],
                hooks_dir=hooks_dir,
                input=diff_bytes,
                text=False,
            )
            # apply_proc.stderr is bytes because text=False.
            stderr_bytes = apply_proc.stderr if isinstance(apply_proc.stderr, (bytes, bytearray)) else (apply_proc.stderr or "").encode()
            result.rehearsal_output = stderr_bytes.decode("utf-8", errors="replace")
            if apply_proc.returncode != 0:
                result.stage = "rehearsal"
                result.error = "diff failed to apply in sibling worktree"
                return result

            # No-op diff (e.g., only context lines): apply succeeds but
            # staged is empty.
            diff_check = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=str(sibling),
                capture_output=True,
            )
            if diff_check.returncode == 0:
                result.stage = "empty"
                result.error = "diff applied but staged no changes (no-op)"
                return result

            # -- Sibling commit ------------------------------------------
            env = os.environ.copy()
            env.update(
                {
                    "GIT_AUTHOR_NAME": author_name,
                    "GIT_AUTHOR_EMAIL": author_email,
                    "GIT_COMMITTER_NAME": author_name,
                    "GIT_COMMITTER_EMAIL": author_email,
                }
            )
            commit_proc = _safe_git(
                sibling,
                ["commit", "--no-gpg-sign", "-m", commit_message],
                hooks_dir=hooks_dir,
                env=env,
            )
            if commit_proc.returncode != 0:
                result.stage = "rehearsal_commit"
                result.error = (
                    f"sibling commit failed: "
                    f"{commit_proc.stderr.strip() or commit_proc.stdout.strip()}"
                )
                return result

            rev_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(sibling),
                capture_output=True,
                text=True,
            )
            if rev_proc.returncode != 0:
                result.stage = "rehearsal_commit"
                result.error = f"sibling rev-parse failed: {rev_proc.stderr.strip()}"
                return result
            new_sha = rev_proc.stdout.strip()
            result.new_head_sha = new_sha

            # -- Post-check: main has not drifted ------------------------
            try:
                head_after_rehearsal, hash_after_rehearsal, porc_after_rehearsal = _main_repo_state(main_repo)
            except Exception as exc:
                result.stage = "postcheck"
                result.error = f"post-rehearsal fingerprint failed: {exc}"
                result.main_state_hash_after = ""
                return result

            if head_after_rehearsal != base_sha or hash_after_rehearsal != hash_before:
                result.stage = "postcheck"
                result.error = (
                    "main drifted between precheck and merge "
                    f"(head {head_after_rehearsal!r} vs {base_sha!r}, "
                    f"hash_equal={hash_after_rehearsal == hash_before})"
                )
                result.main_state_hash_after = hash_after_rehearsal
                result.main_head_after = head_after_rehearsal
                return result

            # Refresh untracked set right before merge so the post-merge
            # comparison uses the freshest baseline. Use raw -z parser
            # so paths with newlines / non-UTF-8 work correctly.
            try:
                _, untracked_pre_merge = _porcelain_z(main_repo)
            except SandboxError as exc:
                result.stage = "postcheck"
                result.error = f"pre-merge porcelain parse failed: {exc}"
                result.main_state_hash_after = hash_before
                return result
            # Snapshot untracked content. Path-set alone is insufficient:
            # a race that rewrites an existing untracked file is invisible
            # to a set-difference check.
            untracked_fp_pre = _fingerprint_untracked(main_repo, untracked_pre_merge)

            # -- FF-only merge into main (hooks DISABLED) ----------------
            merge_proc = _safe_git(
                main_repo,
                ["merge", "--ff-only", "--no-edit", new_sha],
                hooks_dir=hooks_dir,
                env=env,
            )
            result.main_output = (merge_proc.stdout + merge_proc.stderr).strip()
            if merge_proc.returncode != 0:
                result.stage = "main_merge"
                result.error = f"ff-only merge refused: {merge_proc.stderr.strip()}"
                try:
                    _, hash_now, _ = _main_repo_state(main_repo)
                    result.main_state_hash_after = hash_now
                    head_now = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=str(main_repo), capture_output=True, text=True,
                    ).stdout.strip()
                    result.main_head_after = head_now
                except Exception:
                    pass
                return result

            # Merge succeeded — main IS mutated from here on.
            result.applied_to_main = True

            # -- Post-merge verification ---------------------------------
            try:
                head_final, hash_final, porc_final = _main_repo_state(main_repo)
            except Exception as exc:
                result.stage = "post_merge_verify"
                result.error = f"final fingerprint failed: {exc}"
                result.main_state_hash_after = ""
                return result

            result.main_state_hash_after = hash_final
            result.main_head_after = head_final

            if head_final != new_sha:
                result.stage = "post_merge_verify"
                result.error = (
                    f"main HEAD after ff-merge {head_final!r} != new_sha {new_sha!r}"
                )
                return result

            try:
                tracked_dirty_final, untracked_final = _porcelain_z(main_repo)
            except SandboxError as exc:
                result.stage = "post_merge_verify"
                result.error = f"post-merge porcelain parse failed: {exc}"
                return result
            if tracked_dirty_final:
                result.stage = "post_merge_verify"
                result.error = (
                    "main has dirty tracked entries after merge "
                    f"(hook or race): {tracked_dirty_final[:8]!r}"
                )
                return result

            # Any NEW untracked path that wasn't there pre-merge is a
            # hook or race artifact.
            new_untracked = untracked_final - untracked_pre_merge
            if new_untracked:
                result.stage = "post_merge_verify"
                result.error = (
                    "main has new untracked files after merge "
                    f"(hook or race): {sorted(new_untracked)[:8]!r}"
                )
                return result

            # Pre-existing untracked content must be byte-for-byte
            # unchanged. A race that REWRITES an existing untracked file
            # is invisible to the path-set check above.
            #
            # Note on .gitignore changes: if the merge transitions a path
            # from untracked → tracked, it will not appear in
            # untracked_final, so we skip it. If a path was untracked
            # pre-merge and remains untracked post-merge, content must
            # match. Paths that disappear from the untracked set are
            # checked above (they could only have moved to tracked or
            # been deleted; deletion would surface as a fingerprint
            # mismatch in the targeted recheck below).
            still_untracked = untracked_pre_merge & untracked_final
            untracked_fp_post = _fingerprint_untracked(main_repo, still_untracked)
            content_drift = [
                p for p in still_untracked
                if untracked_fp_pre.get(p) != untracked_fp_post.get(p)
            ]
            if content_drift:
                result.stage = "post_merge_verify"
                result.error = (
                    "main has untracked content drift after merge "
                    f"(hook or race): {sorted(content_drift)[:8]!r}"
                )
                return result

            # Paths that pre-existed as untracked but disappeared from
            # untracked_final without being added to the tracked set
            # under new_sha mean the file was deleted or the ignore
            # rules changed. Detect deletion explicitly: if the path
            # still exists on disk but is now ignored, that is fine;
            # if it does not exist on disk, that is drift.
            disappeared = untracked_pre_merge - untracked_final
            deleted = []
            repo_bytes = os.fsencode(str(main_repo))
            for relpath in disappeared:
                full_bytes = repo_bytes + b"/" + relpath
                try:
                    os.lstat(full_bytes)
                except OSError:
                    deleted.append(relpath)
            if deleted:
                result.stage = "post_merge_verify"
                result.error = (
                    "pre-existing untracked files deleted during merge "
                    f"(hook or race): {sorted(deleted)[:8]!r}"
                )
                return result

            result.ok = True
            result.stage = "done"
            return result

        finally:
            cleanup_ok, cleanup_output = _cleanup_sibling()
            result.cleanup_output = cleanup_output
            if not cleanup_ok:
                result.cleanup_failed = True
                # If the merge already landed and the only failure is
                # cleanup, the safe-apply contract is partially violated:
                # main is at new_sha but we leaked a worktree. Force ok=False
                # with a dedicated stage so callers must handle it.
                if result.ok:
                    result.ok = False
                    result.stage = "cleanup"
                    result.error = (
                        f"merge landed but sibling cleanup failed: {cleanup_output}"
                    )
    finally:
        # Always remove the private hooks dir and release the lock,
        # regardless of which exit path we took.
        try:
            shutil.rmtree(hooks_dir, ignore_errors=True)
        except Exception:
            pass
        _release_lock()

    return result
