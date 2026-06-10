"""Slice V — analyze.py validation tooling tests.

Covers each subcommand:
  slice-gate, coverage-matrix, baseline-capture, shadow-compare,
  hypothesis-check, postmortem
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ANALYZE = Path.home() / ".claude" / "bench" / "analyze.py"
CORPUS = Path.home() / ".claude" / "policy" / "fixtures" / "phase_loop_corpus.jsonl"


def _run(*args, expect=None):
    r = subprocess.run(
        [sys.executable, str(ANALYZE), *args],
        capture_output=True, text=True,
    )
    if expect is not None:
        assert r.returncode == expect, (
            f"got exit {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )
    return r


def _write_jsonl(path: Path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


# ===========================================================================
# coverage-matrix
# ===========================================================================

class TestCoverageMatrix:
    def test_shipped_matrix_is_clean(self):
        r = _run("coverage-matrix", expect=0)
        assert "ok" in r.stdout

    def test_shipped_matrix_references_only_real_fixtures(self):
        # Sanity — every fixture in the matrix exists in the corpus
        import json
        ids = set()
        for line in CORPUS.read_text().splitlines():
            if line.strip():
                ids.add(json.loads(line)["task_id"])
        matrix = (Path.home() / ".claude" / "bench" / "coverage_matrix.md").read_text()
        for line in matrix.splitlines():
            if line.startswith("|") and "claim" not in line.lower() and "---" not in line:
                parts = [p.strip() for p in line.strip("|").split("|")]
                if len(parts) >= 2 and parts[1] and "fixture" not in parts[1].lower():
                    assert parts[1] in ids, f"matrix references missing fixture: {parts[1]}"


# ===========================================================================
# slice-gate
# ===========================================================================

class TestSliceGate:
    def test_missing_task_id_returns_2(self, tmp_path):
        log = tmp_path / "log.jsonl"
        log.write_text("")
        r = _run("slice-gate", "--task-id", "does-not-exist",
                 "--track-log", str(log), expect=2)

    def test_r1_lookup_phase_path_empty_passes(self, tmp_path):
        # r1-lookup-1 expects no phase_changed events
        log = tmp_path / "log.jsonl"
        _write_jsonl(log, [
            {"event": "cycle_completed", "cycle": 0},
        ])
        r = _run("slice-gate", "--task-id", "r1-lookup-1",
                 "--track-log", str(log), expect=0)

    def test_phase_path_mismatch_fails(self, tmp_path):
        # r2-impl-1 expects [build, verify, closeout]
        log = tmp_path / "log.jsonl"
        _write_jsonl(log, [
            {"event": "phase_changed", "to_phase": "build"},
            # Missing verify and closeout
        ])
        r = _run("slice-gate", "--task-id", "r2-impl-1",
                 "--track-log", str(log), expect=1)
        assert "phase path mismatch" in r.stdout

    def test_decision_record_changed_true_pass(self, tmp_path):
        # neg-1 expects owned_files changed=false with reason
        log = tmp_path / "log.jsonl"
        _write_jsonl(log, [
            {"event": "phase_changed", "to_phase": "build"},
            {"event": "phase_changed", "to_phase": "verify"},
            {"event": "phase_changed", "to_phase": "closeout"},
            {"event": "decision_record", "decision_kind": "owned_files",
             "changed": False, "no_change_reason": "single_file_already_specified"},
        ])
        r = _run("slice-gate", "--task-id", "neg-1",
                 "--track-log", str(log), expect=0)

    def test_decision_record_changed_mismatch_fails(self, tmp_path):
        # neg-1 expects owned_files changed=false, supply changed=true
        log = tmp_path / "log.jsonl"
        _write_jsonl(log, [
            {"event": "phase_changed", "to_phase": "build"},
            {"event": "phase_changed", "to_phase": "verify"},
            {"event": "phase_changed", "to_phase": "closeout"},
            {"event": "decision_record", "decision_kind": "owned_files",
             "changed": True},
        ])
        r = _run("slice-gate", "--task-id", "neg-1",
                 "--track-log", str(log), expect=1)
        assert "expected_changed" in r.stdout

    def test_changed_false_without_no_change_reason_fails(self, tmp_path):
        log = tmp_path / "log.jsonl"
        _write_jsonl(log, [
            {"event": "phase_changed", "to_phase": "build"},
            {"event": "phase_changed", "to_phase": "verify"},
            {"event": "phase_changed", "to_phase": "closeout"},
            {"event": "decision_record", "decision_kind": "owned_files",
             "changed": False, "no_change_reason": ""},
        ])
        r = _run("slice-gate", "--task-id", "neg-1",
                 "--track-log", str(log), expect=1)
        assert "no_change_reason missing" in r.stdout


# ===========================================================================
# baseline-capture
# ===========================================================================

class TestBaselineCapture:
    def test_capture_writes_file(self, tmp_path, monkeypatch):
        # Redirect bench to tmp
        monkeypatch.setenv("HOME", str(tmp_path))
        # Need to rebuild the script's BENCH_DIR reference — bench dir under tmp
        bench = tmp_path / ".claude" / "bench"
        bench.mkdir(parents=True)
        log = tmp_path / "log.jsonl"
        _write_jsonl(log, [{"event": "cycle_completed", "cycle": 0}])
        r = _run("baseline-capture", "--task-id", "test-1",
                 "--track-log", str(log), "--git-sha", "abc123def", expect=0)
        target = bench / "baselines" / "abc123def" / "test-1.jsonl"
        assert target.exists()

    def test_no_overwrite_without_force(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        bench = tmp_path / ".claude" / "bench"
        bench.mkdir(parents=True)
        log = tmp_path / "log.jsonl"
        _write_jsonl(log, [{"event": "cycle_completed"}])
        _run("baseline-capture", "--task-id", "test-1",
             "--track-log", str(log), "--git-sha", "sha1", expect=0)
        r = _run("baseline-capture", "--task-id", "test-1",
                 "--track-log", str(log), "--git-sha", "sha1", expect=1)
        assert "already exists" in r.stderr


# ===========================================================================
# shadow-compare
# ===========================================================================

class TestShadowCompare:
    def test_shadow_vs_real_phase_divergence(self, tmp_path):
        real = tmp_path / "real.jsonl"
        shadow = tmp_path / "shadow.jsonl"
        _write_jsonl(real, [
            {"event": "phase_changed", "to_phase": "verify"},
        ])
        _write_jsonl(shadow, [
            {"event": "shadow_decision", "decision_kind": "phase_transition",
             "would_emit_phase_changed": True, "to_phase": "verify"},
        ])
        r = _run("shadow-compare", "--real-track-log", str(real),
                 "--shadow-track-log", str(shadow), expect=0)
        report = json.loads(r.stdout)
        assert report["phase_path_diverged"] is False

    def test_shadow_blocked_vs_real_allowed(self, tmp_path):
        real = tmp_path / "real.jsonl"
        shadow = tmp_path / "shadow.jsonl"
        _write_jsonl(real, [{"event": "phase_changed", "to_phase": "verify"}])
        _write_jsonl(shadow, [
            {"event": "shadow_decision", "decision_kind": "phase_transition",
             "would_emit_phase_changed": False, "reason": "verifier_matrix_blocked"},
        ])
        r = _run("shadow-compare", "--real-track-log", str(real),
                 "--shadow-track-log", str(shadow), expect=0)
        report = json.loads(r.stdout)
        assert report["phase_path_diverged"] is True


# ===========================================================================
# hypothesis-check
# ===========================================================================

class TestHypothesisCheck:
    def test_h_alpha_pass_when_decisions_present(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        # 2 logs each with 2 changed decisions → median 2 ≥ pass threshold
        for i in range(2):
            _write_jsonl(log_dir / f"t{i}.jsonl", [
                {"event": "decision_record", "changed": True, "decision_kind": "phase"},
                {"event": "decision_record", "changed": True, "decision_kind": "owned_files"},
            ])
        r = _run("hypothesis-check", "--hypothesis", "H-alpha",
                 "--logs-dir", str(log_dir), expect=0)
        report = json.loads(r.stdout)
        assert report["metric_value"] == 2

    def test_h_alpha_kill_when_below_threshold_with_enough_runs(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        # 10 logs with 0 changed decisions → median 0 < kill threshold
        for i in range(10):
            _write_jsonl(log_dir / f"t{i}.jsonl", [
                {"event": "cycle_completed", "cycle": 0},
            ])
        r = _run("hypothesis-check", "--hypothesis", "H-alpha",
                 "--logs-dir", str(log_dir), expect=1)
        report = json.loads(r.stdout)
        assert report["killed"] is True

    def test_unknown_hypothesis_returns_2(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        r = _run("hypothesis-check", "--hypothesis", "H-omega",
                 "--logs-dir", str(log_dir), expect=2)


# ===========================================================================
# postmortem
# ===========================================================================

class TestPostmortem:
    def test_postmortem_detects_warning_signs(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _write_jsonl(log_dir / "fail1.jsonl", [
            {"event": "verifier_classified", "classification": "unknown_failure",
             "command_id": "ruff_check"},
        ])
        _write_jsonl(log_dir / "fail2.jsonl", [
            {"event": "phase_transition_blocked", "reason": "verifier_matrix_blocked"},
        ])
        _write_jsonl(log_dir / "clean.jsonl", [
            {"event": "cycle_completed", "cycle": 0},
        ])
        r = _run("postmortem", "--logs-dir", str(log_dir), expect=0)
        report = json.loads(r.stdout)
        assert report["track_count"] == 3
        assert report["tracks_with_signal"] == 2
        assert report["warning_sign_ratio"] == pytest.approx(2 / 3)

    def test_postmortem_detects_phase_oscillation(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        _write_jsonl(log_dir / "osc.jsonl", [
            {"event": "phase_changed", "to_phase": "build"},
            {"event": "phase_changed", "to_phase": "verify"},
            {"event": "phase_changed", "to_phase": "build"},  # ← back
        ])
        r = _run("postmortem", "--logs-dir", str(log_dir), expect=0)
        report = json.loads(r.stdout)
        assert "phase_oscillation" in report["per_track"][0]["signals"]
