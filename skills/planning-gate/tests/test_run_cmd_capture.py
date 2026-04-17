#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_cmd_capture.py"


class RunCmdCaptureTests(unittest.TestCase):
    def test_invalid_cwd_returns_structured_blocked_json(self) -> None:
        proc = subprocess.run(
            [
                "python3.11",
                str(SCRIPT),
                "--track-id",
                "t1",
                "--stage",
                "25%",
                "--name",
                "bad-cwd",
                "--cwd",
                "/path/does/not/exist",
                "--",
                "python3.11",
                "-c",
                "print('ok')",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        payload = json.loads(proc.stdout.strip())
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["error"], "invalid_cwd")

    def test_dangerous_command_blocked_without_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = subprocess.run(
                [
                    "python3.11",
                    str(SCRIPT),
                    "--track-id",
                    "t2",
                    "--stage",
                    "25%",
                    "--name",
                    "danger",
                    "--cwd",
                    td,
                    "--",
                    "git",
                    "reset",
                    "--hard",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        payload = json.loads(proc.stdout.strip())
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["error"], "dangerous_command_blocked")

    def test_timeout_marks_capture_failed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = subprocess.run(
                [
                    "python3.11",
                    str(SCRIPT),
                    "--track-id",
                    "t3",
                    "--stage",
                    "50%",
                    "--name",
                    "timeout-case",
                    "--cwd",
                    td,
                    "--timeout-sec",
                    "1",
                    "--",
                    "python3.11",
                    "-c",
                    "import time; time.sleep(2)",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        payload = json.loads(proc.stdout.strip())
        self.assertEqual(proc.returncode, 124)
        self.assertEqual(payload["status"], "fail")
        self.assertTrue(payload["timeout_exceeded"])


if __name__ == "__main__":
    unittest.main()
