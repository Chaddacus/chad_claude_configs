"""Tests for classify_prompt.py — route classification, file counting, and output contract."""

import json
import sys
from pathlib import Path

import pytest

from conftest import CLASSIFY_PROMPT, GOVERN_SCRIPTS

# ---------------------------------------------------------------------------
# Direct import of classify_prompt module via sys.path manipulation
# ---------------------------------------------------------------------------
sys.path.insert(0, str(GOVERN_SCRIPTS))
import classify_prompt as cp_module  # noqa: E402


# ===========================================================================
# Route classification tests (unit)
# ===========================================================================


@pytest.mark.unit
class TestRouteClassification:
    """Verify classify_prompt returns the correct route_hint and governance flag."""

    def test_empty_prompt_R1(self):
        result = cp_module.classify_prompt("")
        assert result["route_hint"] == "R1"
        assert result["governance_recommended"] is False

    def test_whitespace_only_R1(self):
        result = cp_module.classify_prompt("   \n  ")
        assert result["route_hint"] == "R1"
        assert result["governance_recommended"] is False

    def test_simple_question_no_files_R1(self):
        result = cp_module.classify_prompt("what is a closure?")
        assert result["route_hint"] == "R1"
        assert result["governance_recommended"] is False

    def test_govern_command_R3(self):
        result = cp_module.classify_prompt("/govern deploy")
        assert result["route_hint"] == "R3"
        assert result["governance_recommended"] is True

    def test_auth_keyword_R4(self):
        result = cp_module.classify_prompt("add JWT authentication")
        assert result["route_hint"] == "R4"
        assert "auth" in result["reason"]

    def test_security_keyword_R4(self):
        result = cp_module.classify_prompt("fix the XSS vulnerability")
        assert result["route_hint"] == "R4"
        assert "security" in result["reason"]

    def test_migration_keyword_R4(self):
        # "schema" is a strong migration keyword — always triggers R4
        result = cp_module.classify_prompt("create a schema migration for user permissions")
        assert result["route_hint"] == "R4"
        assert "migration" in result["reason"]

    def test_multiple_risk_categories(self):
        result = cp_module.classify_prompt("migrate auth schema")
        assert result["route_hint"] == "R4"
        assert "auth" in result["reason"]
        assert "migration" in result["reason"]

    def test_short_prompt_one_file_R2(self):
        result = cp_module.classify_prompt("fix bug in server.py")
        assert result["route_hint"] == "R2"
        assert result["governance_recommended"] is True

    def test_short_no_files_R2(self):
        # Contract change 2026-07-16 (shared route_classifier): R5 became
        # reachable, and a <5-word implementation ask with no files and no
        # risk/feature signal is now "clarify first" rather than blind R2.
        result = cp_module.classify_prompt("add a button")
        assert result["route_hint"] == "R5"
        # A minimally-specified version routes R2 as before.
        result = cp_module.classify_prompt("add a save button to the settings form")
        assert result["route_hint"] == "R2"
        assert result["governance_recommended"] is False

    def test_broad_feature_no_files_R3(self):
        result = cp_module.classify_prompt(
            "Add a gamified lesson streak feature with dashboard feedback and tests"
        )
        assert result["route_hint"] == "R3"
        assert result["governance_recommended"] is True
        assert "broad feature" in result["reason"]

    def test_long_prompt_R3(self):
        # 60 words, no files, no risk keywords
        words = " ".join(["refactor"] + ["the code structure"] * 20 + ["now"])
        assert len(words.split()) >= 60, f"prompt has {len(words.split())} words, need >= 60"
        result = cp_module.classify_prompt(words)
        assert result["route_hint"] == "R3"

    def test_two_files_R3(self):
        result = cp_module.classify_prompt("refactor utils.ts and helpers.js")
        assert result["route_hint"] == "R3"

    def test_simple_indicator_overridden_by_risk(self):
        # Contract change 2026-07-16 (audit finding M7): a definitional
        # question naming a risk topic is a LOOKUP — "what is CSRF?" routes
        # R1. Risk only overrides the simple indicator when the question
        # carries an implementation imperative.
        result = cp_module.classify_prompt("what is CSRF?")
        assert result["route_hint"] == "R1"
        result = cp_module.classify_prompt("what is the way to fix our CSRF handling?")
        assert result["route_hint"] == "R4"
        assert "security" in result["reason"]

    def test_deploy_alone_not_R4(self):
        # deploy is NOT in high_risk but IS in DEPLOY_KEYWORDS.
        # file_count=0, high_risk=False, touches_deploy=True
        # => skips the R2 branch (touches_deploy is True) => falls to final R3.
        result = cp_module.classify_prompt("deploy to staging")
        assert result["route_hint"] == "R3"

    def test_authorization_triggers_auth(self):
        """'authorization' should trigger auth category via word-boundary match."""
        result = cp_module.classify_prompt("fix authorization checks")
        assert result["route_hint"] == "R4"
        assert "auth" in result["reason"]

    def test_encrypted_triggers_security(self):
        """'encrypted' should trigger security via SECURITY_STRONG."""
        result = cp_module.classify_prompt("handle encrypted data")
        assert result["route_hint"] == "R4"
        assert "security" in result["reason"]


