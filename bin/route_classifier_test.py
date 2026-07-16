"""Tests for the shared route-classification policy (route_classifier.py).

Run from /Users/chadsimon/.claude/bin/:
    python3 -m unittest route_classifier_test

Pins the 2026-07-16 audit fixes:
  H4 — one policy for both consumers (hook + auto_runtime); substring
       keyword matching ("auth" in "author") must never come back.
  M7 — definitional questions naming risk topics stay R1.
  R5 — vague prompts are reachable from the shared policy.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "skills" / "govern" / "scripts"))
os.environ.setdefault("CLAUDE_HOOK_PROFILE", "strict")

import route_classifier as rc
import auto_runtime_common as rt
import classify_prompt as cp


class SubstringRegressionTest(unittest.TestCase):
    """The vendored auto_runtime copy matched substrings; never again."""

    def test_author_is_not_auth(self):
        r = rc.classify("summarize what the author changed in the permissions docs")
        self.assertFalse(r["classification_evidence"]["touches_auth"], r)
        self.assertNotEqual(r["route_hint"], "R4")

    def test_tokens_sessions_singular_weak_needs_compound(self):
        # One weak keyword alone must not trigger auth.
        r = rc.classify("add a permission entry for the new agent in the config")
        self.assertFalse(r["classification_evidence"]["touches_auth"], r)

    def test_two_weak_auth_keywords_compound(self):
        r = rc.classify("rotate the session token handling in the gateway")
        self.assertTrue(r["classification_evidence"]["touches_auth"], r)
        self.assertEqual(r["route_hint"], "R4")

    def test_strong_auth_always_triggers_for_implementation(self):
        r = rc.classify("implement oauth flow for the public api")
        self.assertTrue(r["classification_evidence"]["touches_auth"])
        self.assertEqual(r["route_hint"], "R4")

    def test_audit_task_is_not_r4(self):
        # The exact task string that misrouted this audit's own track to R4
        # via "permissions" substring-matching.
        task = ("Grounded critical audit of global Claude configuration: "
                "CLAUDE.md, settings.json hooks/permissions/env, "
                "route_manifest.json, standards/, hook script "
                "existence+executability, dead references, contradictions, "
                "gates that don't fire")
        r = rc.classify(task)
        self.assertFalse(r["classification_evidence"]["touches_auth"], r)
        self.assertNotEqual(r["route_hint"], "R4")


class QuestionCarveOutTest(unittest.TestCase):
    """M7: risk words in a question are a topic, not a change surface."""

    def test_what_is_a_jwt_is_r1(self):
        r = rc.classify("what is a jwt and how does it differ from a cookie?")
        self.assertEqual(r["route_hint"], "R1")
        self.assertFalse(r["governance_recommended"])

    def test_explain_encryption_is_r1(self):
        r = rc.classify("explain how tls certificate rotation works")
        self.assertEqual(r["route_hint"], "R1")

    def test_imperative_question_falls_through_to_risk(self):
        # "fix" is an implementation imperative — no carve-out.
        r = rc.classify("what is the fastest way to fix the login bug")
        self.assertEqual(r["route_hint"], "R4")

    def test_question_naming_files_falls_through(self):
        # File mentions mean repo work is in scope; not a pure lookup.
        r = rc.classify("what is wrong with auth.py and session.py handlers")
        self.assertNotEqual(r["route_hint"], "R1")


class VagueRouteTest(unittest.TestCase):
    """R5 must be reachable (it never was from the hook)."""

    def test_fix_auth_is_vague(self):
        r = rc.classify("fix auth")
        self.assertEqual(r["route_hint"], "R5")
        self.assertTrue(r["classification_evidence"]["is_vague"])

    def test_make_it_faster_is_vague(self):
        self.assertEqual(rc.classify("make it faster")["route_hint"], "R5")

    def test_short_simple_question_stays_r1(self):
        self.assertEqual(rc.classify("explain hooks")["route_hint"], "R1")


class RegexAnchorTest(unittest.TestCase):
    def test_jsonl_is_not_json(self):
        self.assertEqual(rc.count_file_mentions("/p/transcript.jsonl"), 0)

    def test_real_files_counted(self):
        self.assertEqual(rc.count_file_mentions("touch a.py b.md c.json"), 3)


class ConsumerParityTest(unittest.TestCase):
    """Both consumers must agree — the whole point of the shared module."""

    PROMPTS = [
        "what is the capital of France",
        "fix auth",
        "implement oauth flow for the public api",
        "add a debounce to the search input in SearchBar.tsx",
        "build a dashboard feature for the analytics workflow",
        "summarize what the author changed in the permissions docs",
        "migrate the users table to add a schema column",
    ]

    def test_hook_and_runtime_routes_agree(self):
        for p in self.PROMPTS:
            hook_route = cp.classify_prompt(p)["route_hint"]
            rt_route = rt.classify_route(p)["route_id"]
            self.assertEqual(hook_route, rt_route, f"divergence on: {p!r}")

    def test_runtime_evidence_keys_preserved(self):
        # auto_runtime consumers read these evidence keys; keep the contract.
        ev = rt.classify_route("implement oauth flow for the public api")[
            "classification_evidence"]
        for key in ("file_count", "file_mentions", "word_count", "touches_auth",
                    "touches_security", "touches_migration", "touches_deploy",
                    "is_vague"):
            self.assertIn(key, ev)

    def test_route_override_still_wins(self):
        self.assertEqual(
            rt.classify_route("anything at all", route_override="R2")["route_id"],
            "R2")


class DeliverableKindTest(unittest.TestCase):
    def test_advice(self):
        self.assertEqual(rc.deliverable_kind("critique the flow we have"), "advice")

    def test_artifact_on_imperative(self):
        self.assertEqual(rc.deliverable_kind("review then fix the flow"), "artifact")

    def test_artifact_default(self):
        self.assertEqual(rc.deliverable_kind(""), "artifact")


if __name__ == "__main__":
    unittest.main()
