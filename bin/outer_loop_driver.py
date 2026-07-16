#!/usr/bin/env python3
"""outer_loop_driver.py — CP6 of the autonomous outer-loop driver: the supervisor.

Turns a durable auto_runtime TRACK into a self-driving, fresh-session-per-slice
executor. CP6 owns no editing and holds ~no context: it reads the track frontier,
renders one slice into a SliceSpec (worker = fresh `claude --print` in an isolated
worktree), hands it to CP7 (`slice_retry`) which runs/respawns it, then records the
verdict back into the track — and only writes `accepted` against a real
`verify:*:exit=0` evidence token, so the SUPERVISOR (not the worker) decides done.

Flow per iteration:
    dispatch_track -> next ready slice + slice_contract
      -> build SliceSpec (worker_command = claude --print acceptEdits;
         verifier_command = verifier_shim wrapping the slice's verification_commands)
      -> slice_retry.run_slice_with_retry (fresh worker per attempt)
      -> update_node_state accepted (with evidence) | blocked (on give-up)
      -> refresh_frontier (recompute ready set + closure; CP6 owns dispatch)
    loop until OBJECTIVE_COMPLETE / terminal closure / no dispatchable slice.

Why this fixes the three symptoms: each worker gets a clean context sized to one
slice (no accumulation), runs isolated in a throwaway worktree that can't move
main's HEAD (a mess-up is discarded), and cannot self-declare done — CP6 accepts
only on passing verifier evidence, else CP7 respawns or CP6 blocks.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).parent))

import auto_runtime_common as rt
from slice_executor import ExecutorResult, SliceSpec, execute_slice, _read_head
from slice_retry import run_slice_with_retry

VERIFIER_SHIM = str(Path(__file__).parent / "verifier_shim.py")

# Bounds for slice_contract.context_pack rendering (S4): enough for curated
# gotchas/conventions, small enough to never recreate context accumulation.
CONTEXT_PACK_MAX_ENTRIES = 20
CONTEXT_PACK_MAX_ENTRY_CHARS = 200

TERMINAL_CLOSURE = frozenset({
    "OBJECTIVE_COMPLETE",
    "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK",
    "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED",
    "OBJECTIVE_BLOCKED_MIGRATION_DEFECT",
})


def default_worker_command(model: str) -> list[str]:
    """The spike-proven headless worker: acceptEdits (least-privilege; still honors
    the deny-list), print mode. worker_sandbox appends the prompt as the final arg
    and sets cwd = the worktree, so no --add-dir is needed."""
    return ["claude", "--print", "--permission-mode", "acceptEdits", "--model", model]


def _load_state(track_id: str) -> dict:
    return json.loads((rt.track_dir(track_id) / "objective.state.json").read_text())


def _relative_cite_hints(owned_scope: list[str], main_repo: Path) -> list[str]:
    """owned_scope entries that name a concrete repo-relative file make good CITE
    targets for the verifier shim. Absolute paths / the repo root itself are dropped
    (the shim auto-discovers a file in those cases)."""
    hints: list[str] = []
    for p in owned_scope or []:
        pp = Path(p)
        if pp.is_absolute():
            try:
                rel = pp.resolve().relative_to(main_repo.resolve())
            except ValueError:
                continue
            if str(rel) not in (".", ""):
                hints.append(str(rel))
        elif str(pp) not in (".", ""):
            hints.append(str(pp))
    return hints


def render_worker_prompt(node: dict, attempt: int, last: Optional[ExecutorResult],
                         verification_commands: list[str], main_repo: Path) -> str:
    """Render a self-contained worker prompt from the slice contract. On retry,
    fold the prior failure in so the fresh worker doesn't repeat it.

    CRITICAL: owned_scope is relativized against main_repo and absolute paths are
    dropped. The worker runs in an isolated worktree; leaking the main-repo
    absolute path (owned_scope defaults to [cwd]) makes claude edit the main repo
    directly, tripping worker_sandbox's drift guard (observed live 2026-07-15)."""
    contract = node.get("slice_contract", {})
    title = node.get("title") or node.get("label") or node.get("id", "slice")
    lines = [
        "You are an autonomous coding worker. Complete EXACTLY this one slice of "
        "work using ONLY relative paths inside the current working directory, then "
        "stop. NEVER edit files by absolute path or outside the current directory.",
        "",
        f"# Slice: {title}",
    ]
    if node.get("description"):
        lines += ["", node["description"]]
    criteria = contract.get("acceptance_criteria", []) or []
    if criteria:
        lines += ["", "# Acceptance criteria (all must hold):"]
        lines += [f"- {c}" for c in criteria]
    owned = _relative_cite_hints(node.get("owned_scope", []), Path(main_repo))
    if owned:
        lines += ["", "# You may ONLY edit these paths (relative to the current directory):"]
        lines += [f"- {p}" for p in owned]
    # Curated context (S4 fleet-hardening): planner-curated facts — repo
    # gotchas, conventions, prior-slice decisions — carried by the durable
    # slice contract. Bounded so fresh workers get informed context WITHOUT
    # reopening the ambient-history firehose that clean-per-slice exists to
    # prevent.
    pack = contract.get("context_pack", []) or []
    if pack:
        lines += ["", "# Curated context (facts from the planner — trust but verify):"]
        for entry in pack[:CONTEXT_PACK_MAX_ENTRIES]:
            text = str(entry).strip().replace("\n", " ")
            if len(text) > CONTEXT_PACK_MAX_ENTRY_CHARS:
                text = text[:CONTEXT_PACK_MAX_ENTRY_CHARS] + "…"
            lines += [f"- {text}"]
        if len(pack) > CONTEXT_PACK_MAX_ENTRIES:
            lines += [f"- (+{len(pack) - CONTEXT_PACK_MAX_ENTRIES} more entries truncated)"]
    if verification_commands:
        lines += ["", "# Verification that will be run against your work:"]
        lines += [f"- {vc}" for vc in verification_commands]
    if attempt > 1 and last is not None:
        lines += ["", f"# NOTE: this is attempt {attempt}. Your previous attempt failed at "
                      f"stage '{last.stage}': {(last.error or '').strip()[:300]}. "
                      "Diagnose and fix that; do not repeat the mistake."]
    lines += ["", "Make the necessary file edits, ensure the verification would pass, then stop."]
    return "\n".join(lines)


