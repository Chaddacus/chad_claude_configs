#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import common  # noqa: E402
from common import canonical_python_argv, resolve_python_3_11_bin  # noqa: E402


class PythonRuntimeCanonicalizationTests(unittest.TestCase):
    def tearDown(self) -> None:
        resolve_python_3_11_bin.cache_clear()

    def test_canonical_python_argv_uses_python_3_11_for_child_process(self) -> None:
        completed = subprocess.run(
            canonical_python_argv(
                "-c",
                (
                    "import json, sys; "
                    "print(json.dumps({'major': sys.version_info.major, 'minor': sys.version_info.minor}))"
                ),
            ),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["major"], 3)
        self.assertGreaterEqual(payload["minor"], 11)

    def test_resolve_python_3_11_bin_fails_closed_when_no_candidate_available(self) -> None:
        resolve_python_3_11_bin.cache_clear()
        with (
            mock.patch.dict(os.environ, {common.PLANNING_GATE_PYTHON_ENV_VAR: ""}, clear=False),
            mock.patch.object(common, "_python_candidate_supports_required_version", return_value=False),
            mock.patch.object(common.shutil, "which", return_value=None),
            mock.patch.object(common.sys, "executable", "/tmp/fake-python"),
        ):
            with self.assertRaisesRegex(RuntimeError, "could not resolve a canonical Python 3.11 interpreter"):
                resolve_python_3_11_bin()


if __name__ == "__main__":
    unittest.main()
