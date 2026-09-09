#!/usr/bin/env python3
"""Worker sandbox — CP1 of the autonomous outer-loop driver.

Closes part of sub-problem #3 from
~/.claude/.codex-spar/autonomous-outer-loop/plan-final.md in isolation:
spawn a worker subprocess inside a fresh git worktree, capture what it
changed, and DETECT any drift in the main repository's state.

## What this module actually delivers (honest framing)

CP1 is a **cwd-based sandbox for cooperating workers** with main-repo
drift detection. It is NOT a process-level security boundary against an
actively malicious worker.

Specifically:
  - Sandbox enforcement is by convention: the worker's `cwd` is set to a
    fresh git worktree, and a cooperating worker (e.g. Claude operating
    under a prompt that says "edit only within cwd") will stay there.
  - Main-repo state is captured BEFORE and AFTER as a content-sensitive
    fingerprint: HEAD sha + `git status --porcelain` raw bytes + the
    bytes of every dirty/untracked regular file (no size cap) + symlink
    target bytes + submodule recursion. Any drift in this fingerprint
    is DETECTED and reported as `ok=False, error=...`. CP1 does not
    attempt to restore — that would risk nuking unrelated work.
    Coverage is **Git-visible main-repo state**: explicitly NOT covered
    are ignored files, empty untracked directories,
    assume-unchanged/skip-worktree paths, and mtime-only changes that
    don't shift `git status`. Those are outside CP1's contract.
  - A misbehaving worker (one that runs `git -C <main_repo> ...` or
    otherwise reaches outside cwd) can in fact mutate the main repo.
    The drift will be detected on return; the repo will be left in the
    mutated state. The result is loudly marked invalid.

Hard process-jail isolation (macOS sandbox-exec, Docker, firejail) is
deliberately out of scope. The threat model that motivates CP1 is
"agent makes an honest mistake about which file to edit," not "agent
actively tries to escape." Structural read-only enforcement DOES happen
elsewhere in the pipeline — CP4's verifier runs against an `rsync` +
`chmod -R a-w` snapshot — but that's for the verifier subprocess, not
the worker.

Not in scope for CP1: apply rehearsal (CP2), driver-owned static gates
(CP3), verifier (CP4), composition (CP5), auto_runtime wiring (CP6),
retry/sentinel (CP7).

The `worker_command` parameter is the seam between the sandbox and the
actual agent. In tests, it points at a fixture script that performs
deterministic edits. In production (CP5+), it points at
`claude --print --output-format json ...`. CP1 only cares that the
seam exists and is honored.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WorkerRunResult:
    """Outcome of one worker invocation in a sandbox worktree.

    `ok` is True iff the subprocess exited 0 within the timeout AND the
    sandbox setup/teardown completed without error. It does NOT mean the
    diff is acceptable — that's a downstream concern (CP3/CP4).

    `base_sha` is the commit the worktree started at; `tip_sha` is HEAD
    of the worktree at teardown time. If the worker committed,
    base_sha != tip_sha. If it left uncommitted edits, base_sha == tip_sha
    and `working_tree_dirty` is True.

    `diff` and `changed_files` cover BOTH committed AND uncommitted changes:
    we capture `git diff base_sha` against the working tree (not just HEAD)
    so callers get the full picture. CP4 will tighten this once the worker
    commit contract is enforced.

    `sandbox_path` is the worktree directory. On success it is cleaned up
    and the path no longer exists; on failure it is retained for inspection.

    `stdout`/`stderr` are byte-capped (see CAPTURE_CAP_BYTES) to prevent a
    noisy worker from pressuring the parent.
    """
    ok: bool
    exit_code: int
    base_sha: str
    tip_sha: str
    working_tree_dirty: bool
    diff: str
    changed_files: list[str]
    sandbox_path: Path | None
    sandbox_retained: bool
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    error: str | None = None
    main_head_before: str = ""
    main_head_after: str = ""
    main_state_hash_before: str = ""
    main_state_hash_after: str = ""
    main_porcelain_before: str = ""
    main_porcelain_after: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    cleanup_failed: bool = False


# Output cap per stream. Worker subprocesses can produce arbitrary output;
# this prevents a runaway from pressuring the parent. 8 MB matches what
# automation_architecture/src/live-codex.ts uses for codex output capture
# scaled down (codex caps at 50MB but its JSONL stream is naturally larger
# than a single-shot Claude envelope).
CAPTURE_CAP_BYTES = 8 * 1024 * 1024

DEFAULT_TIMEOUT_S = 600  # 10 minutes; aligns with plan-final's worker default


class SandboxError(Exception):
    """Raised when sandbox setup or teardown fails. Worker failures do NOT
    raise; they return WorkerRunResult(ok=False, ...). Sandbox errors mean
    the safety property couldn't be established."""


