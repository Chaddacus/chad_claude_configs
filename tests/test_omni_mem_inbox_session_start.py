"""Unit tests for omni_mem_inbox_session_start SessionStart hook.

Covers the pure `render_inbox` function (all branches) and, with a mocked
subprocess, exercises the end-to-end `main()` entry point to verify the
hook emits well-formed SessionStart additionalContext.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE_PATH = Path("/Users/chadsimon/.claude/bin/omni_mem_inbox_session_start.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("omni_mem_inbox_session_start", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


MOD = _load_module()


class TestRenderInbox:
    def test_returns_none_for_non_dict(self):
        assert MOD.render_inbox(None) is None
        assert MOD.render_inbox("not a dict") is None
        assert MOD.render_inbox([]) is None

    def test_returns_none_for_missing_tasks_field(self):
        assert MOD.render_inbox({"assignee": "a@b"}) is None

    def test_returns_none_when_no_open_tasks(self):
        report = {
            "assignee": "chad@x",
            "tasks": [
                {"taskId": "t1", "status": "completed", "title": "done", "creator": "m@x", "lastUpdateAt": "2026-04-20T00:00:00Z"},
                {"taskId": "t2", "status": "failed", "title": "oops", "creator": "m@x", "lastUpdateAt": "2026-04-20T01:00:00Z"},
                {"taskId": "t3", "status": "rejected", "title": "nope", "creator": "m@x", "lastUpdateAt": "2026-04-20T02:00:00Z"},
            ],
        }
        assert MOD.render_inbox(report) is None

    def test_filters_terminal_states_and_sorts_desc_by_lastUpdateAt(self):
        report = {
            "assignee": "chad@x",
            "tasks": [
                {"taskId": "t-old", "status": "accepted", "title": "old open", "creator": "m@x", "lastUpdateAt": "2026-04-19T00:00:00Z"},
                {"taskId": "t-done", "status": "completed", "title": "done", "creator": "m@x", "lastUpdateAt": "2026-04-20T10:00:00Z"},
                {"taskId": "t-new", "status": "plan_drafted", "title": "new open", "creator": "m@x", "lastUpdateAt": "2026-04-20T09:00:00Z", "latestNote": "awaiting"},
            ],
        }
        out = MOD.render_inbox(report)
        assert out is not None
        # Header reflects 2 open tasks, not 3.
        assert "2 open task(s)" in out
        # Terminal "done" must not appear.
        assert "t-done" not in out
        # Newer open task (t-new) should appear before older open task (t-old).
        assert out.index("t-new") < out.index("t-old")
        # Note is rendered when present.
        assert "awaiting" in out

    def test_truncates_past_max_entries(self):
        tasks = [
            {"taskId": f"t-{i}", "status": "dispatched", "title": f"task {i}", "creator": "m@x", "lastUpdateAt": f"2026-04-20T{i:02d}:00:00Z"}
            for i in range(12)
        ]
        report = {"assignee": "chad@x", "tasks": tasks}
        out = MOD.render_inbox(report)
        assert out is not None
        # Header reports the full count.
        assert "12 open task(s)" in out
        # Only MAX_ENTRIES entries are rendered; the rest mentioned in a summary line.
        assert out.count("- **[dispatched]**") == MOD.MAX_ENTRIES
        assert f"and {12 - MOD.MAX_ENTRIES} more" in out

    def test_action_hint_tool_names_present(self):
        report = {
            "assignee": "chad@x",
            "tasks": [{"taskId": "t1", "status": "plan_drafted", "title": "x", "creator": "m@x", "lastUpdateAt": "2026-04-20T00:00:00Z"}],
        }
        out = MOD.render_inbox(report)
        assert "list_my_tasks" in out
        assert "record_task_transition" in out


class TestFetchInboxGuards:
    def test_returns_none_when_script_missing(self, tmp_path, monkeypatch):
        # Point OMNI_MEM_REPO_ROOT at a path that doesn't have the CLI — the
        # guard must short-circuit before subprocess runs.
        monkeypatch.setenv("OMNI_MEM_REPO_ROOT", str(tmp_path))
        monkeypatch.setenv("OMNI_MEM_CLOUD_URL", "https://x/omni")
        monkeypatch.setenv("OMNI_MEM_MANAGER_USER_ID", "chad@x")
        assert MOD._fetch_inbox() is None

    def test_returns_none_when_env_missing_and_no_fallback(self, monkeypatch):
        # With the required env absent AND no MCP fallback config available,
        # must return None without attempting to shell out.
        monkeypatch.delenv("OMNI_MEM_CLOUD_URL", raising=False)
        monkeypatch.delenv("OMNI_MEM_MANAGER_USER_ID", raising=False)
        monkeypatch.setenv("OMNI_MEM_REPO_ROOT", "/Users/chadsimon/code/omni-mem")
        monkeypatch.setattr(MOD, "_load_mcp_env_fallback", lambda path=None: {})
        with patch.object(subprocess, "run") as mock_run:
            result = MOD._fetch_inbox()
        assert result is None
        mock_run.assert_not_called()

    def test_uses_mcp_fallback_when_env_missing(self, monkeypatch):
        # Shell env is empty, but the MCP fallback provides both required
        # vars → subprocess should be invoked with the merged env.
        monkeypatch.delenv("OMNI_MEM_CLOUD_URL", raising=False)
        monkeypatch.delenv("OMNI_MEM_MANAGER_USER_ID", raising=False)
        monkeypatch.setenv("OMNI_MEM_REPO_ROOT", "/Users/chadsimon/code/omni-mem")
        monkeypatch.setattr(MOD, "_load_mcp_env_fallback", lambda path=None: {
            "OMNI_MEM_CLOUD_URL": "https://fallback/omni",
            "OMNI_MEM_MANAGER_USER_ID": "fallback-user@x",
        })
        called_with: list[dict[str, str]] = []

        def fake_run(*_args, **kwargs):
            called_with.append(kwargs.get("env") or {})
            return subprocess.CompletedProcess(args=[], returncode=0, stdout='{"assignee":"fallback-user@x","tasks":[]}', stderr="")

        with patch.object(subprocess, "run", side_effect=fake_run):
            result = MOD._fetch_inbox()
        assert result == {"assignee": "fallback-user@x", "tasks": []}
        assert called_with, "subprocess.run should have been invoked"
        assert called_with[0]["OMNI_MEM_CLOUD_URL"] == "https://fallback/omni"
        assert called_with[0]["OMNI_MEM_MANAGER_USER_ID"] == "fallback-user@x"


class TestLoadMcpEnvFallback:
    def test_extracts_env_from_omni_mem_manage_entry(self, tmp_path):
        config = {
            "mcpServers": {
                "other-server": {"env": {"IGNORED": "yes"}},
                "omni-mem-manage": {
                    "command": "npx",
                    "env": {
                        "OMNI_MEM_CLOUD_URL": "https://x/omni",
                        "OMNI_MEM_MANAGER_USER_ID": "chad@x",
                    },
                },
            }
        }
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps(config))
        result = MOD._load_mcp_env_fallback(str(path))
        assert result == {
            "OMNI_MEM_CLOUD_URL": "https://x/omni",
            "OMNI_MEM_MANAGER_USER_ID": "chad@x",
        }

    def test_returns_empty_when_file_missing(self, tmp_path):
        assert MOD._load_mcp_env_fallback(str(tmp_path / "nope.json")) == {}

    def test_returns_empty_when_entry_missing(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"something-else": {}}}))
        assert MOD._load_mcp_env_fallback(str(path)) == {}

    def test_returns_empty_on_malformed_json(self, tmp_path):
        path = tmp_path / "mcp.json"
        path.write_text("not json")
        assert MOD._load_mcp_env_fallback(str(path)) == {}

    def test_returns_none_when_subprocess_fails(self, monkeypatch):
        monkeypatch.setenv("OMNI_MEM_REPO_ROOT", "/Users/chadsimon/code/omni-mem")
        monkeypatch.setenv("OMNI_MEM_CLOUD_URL", "https://x/omni")
        monkeypatch.setenv("OMNI_MEM_MANAGER_USER_ID", "chad@x")
        with patch.object(subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
            assert MOD._fetch_inbox() is None

    def test_returns_none_on_timeout(self, monkeypatch):
        monkeypatch.setenv("OMNI_MEM_REPO_ROOT", "/Users/chadsimon/code/omni-mem")
        monkeypatch.setenv("OMNI_MEM_CLOUD_URL", "https://x/omni")
        monkeypatch.setenv("OMNI_MEM_MANAGER_USER_ID", "chad@x")
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=MOD.TIMEOUT_SECONDS)):
            assert MOD._fetch_inbox() is None

    def test_returns_none_on_invalid_json(self, monkeypatch):
        monkeypatch.setenv("OMNI_MEM_REPO_ROOT", "/Users/chadsimon/code/omni-mem")
        monkeypatch.setenv("OMNI_MEM_CLOUD_URL", "https://x/omni")
        monkeypatch.setenv("OMNI_MEM_MANAGER_USER_ID", "chad@x")
        with patch.object(subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")
            assert MOD._fetch_inbox() is None


class TestMainEndToEnd:
    def test_main_emits_additional_context_for_open_tasks(self, monkeypatch, capsys):
        fake_report = {
            "assignee": "chad@x",
            "tasks": [
                {"taskId": "t1", "status": "plan_drafted", "title": "review this", "creator": "m@x", "lastUpdateAt": "2026-04-20T10:00:00Z"},
            ],
            "counts": {"plan_drafted": 1},
            "notes": [],
        }
        monkeypatch.setattr(MOD, "_fetch_inbox", lambda: fake_report)
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        rc = MOD.main()
        assert rc == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "1 open task(s)" in ctx
        assert "review this" in ctx

    def test_main_is_silent_when_no_open_tasks(self, monkeypatch, capsys):
        # All tasks terminal → render_inbox returns None → main emits nothing.
        fake_report = {"assignee": "chad@x", "tasks": [{"taskId": "t", "status": "completed", "title": "done", "creator": "m@x", "lastUpdateAt": "2026-04-20T00:00:00Z"}]}
        monkeypatch.setattr(MOD, "_fetch_inbox", lambda: fake_report)
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        rc = MOD.main()
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_main_is_silent_when_fetch_fails(self, monkeypatch, capsys):
        monkeypatch.setattr(MOD, "_fetch_inbox", lambda: None)
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        rc = MOD.main()
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out == ""
