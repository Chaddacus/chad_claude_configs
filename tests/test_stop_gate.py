"""Tests for stop_gate.py — lexical pools, deliverable-kind gating, allow
markers, prose stripping, and the functional-claims evidentiary rule.

Added 2026-06-10 with the deliverable-kind redesign; the gate previously had
zero regression coverage."""

import glob
import json
import os
import sys
import time
import uuid
from io import StringIO
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _cleanup_route_files():
    yield
    for f in glob.glob("/tmp/claude-route-sgtest-*.json"):
        try:
            os.unlink(f)
        except OSError:
            pass

# Direct import of stop_gate via sys.path (same pattern as test_classify_prompt)
BIN = Path.home() / ".claude" / "bin"
sys.path.insert(0, str(BIN))
import stop_gate as sg  # noqa: E402


CFG_BLOCK = {
    "lexical": "block",
    "evidentiary": "log",
    "rules": dict(sg.DEFAULT_CONFIG["rules"]),
}


def _route_file(kind: str) -> str:
    """Write a route file for a fresh session id; return the session id."""
    sid = f"sgtest-{uuid.uuid4().hex[:10]}"
    Path(f"/tmp/claude-route-{sid}.json").write_text(
        json.dumps({"route_hint": "R3", "deliverable_kind": kind})
    )
    return sid


def _run_main(monkeypatch, capsys, message: str, session_id: str, cfg=None):
    """Drive sg.main() with a fake stdin payload; return parsed envelope."""
    payload = json.dumps({
        "session_id": session_id,
        "last_assistant_message": message,
    })
    monkeypatch.setattr(sg.sys, "stdin", StringIO(payload))
    monkeypatch.setattr(sg, "load_config", lambda: cfg or CFG_BLOCK)
    # Isolate from real case files: no recorded activity.
    monkeypatch.setattr(sg, "read_summary", lambda *a, **k: {})
    monkeypatch.setattr(sg, "read_completion", lambda *a, **k: None)
    rc = sg.main()
    out = capsys.readouterr().out.strip()
    return rc, (json.loads(out) if out else {})


@pytest.mark.unit
class TestLexicalPools:
    def test_artifact_prompt_blocks_want_me_to(self, monkeypatch, capsys):
        sid = _route_file("artifact")
        rc, env = _run_main(monkeypatch, capsys, "Done with the slice. Want me to implement the rest?", sid)
        assert env.get("decision") == "block"

    def test_advice_prompt_allows_recommendation(self, monkeypatch, capsys):
        sid = _route_file("advice")
        rc, env = _run_main(monkeypatch, capsys, "I recommend approach B for the reasons above.", sid)
        assert env.get("decision") != "block"

    def test_advice_prompt_still_blocks_stall(self, monkeypatch, capsys):
        sid = _route_file("advice")
        rc, env = _run_main(monkeypatch, capsys, "I read the file. Should I keep going with the analysis?", sid)
        assert env.get("decision") == "block"

    def test_missing_route_file_defaults_strict(self, monkeypatch, capsys):
        sid = f"sgtest-missing-{uuid.uuid4().hex[:8]}"
        rc, env = _run_main(monkeypatch, capsys, "Want me to proceed with the migration?", sid)
        assert env.get("decision") == "block"

    def test_recursion_guard_passes(self, monkeypatch, capsys):
        payload = json.dumps({
            "session_id": "any",
            "stop_hook_active": True,
            "last_assistant_message": "Want me to implement it?",
        })
        monkeypatch.setattr(sg.sys, "stdin", StringIO(payload))
        rc = sg.main()
        assert json.loads(capsys.readouterr().out.strip()) == {}

    def test_block_reason_teaches_restatement(self):
        text = sg.BLOCK_REASON.format(match="want me to")
        assert "RESTATE" in text
        assert "never re-blocked" in text
        assert "not a user instruction" in text

    # --- Codex adversarial cases (2026-06-10 review) ---

    def test_want_me_to_blocks_even_on_advice(self, monkeypatch, capsys):
        # Offer-shaped begging is a stall on every prompt kind (finding #1).
        sid = _route_file("advice")
        rc, env = _run_main(monkeypatch, capsys, "Findings delivered. Want me to patch them?", sid)
        assert env.get("decision") == "block"

    def test_allow_marker_does_not_bypass_stall(self, monkeypatch, capsys):
        # "Decision needed" must not launder a stall (finding #2).
        sid = _route_file("artifact")
        rc, env = _run_main(monkeypatch, capsys, "Decision needed: should I continue?", sid)
        assert env.get("decision") == "block"

    def test_say_the_word_does_not_launder_want_me_to(self, monkeypatch, capsys):
        sid = _route_file("advice")
        rc, env = _run_main(monkeypatch, capsys, "Say the word if you want me to continue.", sid)
        assert env.get("decision") == "block"

    def test_stale_route_file_falls_back_strict(self, monkeypatch, capsys):
        # advice older than the freshness window must not relax the gate
        # (finding #4).
        sid = _route_file("advice")
        path = f"/tmp/claude-route-{sid}.json"
        old = time.time() - (sg.ROUTE_FILE_MAX_AGE_S + 60)
        os.utime(path, (old, old))
        rc, env = _run_main(monkeypatch, capsys, "I recommend approach B.", sid)
        assert env.get("decision") == "block"

    def test_world_writable_route_file_ignored(self, monkeypatch, capsys):
        sid = _route_file("advice")
        os.chmod(f"/tmp/claude-route-{sid}.json", 0o666)
        rc, env = _run_main(monkeypatch, capsys, "I recommend approach B.", sid)
        assert env.get("decision") == "block"