def run_worker_in_sandbox(
    *,
    main_repo: Path,
    prompt: str,
    branch_name: str,
    worker_command: list[str],
    timeout_s: int = DEFAULT_TIMEOUT_S,
    workers_dir: Path | None = None,
    env: dict | None = None,
) -> WorkerRunResult:
    """Spawn `worker_command` inside a fresh git worktree of `main_repo`.

    The subprocess's cwd is the worktree path. `prompt` is appended to
    `worker_command` as the final positional argument (matches both the
    `claude --print <prompt>` shape and a test fixture that ignores it).

    Returns WorkerRunResult. Raises SandboxError ONLY for setup/teardown
    failures — anything the worker itself does is reported in the result.

    Main-repo HEAD invariant: this function asserts (before and after)
    that the main repo's HEAD has not moved. If the assertion fails on
    the post-run check, the result is marked ok=False with error set;
    the function does not attempt to "fix" the main repo.
    """
    main_repo = main_repo.resolve()
    if not (main_repo / ".git").exists():
        raise SandboxError(f"main_repo is not a git repository: {main_repo}")

    # Pre-flight: capture a content-sensitive fingerprint of the main repo.
    # Comparing only porcelain text misses content changes to already-dirty
    # paths (porcelain reports " M tracked.txt" regardless of bytes); and
    # `check=False` would swallow index corruption. The fingerprint hashes
    # porcelain output + the bytes of every dirty/untracked file.
    try:
        main_head_before, main_state_hash_before, main_porcelain_before = _main_repo_state(main_repo)
    except (subprocess.CalledProcessError, OSError) as e:
        raise SandboxError(f"failed to fingerprint main repo state before run: {e}") from e

    # Choose worktree path. Workers dir defaults to a sibling tmp area
    # under the main repo's .git/auto-sandbox/ so it shares filesystem and
    # storage policy with the repo, and is gitignored implicitly (inside .git).
    if workers_dir is None:
        workers_dir = main_repo / ".git" / "auto-sandbox"
    workers_dir.mkdir(parents=True, exist_ok=True)

    # Worktree directory name: branch_name + timestamp to avoid collisions
    # if the same branch name is reused.
    timestamp = int(time.time() * 1000)
    worktree_dir_name = f"{_sanitize_branch_name(branch_name)}-{timestamp}"
    sandbox_path = workers_dir / worktree_dir_name

    started_at = time.monotonic()
    sandbox_retained = False
    cleanup_failed = False
    error: str | None = None
    timed_out = False
    exit_code = -1
    stdout = ""
    stderr = ""
    stdout_truncated = False
    stderr_truncated = False
    base_sha = main_head_before
    tip_sha = main_head_before
    working_tree_dirty = False
    diff = ""
    changed_files: list[str] = []

    try:
        # Create worktree on a new branch pointing at HEAD.
        # `--no-track` to avoid inheriting upstream config.
        _git(
            ["worktree", "add", "--no-track", "-b", branch_name, str(sandbox_path), "HEAD"],
            cwd=main_repo,
        )
    except subprocess.CalledProcessError as e:
        raise SandboxError(f"git worktree add failed: {e.stderr or e.stdout or e}") from e

    try:
        # Run the worker.
        argv = list(worker_command) + [prompt]
        run_env = os.environ.copy()
        if env:
            run_env.update(env)

        try:
            proc_result = _run_subprocess_bounded(
                argv=argv,
                cwd=sandbox_path,
                env=run_env,
                timeout_s=timeout_s,
            )
            exit_code = proc_result.exit_code
            stdout = proc_result.stdout
            stderr = proc_result.stderr
            stdout_truncated = proc_result.stdout_truncated
            stderr_truncated = proc_result.stderr_truncated
            timed_out = proc_result.timed_out
        except (FileNotFoundError, PermissionError, OSError) as e:
            # Worker command launch failure (e.g. nonexistent executable,
            # permission denied on the binary). Record and continue to
            # state capture + finally block.
            exit_code = 127  # conventional "command not found"
            error = f"worker launch failed: {type(e).__name__}: {e}"

        # Capture the sandbox state regardless of exit code or launch failure;
        # callers need to see what (if anything) the worker did.
        try:
            if sandbox_path.exists():
                tip_sha = _git(["rev-parse", "HEAD"], cwd=sandbox_path).strip()
                working_tree_dirty = not _is_clean(sandbox_path)
                # Register untracked files as intent-to-add so `git diff` sees
                # them. This modifies the sandbox index only; it cannot leak
                # to main.
                _git(["add", "-N", "."], cwd=sandbox_path, check=False)
                diff = _git(["diff", base_sha], cwd=sandbox_path, check=False)
                changed_raw = _git(
                    ["diff", "--name-only", base_sha],
                    cwd=sandbox_path, check=False,
                )
                changed_files = [
                    line.strip() for line in changed_raw.splitlines() if line.strip()
                ]
            else:
                # Worker deleted its own cwd or sandbox setup partially failed.
                error = (error + "; " if error else "") + "sandbox_path missing after worker exit"
        except (subprocess.CalledProcessError, OSError) as e:
            error = (error + "; " if error else "") + f"sandbox state capture failed: {e}"

    finally:
        # Re-check main repo state. This MUST happen even if everything above
        # failed. If the post-check fingerprint can't be computed, that itself
        # is treated as drift (the repo is in a state we can't measure).
        try:
            main_head_after, main_state_hash_after, main_porcelain_after = _main_repo_state(main_repo)
        except (subprocess.CalledProcessError, OSError) as e:
            main_head_after = ""
            main_state_hash_after = ""
            main_porcelain_after = ""
            error = (error + "; " if error else "") + (
                f"main repo post-state fingerprint failed (treating as drift): {e}"
            )

        # Teardown:
        #  - Keep sandbox on worker failure (exit != 0), timeout, or any error.
        #  - Remove sandbox on clean worker exit (exit == 0, no timeout, no error).
        worker_succeeded = (exit_code == 0) and not timed_out and (error is None)
        if worker_succeeded:
            # Explicit error capture: don't use check=False then silently
            # ignore. Run each cleanup step and surface failures.
            rm_result = subprocess.run(
                ["git", "worktree", "remove", "--force", str(sandbox_path)],
                cwd=str(main_repo), capture_output=True, text=True, check=False,
            )
            if rm_result.returncode != 0:
                cleanup_failed = True
                error = (error + "; " if error else "") + (
                    f"git worktree remove failed (rc={rm_result.returncode}): "
                    f"{rm_result.stderr.strip()[:200]}"
                )
            br_result = subprocess.run(
                ["git", "branch", "-D", branch_name],
                cwd=str(main_repo), capture_output=True, text=True, check=False,
            )
            if br_result.returncode != 0:
                # Branch cleanup is best-effort; not fatal but visible.
                cleanup_failed = True
                error = (error + "; " if error else "") + (
                    f"branch delete failed (rc={br_result.returncode}): "
                    f"{br_result.stderr.strip()[:200]}"
                )
            # Defensive: ensure path is gone even if `worktree remove` reported success.
            if sandbox_path.exists():
                try:
                    shutil.rmtree(sandbox_path)
                except OSError as e:
                    cleanup_failed = True
                    error = (error + "; " if error else "") + f"rmtree sandbox failed: {e}"
            sandbox_retained = cleanup_failed
        else:
            sandbox_retained = True

        duration_ms = int((time.monotonic() - started_at) * 1000)

    # Establish ok using content-sensitive drift detection.
    main_head_held = (main_head_before == main_head_after) and bool(main_head_after)
    main_state_held = (
        main_state_hash_before == main_state_hash_after
        and bool(main_state_hash_after)
    )

    if not main_head_held:
        error = (error + "; " if error else "") + (
            f"MAIN REPO DRIFT DETECTED: HEAD moved {main_head_before} -> {main_head_after}"
        )
    if not main_state_held:
        error = (error + "; " if error else "") + (
            "MAIN REPO DRIFT DETECTED: state fingerprint changed during run "
            f"(before={main_state_hash_before[:12]} after={main_state_hash_after[:12]})"
        )

    ok = (
        exit_code == 0
        and not timed_out
        and error is None
        and main_head_held
        and main_state_held
        and not cleanup_failed
    )

    return WorkerRunResult(
        ok=ok,
        exit_code=exit_code,
        base_sha=base_sha,
        tip_sha=tip_sha,
        working_tree_dirty=working_tree_dirty,
        diff=diff,
        changed_files=changed_files,
        sandbox_path=sandbox_path if sandbox_retained else None,
        sandbox_retained=sandbox_retained,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        timed_out=timed_out,
        error=error,
        main_head_before=main_head_before,
        main_head_after=main_head_after,
        main_state_hash_before=main_state_hash_before,
        main_state_hash_after=main_state_hash_after,
        main_porcelain_before=main_porcelain_before,
        main_porcelain_after=main_porcelain_after,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        cleanup_failed=cleanup_failed,
    )


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

