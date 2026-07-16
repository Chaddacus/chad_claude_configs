"""Tests for hook_profile's gating truth (2026-07-16 audit H5/C2).

Run from /Users/chadsimon/.claude/bin/:
    python3 -m unittest hook_profile_test

Invariants pinned:
  1. Every id listed in a PROFILES set corresponds to a real script that
     calls should_run("<id>") — phantom ids gate nothing and mislead readers
     (the audit found 7, including codex_review_gate for a script that never
     existed).
  2. classify_prompt is in every profile — it is the only route-file writer,
     so excluding it deadlocks the session in that profile (finding C2).
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hook_profile

BIN = Path(__file__).parent
GOVERN_SCRIPTS = BIN.parent / "skills" / "govern" / "scripts"
# Three call shapes exist in this runtime (the first sweep missed the last
# two and wrongly pruned four REAL ids — 2026-07-16 incident):
SHOULD_RUN_LITERAL_RE = re.compile(r'should_run\(\s*["\']([\w-]+)["\']\s*\)')
SHOULD_RUN_FSTRING_RE = re.compile(r'should_run\(\s*f["\']([\w-]+)\{')
SHOULD_RUN_VAR_RE = re.compile(r"should_run\(\s*([A-Z_][A-Z0-9_]*)\s*\)")


def _declared_ids() -> set[str]:
    """Every id some script actually gates itself with — all call shapes."""
    ids: set[str] = set()
    for d in (BIN, GOVERN_SCRIPTS):
        for py in d.glob("*.py"):
            if py.name == Path(__file__).name:
                continue  # this file's own pattern docs aren't declarations
            try:
                text = py.read_text(encoding="utf-8")
            except OSError:
                continue
            ids.update(SHOULD_RUN_LITERAL_RE.findall(text))
            # f-string ids: should_run(f"prefix_{var}") — declare every
            # PROFILES id that starts with the prefix (completion_gate_stop
            # and completion_gate_task both come from one call site).
            for prefix in SHOULD_RUN_FSTRING_RE.findall(text):
                for hooks in hook_profile.PROFILES.values():
                    if hooks:
                        ids.update(h for h in hooks if h.startswith(prefix))
            # variable ids: should_run(CONST) — resolve CONST = "..." locally.
            for var in SHOULD_RUN_VAR_RE.findall(text):
                m = re.search(rf'^{var}\s*=\s*["\']([\w-]+)["\']', text, re.M)
                if m:
                    ids.add(m.group(1))
    return ids


class ProfileTruthTest(unittest.TestCase):
    def test_no_phantom_ids(self):
        declared = _declared_ids()
        for profile, hooks in hook_profile.PROFILES.items():
            if hooks is None:  # strict = everything
                continue
            phantoms = set(hooks) - declared
            self.assertFalse(
                phantoms,
                f"profile {profile!r} lists ids no script gates itself with: "
                f"{sorted(phantoms)} — either add should_run() to the script "
                f"or remove the id")

    def test_classifier_in_every_profile(self):
        for profile, hooks in hook_profile.PROFILES.items():
            if hooks is None:
                continue
            self.assertIn(
                "classify_prompt", hooks,
                f"profile {profile!r} excludes the route-file writer (deadlock)")

    def test_safety_tripwires_in_every_profile(self):
        for tripwire in ("pre_tool_guard", "secret_leak_warn", "web_search_breaker"):
            for profile, hooks in hook_profile.PROFILES.items():
                if hooks is None:
                    continue
                self.assertIn(tripwire, hooks, f"{tripwire} missing from {profile!r}")

    def test_route_profile_mapping_covers_all_routes(self):
        self.assertEqual(
            set(hook_profile.ROUTE_TO_PROFILE), {"R1", "R2", "R3", "R4", "R5"})
        for profile in hook_profile.ROUTE_TO_PROFILE.values():
            self.assertIn(profile, hook_profile.PROFILES)


if __name__ == "__main__":
    unittest.main()