# ===========================================================================
# Overreach tests — benign prompts that must NOT escalate to R4
# ===========================================================================


@pytest.mark.unit
class TestOverreachProtection:
    """Verify that benign prompts with dual-use keywords are not over-classified."""

    def test_audit_css_not_R4(self):
        result = cp_module.classify_prompt("Audit the CSS for accessibility issues.")
        assert result["route_hint"] != "R4", f"'audit' in non-security context should not be R4: {result}"

    def test_security_deposit_not_R4(self):
        result = cp_module.classify_prompt("Security deposit calculation logic.")
        assert result["route_hint"] != "R4", f"'security' in financial context should not be R4: {result}"

    def test_migration_guide_not_R4(self):
        result = cp_module.classify_prompt("Migration guide for the docs site theme.")
        assert result["route_hint"] != "R4", f"'migration' in docs context should not be R4: {result}"

    def test_authorship_not_R4(self):
        result = cp_module.classify_prompt("Authorship metadata in blog posts.")
        assert result["route_hint"] != "R4", f"'authorship' should not trigger auth: {result}"

    def test_exposure_time_not_R4(self):
        result = cp_module.classify_prompt("Check exposure time in camera settings.")
        assert result["route_hint"] != "R4", f"'exposure' in photo context should not be R4: {result}"

    def test_incident_template_not_R4(self):
        result = cp_module.classify_prompt("The incident report template needs a new field.")
        assert result["route_hint"] != "R4", f"'incident' in template context should not be R4: {result}"

    def test_compliance_badge_not_R4(self):
        result = cp_module.classify_prompt("Add compliance badge to the README.")
        assert result["route_hint"] != "R4", f"'compliance' in badge context should not be R4: {result}"

    def test_database_explain_R1(self):
        """'database' was removed from MIGRATION_KEYWORDS — simple explanation stays R1."""
        result = cp_module.classify_prompt("Explain what a database index is in 2 sentences.")
        assert result["route_hint"] == "R1"

    def test_translate_deploy_R1(self):
        """'deploy' in translation context: simple indicator 'translate' overrides."""
        result = cp_module.classify_prompt("Translate this sentence to Spanish: I will deploy at noon.")
        assert result["route_hint"] == "R1"

    def test_deploy_preview_R3(self):
        """'deploy' alone should be R3, not R4."""
        result = cp_module.classify_prompt("Deploy preview styling changes.")
        assert result["route_hint"] == "R3"


# ===========================================================================
# Compound evidence tests — weak keywords SHOULD trigger with compound evidence
# ===========================================================================


@pytest.mark.unit
class TestCompoundEvidence:
    """Verify weak keywords DO escalate when compound evidence is present."""

    def test_audit_with_auth_triggers_R4(self):
        """Single weak keyword + auth signal = compound evidence."""
        result = cp_module.classify_prompt("Audit the authentication middleware.")
        assert result["route_hint"] == "R4"

    def test_multiple_weak_security_triggers_R4(self):
        """2+ distinct weak security keywords = compound evidence."""
        result = cp_module.classify_prompt("Add compliance audit log chain for financial exports.")
        assert result["route_hint"] == "R4"

    def test_incident_exposed_triggers_R4(self):
        """'incident' + 'exposed' = 2 weak keywords = R4."""
        result = cp_module.classify_prompt("Investigate production incident: customer data exposed in logs.")
        assert result["route_hint"] == "R4"

    def test_migration_with_permissions_R4(self):
        """'migration' (weak) + 'permissions' (auth) = compound evidence."""
        result = cp_module.classify_prompt("Design and apply a database migration for user permissions.")
        assert result["route_hint"] == "R4"

    def test_migrate_without_compound_not_R4(self):
        """'migrate' alone without compound evidence should not be R4."""
        result = cp_module.classify_prompt("Migrate component library usage across multiple pages.")
        assert result["route_hint"] != "R4"


# ===========================================================================
# File counting tests (unit)
# ===========================================================================


@pytest.mark.unit
class TestCountFileMentions:
    """Verify count_file_mentions returns correct unique file counts."""

    def test_no_files(self):
        assert cp_module.count_file_mentions("hello world") == 0

    def test_one_ts(self):
        assert cp_module.count_file_mentions("edit src/app.ts") == 1

    def test_dedup(self):
        assert cp_module.count_file_mentions("edit app.ts and app.ts again") == 1

    def test_multiple_types(self):
        assert cp_module.count_file_mentions("app.ts, util.py, main.go") == 3


# ===========================================================================
# Output contract tests (subprocess via run_hook fixture)
# ===========================================================================