@dataclass
class _ProcResult:
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool


def _read_stream_bounded(stream, cap: int, out_buf: bytearray, truncated: list[bool]) -> None:
    """Read from `stream` into `out_buf`, capping at `cap` bytes total.

    After the cap is reached, drain the stream into /dev/null so the
    worker's pipe doesn't block, but stop appending. This keeps parent
    memory bounded regardless of worker output volume.
    """
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            remaining = cap - len(out_buf)
            if remaining > 0:
                out_buf.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated[0] = True
            else:
                # Drain mode — keep reading so the pipe doesn't fill.
                truncated[0] = True
    except (OSError, ValueError):
        # Stream closed unexpectedly.
        pass


def _run_subprocess_bounded(
    *,
    argv: list[str],
    cwd: Path,
    env: dict,
    timeout_s: int,
) -> _ProcResult:
    """Run a subprocess with bounded output (streaming) and a hard timeout.

    On timeout: kill the entire process group (not just the leader) so
    children spawned by the worker don't outlive the deadline.

    Output is read via reader threads with a hard cap of CAPTURE_CAP_BYTES
    per stream. Anything past the cap is drained but not stored — the
    parent's memory stays bounded.
    """
    timed_out = False
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    out_buf = bytearray()
    err_buf = bytearray()
    out_trunc = [False]
    err_trunc = [False]
    t_out = threading.Thread(
        target=_read_stream_bounded,
        args=(proc.stdout, CAPTURE_CAP_BYTES, out_buf, out_trunc),
        daemon=True,
    )
    t_err = threading.Thread(
        target=_read_stream_bounded,
        args=(proc.stderr, CAPTURE_CAP_BYTES, err_buf, err_trunc),
        daemon=True,
    )
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # SIGKILL last resort
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    # Wait for reader threads to finish draining whatever remains.
    t_out.join(timeout=10)
    t_err.join(timeout=10)
    # Close pipes defensively.
    for s in (proc.stdout, proc.stderr):
        try:
            s.close()
        except Exception:
            pass

    stdout = out_buf.decode("utf-8", errors="replace")
    stderr = err_buf.decode("utf-8", errors="replace")
    if out_trunc[0]:
        stdout += f"\n[truncated at {CAPTURE_CAP_BYTES} bytes]"
    if err_trunc[0]:
        stderr += f"\n[truncated at {CAPTURE_CAP_BYTES} bytes]"

    exit_code = proc.returncode if proc.returncode is not None else -1
    if timed_out and exit_code == 0:
        exit_code = 124  # conventional "killed by timeout"
    return _ProcResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=out_trunc[0],
        stderr_truncated=err_trunc[0],
        timed_out=timed_out,
    )


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(args: list[str], *, cwd: Path, check: bool = True) -> str:
    """Run a git command, return stdout as text.

    `check=False` returns stdout even on nonzero exit (used for diff commands
    that may exit 1 when changes exist, depending on flags)."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, ["git", *args],
            output=result.stdout, stderr=result.stderr,
        )
    return result.stdout


def _is_clean(repo: Path) -> bool:
    """Worktree is clean iff `git status --porcelain` is empty."""
    porcelain = _git(["status", "--porcelain"], cwd=repo, check=False)
    return porcelain.strip() == ""


def _git_bytes(args: list[str], *, cwd: Path, check: bool = True) -> bytes:
    """Like _git but returns raw bytes — no text decoding.

    Used by _main_repo_state where porcelain may contain paths with bytes
    that are not valid UTF-8 (the worker can create any filename git allows)
    and we don't want a decode error to turn into a propagated exception
    that bypasses drift detection.
    """
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, check=False,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, ["git", *args],
            output=result.stdout, stderr=result.stderr,
        )
    return result.stdout


def _main_repo_state(repo: Path) -> tuple[str, str, str]:
    """Compute (head_sha, state_hash, raw_porcelain) for the main repo.

    state_hash is a sha256 of:
      - HEAD sha
      - `git status --porcelain=v1 -z --untracked-files=all` raw bytes
      - For each path mentioned by porcelain:
          - path bytes
          - file type marker (regular | symlink | other)
          - for regular files: size + sha256 of FULL contents (no cap)
          - for symlinks: lstat metadata + the bytes of os.readlink()
          - for other (dirs, sockets, fifos): lstat metadata only
      - Sentinel separators between sections

    No file-size cap is applied: bounded by however large the dirty
    set is, but truthful. If you have giant dirty files, fingerprinting
    will take longer — that's a known tradeoff and the documented
    cost of correctness over speed.

    Raises subprocess.CalledProcessError if `rev-parse HEAD` or
    `git status` fails (e.g. corrupted index). Callers MUST treat that
    as drift, not silently swallow it.
    """
    head = _git(["rev-parse", "HEAD"], cwd=repo, check=True).strip()
    # Use bytes to avoid encoding-mismatch issues with exotic paths.
    porcelain_raw = _git_bytes(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=repo, check=True,
    )

    h = hashlib.sha256()
    h.update(head.encode())
    h.update(b"\x00HEAD-END\x00")
    h.update(porcelain_raw)
    h.update(b"\x00PORCELAIN-END\x00")

    # Walk porcelain entries. Format: each record is "XY path\x00" (or
    # "XY orig\x00new\x00" for renames; both halves are paths). We hash
    # any non-empty entry that looks like a path.
    seen_paths: set[bytes] = set()
    for entry in porcelain_raw.split(b"\x00"):
        if len(entry) < 4:
            continue
        # First 2 bytes = status flags, then space, then path bytes.
        path_b = entry[3:] if entry[2:3] == b" " else entry
        if not path_b or path_b in seen_paths:
            continue
        seen_paths.add(path_b)
        # Resolve as bytes-path to handle non-UTF-8 names safely.
        try:
            full = repo / os.fsdecode(path_b)
        except (UnicodeDecodeError, ValueError):
            # Path can't be represented in fs encoding; hash the bytes
            # we have and continue.
            h.update(b"\x00PATH-UNDECODABLE:")
            h.update(path_b)
            h.update(b"\x00")
            continue
        _fingerprint_path(full, path_b, h)

    # Hash the entire index via `git ls-files -z --stage`. The output is
    # `<mode> <sha> <stage>\t<path>\0` for every tracked entry. Hashing the
    # raw bytes catches:
    #   - index-only drift (worker changes a staged blob via
    #     `git update-index --cacheinfo` without touching the worktree;
    #     porcelain text stays " MM path" both ways).
    #   - new staged entries, removed staged entries, mode/stage changes.
    # The byte hash IS the index fingerprint.
    try:
        ls_stage = _git_bytes(["ls-files", "-z", "--stage"], cwd=repo, check=True)
    except subprocess.CalledProcessError as e:
        # ls-files failure is itself drift signal (index broken).
        h.update(b"\x00LS-FILES-ERR\x00")
        h.update(str(e).encode())
        h.update(b"\x00")
        ls_stage = b""
    h.update(b"\x00INDEX-ENTRIES\x00")
    h.update(ls_stage)
    h.update(b"\x00INDEX-ENTRIES-END\x00")

    # Then walk gitlinks (subset of ls_stage) for submodule recursion.

    for entry in ls_stage.split(b"\x00"):
        if not entry or b"\t" not in entry:
            continue
        meta, sub_path_b = entry.split(b"\t", 1)
        meta_parts = meta.split(b" ")
        if len(meta_parts) < 3:
            continue
        if meta_parts[0] != b"160000":
            continue  # not a gitlink
        if sub_path_b in seen_paths:
            continue  # already fingerprinted via porcelain walk
        seen_paths.add(sub_path_b)
        gitlink_sha = meta_parts[1]
        h.update(b"\x00TRACKED-SUBMODULE:")
        h.update(sub_path_b)
        h.update(b"\x00EXPECTED=")
        h.update(gitlink_sha)
        h.update(b"\x00")
        try:
            sub_full = repo / os.fsdecode(sub_path_b)
        except (UnicodeDecodeError, ValueError):
            h.update(b"PATH-UNDECODABLE\x00")
            continue
        _fingerprint_submodule(sub_full, h)

    # Surface a human-readable porcelain too (truncated, lossy decode for
    # display only — not used for comparison).
    porcelain_text = porcelain_raw.decode("utf-8", errors="replace").replace("\x00", "\n")
    if len(porcelain_text) > 4096:
        porcelain_text = porcelain_text[:4096] + "\n[truncated]"
    return head, h.hexdigest(), porcelain_text


def _fingerprint_path(full: Path, path_b: bytes, h) -> None:
    """Hash a single path into `h`, handling regular files, symlinks,
    directories, and missing/vanished entries.

    Uses lstat (not stat) so symlinks are inspected directly, not their
    targets.
    """
    h.update(b"\x00PATH:")
    h.update(path_b)
    h.update(b"\x00")
    try:
        st = full.lstat()
    except FileNotFoundError:
        h.update(b"VANISHED\x00")
        return
    except OSError as e:
        h.update(f"LSTAT-ERR:{e.errno}\x00".encode())
        return

    import stat as statmod
    mode = st.st_mode
    if statmod.S_ISLNK(mode):
        h.update(b"SYMLINK\x00")
        h.update(f"SIZE={st.st_size}\x00".encode())
        try:
            target = os.readlink(str(full))
            h.update(b"TARGET=")
            h.update(target.encode("utf-8", errors="surrogateescape"))
            h.update(b"\x00")
        except OSError as e:
            h.update(f"READLINK-ERR:{e.errno}\x00".encode())
    elif statmod.S_ISREG(mode):
        h.update(b"REGULAR\x00")
        h.update(f"SIZE={st.st_size}\x00".encode())
        try:
            with open(full, "rb") as fp:
                while True:
                    chunk = fp.read(64 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
        except OSError as e:
            h.update(f"READ-ERR:{e.errno}\x00".encode())
    elif statmod.S_ISDIR(mode):
        h.update(b"DIR\x00")
        # Submodule case: directory contains a .git file or dir. Delegate
        # to the dedicated helper, which falls back to a content walk if
        # the submodule isn't measurable as a git repo.
        if (full / ".git").exists():
            _fingerprint_submodule(full, h)
        # Non-submodule directories contribute only their mode; their entries
        # appear as their own porcelain rows.
    else:
        # FIFO, socket, char/block device, etc.
        h.update(f"OTHER:mode={oct(mode)}\x00".encode())
    h.update(b"\x00PATH-END\x00")


def _fingerprint_submodule(sub_full: Path, h) -> None:
    """Hash a submodule path's measurable state into `h`.

    Three branches:
      1. Path doesn't exist → "MISSING" + dir-walk-empty marker.
      2. Path exists but lacks a .git pointer → fail-closed walk of the
         directory contents. This catches the case where a worker
         deletes <sub>/.git and rewrites files inside (which leaves
         the superproject porcelain clean).
      3. Path is a usable submodule repo → recurse into _main_repo_state.
         On recursion/OS error, ALSO fall back to dir-walk hashing so we
         don't collapse different states to a stable error marker.
    """
    if not sub_full.exists():
        h.update(b"SUB-STATE=MISSING\x00")
        return
    if not (sub_full / ".git").exists():
        h.update(b"SUB-STATE=GITDIR-MISSING\x00")
        _hash_dir_walk(sub_full, h)
        return
    try:
        sub_head, sub_state, _ = _main_repo_state(sub_full)
        h.update(b"SUB-HEAD=")
        h.update(sub_head.encode())
        h.update(b"\x00SUB-STATE=")
        h.update(sub_state.encode())
        h.update(b"\x00")
    except (subprocess.CalledProcessError, OSError, RecursionError) as e:
        # Unmeasurable submodule → fall back to a directory-content hash
        # so a worker that breaks the submodule AND mutates inside it can
        # still be detected.
        h.update(f"SUB-ERR:{type(e).__name__}\x00".encode())
        _hash_dir_walk(sub_full, h)


# Cap the number of files walked in a fail-closed directory hash to
# prevent a pathological submodule from making fingerprinting unbounded.
# At the cap, append a "OVER-CAP" sentinel so two over-cap states still
# compare unequal if the file count itself differs (it's hashed too).
_DIR_WALK_FILE_CAP = 100_000


def _hash_dir_walk(root: Path, h) -> None:
    """Fail-closed content hash over a directory tree.

    Used as a fallback when a submodule's git state is unmeasurable.
    Walks every regular file and symlink in deterministic (sorted) order,
    hashing relative path + lstat metadata + file content (or symlink
    target). Caps the file count at _DIR_WALK_FILE_CAP to bound work.
    """
    import stat as statmod
    count = 0
    # NOTE: no outer try/except wrapping the walk. Per-entry OSErrors are
    # caught individually so one missing file doesn't fail the whole walk,
    # but cap-exceeded MUST propagate up to mark the fingerprint unmeasurable.
    # A stable "WALK-ERR" marker here would re-introduce fail-open behavior.
    for sub in sorted(root.rglob("*")):
        count += 1
        if count > _DIR_WALK_FILE_CAP:
            # Fail-closed: caller treats this as unmeasurable → drift signal.
            raise OSError(
                f"dir walk exceeded cap of {_DIR_WALK_FILE_CAP} files at {root}; "
                "fingerprint unmeasurable — treating as drift"
            )
        try:
            rel = sub.relative_to(root)
            st = sub.lstat()
        except (ValueError, OSError):
            continue
        h.update(b"WALK-PATH=")
        h.update(str(rel).encode("utf-8", errors="surrogateescape"))
        h.update(f"\x00mode={oct(st.st_mode)}\x00size={st.st_size}\x00".encode())
        if statmod.S_ISLNK(st.st_mode):
            try:
                h.update(b"WALK-TARGET=")
                h.update(os.readlink(str(sub)).encode("utf-8", errors="surrogateescape"))
                h.update(b"\x00")
            except OSError:
                pass
        elif statmod.S_ISREG(st.st_mode):
            try:
                with open(sub, "rb") as fp:
                    while True:
                        chunk = fp.read(64 * 1024)
                        if not chunk:
                            break
                        h.update(chunk)
            except OSError:
                pass
    h.update(f"WALK-COUNT={count}\x00".encode())


def _sanitize_branch_name(name: str) -> str:
    """Normalize a branch name to git-safe characters."""
    safe = "".join(c if c.isalnum() or c in "-_/" else "-" for c in name)
    return safe.strip("-/") or "auto-sandbox"


if __name__ == "__main__":
    # Smoke entrypoint — not the main interface; tests are.
    import argparse, sys
    parser = argparse.ArgumentParser(description="Worker sandbox CP1 smoke runner.")
    parser.add_argument("--main-repo", required=True, type=Path)
    parser.add_argument("--branch-name", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--worker-command", required=True, help="space-separated argv")
    parser.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    args = parser.parse_args()
    result = run_worker_in_sandbox(
        main_repo=args.main_repo,
        prompt=args.prompt,
        branch_name=args.branch_name,
        worker_command=args.worker_command.split(),
        timeout_s=args.timeout_s,
    )
    import json, dataclasses
    print(json.dumps(dataclasses.asdict(result), default=str, indent=2))
    sys.exit(0 if result.ok else 1)
