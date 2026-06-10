"""Tests for verifier.create_readonly_snapshot, parse_verifier_output,
enforce_citations.

Run from /Users/chadsimon/.claude/bin/:
    python3 -m unittest verifier_test
"""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from unittest import mock

import verifier
from verifier import (
    Citation,
    ClaimEvidence,
    cleanup_snapshot,
    create_readonly_snapshot,
    enforce_citations,
    parse_verifier_output,
    validate_verifier_output,
)


class SnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="verifier-"))
        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "a.py").write_text("def a():\n    return 1\n")
        (self.src / "sub").mkdir()
        (self.src / "sub" / "b.py").write_text("def b():\n    return 2\n")
        (self.src / "README").write_text("# repo\n")

    def tearDown(self) -> None:
        # Be defensive — snapshot may have chmod'd things.
        for p in self.tmp.rglob("*"):
            try:
                if p.is_dir():
                    os.chmod(p, 0o755)
                else:
                    os.chmod(p, 0o644)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_snapshot_creates_readonly_copy(self) -> None:
        r = create_readonly_snapshot(self.src, dest_parent=self.tmp / "snaps")
        self.assertTrue(r.ok, r.error)
        snap = Path(r.path)
        # All expected files present.
        self.assertTrue((snap / "a.py").is_file())
        self.assertTrue((snap / "sub" / "b.py").is_file())
        self.assertTrue((snap / "README").is_file())
        # Files are mode 0444.
        for rel in ("a.py", "sub/b.py", "README"):
            mode = (snap / rel).stat().st_mode & 0o777
            self.assertEqual(mode, 0o444, f"{rel} mode={oct(mode)}")
        # Dirs are 0555.
        for rel in ("", "sub"):
            d = snap / rel if rel else snap
            mode = d.stat().st_mode & 0o777
            self.assertEqual(mode, 0o555, f"{rel} mode={oct(mode)}")

    def test_snapshot_writes_fail(self) -> None:
        r = create_readonly_snapshot(self.src, dest_parent=self.tmp / "snaps")
        self.assertTrue(r.ok, r.error)
        snap = Path(r.path)
        # Write to existing file → fails.
        with self.assertRaises(PermissionError):
            with open(snap / "a.py", "w") as fh:
                fh.write("nope")
        # Append → fails.
        with self.assertRaises(PermissionError):
            with open(snap / "a.py", "a") as fh:
                fh.write("nope")
        # New file in snapshot → fails.
        with self.assertRaises(PermissionError):
            (snap / "evil.py").write_text("rogue")
        # New file in subdir → fails.
        with self.assertRaises(PermissionError):
            (snap / "sub" / "evil.py").write_text("rogue")

    def test_snapshot_does_not_affect_source(self) -> None:
        # Source is still writable after snapshot creation.
        r = create_readonly_snapshot(self.src, dest_parent=self.tmp / "snaps")
        self.assertTrue(r.ok, r.error)
        # Source remains writable.
        (self.src / "c.py").write_text("new\n")  # must not raise
        self.assertEqual((self.src / "c.py").read_text(), "new\n")

    def test_cleanup_snapshot_removes_tree(self) -> None:
        r = create_readonly_snapshot(self.src, dest_parent=self.tmp / "snaps")
        self.assertTrue(r.ok, r.error)
        snap = Path(r.path)
        self.assertTrue(snap.exists())
        ok = cleanup_snapshot(snap)
        self.assertTrue(ok)
        self.assertFalse(snap.exists())

    def test_snapshot_with_symlink_preserves_link(self) -> None:
        # Create a symlink in source pointing to a relative target.
        (self.src / "link.py").symlink_to("a.py")
        r = create_readonly_snapshot(self.src, dest_parent=self.tmp / "snaps")
        self.assertTrue(r.ok, r.error)
        snap = Path(r.path)
        link = snap / "link.py"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "a.py")

    def test_snapshot_missing_source(self) -> None:
        r = create_readonly_snapshot(self.tmp / "nonexistent")
        self.assertFalse(r.ok)
        self.assertIn("not found", r.error)

    # ---- R1 regression: outbound symlinks, chmod failures ---------

    def test_outbound_symlink_rejected(self) -> None:
        """A symlink in the source whose target falls outside the
        source root would let a verifier write to an external file
        through `snapshot/link`. Snapshot creation must refuse.

        Two flavors:
          - relative outbound: `link -> ../outside.txt` (caught by the
            outbound-resolve check).
          - absolute (any path): caught by the absolute-symlink check
            even if the absolute target happens to be inside the source.
        """
        outside = self.tmp / "outside.txt"
        outside.write_text("victim\n")
        # Use a relative path that escapes via .. so we exercise the
        # outbound-resolve branch specifically.
        rel_target = os.path.relpath(str(outside), str(self.src))
        (self.src / "evil_link.txt").symlink_to(rel_target)

        r = create_readonly_snapshot(self.src, dest_parent=self.tmp / "snaps")
        self.assertFalse(r.ok)
        self.assertIn("outbound symlink", r.error)
        # No snapshot directory should have been left behind.
        snaps = self.tmp / "snaps"
        if snaps.exists():
            self.assertEqual(list(snaps.iterdir()), [])

    def test_internal_symlink_allowed(self) -> None:
        """A symlink pointing INSIDE the source is fine."""
        (self.src / "internal_link.py").symlink_to("a.py")
        r = create_readonly_snapshot(self.src, dest_parent=self.tmp / "snaps")
        self.assertTrue(r.ok, r.error)

    def test_absolute_symlink_rejected(self) -> None:
        """An absolute symlink inside source — even if its target is
        another file inside source — must be rejected. After copytree
        the snapshot's symlink would still point to the original path,
        letting a verifier mutate the source through `snapshot/link`.

        R2 regression: pre-fix, the resolved-target-under-source check
        accepted these because the absolute path WAS under source. The
        fix rejects all absolute symlink text.
        """
        target_inside = self.src / "a.py"
        (self.src / "abs_link.py").symlink_to(str(target_inside.resolve()))

        r = create_readonly_snapshot(self.src, dest_parent=self.tmp / "snaps")
        self.assertFalse(r.ok)
        self.assertIn("absolute symlink", r.error)
        # No snapshot left behind.
        snaps = self.tmp / "snaps"
        if snaps.exists():
            self.assertEqual(list(snaps.iterdir()), [])

    def test_symlink_loop_returns_structured_failure(self) -> None:
        """A self-referential symlink (`link -> link`) makes
        Path.resolve() raise RuntimeError. Snapshot creation must
        return SnapshotResult(ok=False) instead of letting the
        exception escape — public API contract is structured result."""
        (self.src / "self.py").symlink_to("self.py")
        r = create_readonly_snapshot(self.src, dest_parent=self.tmp / "snaps")
        self.assertFalse(r.ok)
        self.assertIn("resolution failed", r.error)
        # No leftover snapshot dir.
        snaps = self.tmp / "snaps"
        if snaps.exists():
            self.assertEqual(list(snaps.iterdir()), [])

    def test_absolute_dangling_symlink_rejected(self) -> None:
        """An absolute symlink to a path that doesn't yet exist is also
        rejected — writing through it post-snapshot would create the
        file outside the snapshot boundary."""
        (self.src / "dangling.py").symlink_to("/tmp/nonexistent-target-cp4.py")
        r = create_readonly_snapshot(self.src, dest_parent=self.tmp / "snaps")
        self.assertFalse(r.ok)
        self.assertIn("absolute symlink", r.error)

    def test_chmod_failure_returns_ok_false(self) -> None:
        """If any chmod call in the snapshot walk fails, the function
        must return ok=False and clean up the partial snapshot.

        Pre-R1 fix: per-entry chmod errors were silently swallowed and
        the function returned ok=True with a file still writable.
        """
        real_chmod = os.chmod
        # Path that will be in the snapshot.
        # We'll trigger failure for the FILE chmod on a.py only.
        target_basename = "a.py"

        def selectively_failing_chmod(path, mode, **kwargs):
            p_str = str(path) if not isinstance(path, str) else path
            # Fail when chmodding 0444 on a.py inside a snapshot dir.
            if p_str.endswith("/" + target_basename) and mode == 0o444:
                raise PermissionError("injected chmod failure")
            return real_chmod(path, mode, **kwargs)

        with mock.patch.object(verifier.os, "chmod", side_effect=selectively_failing_chmod):
            r = create_readonly_snapshot(self.src, dest_parent=self.tmp / "snaps")
        self.assertFalse(r.ok)
        self.assertIn("chmod", r.error)
        # No snapshot left behind.
        snaps = self.tmp / "snaps"
        if snaps.exists():
            self.assertEqual(list(snaps.iterdir()), [])


