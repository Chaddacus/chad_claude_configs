#!/usr/bin/env python3
"""obsessive_loop.py — "Improve until you can't" rubric-driven iteration loop.

Spec: ~/.claude/plans/users-chadsimon-thoughts-md-take-a-giggly-moore.md (slice 7)

Usage:
    obsessive_loop.py --repo <path> [--out <dir>] [--max-iters N]
                      [--no-gain-cycles N] [--regression-threshold-pp X]
                      [--worker-runtime claude|goose|opencode]
                      [--wallclock-hours N] [--force-dirty]

Termination reasons (written to summary.json):
    all_gates_pass   — every rubric's highest-tier hard gates are satisfied
    plateau          — N consecutive iters with delta < 0.1 pp
    wallclock_cap    — wall-clock limit exceeded
    iteration_cap    — max-iters reached
    regression_brake — rubric regression detected; worktree reverted
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOME = Path(os.path.expanduser("~"))
STATE_ROOT = HOME / ".claude" / "state" / "obsessive-loop"
AUTO_RUNTIME = str(HOME / ".claude" / "bin" / "auto_runtime.py")
RUBRIC_SUITE = str(HOME / ".claude" / "bin" / "run_rubric_suite.py")

MAX_TRACK_CYCLES = 40          # per iteration track budget


# ---------------------------------------------------------------------------
# Rate-limit guard (shared module)
# ---------------------------------------------------------------------------

# Import the shared guard. obsessive_loop.py and any future autonomous runner
# (slice 4 hermes flow, anthropic-concurrency-system runner, etc.) share the
# same quota/wallclock semantics via ~/.claude/bin/rate_limit_guard.py.
sys.path.insert(0, str(HOME / ".claude" / "bin"))
from rate_limit_guard import check_rate_limit_signals as _check_rate_limit_signals  # noqa: E402


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[obsessive {ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path | None = None, check: bool = False) -> tuple[int, str, str]:
    cmd = ["git"] + args
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _is_git_repo(path: Path) -> bool:
    rc, _, _ = _git(["-C", str(path), "rev-parse", "--git-dir"])
    return rc == 0


def _is_dirty(path: Path) -> bool:
    rc, out, _ = _git(["-C", str(path), "status", "--porcelain"])
    return rc == 0 and bool(out.strip())


def _current_commit(path: Path) -> str:
    _, sha, _ = _git(["-C", str(path), "rev-parse", "HEAD"], check=True)
    return sha


def _current_branch(path: Path) -> str:
    _, branch, _ = _git(["-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"])
    return branch or "HEAD"


def _create_worktree(repo: Path, branch: str, worktree_path: Path) -> None:
    """Create a git worktree on a fresh branch."""
    _git(["-C", str(repo), "worktree", "add", "-b", branch, str(worktree_path)], check=True)


def _remove_worktree(repo: Path, worktree_path: Path) -> None:
    _git(["-C", str(repo), "worktree", "remove", "--force", str(worktree_path)])
    _git(["-C", str(repo), "worktree", "prune"])


def _make_diff_patch(worktree: Path, before_sha: str, out_file: Path) -> None:
    rc, out, _ = _git(["-C", str(worktree), "diff", before_sha], cwd=worktree)
    if rc == 0:
        out_file.write_text(out or "# no diff\n")


def _revert_to_commit(worktree: Path, sha: str) -> None:
    _git(["-C", str(worktree), "reset", "--hard", sha], check=True)


# ---------------------------------------------------------------------------
# Rubric suite runner
# ---------------------------------------------------------------------------

def _run_rubric_suite(worktree: Path, out_file: Path, bypass: str | None = None) -> dict[str, Any] | None:
    """Run rubric suite and return parsed scorecard or None on failure."""
    cmd = [sys.executable, RUBRIC_SUITE, "--repo", str(worktree), "--out", str(out_file)]
    if bypass:
        cmd += ["--rubric-bypass", bypass]
    p = subprocess.run(cmd, capture_output=True, text=True)
    _log(f"  rubric suite exit={p.returncode}")
    if p.stdout:
        _log(f"  stdout: {p.stdout.strip()[:300]}")
    if p.stderr:
        _log(f"  stderr: {p.stderr.strip()[:300]}")
    if not out_file.exists():
        _log("  WARN: scorecard file not produced")
        return None
    try:
        return json.loads(out_file.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        _log(f"  WARN: unparseable scorecard: {exc}")
        return None


# ---------------------------------------------------------------------------
# Scorecard analysis
# ---------------------------------------------------------------------------

def _weighted_avg(scorecard: dict[str, Any]) -> float:
    return float(scorecard.get("merged", {}).get("weightedAverage", 0.0))


def _all_hard_gates(scorecard: dict[str, Any]) -> list[dict[str, Any]]:
    return scorecard.get("merged", {}).get("allHardGates", [])


def _passing_gate_ids(scorecard: dict[str, Any]) -> set[str]:
    return {
        g.get("id", g.get("name", "?"))
        for g in _all_hard_gates(scorecard)
        if g.get("status") == "pass"
    }


def _any_critical_failed(scorecard: dict[str, Any]) -> bool:
    return bool(scorecard.get("merged", {}).get("anyCriticalGateFailed", False))


def _all_gates_pass_highest_tier(scorecard: dict[str, Any]) -> bool:
    """True when no critical gate is failing and the *worst* rubric is already
    at a high band. We use minBand (not maxBand) because the loop is only done
    when every rubric is high — one rubric being maxed doesn't mean the others
    don't need work."""
    if _any_critical_failed(scorecard):
        return False
    min_band = scorecard.get("merged", {}).get("minBand", "")
    high_bands = {"Enterprise-Mature", "Enterprise-Design-Mature", "Enterprise-Design-Ready"}
    return min_band in high_bands


