"""Slice 2 — registry_lint.py schema validation tests."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path.home() / ".claude" / "bin"))
import registry_lint as lint  # noqa: E402


REGISTRY = Path.home() / ".claude" / "policy" / "phase_questions.yaml"
LINT_BIN = Path.home() / ".claude" / "bin" / "registry_lint.py"


def _good_question(qid="q1", target="owned_files"):
    return {
        "id": qid,
        "question": "Is X true?",
        "any_evidence_required": ["repo_search"],
        "targets_decision_kind": target,
    }


def _minimal_registry(**overrides):
    base = {
        "registry_version": "v1",
        "phases": {
            "build": {"questions": [_good_question()]},
        },
        "loop_invariant": {
            "triggers": {"event_count_since_last": 5, "on_route_promotion": True},
            "max_invariant_tokens": 400,
            "questions": [_good_question("inv1", "next_action")],
        },
    }
    base.update(overrides)
    return base


class TestShippedRegistryLints:
    def test_real_registry_is_clean(self):
        data = yaml.safe_load(REGISTRY.read_text())
        assert lint.lint_registry(data) == []

    def test_cli_exits_zero_on_real_registry(self):
        result = subprocess.run(
            [sys.executable, str(LINT_BIN), str(REGISTRY)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr


class TestMissingRegistryVersion:
    def test_missing_registry_version(self):
        reg = _minimal_registry()
        del reg["registry_version"]
        errs = lint.lint_registry(reg)
        assert any("registry_version" in e for e in errs)

    def test_empty_registry_version(self):
        errs = lint.lint_registry(_minimal_registry(registry_version=""))
        assert any("registry_version" in e for e in errs)


class TestPhaseValidation:
    def test_unknown_phase_name(self):
        reg = _minimal_registry()
        reg["phases"]["bogus_phase"] = {"questions": [_good_question("q2")]}
        errs = lint.lint_registry(reg)
        assert any("unknown phase name" in e for e in errs)

    def test_phase_block_not_mapping(self):
        reg = _minimal_registry()
        reg["phases"]["build"] = "not a dict"
        errs = lint.lint_registry(reg)
        assert any("must be a mapping" in e for e in errs)


class TestQuestionValidation:
    def test_missing_id(self):
        reg = _minimal_registry()
        del reg["phases"]["build"]["questions"][0]["id"]
        errs = lint.lint_registry(reg)
        assert any("id" in e for e in errs)

    def test_duplicate_id_across_blocks(self):
        reg = _minimal_registry()
        reg["loop_invariant"]["questions"][0]["id"] = "q1"  # collides with build q1
        errs = lint.lint_registry(reg)
        assert any("duplicate question id" in e for e in errs)

    def test_unknown_targets_decision_kind(self):
        reg = _minimal_registry()
        reg["phases"]["build"]["questions"][0]["targets_decision_kind"] = "scope"
        # scope is in plan-final but not in OBSERVABLE_DECISION_KINDS (Slice 1b)
        errs = lint.lint_registry(reg)
        assert any("targets_decision_kind" in e for e in errs)

    def test_empty_any_evidence_required(self):
        reg = _minimal_registry()
        reg["phases"]["build"]["questions"][0]["any_evidence_required"] = []
        errs = lint.lint_registry(reg)
        assert any("any_evidence_required" in e for e in errs)

    def test_invalid_route_in_skip_when(self):
        reg = _minimal_registry()
        reg["phases"]["build"]["questions"][0]["skip_when"] = {"route_in": ["R9"]}
        errs = lint.lint_registry(reg)
        assert any("invalid route" in e for e in errs)

    def test_skip_when_unknown_key(self):
        reg = _minimal_registry()
        reg["phases"]["build"]["questions"][0]["skip_when"] = {"only_for": ["R3"]}
        errs = lint.lint_registry(reg)
        assert any("unknown key" in e for e in errs)


class TestLoopInvariantValidation:
    def test_unknown_trigger_key(self):
        reg = _minimal_registry()
        reg["loop_invariant"]["triggers"]["bogus_trigger"] = True
        errs = lint.lint_registry(reg)
        assert any("unknown key: bogus_trigger" in e for e in errs)

    def test_zero_max_invariant_tokens(self):
        reg = _minimal_registry()
        reg["loop_invariant"]["max_invariant_tokens"] = 0
        errs = lint.lint_registry(reg)
        assert any("max_invariant_tokens" in e for e in errs)

    def test_negative_max_invariant_tokens(self):
        reg = _minimal_registry()
        reg["loop_invariant"]["max_invariant_tokens"] = -1
        errs = lint.lint_registry(reg)
        assert any("max_invariant_tokens" in e for e in errs)


class TestRootValidation:
    def test_root_must_be_mapping(self):
        errs = lint.lint_registry(["not", "a", "mapping"])
        assert any("must be a mapping" in e for e in errs)


class TestCLI:
    def test_cli_exits_nonzero_on_bad_registry(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("phases: {}\n")  # missing registry_version
        result = subprocess.run(
            [sys.executable, str(LINT_BIN), str(bad)],
            capture_output=True, text=True,
        )
        assert result.returncode == 1
        assert "registry_lint" in result.stderr

    def test_cli_exits_one_on_missing_file(self, tmp_path):
        result = subprocess.run(
            [sys.executable, str(LINT_BIN), str(tmp_path / "nope.yaml")],
            capture_output=True, text=True,
        )
        assert result.returncode == 1
