"""Tests for classify_prompt's hook-input contract and the profile deadlock fix.

Run from this directory:
    python3 -m unittest classify_prompt_test

Regression for the 2026-07-16 audit findings C1/C2:
  C1 — the hook read CLAUDE_USER_PROMPT (never set by the product) and fell
       back to classifying the RAW stdin JSON envelope, so every real prompt
       for months was misrouted (0 R1 rows in 2,550 production decisions; the
       transcript_path matched the unanchored ".json" regex on every prompt).
  C2 — hook_profile's "minimal" profile excluded classify_prompt, so one R1
       classification locked the session out of ever reclassifying.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE / "classify_prompt.py"

# Import must be safe regardless of profile state (the old import-time
# sys.exit(0) is itself part of what these tests pin).
os.environ.setdefault("CLAUDE_HOOK_PROFILE", "strict")
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path.home() / ".claude" / "bin"))
import classify_prompt as cp  # noqa: E402
import hook_profile  # noqa: E402

# A realistic UserPromptSubmit payload per the v2.1.211 dispatch source.
def _envelope(prompt: str) -> str:
    return json.dumps({
        "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "transcript_path": "/Users/x/.claude/projects/-Users-x-proj/aaaaaaaa.jsonl",
        "cwd": "/Users/x/proj",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    })


class ReadHookPayloadTest(unittest.TestCase):
    def _read(self, stdin_text: str, env_prompt: str | None = None):
        old_stdin = sys.stdin
        old_env = os.environ.get("CLAUDE_USER_PROMPT")
        try:
            sys.stdin = io.StringIO(stdin_text)
            if env_prompt is None:
                os.environ.pop("CLAUDE_USER_PROMPT", None)
            else:
                os.environ["CLAUDE_USER_PROMPT"] = env_prompt
            return cp._read_hook_payload()
        finally:
            sys.stdin = old_stdin
            if old_env is None:
                os.environ.pop("CLAUDE_USER_PROMPT", None)
            else:
                os.environ["CLAUDE_USER_PROMPT"] = old_env

    def test_stdin_json_extracts_prompt_not_envelope(self):
        prompt, session = self._read(_envelope("what is the capital of France"))
        self.assertEqual(prompt, "what is the capital of France")
        self.assertEqual(session, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    def test_env_override_wins(self):
        prompt, _ = self._read(_envelope("from stdin"), env_prompt="from env")
        self.assertEqual(prompt, "from env")

    def test_plain_text_stdin_still_works(self):
        prompt, _ = self._read("just a plain piped prompt")
        self.assertEqual(prompt, "just a plain piped prompt")

    def test_empty_stdin_yields_empty_prompt(self):
        prompt, _ = self._read("")
        self.assertEqual(prompt, "")


class FileRegexTest(unittest.TestCase):
    def test_jsonl_transcript_path_is_not_a_file_mention(self):
        # The exact phantom that inflated file_count on 99.9% of prompts.
        self.assertEqual(
            cp.count_file_mentions("see /Users/x/.claude/projects/p/abc.jsonl"), 0)

    def test_real_extensions_still_match(self):
        self.assertEqual(cp.count_file_mentions("edit config.json and app.py"), 2)
        self.assertEqual(cp.count_file_mentions("fix src/main.tsx, run build.sh."), 2)

    def test_classification_of_prompt_text_not_envelope(self):
        # End-to-end through classify_prompt(): a trivial question is R1 again.
        result = cp.classify_prompt("what is the capital of France")
        self.assertEqual(result["route_hint"], "R1")
        self.assertEqual(result["reason"], "simple factual query")


class ProfileDeadlockTest(unittest.TestCase):
    def test_minimal_profile_includes_classifier(self):
        # C2 regression: the route-file writer must survive every profile,
        # or the session can never be reclassified out of that profile.
        for profile, hooks in hook_profile.PROFILES.items():
            if hooks is None:  # strict = all hooks
                continue
            self.assertIn("classify_prompt", hooks,
                          f"profile {profile!r} excludes classify_prompt (deadlock)")

    def test_import_is_side_effect_free(self):
        # Importing the module must never sys.exit, even when the profile
        # would gate the hook off (the gate belongs in main()).
        code = (
            "import os, sys\n"
            "os.environ['CLAUDE_HOOK_PROFILE'] = 'minimal'\n"
            "os.environ['CLAUDE_CODE_SESSION_ID'] = 'no-such-session'\n"
            f"sys.path.insert(0, {str(HERE)!r})\n"
            f"sys.path.insert(0, {str(Path.home() / '.claude' / 'bin')!r})\n"
            "import classify_prompt\n"
            "print('IMPORT-OK')\n"
        )
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=30)
        self.assertIn("IMPORT-OK", out.stdout)


class EndToEndTest(unittest.TestCase):
    def test_r1_reachable_via_real_hook_delivery(self):
        # Full subprocess run with the real stdin contract: an R1 question
        # must classify R1 (it never could before this fix).
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.pop("CLAUDE_USER_PROMPT", None)
            env.update({
                "CLAUDE_HOME": tmp,                       # decision log goes here
                "CLAUDE_HOOK_PROFILE": "strict",          # bypass route-file gating
                "CLAUDE_CODE_SESSION_ID": "cp-test-e2e",  # isolate route file
            })
            out = subprocess.run(
                [sys.executable, str(SCRIPT)],
                input=_envelope("what is the capital of France"),
                capture_output=True, text=True, env=env, timeout=30)
            envelope = json.loads(out.stdout)
            ctx = envelope["hookSpecificOutput"]["additionalContext"]
            self.assertIn("route_hint=R1", ctx)
            self.assertIn("simple factual query", ctx)
            # Decision log row records the prompt's stats, not the envelope's.
            log = Path(tmp) / "state" / "route_decisions.jsonl"
            row = json.loads(log.read_text().strip().splitlines()[-1])
            self.assertEqual(row["route_hint"], "R1")
            self.assertEqual(row["file_count"], 0)
            self.assertEqual(row["word_count"], 6)
            self.assertEqual(row["session_id"], "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
            os.unlink("/tmp/claude-route-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.json")


if __name__ == "__main__":
    unittest.main()
