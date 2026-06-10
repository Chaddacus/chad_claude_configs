#!/usr/bin/env python3
"""Slice V — validation tooling for the stage-aware orchestrator loop.

Subcommands (plan-final §7):
  slice-gate         L1+L2 fixture gate: track events match fixture expectations
  coverage-matrix    L2: every architecture-claim row has a passing fixture
  baseline-capture   L3: snapshot per-fixture event-log baselines under bench/baselines/<sha>/
  shadow-compare     L3: diff a real track event log vs a shadow track event log
  hypothesis-check   L3: evaluate a pre-registered hypothesis from hypotheses.yaml
  postmortem         L3 (H-ε): warning-sign-ratio audit across failure tracks

Exit codes:
  0 = check passed / report printed
  1 = check failed / hypothesis killed
  2 = invocation error (bad args, missing file)

This script is intentionally independent of auto_runtime.py — measurement
vs execution concern.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

CLAUDE_HOME = Path.home() / ".claude"
BENCH_DIR = CLAUDE_HOME / "bench"
POLICY_DIR = CLAUDE_HOME / "policy"
CORPUS = POLICY_DIR / "fixtures" / "phase_loop_corpus.jsonl"
HYPOTHESES = POLICY_DIR / "hypotheses.yaml"
COVERAGE_MATRIX = BENCH_DIR / "coverage_matrix.md"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"{path}:{i}: invalid JSON: {e}", file=sys.stderr)
                sys.exit(2)
    return out


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore
    except ImportError:
        print("pyyaml required", file=sys.stderr)
        sys.exit(2)
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load_corpus() -> list[dict[str, Any]]:
    if not CORPUS.exists():
        print(f"corpus not found: {CORPUS}", file=sys.stderr)
        sys.exit(2)
    return _load_jsonl(CORPUS)


# ---------------------------------------------------------------------------
# slice-gate
# ---------------------------------------------------------------------------

def cmd_slice_gate(args: argparse.Namespace) -> int:
    """Verify a track's event log matches its fixture's expected_decisions
    and expected_phase_path. Exit 0 = pass, 1 = fail."""
    fixture_id = args.task_id
    log_path = Path(args.track_log)
    if not log_path.exists():
        print(f"track log not found: {log_path}", file=sys.stderr)
        return 2
    events = _load_jsonl(log_path)
    corpus = _load_corpus()
    fixture = next((f for f in corpus if f["task_id"] == fixture_id), None)
    if not fixture:
        print(f"fixture not found in corpus: {fixture_id}", file=sys.stderr)
        return 2

    failures: list[str] = []

    # Expected phase path
    actual_phases = [e["to_phase"] for e in events if e.get("event") == "phase_changed"]
    expected = fixture.get("expected_phase_path", [])
    if actual_phases != expected:
        failures.append(
            f"phase path mismatch: expected={expected} actual={actual_phases}"
        )

    # Expected decisions
    decision_events = [e for e in events if e.get("event") == "decision_record"]
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for d in decision_events:
        by_kind.setdefault(d.get("decision_kind", ""), []).append(d)

    for exp in fixture.get("expected_decisions", []):
        kind = exp["kind"]
        observed = by_kind.get(kind, [])
        if not observed:
            failures.append(f"no decision_record observed for kind={kind}")
            continue
        # Last observed decision is authoritative for changed state
        last = observed[-1]
        if "expected_changed" in exp:
            if bool(last.get("changed")) != bool(exp["expected_changed"]):
                failures.append(
                    f"kind={kind} expected_changed={exp['expected_changed']} "
                    f"actual={last.get('changed')}"
                )
        if exp.get("expected_changed") is False:
            if not last.get("no_change_reason"):
                failures.append(f"kind={kind} changed=false but no_change_reason missing")
        min_evidence = exp.get("min_evidence_types", [])
        if min_evidence and bool(last.get("changed")):
            evidence_present = set(last.get("evidence_types", []) or [])
            missing = [m for m in min_evidence if m not in evidence_present]
            if missing:
                failures.append(
                    f"kind={kind} missing evidence types: {missing}"
                )

    if failures:
        print(f"slice-gate FAIL for {fixture_id}:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"slice-gate ok for {fixture_id}")
    return 0


# ---------------------------------------------------------------------------
# coverage-matrix
# ---------------------------------------------------------------------------

def cmd_coverage_matrix(args: argparse.Namespace) -> int:
    """Verify every architecture-claim row in coverage_matrix.md has a
    fixture that exists in the corpus."""
    if not COVERAGE_MATRIX.exists():
        print(f"coverage matrix not found: {COVERAGE_MATRIX}", file=sys.stderr)
        return 2
    corpus = _load_corpus()
    known_ids = {f["task_id"] for f in corpus}
    failures: list[str] = []
    rows = 0
    for line in COVERAGE_MATRIX.read_text().splitlines():
        line = line.strip()
        # Expect rows like: `| claim text | fixture-task-id |`
        if not line.startswith("|") or line.startswith("| ---") or line.startswith("|---"):
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) < 2 or parts[0].lower() in ("claim", "architecture claim"):
            continue
        claim, fid = parts[0], parts[1]
        if not claim or not fid:
            continue
        rows += 1
        if fid not in known_ids:
            failures.append(f"row '{claim}' references unknown fixture: {fid}")
    if failures:
        print("coverage-matrix FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"coverage-matrix ok ({rows} claim rows checked)")
    return 0


# ---------------------------------------------------------------------------
# baseline-capture
# ---------------------------------------------------------------------------

def cmd_baseline_capture(args: argparse.Namespace) -> int:
    """Snapshot a track event log into bench/baselines/<git_sha>/<task_id>.jsonl."""
    log_path = Path(args.track_log)
    if not log_path.exists():
        print(f"track log not found: {log_path}", file=sys.stderr)
        return 2
    sha = args.git_sha
    if not sha:
        print("--git-sha required", file=sys.stderr)
        return 2
    out_dir = BENCH_DIR / "baselines" / sha
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{args.task_id}.jsonl"
    if target.exists() and not args.force:
        print(f"baseline already exists (use --force to overwrite): {target}", file=sys.stderr)
        return 1
    target.write_text(log_path.read_text())
    print(f"baseline captured: {target}")
    return 0


# ---------------------------------------------------------------------------
# shadow-compare
# ---------------------------------------------------------------------------

def cmd_shadow_compare(args: argparse.Namespace) -> int:
    """Diff a real track's events vs a shadow track's events, focused on
    decision divergence."""
    real = _load_jsonl(Path(args.real_track_log))
    shadow = _load_jsonl(Path(args.shadow_track_log))
    real_decisions = [
        (e.get("decision_kind"), e.get("after_state_hash"))
        for e in real if e.get("event") == "decision_record"
    ]
    shadow_decisions = [
        (s.get("decision_kind"), s.get("would_emit_phase_changed"))
        for s in shadow if s.get("event") == "shadow_decision"
    ]
    real_phase_path = [e.get("to_phase") for e in real if e.get("event") == "phase_changed"]
    shadow_would = [
        s.get("to_phase") for s in shadow
        if s.get("event") == "shadow_decision"
        and s.get("would_emit_phase_changed") is True
    ]
    report = {
        "real_decision_count": len(real_decisions),
        "shadow_decision_count": len(shadow_decisions),
        "real_phase_path": real_phase_path,
        "shadow_would_emit_phases": shadow_would,
        "phase_path_diverged": real_phase_path != shadow_would,
    }
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0


# ---------------------------------------------------------------------------
# hypothesis-check
# ---------------------------------------------------------------------------

def cmd_hypothesis_check(args: argparse.Namespace) -> int:
    """Evaluate a pre-registered hypothesis from policy/hypotheses.yaml
    against a directory of track event logs."""
    if not HYPOTHESES.exists():
        print(f"hypotheses file not found: {HYPOTHESES}", file=sys.stderr)
        return 2
    hyps = _load_yaml(HYPOTHESES)
    hyp = next(
        (h for h in hyps.get("hypotheses", []) if h.get("id") == args.hypothesis),
        None,
    )
    if not hyp:
        print(f"unknown hypothesis: {args.hypothesis}", file=sys.stderr)
        return 2
    log_dir = Path(args.logs_dir)
    if not log_dir.is_dir():
        print(f"logs dir not found: {log_dir}", file=sys.stderr)
        return 2

    # Simple implementations of metrics referenced by H-α/H-ε. Others stub a
    # not_implemented marker so the harness fails loudly rather than silently.
    logs = [_load_jsonl(p) for p in sorted(log_dir.glob("*.jsonl"))]
    hid = args.hypothesis

    if hid == "H-alpha":
        # median decision_record events per task where changed=true
        counts = []
        for events in logs:
            counts.append(sum(
                1 for e in events
                if e.get("event") == "decision_record" and e.get("changed")
            ))
        if not counts:
            metric_value = 0
        else:
            counts.sort()
            mid = len(counts) // 2
            metric_value = (
                counts[mid] if len(counts) % 2 == 1
                else (counts[mid - 1] + counts[mid]) / 2
            )
    elif hid == "H-epsilon":
        # ratio of failure tracks with at least one warning sign
        warning_events = {"unknown_failure", "phase_transition_blocked",
                          "decision_record"}  # crude proxy
        if not logs:
            metric_value = 0.0
        else:
            with_signal = 0
            for events in logs:
                if any(
                    e.get("event") == "verifier_classified"
                    and e.get("classification") == "unknown_failure"
                    for e in events
                ) or any(
                    e.get("event") == "phase_transition_blocked" for e in events
                ):
                    with_signal += 1
            metric_value = with_signal / len(logs)
    else:
        print(f"metric for {hid} not implemented in this Slice V version",
              file=sys.stderr)
        return 2

    report = {
        "hypothesis": hid,
        "metric_value": metric_value,
        "kill_threshold": hyp.get("kill_threshold"),
        "pass_threshold": hyp.get("pass_threshold"),
        "log_count": len(logs),
    }
    # Determine pass/fail/kill — H-α: kill if <1 across ≥10 runs; pass if ≥2
    killed = False
    if hid == "H-alpha":
        if len(logs) >= 10 and metric_value < 1:
            killed = True
    elif hid == "H-epsilon":
        if len(logs) >= 10 and metric_value < 0.30:
            killed = True
    report["killed"] = killed
    json.dump(report, sys.stdout, indent=2)
    print()
    return 1 if killed else 0


# ---------------------------------------------------------------------------
# postmortem
# ---------------------------------------------------------------------------

def cmd_postmortem(args: argparse.Namespace) -> int:
    """H-ε audit: ratio of failure tracks whose event log contains a
    retroactively-identifiable warning sign (unknown_failure, phase
    oscillation, decision churn)."""
    log_dir = Path(args.logs_dir)
    if not log_dir.is_dir():
        print(f"logs dir not found: {log_dir}", file=sys.stderr)
        return 2
    logs = list(sorted(log_dir.glob("*.jsonl")))
    if not logs:
        print("no logs found", file=sys.stderr)
        return 2

    with_signal = 0
    per_track: list[dict[str, Any]] = []
    for p in logs:
        events = _load_jsonl(p)
        signals = []
        if any(e.get("event") == "verifier_classified"
               and e.get("classification") == "unknown_failure" for e in events):
            signals.append("unknown_failure")
        if any(e.get("event") == "phase_transition_blocked" for e in events):
            signals.append("phase_transition_blocked")
        phase_seq = [e.get("to_phase") for e in events
                     if e.get("event") == "phase_changed"]
        # Phase oscillation: any phase appears more than once non-adjacent
        if len(phase_seq) != len(set(phase_seq)):
            signals.append("phase_oscillation")
        decisions = [e for e in events if e.get("event") == "decision_record"]
        if len(decisions) >= 5:
            kinds = Counter(d.get("decision_kind") for d in decisions)
            if max(kinds.values()) >= 3:
                signals.append("decision_churn")
        if signals:
            with_signal += 1
        per_track.append({"file": p.name, "signals": signals})
    ratio = with_signal / len(logs)
    report = {
        "warning_sign_ratio": ratio,
        "track_count": len(logs),
        "tracks_with_signal": with_signal,
        "per_track": per_track,
    }
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="analyze")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("slice-gate")
    p.add_argument("--task-id", required=True)
    p.add_argument("--track-log", required=True)
    p.set_defaults(func=cmd_slice_gate)

    p = sub.add_parser("coverage-matrix")
    p.set_defaults(func=cmd_coverage_matrix)

    p = sub.add_parser("baseline-capture")
    p.add_argument("--task-id", required=True)
    p.add_argument("--track-log", required=True)
    p.add_argument("--git-sha", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_baseline_capture)

    p = sub.add_parser("shadow-compare")
    p.add_argument("--real-track-log", required=True)
    p.add_argument("--shadow-track-log", required=True)
    p.set_defaults(func=cmd_shadow_compare)

    p = sub.add_parser("hypothesis-check")
    p.add_argument("--hypothesis", required=True,
                   help="hypothesis id from policy/hypotheses.yaml (e.g., H-alpha)")
    p.add_argument("--logs-dir", required=True)
    p.set_defaults(func=cmd_hypothesis_check)

    p = sub.add_parser("postmortem")
    p.add_argument("--logs-dir", required=True)
    p.set_defaults(func=cmd_postmortem)

    args = parser.parse_args(argv[1:])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