def _lowest_scoring_category(scorecard: dict[str, Any]) -> tuple[str, str]:
    """Return (rubric_name, category_key) of the lowest-scoring actionable category."""
    worst_score = 999.0
    worst_rubric = "enterprise"
    worst_cat = "general"

    for rubric_name in ("enterprise", "security", "design"):
        rubric_data = scorecard.get("rubrics", {}).get(rubric_name, {})
        sc = rubric_data.get("scorecard", {})
        categories = sc.get("categories", [])
        hard_gates = sc.get("hardGates", sc.get("hard_gates", []))

        # Prefer failed hard gates
        for gate in hard_gates:
            if gate.get("status") == "fail":
                gid = gate.get("id", gate.get("name", "gate"))
                return rubric_name, gid

        # Then lowest-score category
        for cat in categories:
            score = float(cat.get("score", cat.get("adjustedScore", 5.0)))
            if score < worst_score:
                worst_score = score
                worst_rubric = rubric_name
                worst_cat = cat.get("key", cat.get("id", cat.get("name", "unknown")))

    return worst_rubric, worst_cat


# ---------------------------------------------------------------------------
# auto_runtime track helpers
# ---------------------------------------------------------------------------

def _ar(args: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    cmd = [sys.executable, AUTO_RUNTIME] + args
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _spawn_track(objective: str, worktree: Path, worker_runtime: str) -> str | None:
    """Init an auto_runtime track; return track_id or None."""
    rc, out, err = _ar([
        "init",
        "--task", objective,
        "--cwd", str(worktree),
        "--route", "R3",
        "--mode", "default",
    ])
    _log(f"  track init exit={rc}")
    combined = (out + err).lower()
    if rc != 0:
        _log(f"  track init stderr: {err.strip()[:300]}")
        return None
    try:
        data = json.loads(out)
        return data.get("track_id")
    except (json.JSONDecodeError, ValueError):
        # Fallback: scan for track_id pattern in output
        m = re.search(r'"track_id"\s*:\s*"([^"]+)"', out)
        if m:
            return m.group(1)
        _log(f"  could not parse track_id from init output: {out[:200]}")
        return None


def _run_track_to_completion(track_id: str, worktree: Path, max_cycles: int) -> tuple[bool, str]:
    """Run auto_runtime cycle loop until OBJECTIVE_COMPLETE or budget. Returns (completed, combined_output)."""
    completed = False
    combined_output = ""
    for cycle_num in range(max_cycles):
        rc, out, err = _ar(["cycle", "--track-id", track_id, "--max-cycles", "1"], cwd=worktree)
        combined_output += out + err
        _log(f"    cycle {cycle_num+1}: exit={rc}")
        if rc != 0:
            break
        try:
            data = json.loads(out)
            status = data.get("status", data.get("track_status", ""))
            if status in ("OBJECTIVE_COMPLETE", "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK", "complete"):
                completed = True
                break
        except (json.JSONDecodeError, ValueError):
            # Check for completion strings in raw output
            if "OBJECTIVE_COMPLETE" in out:
                completed = True
                break
        # Small yield between cycles
        time.sleep(2)
    return completed, combined_output


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _write_summary_md(out_dir: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# Obsessive Loop Summary",
        "",
        f"**Repo:** {summary.get('repo')}",
        f"**Branch:** {summary.get('branch')}",
        f"**Started:** {summary.get('started_at')}",
        f"**Ended:** {summary.get('ended_at')}",
        f"**Status:** {summary.get('status')}",
        f"**Terminate reason:** {summary.get('terminate_reason')}",
        f"**Iterations run:** {summary.get('iter_count')}",
        f"**Baseline weighted avg:** {summary.get('baseline_weighted_avg')}%",
        f"**Final weighted avg:** {summary.get('final_weighted_avg')}%",
        f"**Total delta:** +{summary.get('total_delta', 0):.2f} pp",
        "",
        "## Iteration log",
        "",
    ]
    for i, it in enumerate(summary.get("iterations", []), 1):
        lines.append(f"### Iter {i:04d}")
        lines.append(f"- Objective: {it.get('objective')}")
        lines.append(f"- Track: `{it.get('track_id')}`")
        lines.append(f"- Delta: {it.get('delta', 0):+.2f} pp")
        lines.append(f"- Weighted avg after: {it.get('weighted_avg_after')}%")
        lines.append(f"- Reverted: {it.get('reverted', False)}")
        lines.append("")

    (out_dir / "summary.md").write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Obsessive rubric-driven improvement loop — iterate until you can't improve."
    )
    ap.add_argument("--repo", required=True, type=Path, help="Target git repository path.")
    ap.add_argument("--out", type=Path, default=None, help="Output directory (default: auto under state/).")
    ap.add_argument("--max-iters", type=int, default=int(os.environ.get("OBSESSIVE_MAX_ITERS", 50)))
    ap.add_argument("--no-gain-cycles", type=int, default=3,
                    help="Consecutive iters with delta < 0.1pp before plateau termination.")
    ap.add_argument("--regression-threshold-pp", type=float, default=0.5,
                    help="Max allowed negative delta pp before regression brake (default 0.5).")
    ap.add_argument("--worker-runtime", choices=["claude", "goose", "opencode"], default="claude")
    ap.add_argument("--wallclock-hours", type=float,
                    default=float(os.environ.get("OBSESSIVE_WALLCLOCK_HOURS", 24)))
    ap.add_argument("--force-dirty", action="store_true",
                    help="Allow running on a dirty working tree (dangerous).")
    ap.add_argument("--keep-worktree", action="store_true",
                    help="Don't remove the iteration worktree on exit (debug).")
    args = ap.parse_args()

    repo = args.repo.resolve()

    # --- Validate repo ---
    if not repo.is_dir() or not _is_git_repo(repo):
        print(f"error: repo path does not exist or not a git repo: {repo}", file=sys.stderr)
        return 1

    if _is_dirty(repo) and not args.force_dirty:
        print(
            f"error: repo has uncommitted changes. Commit or stash first, or pass --force-dirty.\n"
            f"  repo: {repo}",
            file=sys.stderr,
        )
        return 1

    # --- Build output dir ---
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    repo_basename = repo.name
    branch_name = f"codex/obsessive-{repo_basename}-{ts}"

    out_dir = args.out or (STATE_ROOT / repo_basename / ts)
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- Create worktree ---
    worktree = HOME / ".claude" / "state" / "obsessive-loop" / f"{repo_basename}-{ts}"
    _log(f"Creating worktree at {worktree} on branch {branch_name}")
    try:
        _create_worktree(repo, branch_name, worktree)
    except RuntimeError as exc:
        print(f"error: failed to create worktree: {exc}", file=sys.stderr)
        return 1

    wall_start = time.time()
    started_at = datetime.now(timezone.utc).isoformat()
    summary_data: dict[str, Any] = {
        "repo": str(repo),
        "branch": branch_name,
        "worktree": str(worktree),
        "started_at": started_at,
        "status": "running",
        "terminate_reason": None,
        "iter_count": 0,
        "baseline_weighted_avg": None,
        "final_weighted_avg": None,
        "total_delta": 0.0,
        "iterations": [],
        "worker_runtime": args.worker_runtime,
    }

    try:
        # --- Baseline ---
        _log("Running baseline rubric suite...")
        baseline_file = out_dir / "baseline-scorecard.json"
        baseline = _run_rubric_suite(worktree, baseline_file, bypass="obsessive-baseline")
        if baseline is None:
            _log("WARN: baseline scorecard not produced; using zero baseline")
            baseline = {"merged": {"weightedAverage": 0.0, "allHardGates": [], "anyCriticalGateFailed": True, "maxBand": "Foundational"}}

        baseline_avg = _weighted_avg(baseline)
        summary_data["baseline_weighted_avg"] = baseline_avg
        _log(f"Baseline: weightedAverage={baseline_avg}%")

        prior_scorecard = baseline
        prior_avg = baseline_avg
        no_gain_count = 0
        terminate_reason = "plateau"  # default

        for iter_num in range(1, args.max_iters + 1):
            # --- Wall-clock check ---
            elapsed_h = (time.time() - wall_start) / 3600.0
            if elapsed_h >= args.wallclock_hours:
                _log(f"Wall-clock cap reached ({elapsed_h:.2f}h >= {args.wallclock_hours}h)")
                terminate_reason = "wallclock_cap"
                break

            # --- Check all-gates-pass early exit ---
            if _all_gates_pass_highest_tier(prior_scorecard):
                _log("All hard gates passing at highest tier — done.")
                terminate_reason = "all_gates_pass"
                break

            iter_dir = out_dir / f"iter-{iter_num:04d}"
            iter_dir.mkdir(parents=True, exist_ok=True)

            rubric_key, category_key = _lowest_scoring_category(prior_scorecard)
            objective = (
                f"raise {category_key} in {rubric_key} rubric "
                f"by addressing top failing gate or finding"
            )
            _log(f"--- Iter {iter_num:04d}: {objective}")
            (iter_dir / "objective.txt").write_text(objective)

            # --- Record pre-iteration commit ---
            pre_commit_sha = _current_commit(worktree)

            # --- Spawn auto_runtime track ---
            track_id = _spawn_track(objective, worktree, args.worker_runtime)
            if track_id:
                (iter_dir / "track-id").write_text(track_id)
                _log(f"  track_id={track_id}")
                completed, combined_out = _run_track_to_completion(track_id, worktree, MAX_TRACK_CYCLES)
                _log(f"  track completed={completed}")

                # Rate-limit check (passes obsessive's logger so messages stay tagged)
                ok = _check_rate_limit_signals(combined_out, wall_start, args.wallclock_hours, log_fn=_log)
                if not ok:
                    _log("Wall-clock cap hit after rate-limit sleep — stopping.")
                    terminate_reason = "wallclock_cap"
                    break
            else:
                _log("  WARN: could not init track — skipping iteration scoring")
                combined_out = ""

            # --- Capture diff ---
            diff_file = iter_dir / "diff.patch"
            _make_diff_patch(worktree, pre_commit_sha, diff_file)

            # --- Re-run rubric suite ---
            scorecard_file = iter_dir / "scorecard.json"
            candidate = _run_rubric_suite(worktree, scorecard_file)
            if candidate is None:
                _log("  WARN: iteration scorecard failed — treating as no-gain")
                candidate = prior_scorecard
            candidate_avg = _weighted_avg(candidate)

            delta = candidate_avg - prior_avg
            prior_passing = _passing_gate_ids(prior_scorecard)
            now_passing = _passing_gate_ids(candidate)
            regression_gates = prior_passing - now_passing  # gates that were passing and now fail

            deltas_data = {
                "prior_weighted_avg": prior_avg,
                "candidate_weighted_avg": candidate_avg,
                "delta_pp": delta,
                "regression_gates": list(regression_gates),
                "reverted": False,
            }

            _log(f"  delta={delta:+.2f}pp (prior={prior_avg}% → candidate={candidate_avg}%)")

            # --- Regression brake ---
            regression = False
            if delta < -args.regression_threshold_pp:
                _log(f"  REGRESSION: delta {delta:.2f} < -{args.regression_threshold_pp}pp — reverting")
                regression = True
            if regression_gates:
                _log(f"  REGRESSION: previously-passing gates now failing: {regression_gates}")
                regression = True

            if regression:
                _revert_to_commit(worktree, pre_commit_sha)
                deltas_data["reverted"] = True
                _write_json(iter_dir / "deltas.json", deltas_data)
                iter_record = {
                    "iter": iter_num,
                    "objective": objective,
                    "track_id": track_id,
                    "delta": delta,
                    "weighted_avg_after": prior_avg,
                    "reverted": True,
                    "regression_gates": list(regression_gates),
                }
                summary_data["iterations"].append(iter_record)
                summary_data["iter_count"] = iter_num
                terminate_reason = "regression_brake"
                break

            # --- Update state ---
            _write_json(iter_dir / "deltas.json", deltas_data)
            iter_record = {
                "iter": iter_num,
                "objective": objective,
                "track_id": track_id,
                "delta": delta,
                "weighted_avg_after": candidate_avg,
                "reverted": False,
                "regression_gates": [],
            }
            summary_data["iterations"].append(iter_record)
            summary_data["iter_count"] = iter_num

            prior_scorecard = candidate
            prior_avg = candidate_avg

            # --- Plateau detection ---
            if delta < 0.1:
                no_gain_count += 1
                _log(f"  no-gain count={no_gain_count}/{args.no_gain_cycles}")
                if no_gain_count >= args.no_gain_cycles:
                    _log("Plateau reached — no meaningful gain in recent iterations.")
                    terminate_reason = "plateau"
                    break
            else:
                no_gain_count = 0

        # --- Finalize ---
        final_avg = _weighted_avg(prior_scorecard)
        ended_at = datetime.now(timezone.utc).isoformat()
        total_delta = final_avg - baseline_avg

        summary_data.update({
            "status": "complete",
            "terminate_reason": terminate_reason,
            "ended_at": ended_at,
            "final_weighted_avg": final_avg,
            "total_delta": total_delta,
        })

        _log(f"Loop complete: {terminate_reason} | baseline={baseline_avg}% final={final_avg}% delta={total_delta:+.2f}pp iters={summary_data['iter_count']}")

    except KeyboardInterrupt:
        _log("Interrupted by user.")
        summary_data["status"] = "interrupted"
        summary_data["terminate_reason"] = "user_interrupt"
        summary_data["ended_at"] = datetime.now(timezone.utc).isoformat()
    except Exception as exc:  # noqa: BLE001
        _log(f"Unexpected error: {exc}")
        summary_data["status"] = "error"
        summary_data["terminate_reason"] = f"error: {exc}"
        summary_data["ended_at"] = datetime.now(timezone.utc).isoformat()
    finally:
        # Write outputs regardless
        _write_json(out_dir / "summary.json", summary_data)
        _write_summary_md(out_dir, summary_data)
        _log(f"Summary written to {out_dir}/summary.json")
        # Clean up worktree unless caller asked to keep it for inspection.
        if not args.keep_worktree:
            try:
                _remove_worktree(repo, worktree)
                _log(f"Worktree removed: {worktree}")
            except Exception as exc:  # noqa: BLE001
                _log(f"WARN worktree cleanup failed: {exc}")

    # Determine exit code
    reason = summary_data.get("terminate_reason", "")
    if reason == "regression_brake":
        return 2
    if reason and reason.startswith("error"):
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
