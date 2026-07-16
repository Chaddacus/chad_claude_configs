"""Tests for verifier_shim.py (S3 fleet-hardening).

Run from /Users/chadsimon/.claude/bin/:
    python3 -m unittest verifier_shim_test

Covers: pass/fail exit propagation, fail-closed (no CLAIM on failure), the
honesty label naming the evidence tier, and cite-source selection
(slice-owned --cite-file vs anchor-only auto-discovery).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SHIM = str(Path(__file__).parent / "verifier_shim.py")


def _run_shim(cwd: Path, check: str, cite_files: list[str] | None = None):
    argv = [sys.executable, SHIM, "--check", check]
    for cf in cite_files or []:
        argv += ["--cite-file", cf]
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True)


class VerifierShimTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="shim-"))
        (self.tmp / "owned.py").write_text("x = 1\n")
        (self.tmp / "aaa.txt").write_text("first-alphabetical file\n")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pass_emits_claim_with_honesty_label_and_owned_cite(self) -> None:
        proc = _run_shim(self.tmp, "true", cite_files=["owned.py"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CLAIM: acceptance command passed (exit 0): true", proc.stdout)
        # S3: the CLAIM must name the evidence tier and the cite source.
        self.assertIn("[evidence-tier: exit-code; cite=slice-owned artifact]", proc.stdout)
        self.assertIn("CITE: owned.py:1", proc.stdout)

    def test_pass_without_cite_file_labels_anchor_only(self) -> None:
        proc = _run_shim(self.tmp, "true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("cite=anchor-only", proc.stdout)
        self.assertIn("CITE: aaa.txt:1", proc.stdout)  # deterministic walk

    def test_missing_cite_file_falls_back_to_anchor(self) -> None:
        proc = _run_shim(self.tmp, "true", cite_files=["nope.py"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("cite=anchor-only", proc.stdout)

    def test_failure_exits_nonzero_with_no_claim(self) -> None:
        proc = _run_shim(self.tmp, "exit 7", cite_files=["owned.py"])
        self.assertEqual(proc.returncode, 7)
        self.assertNotIn("CLAIM:", proc.stdout)  # fail closed: no evidence emitted
        self.assertIn("acceptance command failed (exit 7)", proc.stderr)

    def test_check_output_routed_to_stderr(self) -> None:
        # The check's own OUTPUT must go to stderr; stdout carries only
        # CLAIM/CITE lines (the CLAIM legitimately quotes the command TEXT).
        proc = _run_shim(self.tmp, "echo hello-from-check", cite_files=["owned.py"])
        self.assertEqual(proc.returncode, 0)
        for line in proc.stdout.splitlines():
            if line.strip():
                self.assertTrue(line.startswith(("CLAIM:", "CITE:")),
                                f"non-evidence line on stdout: {line!r}")
        self.assertIn("hello-from-check", proc.stderr)


if __name__ == "__main__":
    unittest.main()
