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


class TestRouteDirectives(unittest.TestCase):
    """Pin the 2026-08-16 dispatch-directive contract.

    Regression for the routing audit: route_policy_block returned "" for
    R1/R2, so the classification routed nothing and every task executed on
    the frontier main model. Every route must now carry its dispatch
    directive; R3/R4/R5 keep the governance gates on top.
    """

    def test_every_route_has_a_directive(self):
        for route in ("R1", "R2", "R3", "R4", "R5"):
            self.assertIn(route, cp.ROUTE_DIRECTIVES)
            self.assertIn("[route-directive]", cp.ROUTE_DIRECTIVES[route])

    def test_r1_r2_inject_directive_only(self):
        for route in ("R1", "R2"):
            block = cp.route_policy_block({"route_hint": route})
            self.assertIn(f"[route-directive] {route}", block)
            self.assertIn("sonnet", block)
            self.assertNotIn("Anti-stop patterns", block)

    def test_r3_gets_directive_plus_gates(self):
        block = cp.route_policy_block({"route_hint": "R3"})
        self.assertTrue(block.startswith("[route-directive] R3"))
        self.assertIn("opus", block.split("\n\n")[0])
        self.assertIn("Anti-stop patterns", block)
        self.assertIn("R3/R4 governed lanes", block)

    def test_r4_is_uncapped_but_disciplined(self):
        block = cp.route_policy_block({"route_hint": "R4"})
        self.assertIn("no spawn ceiling", block.split("\n\n")[0])
        self.assertIn("explicit model", block.split("\n\n")[0])
        self.assertIn("Anti-stop patterns", block)

    def test_gated_routes_name_gate_enforcement(self):
        # The directive must tell the orchestrator the ceiling is enforced,
        # not advisory — that distinction was the whole audit finding.
        for route in ("R1", "R2", "R3", "R5"):
            self.assertIn("gate-enforced", cp.ROUTE_DIRECTIVES[route])

    def test_unknown_route_still_gets_gates(self):
        # Defensive: an unmapped route falls back to the governed-lane block.
        block = cp.route_policy_block({"route_hint": "R9"})
        self.assertIn("Anti-stop patterns", block)

    def test_directive_ceilings_match_the_plugin_gate(self):
        # This machine's injected directives and the foundation plugin's
        # enforced ceilings must never drift — that is exactly the H4
        # two-classifiers failure mode. The gate ships in the
        # claude-engineering-foundation plugin (SPEC §17.5); this classifier
        # stands the plugin's classifier down (FOUNDATION_ROUTE_CLASSIFIER=
        # external) and injects the directives itself, so it owns keeping
        # them aligned. Skips on machines without the plugin installed.
        import glob
        import importlib.util
        candidates = sorted(glob.glob(os.path.expanduser(
            "~/.claude/plugins/cache/claude-engineering-foundation/"
            "claude-engineering-foundation/*/hooks/scripts/"
            "route_classifier.py")))
        if not candidates:
            self.skipTest("foundation plugin with routing module not installed")
        spec = importlib.util.spec_from_file_location(
            "foundation_route_policy", candidates[-1])
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)
        expected = {"R1": "sonnet", "R2": "sonnet", "R3": "opus",
                    "R4": None, "R5": "sonnet"}
        self.assertEqual(policy.CEILINGS, expected)
        for route, ceiling in expected.items():
            directive = cp.ROUTE_DIRECTIVES[route]
            if ceiling is None:
                self.assertIn("no spawn ceiling", directive)
            else:
                self.assertIn(f"spawn ceiling: {ceiling}", directive)


if __name__ == "__main__":
    unittest.main()