def build_slice_spec(*, node: dict, slice_id: str, attempt: int, last: Optional[ExecutorResult],
                     verification_commands: list[str], worker_command: list[str],
                     main_repo: Path, workers_dir: Optional[Path] = None) -> SliceSpec:
    """Assemble the CP5 SliceSpec for one attempt of one slice.

    `workers_dir` places the worker's worktree OUTSIDE main_repo. This matters
    for a real `claude` worker: if the worktree lives under main_repo/.git,
    claude resolves the *enclosing* repo as its workspace root and edits main
    directly, tripping worker_sandbox's HEAD/drift guard (observed live
    2026-07-15). A sibling dir outside the repo makes the worktree claude's root.
    """
    check_cmd = " && ".join(verification_commands)
    verifier_command = ["python3", VERIFIER_SHIM, "--check", check_cmd]
    for hint in _relative_cite_hints(node.get("owned_scope", []), main_repo):
        verifier_command += ["--cite-file", hint]
    title = node.get("title") or slice_id
    return SliceSpec(
        prompt=render_worker_prompt(node, attempt, last, verification_commands, main_repo),
        commit_message=f"cp6({slice_id}): {title}",
        worker_command=list(worker_command),
        verifier_command=verifier_command,
        branch_name=f"codex/{slice_id}-try{attempt}",
        workers_dir=workers_dir,
    )


def _claim_summaries(res: Optional[ExecutorResult]) -> list[str]:
    vr = getattr(res, "validation_result", None) if res else None
    claims = getattr(vr, "claims", None) or []
    return [f"claim:{getattr(c, 'claim', '')[:120]}" for c in claims]


