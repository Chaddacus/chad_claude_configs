"""omni-mem checkpoint emission on slice boundaries.

Covers the deterministic boundary -> checkpoint wiring added to update_node_state:
the helper gates on OMNI_MEM_CHECKPOINTS_ENABLED (default off so existing tracks
are unchanged), builds the right CLI invocation (container vs local), and never
raises on failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path.home() / ".claude" / "bin"))
import auto_runtime_common as rt  # noqa: E402


NODE = {"kind": "slice", "title": "do the thing"}


class TestOmniMemCheckpoint:
    def test_disabled_by_default_no_subprocess(self, monkeypatch):
        monkeypatch.delenv("OMNI_MEM_CHECKPOINTS_ENABLED", raising=False)
        with patch.object(rt.subprocess, "run") as run:
            out = rt._omni_mem_checkpoint("trk-1", "s1", NODE, "/repos/acme", "slice_complete")
        assert out == {"status": "skipped", "reason": "disabled"}
        run.assert_not_called()

    def test_enabled_records_with_container_invocation(self, monkeypatch):
        monkeypatch.setenv("OMNI_MEM_CHECKPOINTS_ENABLED", "1")
        monkeypatch.delenv("OMNI_MEM_CONTAINER", raising=False)  # default 'omni-mem'
        proc = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch.object(rt.subprocess, "run", return_value=proc) as run:
            out = rt._omni_mem_checkpoint("trk-1", "s1", NODE, "/repos/acme", "slice_complete")
        assert out["status"] == "recorded"
        assert out["workspace_id"] == "acme"  # cwd basename
        assert out["boundary"] == "slice_complete"
        cmd = run.call_args[0][0]
        assert cmd[:3] == ["docker", "exec", "omni-mem"]
        assert "generate_checkpoint" in cmd
        assert cmd[cmd.index("--workspaceId") + 1] == "acme"
        assert cmd[cmd.index("--sessionId") + 1] == "trk-1"
        assert cmd[cmd.index("--boundary") + 1] == "slice_complete"

    def test_local_fallback_when_container_blank(self, monkeypatch):
        monkeypatch.setenv("OMNI_MEM_CHECKPOINTS_ENABLED", "true")
        monkeypatch.setenv("OMNI_MEM_CONTAINER", "")
        proc = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch.object(rt.subprocess, "run", return_value=proc) as run:
            rt._omni_mem_checkpoint("trk-1", "s1", NODE, "/repos/acme", "escalation")
        cmd = run.call_args[0][0]
        assert cmd[0] != "docker"
        assert cmd[1] == "generate_checkpoint"

    def test_nonzero_exit_is_error_not_raise(self, monkeypatch):
        monkeypatch.setenv("OMNI_MEM_CHECKPOINTS_ENABLED", "yes")
        proc = MagicMock(returncode=1, stdout="", stderr="unknown command")
        with patch.object(rt.subprocess, "run", return_value=proc):
            out = rt._omni_mem_checkpoint("trk-1", "s1", NODE, "/repos/acme", "escalation")
        assert out["status"] == "error"
        assert out["reason"] == "cli_failed"

    def test_subprocess_exception_is_swallowed(self, monkeypatch):
        monkeypatch.setenv("OMNI_MEM_CHECKPOINTS_ENABLED", "1")
        with patch.object(rt.subprocess, "run", side_effect=FileNotFoundError("no docker")):
            out = rt._omni_mem_checkpoint("trk-1", "s1", NODE, "/repos/acme", "slice_complete")
        assert out["status"] == "error"
        assert out["reason"] == "FileNotFoundError"

    def test_blank_cwd_defaults_workspace_global(self, monkeypatch):
        monkeypatch.setenv("OMNI_MEM_CHECKPOINTS_ENABLED", "1")
        proc = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch.object(rt.subprocess, "run", return_value=proc) as run:
            out = rt._omni_mem_checkpoint("trk-1", "s1", NODE, "", "slice_complete")
        assert out["workspace_id"] == "global"
        cmd = run.call_args[0][0]
        assert cmd[cmd.index("--workspaceId") + 1] == "global"
