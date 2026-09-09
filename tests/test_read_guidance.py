"""Verify excerpt boundaries and failure behavior before using packets as review context."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("read_guidance", Path(__file__).resolve().parents[1] / "scripts/read-guidance.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GuidanceExcerptTests(unittest.TestCase):
    """Guard against dropped introductory rules, unrelated sections, and ambiguous input."""

    def test_exact_excerpt_preserves_intro_and_fenced_headings(self):
        """A selected section includes its intro and code, but excludes neighboring policy."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "coding.md").write_text("# Code\nGeneral rule.\n## One\nFirst.\n```md\n## Fake\n```\n## Two\nSecond.\n")
            result = MODULE.render(["coding:One", "coding:One"], root)
            self.assertIn("General rule.", result)
            self.assertIn("## Fake", result)
            self.assertNotIn("Second.", result)
            self.assertEqual(result.count("First."), 1)
            self.assertIn("SHA-256:", result)
            self.assertIn("Lines 3-7:", result)

    def test_fence_with_info_suffix_does_not_close_code_block(self):
        """A marker with trailing text remains code, so its following heading is not a section."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "coding.md").write_text("# Code\n## Real\n~~~text\n~~~still-code\n## Fake\n~~~\n## Next\nOther.\n")
            result = MODULE.render(["coding:Real"], root)
            self.assertIn("## Fake", result)
            self.assertNotIn("Other.", result)
            with self.assertRaises(ValueError):
                MODULE.render(["coding:Fake"], root)

    def test_invalid_or_ambiguous_requests_fail(self):
        """Typos, path traversal, and duplicate headings must not yield misleading excerpts."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "coding.md").write_text("## Same\nA\n## Same\nB\n")
            for selectors in [["coding:Same"], ["coding:Missing"], ["../coding:Same"], ["missing:Same"]]:
                with self.subTest(selectors=selectors), self.assertRaises(ValueError):
                    MODULE.render(selectors, root)

    def test_all_live_sections_are_retrievable(self):
        """Every current heading must resolve uniquely against the maintained guide tree."""
        for source in MODULE.GUIDANCE.glob("*.md"):
            _, spans = MODULE.sections(source.read_text())
            for heading, _, _ in spans:
                with self.subTest(guide=source.stem, heading=heading):
                    self.assertIn("## " + heading, MODULE.render([source.stem + ":" + heading]))


if __name__ == "__main__":
    unittest.main()