class CitationParseTest(unittest.TestCase):
    def test_parse_single_claim_single_cite(self) -> None:
        text = (
            "CLAIM: Implemented foo\n"
            "CITE: src/foo.py:10-25\n"
        )
        claims = parse_verifier_output(text)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].claim, "Implemented foo")
        self.assertEqual(len(claims[0].citations), 1)
        c = claims[0].citations[0]
        self.assertEqual(c.path, "src/foo.py")
        self.assertEqual((c.line_start, c.line_end), (10, 25))
        self.assertIsNone(c.expected_substring)

    def test_parse_single_line_citation(self) -> None:
        text = "CLAIM: x\nCITE: a.py:42\n"
        claims = parse_verifier_output(text)
        self.assertEqual(claims[0].citations[0].line_start, 42)
        self.assertEqual(claims[0].citations[0].line_end, 42)

    def test_parse_citation_with_substring(self) -> None:
        text = 'CLAIM: x\nCITE: a.py:10-12 "def foo"\n'
        claims = parse_verifier_output(text)
        self.assertEqual(claims[0].citations[0].expected_substring, "def foo")

    def test_parse_multiple_cites_per_claim(self) -> None:
        text = (
            "CLAIM: feature complete\n"
            "CITE: src/a.py:1-10\n"
            "CITE: tests/t.py:5\n"
        )
        claims = parse_verifier_output(text)
        self.assertEqual(len(claims[0].citations), 2)
        self.assertEqual(claims[0].citations[1].path, "tests/t.py")

    def test_parse_multiple_claims(self) -> None:
        text = (
            "CLAIM: a\nCITE: 1.py:1\n"
            "CLAIM: b\nCITE: 2.py:2\n"
        )
        claims = parse_verifier_output(text)
        self.assertEqual(len(claims), 2)
        self.assertEqual(claims[0].claim, "a")
        self.assertEqual(claims[1].claim, "b")

    def test_parse_ignores_non_claim_lines(self) -> None:
        text = (
            "preamble\n"
            "CLAIM: x\n"
            "some commentary\n"
            "CITE: a.py:1\n"
            "trailing\n"
        )
        claims = parse_verifier_output(text)
        self.assertEqual(len(claims), 1)
        self.assertEqual(len(claims[0].citations), 1)

    def test_parse_cite_without_claim_dropped(self) -> None:
        text = "CITE: a.py:1\nCLAIM: x\nCITE: b.py:2\n"
        claims = parse_verifier_output(text)
        self.assertEqual(len(claims), 1)
        # Only the CITE under x counts.
        self.assertEqual(len(claims[0].citations), 1)
        self.assertEqual(claims[0].citations[0].path, "b.py")

    def test_parse_empty_input(self) -> None:
        self.assertEqual(parse_verifier_output(""), [])

    def test_parse_malformed_cite_ignored(self) -> None:
        text = "CLAIM: x\nCITE: bad\nCITE: a.py:5-3\nCITE: a.py:5\n"
        claims = parse_verifier_output(text)
        # `bad` doesn't match the format → ignored.
        # `5-3` matches (start=5, end=3); range validity is checked by enforcer.
        # `a.py:5` matches.
        self.assertEqual(len(claims[0].citations), 2)


class CitationEnforceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="enforce-"))
        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "a.py").write_text(
            "def a():\n"      # line 1
            "    return 1\n"  # line 2
            "\n"              # line 3
            "def b():\n"      # line 4
            "    return 2\n"  # line 5
        )
        (self.src / "subdir").mkdir()
        (self.src / "subdir" / "c.py").write_text("# c\n")
        r = create_readonly_snapshot(self.src, dest_parent=self.tmp / "snaps")
        self.assertTrue(r.ok, r.error)
        self.snap = Path(r.path)

    def tearDown(self) -> None:
        cleanup_snapshot(self.snap)
        for p in self.tmp.rglob("*"):
            try:
                if p.is_dir():
                    os.chmod(p, 0o755)
                else:
                    os.chmod(p, 0o644)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_valid_citation_passes(self) -> None:
        claims = [
            ClaimEvidence(
                claim="x",
                citations=[Citation(path="a.py", line_start=1, line_end=2)],
            )
        ]
        errors, no_cite = enforce_citations(self.snap, claims)
        self.assertEqual(errors, [])
        self.assertEqual(no_cite, [])

    def test_citation_with_correct_substring_passes(self) -> None:
        claims = [
            ClaimEvidence(
                claim="x",
                citations=[
                    Citation(path="a.py", line_start=1, line_end=2, expected_substring="def a"),
                ],
            )
        ]
        errors, _ = enforce_citations(self.snap, claims)
        self.assertEqual(errors, [])

    def test_citation_missing_file(self) -> None:
        claims = [
            ClaimEvidence(claim="x", citations=[Citation(path="nope.py", line_start=1, line_end=1)])
        ]
        errors, _ = enforce_citations(self.snap, claims)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].reason, "file_not_found")

    def test_citation_range_out_of_bounds(self) -> None:
        claims = [
            ClaimEvidence(claim="x", citations=[Citation(path="a.py", line_start=1, line_end=999)])
        ]
        errors, _ = enforce_citations(self.snap, claims)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].reason, "range_invalid")

    def test_citation_range_inverted(self) -> None:
        claims = [
            ClaimEvidence(claim="x", citations=[Citation(path="a.py", line_start=5, line_end=2)])
        ]
        errors, _ = enforce_citations(self.snap, claims)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].reason, "range_invalid")

    def test_citation_substring_not_found(self) -> None:
        claims = [
            ClaimEvidence(
                claim="x",
                citations=[
                    Citation(path="a.py", line_start=1, line_end=2, expected_substring="not in file"),
                ],
            )
        ]
        errors, _ = enforce_citations(self.snap, claims)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].reason, "substring_not_found")

    def test_claim_without_citations_flagged(self) -> None:
        claims = [ClaimEvidence(claim="orphan", citations=[])]
        errors, no_cite = enforce_citations(self.snap, claims)
        self.assertEqual(errors, [])
        self.assertEqual(len(no_cite), 1)
        self.assertEqual(no_cite[0].claim, "orphan")

    def test_subdir_citation_works(self) -> None:
        claims = [
            ClaimEvidence(
                claim="x",
                citations=[Citation(path="subdir/c.py", line_start=1, line_end=1)],
            )
        ]
        errors, _ = enforce_citations(self.snap, claims)
        self.assertEqual(errors, [])

    def test_path_traversal_rejected(self) -> None:
        # Anyone trying to cite outside the snapshot via .. must fail.
        claims = [
            ClaimEvidence(
                claim="evil",
                citations=[Citation(path="../outside.py", line_start=1, line_end=1)],
            )
        ]
        errors, _ = enforce_citations(self.snap, claims)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].reason, "file_not_found")

    def test_absolute_path_rejected(self) -> None:
        claims = [
            ClaimEvidence(
                claim="evil",
                citations=[Citation(path="/etc/passwd", line_start=1, line_end=1)],
            )
        ]
        errors, _ = enforce_citations(self.snap, claims)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].reason, "file_not_found")

    def test_caching_does_not_misreport(self) -> None:
        """Two valid citations against the same file both pass."""
        claims = [
            ClaimEvidence(
                claim="x",
                citations=[
                    Citation(path="a.py", line_start=1, line_end=2),
                    Citation(path="a.py", line_start=4, line_end=5),
                ],
            )
        ]
        errors, _ = enforce_citations(self.snap, claims)
        self.assertEqual(errors, [])

    def test_phantom_trailing_line_rejected(self) -> None:
        """Pre-R1 fix: a 5-line file (`a.py` ends with newline) was
        counted as 6 lines, so `CITE: a.py:6` passed enforcement even
        though there is no real line 6. splitlines() fixes this.
        """
        # a.py has 5 real lines.
        claims = [
            ClaimEvidence(
                claim="phantom",
                citations=[Citation(path="a.py", line_start=6, line_end=6)],
            )
        ]
        errors, _ = enforce_citations(self.snap, claims)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].reason, "range_invalid")