@pytest.mark.unit
class TestAllowMarkers:
    def test_fork_phrasing_passes(self, monkeypatch, capsys):
        sid = _route_file("artifact")
        msg = "Two paths: patch the gate or rewrite it. Which do you prefer?"
        rc, env = _run_main(monkeypatch, capsys, msg, sid)
        assert env.get("decision") != "block"

    def test_your_call_passes(self, monkeypatch, capsys):
        sid = _route_file("artifact")
        msg = "Design delivered. Implementing it is a separate decision — your call. I recommend the minimal cut."
        rc, env = _run_main(monkeypatch, capsys, msg, sid)
        assert env.get("decision") != "block"

    def test_ratify_passes(self, monkeypatch, capsys):
        sid = _route_file("artifact")
        msg = "Ratify F1-F6 and the next step would be implementation."
        rc, env = _run_main(monkeypatch, capsys, msg, sid)
        assert env.get("decision") != "block"


@pytest.mark.unit
class TestProseStripping:
    def test_italic_meta_mention_stripped(self):
        prose = sg.strip_non_prose("The STALL pool gates *should I* and *shall I* phrases.")
        assert sg.STALL_SEEKING.search(prose) is None

    def test_inline_code_meta_mention_stripped(self):
        prose = sg.strip_non_prose("The pool gates `want me to` and `i recommend`.")
        assert sg.DELIVERABLE_SEEKING.search(prose) is None

    def test_bold_is_not_stripped(self):
        # Real stall phrasing must not be able to hide in bold.
        prose = sg.strip_non_prose("**Should I proceed with the merge?**")
        assert sg.STALL_SEEKING.search(prose) is not None

    def test_plain_stall_still_matches(self):
        prose = sg.strip_non_prose("All done here. Should I continue?")
        assert sg.STALL_SEEKING.search(prose) is not None

    def test_full_phrase_italic_evasion_still_matches(self):
        # Italic stripping is capped at 3 words — a whole stall sentence
        # cannot hide in emphasis (Codex finding #7).
        prose = sg.strip_non_prose("*Should I continue with the merge?*")
        assert sg.STALL_SEEKING.search(prose) is not None


@pytest.mark.unit
class TestFunctionalClaims:
    def test_operational_without_execution_flagged(self, monkeypatch):
        monkeypatch.setattr(sg, "read_summary", lambda *a, **k: {"verifications": []})
        monkeypatch.setattr(sg, "read_completion", lambda *a, **k: None)
        findings = sg.evidentiary_check("The pipeline is operational now.", dict(CFG_BLOCK), "sid")
        assert any(f["rule"] == "functional_claims" for f in findings)

    def test_operational_with_execution_passes(self, monkeypatch):
        monkeypatch.setattr(
            sg, "read_summary",
            lambda *a, **k: {"verifications": [{"kind": "test", "exit": 0, "summary": "10 passed"}]},
        )
        monkeypatch.setattr(sg, "read_completion", lambda *a, **k: None)
        findings = sg.evidentiary_check("The pipeline is operational now.", dict(CFG_BLOCK), "sid")
        assert not any(f["rule"] == "functional_claims" for f in findings)

    def test_written_and_parses_not_flagged(self, monkeypatch):
        monkeypatch.setattr(sg, "read_summary", lambda *a, **k: {"verifications": []})
        monkeypatch.setattr(sg, "read_completion", lambda *a, **k: None)
        findings = sg.evidentiary_check("Files written; frontmatter parses cleanly.", dict(CFG_BLOCK), "sid")
        assert not any(f["rule"] == "functional_claims" for f in findings)
