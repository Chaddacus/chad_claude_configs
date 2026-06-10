#!/usr/bin/env python3
"""operator_benchmark.py — Chad-vs-team operator benchmark harness.

Runs the same prompt against a target repo in isolated worktrees, scores each
via run_rubric_suite.py, and emits a delta report.

Spec: ~/.claude/plans/users-chadsimon-thoughts-md-take-a-giggly-moore.md (slice 5)

Usage:
    operator_benchmark.py --target <repo-path> \
        --operators <id1>:<prompt-file1> [<id2>:<prompt-file2> ...] \
        [--out <dir>] [--runtime claude|goose|opencode]

Exit codes:
    0  — all operators ran and scorecard emitted
    1  — one or more operator runs failed (scorecard still emitted with partial data)
    3  — consent file absent (template emitted; populate and re-run)
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

HOME = Path(os.path.expanduser("~"))
BENCH_STATE = HOME / ".claude" / "state" / "operator-benchmark"
CONSENT_FILE = BENCH_STATE / ".operator-benchmark-consent.json"
RUBRIC_RUNNER = HOME / ".claude" / "bin" / "run_rubric_suite.py"
GOOSE_DISPATCH = HOME / ".claude" / "bin" / "goose_dispatch.py"

# Regex constants for prompt-shape and behavior metrics
RE_CONSTRAINT = re.compile(r"\b(must|never|always|do not|don't|MUST|NEVER)\b")
RE_EXAMPLE = re.compile(r"^\s*(?:Example|For example|e\.g\.)", re.MULTILINE)
RE_NEGATION = re.compile(r"\b(do not|don't|never|NEVER|avoid|prohibited)\b", re.IGNORECASE)
RE_HEDGE = re.compile(r"\b(should work|probably|seems|might|likely|I think|maybe)\b", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_cmd(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int = 1800,
    capture: bool = True,
) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError as exc:
        return -1, "", f"FileNotFoundError: {exc}"
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout}s"


# ---------------------------------------------------------------------------
# Consent gate
# ---------------------------------------------------------------------------


def check_consent(operator_ids: list[str]) -> None:
    """Halt with exit 3 if consent file is absent; validate operator ids if present."""
    if not CONSENT_FILE.exists():
        BENCH_STATE.mkdir(parents=True, exist_ok=True)
        template = {
            "consent_version": "1.0",
            "operators": [
                {
                    "id": oid,
                    "scope": "prompt-traces-only",
                    "consented_at": "<ISO-8601>",
                }
                for oid in operator_ids
            ],
        }
        CONSENT_FILE.write_text(json.dumps(template, indent=2))
        print(
            f"consent file required — populate {CONSENT_FILE} then re-run",
            file=sys.stderr,
        )
        sys.exit(3)

    try:
        consent = json.loads(CONSENT_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"error: consent file unreadable: {exc}", file=sys.stderr)
        sys.exit(3)

    consented_ids = {op["id"] for op in consent.get("operators", [])}
    for oid in operator_ids:
        if oid not in consented_ids:
            print(
                f"error: operator '{oid}' not in consent file {CONSENT_FILE}. "
                "Add entry with consented_at timestamp and re-run.",
                file=sys.stderr,
            )
            sys.exit(3)


# ---------------------------------------------------------------------------
# Prompt analysis
# ---------------------------------------------------------------------------


def analyze_prompt(prompt_text: str) -> dict[str, Any]:
    words = prompt_text.split()
    token_estimate = int(len(words) * 1.3)
    return {
        "token_estimate": token_estimate,
        "constraint_count": len(RE_CONSTRAINT.findall(prompt_text)),
        "example_count": len(RE_EXAMPLE.findall(prompt_text)),
        "negation_count": len(RE_NEGATION.findall(prompt_text)),
        "hedge_count": len(RE_HEDGE.findall(prompt_text)),
    }


# ---------------------------------------------------------------------------
# Worktree management
# ---------------------------------------------------------------------------


def create_worktree(target: Path, run_id: str, operator_id: str) -> Path:
    """Create a git worktree for one operator run. Returns the worktree path."""
    worktree_dir = BENCH_STATE / run_id / operator_id
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    branch = f"codex/op-bench-{run_id}-{operator_id}"

    # Remove stale worktree at same path if it exists (from interrupted prior run)
    if worktree_dir.exists():
        run_cmd(["git", "worktree", "remove", "--force", str(worktree_dir)], cwd=target)

    rc, out, err = run_cmd(
        ["git", "worktree", "add", "-b", branch, str(worktree_dir), "HEAD"],
        cwd=target,
        timeout=60,
    )
    if rc != 0:
        raise RuntimeError(f"git worktree add failed (exit {rc}): {err.strip()}")
    return worktree_dir


def remove_worktree(target: Path, worktree_dir: Path) -> None:
    run_cmd(["git", "worktree", "remove", "--force", str(worktree_dir)], cwd=target, timeout=30)
    run_cmd(["git", "worktree", "prune"], cwd=target, timeout=30)


# ---------------------------------------------------------------------------
# Runtime dispatch
# ---------------------------------------------------------------------------


def dispatch_claude(prompt: str, worktree: Path, transcript_path: Path) -> tuple[int, str]:
    """Run prompt via `claude -p`. Returns (exit_code, combined_output)."""
    rc, out, err = run_cmd(
        ["claude", "-p", prompt],
        cwd=worktree,
        timeout=1800,
    )
    combined = f"=== STDOUT ===\n{out}\n=== STDERR ===\n{err}"
    transcript_path.write_text(combined)
    return rc, combined


def dispatch_goose(prompt: str, worktree: Path, transcript_path: Path) -> tuple[int, str]:
    """Route via goose_dispatch.py with a synthetic slice spec."""
    if not GOOSE_DISPATCH.exists():
        return -1, f"goose_dispatch.py not found at {GOOSE_DISPATCH}"

    rc, out, err = run_cmd(
        [
            "python3",
            str(GOOSE_DISPATCH),
            "--slice-id", "op-bench",
            "--brief", prompt,
            "--verify", "echo ok",
            "--cwd", str(worktree),
        ],
        cwd=worktree,
        timeout=1800,
    )
    combined = f"=== STDOUT ===\n{out}\n=== STDERR ===\n{err}"
    transcript_path.write_text(combined)
    return rc, combined


def dispatch_opencode(prompt: str, worktree: Path, transcript_path: Path) -> tuple[int, str]:
    """Route via anthropic-concurrency-system if available; fall back to claude."""
    # Check if opencode runner exists via PATH
    import shutil
    runner = shutil.which("opencode")
    if runner:
        rc, out, err = run_cmd(["opencode", "--prompt", prompt], cwd=worktree, timeout=1800)
        note = ""
    else:
        note = "[opencode runtime not found — fell back to claude]\n"
        rc, out, err = run_cmd(["claude", "-p", prompt], cwd=worktree, timeout=1800)
    combined = f"{note}=== STDOUT ===\n{out}\n=== STDERR ===\n{err}"
    transcript_path.write_text(combined)
    return rc, combined


# ---------------------------------------------------------------------------
# Diff capture
# ---------------------------------------------------------------------------


def capture_diff(worktree: Path, base_sha: str | None = None) -> str:
    """Diff the worktree against base_sha (the SHA at worktree creation).
    Falls back to `git diff HEAD` if base_sha is None — but that misses
    operator-made commits, so callers should pass base_sha when possible."""
    ref = base_sha or "HEAD"
    rc, out, err = run_cmd(["git", "diff", ref], cwd=worktree, timeout=60)
    if rc != 0:
        return f"# git diff failed: {err}"
    return out


def count_files_touched(diff_text: str) -> int:
    """Count distinct files in a unified diff."""
    return len(set(re.findall(r"^\+\+\+ b/(.+)$", diff_text, re.MULTILINE)))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def run_rubric(worktree: Path, out_dir: Path, bypass_reason: str) -> dict[str, Any]:
    scorecard_path = out_dir / "scorecard.json"
    rc, out, err = run_cmd(
        [
            "python3",
            str(RUBRIC_RUNNER),
            "--repo", str(worktree),
            "--rubric-bypass", bypass_reason,
            "--out", str(scorecard_path),
        ],
        timeout=900,
    )
    if scorecard_path.exists():
        try:
            return json.loads(scorecard_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"error": f"rubric runner exit {rc}: {err[-400:] if err else 'no output'}"}


def extract_scorecard_metrics(scorecard: dict[str, Any]) -> dict[str, Any]:
    merged = scorecard.get("merged", {})
    return {
        "weighted_average": merged.get("weightedAverage", 0.0),
        "min_band": merged.get("minBand", "unknown"),
        "max_band": merged.get("maxBand", "unknown"),
        "any_critical_gate_failed": merged.get("anyCriticalGateFailed", False),
        "rubric_count": merged.get("rubricCount", 0),
    }


# ---------------------------------------------------------------------------
# Per-operator run
# ---------------------------------------------------------------------------


def run_operator(
    operator_id: str,
    prompt_file: Path,
    target: Path,
    run_id: str,
    out_root: Path,
    runtime: str,
) -> dict[str, Any]:
    per_op_dir = out_root / "per-operator" / operator_id
    per_op_dir.mkdir(parents=True, exist_ok=True)

    prompt_text = prompt_file.read_text()
    prompt_dest = per_op_dir / "prompt.txt"
    prompt_dest.write_text(prompt_text)

    prompt_metrics = analyze_prompt(prompt_text)
    transcript_path = per_op_dir / "transcript.log"

    # Create isolated worktree
    try:
        worktree = create_worktree(target, run_id, operator_id)
    except RuntimeError as exc:
        return {
            "operator_id": operator_id,
            "ok": False,
            "error": str(exc),
            "prompt_metrics": prompt_metrics,
        }

    # Record the start-commit so capture_diff catches operator commits, not
    # just unstaged work.
    rc, base_sha, _ = run_cmd(["git", "rev-parse", "HEAD"], cwd=worktree, timeout=10)
    base_sha = base_sha.strip() if rc == 0 else None

    wall_start = time.monotonic()

    # Dispatch
    if runtime == "goose":
        exit_code, transcript = dispatch_goose(prompt_text, worktree, transcript_path)
    elif runtime == "opencode":
        exit_code, transcript = dispatch_opencode(prompt_text, worktree, transcript_path)
    else:
        exit_code, transcript = dispatch_claude(prompt_text, worktree, transcript_path)

    wall_seconds = round(time.monotonic() - wall_start, 1)

    # Capture diff vs the recorded start-commit (catches operator commits)
    diff_text = capture_diff(worktree, base_sha)
    diff_path = per_op_dir / "diff.patch"
    diff_path.write_text(diff_text)
    files_touched = count_files_touched(diff_text)

    # Score
    scorecard = run_rubric(worktree, per_op_dir, f"operator-benchmark/{run_id}/{operator_id}")
    score_metrics = extract_scorecard_metrics(scorecard)

    # Persist scorecard copy
    sc_path = per_op_dir / "scorecard.json"
    if not sc_path.exists():
        sc_path.write_text(json.dumps(scorecard, indent=2))

    metrics = {
        "operator_id": operator_id,
        "run_id": run_id,
        "runtime": runtime,
        "prompt_file": str(prompt_file),
        # Process
        "wall_seconds": wall_seconds,
        "exit_code": exit_code,
        # Prompt shape
        **prompt_metrics,
        # Behavior
        "files_touched": files_touched,
        # Outcome
        **score_metrics,
        # Timestamps
        "generated_at": utc_now(),
        "ok": True,
    }
    metrics_path = per_op_dir / "metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2))

    # Clean up worktree (leave branch for audit; prune separately)
    remove_worktree(target, worktree)

    return metrics


# ---------------------------------------------------------------------------
# Delta report
# ---------------------------------------------------------------------------


def compute_delta(baseline: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """Compute numeric deltas from baseline to other; non-numeric fields skipped."""
    numeric_keys = [
        "weighted_average", "wall_seconds", "token_estimate",
        "constraint_count", "example_count", "negation_count",
        "hedge_count", "files_touched",
    ]
    return {k: round(other.get(k, 0) - baseline.get(k, 0), 3) for k in numeric_keys}


def build_summary_md(run_id: str, results: list[dict[str, Any]], out_root: Path) -> str:
    lines = [
        f"# Operator Benchmark — {run_id}",
        "",
        f"Generated: {utc_now()}",
        "",
        "## Delta Table",
        "",
    ]
    header_cols = [
        "operator", "runtime", "exit", "wall_s", "tokens",
        "constraints", "examples", "negations", "hedges",
        "files_touched", "weighted_avg", "min_band", "max_band",
    ]
    lines.append("| " + " | ".join(header_cols) + " |")
    lines.append("| " + " | ".join(["---"] * len(header_cols)) + " |")

    baseline = results[0] if results else {}
    for i, r in enumerate(results):
        if not r.get("ok"):
            row = [r.get("operator_id", "?"), "—", "ERR"] + ["—"] * (len(header_cols) - 3)
            lines.append("| " + " | ".join(str(v) for v in row) + " |")
            continue

        delta_suffix = ""
        if i > 0 and baseline.get("ok"):
            d = compute_delta(baseline, r)
            delta_suffix = f" (Δ={d['weighted_average']:+.1f}%)"

        row = [
            r["operator_id"],
            r["runtime"],
            str(r["exit_code"]),
            str(r["wall_seconds"]),
            str(r["token_estimate"]),
            str(r["constraint_count"]),
            str(r["example_count"]),
            str(r["negation_count"]),
            str(r["hedge_count"]),
            str(r["files_touched"]),
            f"{r['weighted_average']}{delta_suffix}",
            r["min_band"],
            r["max_band"],
        ]
        lines.append("| " + " | ".join(str(v) for v in row) + " |")

    lines += [
        "",
        "## Per-Operator Artifacts",
        "",
    ]
    for r in results:
        oid = r.get("operator_id", "?")
        lines.append(f"- `per-operator/{oid}/` — prompt.txt, transcript.log, diff.patch, scorecard.json, metrics.json")

    return "\n".join(lines)


def build_delta_json(run_id: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = results[0] if results and results[0].get("ok") else None
    deltas = []
    for i, r in enumerate(results):
        entry: dict[str, Any] = {"operator_id": r.get("operator_id"), "metrics": r}
        if i > 0 and baseline:
            entry["delta_vs_baseline"] = compute_delta(baseline, r)
        deltas.append(entry)
    return {
        "run_id": run_id,
        "generated_at": utc_now(),
        "baseline_operator": baseline["operator_id"] if baseline else None,
        "operators": deltas,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Operator benchmark: run per-operator prompts against a target repo and diff the results."
    )
    ap.add_argument("--target", required=True, type=Path, help="Path to the target git repo.")
    ap.add_argument(
        "--operators",
        required=True,
        nargs="+",
        metavar="ID:PROMPT_FILE",
        help="One or more operator specs in the form <id>:<prompt-file>.",
    )
    ap.add_argument("--out", type=Path, default=None, help="Output directory (default: state dir for run-id).")
    ap.add_argument(
        "--runtime",
        choices=["claude", "goose", "opencode"],
        default="claude",
        help="Execution runtime (default: claude).",
    )
    args = ap.parse_args()

    # Parse operator specs
    operator_specs: list[tuple[str, Path]] = []
    for spec in args.operators:
        if ":" not in spec:
            print(f"error: operator spec must be ID:FILE, got '{spec}'", file=sys.stderr)
            return 1
        oid, pfile = spec.split(":", 1)
        ppath = Path(pfile).expanduser().resolve()
        if not ppath.exists():
            print(f"error: prompt file not found: {ppath}", file=sys.stderr)
            return 1
        operator_specs.append((oid, ppath))

    operator_ids = [oid for oid, _ in operator_specs]

    # Authority gate
    check_consent(operator_ids)

    # Validate target
    target = args.target.resolve()
    if not target.is_dir():
        print(f"error: --target {target} is not a directory", file=sys.stderr)
        return 1
    rc, _, err = run_cmd(["git", "rev-parse", "--git-dir"], cwd=target, timeout=5)
    if rc != 0:
        print(f"error: --target {target} is not a git repo: {err.strip()}", file=sys.stderr)
        return 1

    # Rubric runner check
    if not RUBRIC_RUNNER.exists():
        print(f"error: run_rubric_suite.py not found at {RUBRIC_RUNNER}", file=sys.stderr)
        return 1

    # Build run-id and output dir
    run_id = "bench-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_root = args.out if args.out else (BENCH_STATE / run_id)
    out_root.mkdir(parents=True, exist_ok=True)

    print(f"operator-benchmark: run_id={run_id} target={target} runtime={args.runtime}")
    print(f"  operators: {', '.join(operator_ids)}")
    print(f"  output:    {out_root}")

    # Run each operator sequentially (worktrees on same repo; avoid branch conflicts)
    results: list[dict[str, Any]] = []
    any_failed = False
    for oid, pfile in operator_specs:
        print(f"  [operator={oid}] dispatching ...", flush=True)
        result = run_operator(oid, pfile, target, run_id, out_root, args.runtime)
        results.append(result)
        if not result.get("ok"):
            any_failed = True
            print(f"  [operator={oid}] FAILED: {result.get('error', '?')}", file=sys.stderr)
        else:
            print(
                f"  [operator={oid}] done — wall={result['wall_seconds']}s "
                f"score={result['weighted_average']}% exit={result['exit_code']}"
            )

    # Emit summary.md
    summary_md = build_summary_md(run_id, results, out_root)
    summary_path = out_root / "summary.md"
    summary_path.write_text(summary_md)

    # Emit delta.json
    delta = build_delta_json(run_id, results)
    delta_path = out_root / "delta.json"
    delta_path.write_text(json.dumps(delta, indent=2))

    print(f"\noperator-benchmark complete:")
    print(f"  summary.md  → {summary_path}")
    print(f"  delta.json  → {delta_path}")

    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