class ValidationTest(unittest.TestCase):
    """Driver-facing validate_verifier_output."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="validate-"))
        src = self.tmp / "src"
        src.mkdir()
        (src / "a.py").write_text("def a():\n    return 1\n")
        r = create_readonly_snapshot(src, dest_parent=self.tmp / "snaps")
        self.assertTrue(r.ok, r.error)
        self.snap = Path(r.path)

    def tearDown(self) -> None:
        cleanup_snapshot(self.snap)
        for p in self.tmp.rglob("*"):
            try:
                if p.is_dir():
                    os.chmod(p, 0o755)
                else:
                    os.chmod(p, 0o644)
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_output_rejected(self) -> None:
        r = validate_verifier_output(self.snap, "")
        self.assertFalse(r.ok)
        self.assertIn("no CLAIM", r.error)

    def test_output_with_only_pass_text_rejected(self) -> None:
        """Verifier saying 'READY' / 'PASS' without structured CLAIM
        lines must be rejected (closure without evidence)."""
        r = validate_verifier_output(self.snap, "READY\nPASS\nverification complete\n")
        self.assertFalse(r.ok)
        self.assertIn("no CLAIM", r.error)

    def test_claim_without_citation_rejected(self) -> None:
        r = validate_verifier_output(self.snap, "CLAIM: I did the work\n")
        self.assertFalse(r.ok)
        self.assertIn("without citations", r.error)

    def test_claim_with_invalid_citation_rejected(self) -> None:
        r = validate_verifier_output(
            self.snap,
            "CLAIM: x\nCITE: nope.py:1\n",
        )
        self.assertFalse(r.ok)
        self.assertIn("citation error", r.error)

    def test_claim_with_valid_citation_passes(self) -> None:
        r = validate_verifier_output(
            self.snap,
            'CLAIM: a is implemented\nCITE: a.py:1-2 "def a():"\n',
        )
        self.assertTrue(r.ok, r.error)
        self.assertEqual(len(r.claims), 1)


class IntegrationTest(unittest.TestCase):
    def test_parse_then_enforce_real_output(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="integ-"))
        try:
            src = tmp / "src"
            src.mkdir()
            (src / "foo.py").write_text(
                "def foo(x: int) -> int:\n"  # line 1
                "    return x + 1\n"          # line 2
            )
            r = create_readonly_snapshot(src, dest_parent=tmp / "snaps")
            self.assertTrue(r.ok, r.error)
            snap = Path(r.path)
            try:
                verifier_output = (
                    "Verifier summary:\n"
                    'CLAIM: foo is implemented with int type signature\n'
                    'CITE: foo.py:1 "def foo(x: int)"\n'
                    'CLAIM: missing evidence claim\n'
                )
                claims = parse_verifier_output(verifier_output)
                errors, no_cite = enforce_citations(snap, claims)
                self.assertEqual(errors, [])
                self.assertEqual(len(no_cite), 1)
                self.assertEqual(no_cite[0].claim, "missing evidence claim")
            finally:
                cleanup_snapshot(snap)
        finally:
            for p in tmp.rglob("*"):
                try:
                    if p.is_dir():
                        os.chmod(p, 0o755)
                    else:
                        os.chmod(p, 0o644)
                except OSError:
                    pass
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