@pytest.mark.unit
class TestOutputContract:
    """Verify the script's stdout envelope matches the hook system contract."""

    def test_stdout_valid_json(self, run_hook):
        result = run_hook(
            CLASSIFY_PROMPT,
            env={"CLAUDE_USER_PROMPT": "hello"},
        )
        parsed = json.loads(result["stdout"].strip())
        assert isinstance(parsed, dict)

    def test_has_hookSpecificOutput(self, run_hook):
        result = run_hook(
            CLASSIFY_PROMPT,
            env={"CLAUDE_USER_PROMPT": "hello"},
        )
        parsed = json.loads(result["stdout"].strip())
        assert "hookSpecificOutput" in parsed

    def test_hookEventName_UserPromptSubmit(self, run_hook):
        result = run_hook(
            CLASSIFY_PROMPT,
            env={"CLAUDE_USER_PROMPT": "hello"},
        )
        parsed = json.loads(result["stdout"].strip())
        assert parsed["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"

    def test_additionalContext_has_route_hint(self, run_hook):
        result = run_hook(
            CLASSIFY_PROMPT,
            env={"CLAUDE_USER_PROMPT": "hello"},
        )
        parsed = json.loads(result["stdout"].strip())
        context = parsed["hookSpecificOutput"]["additionalContext"]
        assert "route_hint=" in context

    def test_env_var_input(self, run_hook):
        # Risk beats vague (2026-07-16 shared route_classifier): a terse
        # prompt naming an auth-strong topic still routes R4 — R5 is reserved
        # for signal-free prompts ("fix it", "make it faster").
        result = run_hook(
            CLASSIFY_PROMPT,
            env={"CLAUDE_USER_PROMPT": "add JWT authentication"},
        )
        parsed = json.loads(result["stdout"].strip())
        context = parsed["hookSpecificOutput"]["additionalContext"]
        assert "route_hint=R4" in context

    def test_signal_free_short_prompt_is_r5(self, run_hook):
        # R5 became reachable 2026-07-16 (it never was from this hook).
        result = run_hook(
            CLASSIFY_PROMPT,
            env={"CLAUDE_USER_PROMPT": "make it faster"},
        )
        parsed = json.loads(result["stdout"].strip())
        context = parsed["hookSpecificOutput"]["additionalContext"]
        assert "route_hint=R5" in context

    def test_stdin_fallback(self, run_hook):
        # Clear the env var so the script falls back to stdin
        result = run_hook(
            CLASSIFY_PROMPT,
            stdin_json="fix bug in server.py",
            env={"CLAUDE_USER_PROMPT": ""},
        )
        parsed = json.loads(result["stdout"].strip())
        context = parsed["hookSpecificOutput"]["additionalContext"]
        assert "route_hint=R2" in context


# ===========================================================================
# Deliverable-kind tests (advice vs artifact channel read by stop_gate.py)
# ===========================================================================


@pytest.mark.unit
class TestDeliverableKind:
    """deliverable_kind: advisory prompts -> advice; anything with an
    implementation imperative or no advisory signal -> artifact (strict)."""

    def test_advisory_question_is_advice(self):
        prompt = "what would you think of creating a coding team. do deep research on this concept"
        assert cp_module.deliverable_kind(prompt) == "advice"

    def test_critique_is_advice(self):
        assert cp_module.deliverable_kind("adversarially critique the process end to end") == "advice"

    def test_imperative_overrides_advisory(self):
        # "fix them" makes it artifact even though "reviewer"/review appears
        prompt = "why did you implement it with so many flaws? fix them, then run a separate reviewer"
        assert cp_module.deliverable_kind(prompt) == "artifact"

    def test_go_with_recommendations_is_artifact(self):
        assert cp_module.deliverable_kind("go with your recommendations, run it by codex") == "artifact"

    def test_analyze_plus_patch_is_artifact(self):
        # Codex finding #6: advisory verb + repair-class imperative
        assert cp_module.deliverable_kind("analyze the failing tests and patch the implementation") == "artifact"

    def test_review_plus_debug_is_artifact(self):
        assert cp_module.deliverable_kind("review the race condition and debug the scheduler") == "artifact"

    def test_plain_implementation_is_artifact(self):
        assert cp_module.deliverable_kind("add retry logic to the sync daemon") == "artifact"

    def test_empty_is_artifact(self):
        assert cp_module.deliverable_kind("") == "artifact"

    def test_field_written_to_route_file_payload(self):
        result = cp_module.classify_prompt("explain how the scheduler works")
        # classify_prompt itself doesn't add the field; main() does — verify
        # the function used by main() exists and the merge shape is sane.
        result["deliverable_kind"] = cp_module.deliverable_kind("explain how the scheduler works")
        assert result["deliverable_kind"] in ("advice", "artifact")

    def test_anti_overrun_injected_for_r3(self):
        block = cp_module.route_policy_block({"route_hint": "R3"})
        assert "Anti-overrun patterns" in block
        assert "Anti-stop patterns" in block

    def test_anti_overrun_not_injected_for_r2(self):
        assert cp_module.route_policy_block({"route_hint": "R2"}) == ""