def run_track(
    *,
    track_id: str,
    main_repo: str | Path,
    model: str = "sonnet",
    max_slices: int = 50,
    max_attempts: int = 3,
    worker_command: Optional[list[str]] = None,
    default_verify: Optional[str] = None,
    execute_fn: Callable[..., ExecutorResult] = execute_slice,
    sleep: Callable[[float], None] = time.sleep,
    on_event: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Drive `track_id` to closure, one fresh-worker slice at a time.

    `worker_command`, `execute_fn`, and `sleep` are injectable so tests can drive
    the full loop deterministically without spawning real claude workers.
    """
    main_repo = Path(main_repo).resolve()
    wc = worker_command or default_worker_command(model)
    results: list[dict] = []
    closure_state = None

    # Record the repo's HEAD before any slice lands so the supervisor can
    # run the mandatory post-track review over `git diff base_sha..final_sha`
    # (S2 fleet-hardening: one review per track, catching cross-slice issues).
    try:
        base_sha: Optional[str] = _read_head(main_repo)
    except Exception:
        base_sha = None

    # Worker worktrees live OUTSIDE main_repo so a real claude worker resolves
    # its own worktree as the workspace root (see build_slice_spec). Cleaned at end.
    workers_root = Path(tempfile.mkdtemp(prefix="cp6-workers-"))

    try:
      for i in range(max_slices):
        with rt.TrackLock(track_id):
            dispatch = rt.dispatch_track(track_id)
        if dispatch.get("status") != "dispatched":
            results.append({"iteration": i, "event": "no_dispatch",
                            "status": dispatch.get("status"), "reason": dispatch.get("reason")})
            break

        slice_id = dispatch["slice_id"]
        node = _load_state(track_id)["views"]["graph"]["nodes"][slice_id]
        verification_commands = list(node.get("slice_contract", {}).get("verification_commands", []) or [])

        # Fallback: slices without an explicit verify command use default_verify
        # (e.g. the project test command) so CP6 is usable as a /drive default
        # where the planner didn't populate per-slice verification_commands.
        if not verification_commands and default_verify:
            verification_commands = [default_verify]

        # Fail closed: no verify command and no fallback -> can't prove done.
        if not verification_commands:
            with rt.TrackLock(track_id):
                rt.update_node_state(track_id, slice_id, "blocked",
                                     evidence_refs=["cp6:no_verification_commands"],
                                     blockers=["no_verification_commands"])
            results.append({"iteration": i, "slice_id": slice_id, "result": "blocked",
                            "reason": "no_verification_commands"})
            with rt.TrackLock(track_id):
                rt.refresh_frontier(track_id)
            continue

        def build_spec(attempt: int, last: Optional[ExecutorResult],
                       _node=node, _sid=slice_id, _vc=verification_commands, _wr=workers_root) -> SliceSpec:
            return build_slice_spec(node=_node, slice_id=_sid, attempt=attempt, last=last,
                                    verification_commands=_vc, worker_command=wc,
                                    main_repo=main_repo, workers_dir=_wr)

        outcome = run_slice_with_retry(
            main_repo=main_repo, build_spec=build_spec, max_attempts=max_attempts,
            execute_fn=execute_fn, sleep=sleep, on_event=on_event,
        )

        if outcome.ok:
            r = outcome.final_result
            evidence = ["verify:cp6-acceptance:exit=0", f"sha:{r.new_head_sha}"] + _claim_summaries(r)
            with rt.TrackLock(track_id):
                upd = rt.update_node_state(track_id, slice_id, "accepted",
                                           evidence_refs=evidence, acceptance_source="cp6_outer_loop")
            results.append({"iteration": i, "slice_id": slice_id,
                            "result": "accepted" if not upd.get("rejected") else "accept_rejected",
                            "attempts": outcome.attempts, "new_head_sha": r.new_head_sha,
                            "reject_reason": upd.get("reason")})
        else:
            r = outcome.final_result
            stage = r.stage if r else "?"
            with rt.TrackLock(track_id):
                rt.update_node_state(track_id, slice_id, "blocked",
                                     evidence_refs=[f"cp6:{outcome.gave_up_reason}", f"stage:{stage}"],
                                     blockers=[f"{outcome.gave_up_reason}:{stage}"])
            results.append({"iteration": i, "slice_id": slice_id, "result": "blocked",
                            "reason": outcome.gave_up_reason, "stage": stage,
                            "attempts": outcome.attempts})

        # refresh_frontier (NOT cycle_track): recompute the ready set + closure
        # without side-effect auto-dispatching the next slice — CP6 owns dispatch.
        with rt.TrackLock(track_id):
            rt.refresh_frontier(track_id)

        closure_state = _load_state(track_id)["views"]["closure"].get("closure_state")
        if closure_state in TERMINAL_CLOSURE:
            break
    finally:
        shutil.rmtree(workers_root, ignore_errors=True)

    accepted = sum(1 for r in results if r.get("result") == "accepted")
    blocked = sum(1 for r in results if r.get("result") == "blocked")
    try:
        final_sha: Optional[str] = _read_head(main_repo)
    except Exception:
        final_sha = None
    return {
        "track_id": track_id,
        "closure_state": closure_state,
        "iterations": len(results),
        "accepted": accepted,
        "blocked": blocked,
        # Review span for the mandatory post-track reviewer pass:
        # `git diff base_sha..final_sha` is the whole track's change.
        "base_sha": base_sha,
        "final_sha": final_sha,
        "results": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="CP6 outer-loop driver: run an auto_runtime track to closure with fresh-claude-per-slice workers")
    ap.add_argument("--track-id", required=True)
    ap.add_argument("--main-repo", required=True, help="Path to the git repo slices are applied to")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--max-slices", type=int, default=50)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--default-verify", default=None,
                    help="Verify command for slices lacking explicit verification_commands (e.g. the project test command). Without it such slices are blocked.")
    args = ap.parse_args()

    summary = run_track(
        track_id=args.track_id,
        main_repo=args.main_repo,
        model=args.model,
        max_slices=args.max_slices,
        max_attempts=args.max_attempts,
        default_verify=args.default_verify,
        on_event=lambda ev: print(json.dumps({"cp7_event": ev}), file=sys.stderr),
    )
    json.dump(summary, sys.stdout, indent=2, default=str)
    print()
    return 0 if summary["closure_state"] == "OBJECTIVE_COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
