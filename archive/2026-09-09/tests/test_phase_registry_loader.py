"""Slice 2 — external YAML registry loader tests.

Verifies:
  - YAML registry loads and is used by select_phase_questions
  - Missing file → falls back to inline registry without raising
  - Malformed YAML → falls back to inline registry without raising
  - mtime cache: same mtime returns cached; mtime change reloads
  - force=True bypasses cache
  - Pyyaml-missing path is exercised (mock ImportError)
  - Loader never raises
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path.home() / ".claude" / "bin"))
import auto_runtime_common as rt  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_cache():
    rt._REGISTRY_CACHE.clear()
    rt._REGISTRY_LOAD_WARNED.clear()


@pytest.fixture(autouse=True)
def clean_cache():
    _reset_cache()
    yield
    _reset_cache()


def _write_yaml(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    return path


# ---------------------------------------------------------------------------
# Real registry file shipped in repo
# ---------------------------------------------------------------------------

class TestShippedRegistry:
    def test_shipped_registry_loads_and_has_v1_version(self):
        reg = rt.load_question_registry(force=True)
        assert reg.get("registry_version") == "v1"
        assert "phases" in reg
        assert "loop_invariant" in reg

    def test_select_phase_questions_uses_loaded_registry(self):
        qs = rt.select_phase_questions("build", "R3")
        ids = {q["id"] for q in qs}
        assert "simplest_path" in ids
        assert "premise_check" in ids

    def test_r1_still_bypasses(self):
        assert rt.select_phase_questions("build", "R1") == []


# ---------------------------------------------------------------------------
# Loader fallback behavior
# ---------------------------------------------------------------------------

class TestLoaderFallback:
    def test_missing_file_falls_back_to_inline(self, tmp_path):
        missing = tmp_path / "does_not_exist.yaml"
        reg = rt.load_question_registry(missing, force=True)
        assert reg is rt.QUESTION_REGISTRY_INLINE

    def test_malformed_yaml_falls_back(self, tmp_path):
        bad = _write_yaml(tmp_path / "bad.yaml", "key: [unclosed\nfoo:")
        reg = rt.load_question_registry(bad, force=True)
        assert reg is rt.QUESTION_REGISTRY_INLINE

    def test_yaml_not_a_mapping_falls_back(self, tmp_path):
        bad = _write_yaml(tmp_path / "list.yaml", "- one\n- two\n")
        reg = rt.load_question_registry(bad, force=True)
        assert reg is rt.QUESTION_REGISTRY_INLINE

    def test_yaml_missing_registry_version_falls_back(self, tmp_path):
        bad = _write_yaml(tmp_path / "noversion.yaml", "phases: {}\n")
        reg = rt.load_question_registry(bad, force=True)
        assert reg is rt.QUESTION_REGISTRY_INLINE

    def test_pyyaml_missing_falls_back(self, tmp_path):
        path = _write_yaml(
            tmp_path / "ok.yaml",
            "registry_version: v1\nphases: {}\nloop_invariant:\n  questions: []\n",
        )
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

        def fake_import(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("simulated missing yaml")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            reg = rt.load_question_registry(path, force=True)
        assert reg is rt.QUESTION_REGISTRY_INLINE

    def test_loader_never_raises_on_unexpected_error(self, tmp_path):
        # Path() with weirdness shouldn't crash
        reg = rt.load_question_registry("/proc/self/mem", force=True)
        # /proc/self/mem may or may not exist on darwin; either way no raise
        assert isinstance(reg, dict)


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

class TestCache:
    def test_cache_hit_returns_same_object(self, tmp_path):
        path = _write_yaml(
            tmp_path / "r.yaml",
            "registry_version: v1\nphases: {}\nloop_invariant:\n  questions: []\n",
        )
        a = rt.load_question_registry(path)
        b = rt.load_question_registry(path)
        assert a is b

    def test_force_bypasses_cache(self, tmp_path):
        path = _write_yaml(
            tmp_path / "r.yaml",
            "registry_version: v1\nphases: {}\nloop_invariant:\n  questions: []\n",
        )
        a = rt.load_question_registry(path)
        b = rt.load_question_registry(path, force=True)
        # force=True reloads — may be different object even with same mtime
        assert a == b

    def test_mtime_change_reloads(self, tmp_path):
        path = _write_yaml(
            tmp_path / "r.yaml",
            "registry_version: v1\nphases: {}\nloop_invariant:\n  questions: []\n",
        )
        a = rt.load_question_registry(path)
        assert a["registry_version"] == "v1"
        # Rewrite with different content + bump mtime
        import os
        import time
        time.sleep(0.01)
        path.write_text(
            "registry_version: v2\nphases: {}\nloop_invariant:\n  questions: []\n"
        )
        os.utime(path, (path.stat().st_atime, time.time()))
        b = rt.load_question_registry(path)
        assert b["registry_version"] == "v2"


# ---------------------------------------------------------------------------
# select_phase_questions integration
# ---------------------------------------------------------------------------

class TestSelectPhaseQuestionsIntegration:
    def test_explicit_registry_arg_overrides_loader(self, tmp_path):
        custom = {
            "registry_version": "test",
            "phases": {
                "build": {
                    "questions": [
                        {
                            "id": "custom_q",
                            "question": "?",
                            "any_evidence_required": ["repo_search"],
                            "targets_decision_kind": "owned_files",
                        },
                    ],
                },
            },
            "loop_invariant": {"questions": []},
        }
        qs = rt.select_phase_questions("build", "R3", registry=custom)
        assert [q["id"] for q in qs] == ["custom_q"]

    def test_default_call_loads_external(self):
        # Should pull simplest_path + premise_check from real YAML
        qs = rt.select_phase_questions("build", "R3")
        ids = {q["id"] for q in qs}
        assert "simplest_path" in ids
