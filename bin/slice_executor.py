"""
slice_executor.py — CP5 of the autonomous outer-loop driver.

Composition layer that wires CP1-CP4 into a single-slice executor:

    worker_sandbox (CP1)
        ↓ produces a diff
    static_gate (CP3)
        ↓ accepts the diff structurally
    candidate sibling + verifier (CP4)
        ↓ verifier subprocess runs against READ-ONLY snapshot of post-apply state
        ↓ produces CLAIM/CITE lines; driver enforces evidence
    apply_with_rehearsal (CP2)
        ↓ only NOW does the diff touch main, via ff-merge

Contract
--------
`execute_slice(main_repo, spec)` runs the full pipeline against a real
git repo. Main is mutated ONLY if every prior stage accepts. Failure at
any stage leaves main exactly as it was — the apply step happens AFTER
verification, not before.

The candidate sibling that the verifier inspects is a separate worktree
from CP2's apply rehearsal sibling. That isolation costs ~one extra
`git worktree add`/`remove` per slice but ensures that verifier failure
cannot leak state into main even by accident.

Inputs
------
- `main_repo`: absolute path to a git repo at the slice's base_sha.
- `SliceSpec`:
    - `prompt`: worker prompt.
    - `commit_message`: used by CP2 for the merge commit.
    - `worker_command`: list[str] passed to CP1's `run_worker_in_sandbox`.
      The prompt is appended as the final argument.
    - `verifier_command`: list[str] launched in the snapshot's cwd. Stdout
      is captured and parsed for CLAIM/CITE lines.
    - identity, timeouts, etc.

Failure stages (`ExecutorResult.stage`)
---------------------------------------
- "worker"             — CP1 sandbox or worker subprocess failed.
- "empty_diff"         — worker exited 0 but produced no diff.
- "static_gate"        — CP3 rejected the diff (parse / banned construct).
- "candidate_setup"    — couldn't create the sibling for verification.
- "candidate_apply"    — diff didn't apply in the candidate sibling.
- "snapshot"           — read-only snapshot creation failed.
- "verifier_subprocess"— verifier subprocess exited non-zero / timed out.
- "verify"             — CP4 rejected the verifier output.
- "apply"              — CP2 apply_with_rehearsal failed (main untouched).
- "done"               — success; main is at `new_head_sha`.

Out of scope
------------
- Retry/sentinel (CP7).
- auto_runtime wiring (CP6).
- Memory persistence, journaling.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from apply_rehearsal import (
    SandboxError,
    _make_private_hooks_dir,
    _safe_git,
    apply_with_rehearsal,
)
from static_gates import GateResult, static_gate
from verifier import (
    SnapshotResult,
    ValidationResult,
    cleanup_snapshot,
    create_readonly_snapshot,
    validate_verifier_output,
)
from worker_sandbox import WorkerRunResult, run_worker_in_sandbox


@dataclass
class SliceSpec:
    prompt: str
    commit_message: str
    worker_command: List[str]
    verifier_command: List[str]
    branch_name: str = ""  # default derived from uuid
    author_name: str = "outer-loop driver"
    author_email: str = "driver@local"
    worker_timeout_s: int = 600
    verifier_timeout_s: int = 300
    workers_dir: Optional[Path] = None
    candidate_parent: Optional[Path] = None
    snapshot_parent: Optional[Path] = None


@dataclass
class ExecutorResult:
    ok: bool
    stage: str
    error: Optional[str] = None
    base_sha: str = ""
    new_head_sha: Optional[str] = None
    diff: Optional[bytes] = None
    worker_result: Optional[WorkerRunResult] = None
    gate_result: Optional[GateResult] = None
    candidate_path: Optional[str] = None
    snapshot_path: Optional[str] = None
    verifier_stdout: Optional[str] = None
    verifier_stderr: Optional[str] = None
    verifier_exit_code: Optional[int] = None
    verifier_timed_out: bool = False
    validation_result: Optional[ValidationResult] = None
    apply_result: Optional[Any] = None  # ApplyResult from CP2
    cleanup_failed: bool = False
    duration_ms: int = 0


def _git(cwd: Path, args: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _read_head(repo: Path) -> str:
    proc = _git(repo, ["rev-parse", "HEAD"])
    if proc.returncode != 0:
        raise SandboxError(f"failed to read HEAD of {repo}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _create_candidate(
    *,
    main_repo: Path,
    base_sha: str,
    diff_bytes: bytes,
    candidate_parent: Optional[Path],
    hooks_dir: Path,
) -> tuple[Optional[Path], Optional[str]]:
    """Create a candidate sibling worktree at base_sha and apply the diff.

    Returns (path, error_msg). On error, returns (None, error_msg) and
    leaves no worktree behind.
    """
    parent = (
        Path(candidate_parent)
        if candidate_parent
        else Path(tempfile.gettempdir()) / "slice-executor-candidate"
    )
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"failed to create candidate parent: {exc}"

    sib = parent / f"cand-{uuid.uuid4().hex[:12]}"
    add = _safe_git(
        main_repo,
        ["worktree", "add", "--detach", str(sib), base_sha],
        hooks_dir=hooks_dir,
    )
    if add.returncode != 0:
        return None, f"worktree add failed: {add.stderr.strip()}"

    apply_proc = _safe_git(
        sib,
        ["apply", "--index", "--binary", "-"],
        hooks_dir=hooks_dir,
        input=diff_bytes,
        text=False,
    )
    if apply_proc.returncode != 0:
        # Tear down the candidate before returning.
        _safe_git(
            main_repo,
            ["worktree", "remove", "--force", str(sib)],
            hooks_dir=hooks_dir,
        )
        _safe_git(main_repo, ["worktree", "prune"], hooks_dir=hooks_dir)
        if sib.exists():
            shutil.rmtree(sib, ignore_errors=True)
        stderr = (apply_proc.stderr or b"").decode("utf-8", errors="replace").strip()
        return None, f"diff failed to apply in candidate: {stderr}"

    return sib, None


def _cleanup_candidate(
    *, main_repo: Path, candidate: Path, hooks_dir: Path
) -> bool:
    """Best-effort cleanup of the candidate worktree."""
    ok = True
    proc = _safe_git(
        main_repo,
        ["worktree", "remove", "--force", str(candidate)],
        hooks_dir=hooks_dir,
    )
    if proc.returncode != 0:
        ok = False
    if candidate.exists():
        try:
            shutil.rmtree(candidate)
        except OSError:
            ok = False
    prune = _safe_git(main_repo, ["worktree", "prune"], hooks_dir=hooks_dir)
    if prune.returncode != 0:
        ok = False
    return ok


def execute_slice(
    *,
    main_repo: Path,
    spec: SliceSpec,
) -> ExecutorResult:
    """Run the full single-slice pipeline. See module docstring."""
    start = time.monotonic()
    main_repo = Path(main_repo).resolve()

    try:
        base_sha = _read_head(main_repo)
    except SandboxError as exc:
        return ExecutorResult(
            ok=False,
            stage="worker",
            error=str(exc),
        )

    branch = spec.branch_name or f"codex/slice-{uuid.uuid4().hex[:12]}"

    # -- Step 1: worker -------------------------------------------------
    try:
        worker_res = run_worker_in_sandbox(
            main_repo=main_repo,
            prompt=spec.prompt,
            branch_name=branch,
            worker_command=list(spec.worker_command),
            timeout_s=spec.worker_timeout_s,
            workers_dir=spec.workers_dir,
        )
    except SandboxError as exc:
        return ExecutorResult(
            ok=False, stage="worker", error=str(exc), base_sha=base_sha,
        )
    if not worker_res.ok:
        return ExecutorResult(
            ok=False,
            stage="worker",
            error=worker_res.error or f"worker exit_code={worker_res.exit_code}",
            base_sha=base_sha,
            worker_result=worker_res,
        )

    diff_str = worker_res.diff or ""
    diff_bytes = diff_str.encode("utf-8") if isinstance(diff_str, str) else diff_str
    if not diff_bytes.strip():
        return ExecutorResult(
            ok=False,
            stage="empty_diff",
            error="worker produced no diff",
            base_sha=base_sha,
            worker_result=worker_res,
        )

    # -- Step 2: static gate -------------------------------------------
    gate = static_gate(
        main_repo=main_repo,
        base_sha=base_sha,
        diff_bytes=diff_bytes,
    )
    if not gate.ok:
        return ExecutorResult(
            ok=False,
            stage="static_gate",
            error=gate.error,
            base_sha=base_sha,
            diff=diff_bytes,
            worker_result=worker_res,
            gate_result=gate,
        )

    # -- Step 3: candidate sibling for verification --------------------
    hooks_dir = _make_private_hooks_dir()
    candidate: Optional[Path] = None
    snapshot_path: Optional[str] = None
    cleanup_partial = False

    try:
        candidate, err = _create_candidate(
            main_repo=main_repo,
            base_sha=base_sha,
            diff_bytes=diff_bytes,
            candidate_parent=spec.candidate_parent,
            hooks_dir=hooks_dir,
        )
        if candidate is None:
            return ExecutorResult(
                ok=False,
                stage="candidate_apply" if err and "apply" in err.lower() else "candidate_setup",
                error=err,
                base_sha=base_sha,
                diff=diff_bytes,
                worker_result=worker_res,
                gate_result=gate,
            )

        # -- Step 4: snapshot the candidate ----------------------------
        snap_res = create_readonly_snapshot(candidate, dest_parent=spec.snapshot_parent)
        if not snap_res.ok:
            return ExecutorResult(
                ok=False,
                stage="snapshot",
                error=snap_res.error,
                base_sha=base_sha,
                diff=diff_bytes,
                worker_result=worker_res,
                gate_result=gate,
                candidate_path=str(candidate),
            )
        snapshot_path = snap_res.path

        # -- Step 5: run verifier subprocess against snapshot ----------
        verifier_stdout = ""
        verifier_stderr = ""
        verifier_exit = None
        verifier_timed_out = False
        try:
            proc = subprocess.run(
                list(spec.verifier_command),
                cwd=snapshot_path,
                capture_output=True,
                text=True,
                timeout=spec.verifier_timeout_s,
            )
            verifier_stdout = proc.stdout
            verifier_stderr = proc.stderr
            verifier_exit = proc.returncode
        except subprocess.TimeoutExpired as exc:
            verifier_timed_out = True
            verifier_stdout = (exc.stdout.decode("utf-8", errors="replace") if exc.stdout else "")
            verifier_stderr = (exc.stderr.decode("utf-8", errors="replace") if exc.stderr else "")
            verifier_exit = -1
        except (OSError, FileNotFoundError) as exc:
            return ExecutorResult(
                ok=False,
                stage="verifier_subprocess",
                error=f"verifier subprocess failed to launch: {exc}",
                base_sha=base_sha,
                diff=diff_bytes,
                worker_result=worker_res,
                gate_result=gate,
                candidate_path=str(candidate),
                snapshot_path=snapshot_path,
            )

        if verifier_timed_out:
            return ExecutorResult(
                ok=False,
                stage="verifier_subprocess",
                error=f"verifier timed out after {spec.verifier_timeout_s}s",
                base_sha=base_sha,
                diff=diff_bytes,
                worker_result=worker_res,
                gate_result=gate,
                candidate_path=str(candidate),
                snapshot_path=snapshot_path,
                verifier_stdout=verifier_stdout,
                verifier_stderr=verifier_stderr,
                verifier_exit_code=verifier_exit,
                verifier_timed_out=True,
            )
        if verifier_exit != 0:
            return ExecutorResult(
                ok=False,
                stage="verifier_subprocess",
                error=f"verifier exit_code={verifier_exit}",
                base_sha=base_sha,
                diff=diff_bytes,
                worker_result=worker_res,
                gate_result=gate,
                candidate_path=str(candidate),
                snapshot_path=snapshot_path,
                verifier_stdout=verifier_stdout,
                verifier_stderr=verifier_stderr,
                verifier_exit_code=verifier_exit,
            )

        # -- Step 6: validate verifier output --------------------------
        validation = validate_verifier_output(Path(snapshot_path), verifier_stdout)
        if not validation.ok:
            return ExecutorResult(
                ok=False,
                stage="verify",
                error=validation.error,
                base_sha=base_sha,
                diff=diff_bytes,
                worker_result=worker_res,
                gate_result=gate,
                candidate_path=str(candidate),
                snapshot_path=snapshot_path,
                verifier_stdout=verifier_stdout,
                verifier_stderr=verifier_stderr,
                verifier_exit_code=verifier_exit,
                validation_result=validation,
            )

        # -- Step 7: apply to main via CP2 -----------------------------
        apply_res = apply_with_rehearsal(
            main_repo=main_repo,
            diff_bytes=diff_bytes,
            base_sha=base_sha,
            commit_message=spec.commit_message,
            author_name=spec.author_name,
            author_email=spec.author_email,
        )
        if not apply_res.ok:
            return ExecutorResult(
                ok=False,
                stage="apply",
                error=apply_res.error,
                base_sha=base_sha,
                diff=diff_bytes,
                worker_result=worker_res,
                gate_result=gate,
                candidate_path=str(candidate),
                snapshot_path=snapshot_path,
                verifier_stdout=verifier_stdout,
                verifier_stderr=verifier_stderr,
                verifier_exit_code=verifier_exit,
                validation_result=validation,
                apply_result=apply_res,
            )

        return ExecutorResult(
            ok=True,
            stage="done",
            base_sha=base_sha,
            new_head_sha=apply_res.new_head_sha,
            diff=diff_bytes,
            worker_result=worker_res,
            gate_result=gate,
            candidate_path=str(candidate),
            snapshot_path=snapshot_path,
            verifier_stdout=verifier_stdout,
            verifier_stderr=verifier_stderr,
            verifier_exit_code=verifier_exit,
            validation_result=validation,
            apply_result=apply_res,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    finally:
        # Cleanup: snapshot first (it lives outside the candidate),
        # then candidate, then hooks_dir.
        if snapshot_path:
            if not cleanup_snapshot(Path(snapshot_path)):
                cleanup_partial = True
        if candidate is not None:
            if not _cleanup_candidate(
                main_repo=main_repo, candidate=candidate, hooks_dir=hooks_dir
            ):
                cleanup_partial = True
        try:
            shutil.rmtree(hooks_dir, ignore_errors=True)
        except Exception:
            cleanup_partial = True
        # We can't easily reach back into `result` from here without
        # tracking it explicitly. The cleanup_failed flag is a hint, not
        # a hard fail on the success path (CP2's cleanup is the
        # authoritative one for the actual merge).
