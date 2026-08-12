"""Unit tests for the omni_mem_session_start SessionStart hook.

The property under test is that the injected briefing is *budgeted*, not
*prefix-cut*. build_briefing returns one JSON document whose sections serialise
in a fixed order, so cutting the raw string at a character count deleted whole
sections — always the same ones — on every session. These tests assert that
every non-empty section survives, that every drop is announced, and that a
payload the renderer does not understand degrades to the old behaviour instead
of raising: this hook must never block session start.

The module is loaded relative to THIS FILE, not from ~/.claude. The rest of the
suite hard-codes the live config home, which means a test run inside a worktree
silently exercises the installed copy rather than the code under review.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
MODULE_PATH = BIN / "omni_mem_session_start.py"


def _load_module():
    sys.path.insert(0, str(BIN))  # the hook imports omni_mem_route as a sibling
    try:
        spec = importlib.util.spec_from_file_location("omni_mem_session_start", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(BIN))


MOD = _load_module()


def _observation(title: str, text: str = "body text") -> dict:
    return {"id": "o1", "title": title, "text": text, "createdAt": "2026-08-01T00:00:00Z"}


def _fact(predicate: str, obj: str = "an object", subject: str = "fact-entity:abcdef1234") -> dict:
    return {"subjectId": subject, "predicate": predicate, "object": obj, "status": "active"}


def _page(title: str, body: str = "page body") -> dict:
    return {"title": title, "body": body, "trustLevel": 0.7}


def _topic(label: str, count: int = 3) -> dict:
    return {"label": label, "topicKey": label.lower(), "count": count}


def _full_payload(observation_text: str = "short observation") -> dict:
    return {
        "generatedAt": "2026-08-12T00:00:00Z",
        "workspaceId": "ws",
        "recentObservations": [_observation("obs one", observation_text)],
        "activeFacts": [_fact("is a fact about")],
        "synthesisPages": [_page("page one")],
        "relevantTopics": [_topic("Topic One")],
        "summary": {"tokenEstimate": 99},
    }


class TestSectionSurvival:
    def test_every_non_empty_section_is_rendered(self):
        out = MOD.render_briefing(_full_payload(), max_chars=4000)
        assert out is not None
        for heading in ("Recent observations", "Active facts", "Synthesis pages", "Topics"):
            assert heading in out, f"{heading} missing — a section was dropped whole"

    def test_a_section_survives_an_item_bigger_than_its_whole_share(self):
        # A 30k-char observation cannot fit the share of the budget its section
        # is entitled to. Showing it trimmed is the guarantee; dropping the
        # section because its first item did not fit is the defect.
        payload = _full_payload(observation_text="x " * 15000)
        out = MOD.render_briefing(payload, max_chars=4000)
        assert out is not None
        for heading in ("Recent observations", "Active facts", "Synthesis pages", "Topics"):
            assert heading in out, f"{heading} disappeared under a starving payload"

    def test_a_long_leading_section_cannot_spend_the_whole_budget(self):
        # Measured before this was asserted: with the forward reserve removed
        # the same payload renders 5,878 chars against a 4,000 budget. The
        # slack allows the overspill that section survival requires, and no
        # more.
        payload = {
            "recentObservations": [_observation("huge", "x " * 20000)],
            "activeFacts": [_fact("p", "o " * 200) for _ in range(6)],
            "synthesisPages": [_page(f"page {i}", "b " * 300) for i in range(5)],
            "relevantTopics": [_topic(f"T{i}", i) for i in range(8)],
        }
        out = MOD.render_briefing(payload, max_chars=4000)
        assert len(out) <= 4000 * 1.25, f"budget overrun: {len(out)} chars"

    def test_empty_sections_produce_no_heading(self):
        payload = _full_payload()
        payload["identityFacts"] = []
        payload["preferences"] = []
        out = MOD.render_briefing(payload, max_chars=4000)
        assert "Identity facts" not in out
        assert "Preferences" not in out

    def test_sections_are_ordered_as_declared(self):
        out = MOD.render_briefing(_full_payload(), max_chars=4000)
        assert out.index("Recent observations") < out.index("Active facts")
        assert out.index("Active facts") < out.index("Synthesis pages")
        assert out.index("Synthesis pages") < out.index("Topics")


class TestOverflowIsAnnounced:
    def test_heading_carries_the_full_count_when_nothing_is_dropped(self):
        out = MOD.render_briefing(_full_payload(), max_chars=4000)
        assert "### Recent observations (1)" in out

    def test_heading_says_how_many_were_dropped(self):
        payload = {
            "recentObservations": [_observation(f"obs {i}", "y " * 400) for i in range(20)],
        }
        out = MOD.render_briefing(payload, max_chars=1200)
        assert "showing" in out and "of 20" in out
        shown = int(out.split("showing ")[1].split(" of")[0])
        assert 0 < shown < 20
        # The count in the heading must match the bullets actually rendered.
        assert out.count("\n- **") == shown

    def test_trimmed_text_is_marked(self):
        out = MOD.render_briefing(
            {"recentObservations": [_observation("obs", "z " * 4000)]}, max_chars=800
        )
        assert "[…]" in out, "a silent cut reads as a complete statement"

    def test_trim_leaves_short_text_alone(self):
        assert MOD._trim("already short", 100) == "already short"
        assert "[…]" not in MOD._trim("already short", 100)


class TestFactRendering:
    def test_unresolved_subject_is_shown_not_dropped(self):
        # build_briefing ships subjectId as an opaque handle with no name, so a
        # bare predicate reads as a statement about whatever came before it.
        out = MOD.render_briefing({"activeFacts": [_fact("are enforced as", "a rule")]}, 4000)
        assert "(subject abcdef12)" in out
        assert "are enforced as a rule" in out

    def test_falls_back_to_a_plain_statement_field(self):
        out = MOD.render_briefing({"preferences": [{"text": "prefers terse output"}]}, 4000)
        assert "prefers terse output" in out

    def test_item_with_nothing_renderable_is_skipped(self):
        assert MOD._render_fact({"status": "active"}, 100) is None
        assert MOD._render_observation({"id": "x"}, 100) is None
        assert MOD._render_page({"id": "x"}, 100) is None
        assert MOD._render_topic({"count": 4}, 100) is None


class TestRefusesToRenderNothing:
    def test_returns_none_for_non_dict(self):
        assert MOD.render_briefing(None) is None
        assert MOD.render_briefing("not a dict") is None
        assert MOD.render_briefing([]) is None

    def test_returns_none_when_no_section_has_content(self):
        payload = {"generatedAt": "x", "workspaceId": "ws", "recentObservations": [], "summary": {}}
        assert MOD.render_briefing(payload) is None

    def test_returns_none_when_items_are_not_dicts(self):
        assert MOD.render_briefing({"recentObservations": ["a string", 42]}) is None


class TestMainDegradesInsteadOfRaising:
    """The hook's contract is that it never blocks session start."""

    def _run(self, monkeypatch, capsys, briefing):
        monkeypatch.setattr(MOD, "_mcp_call", lambda _ws: briefing)
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        rc = MOD.main()
        return rc, capsys.readouterr().out

    def test_non_json_briefing_falls_back_to_the_raw_text(self, monkeypatch, capsys):
        rc, out = self._run(monkeypatch, capsys, "this is not JSON at all")
        assert rc == 0
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "this is not JSON at all" in ctx

    def test_oversized_non_json_briefing_is_still_capped(self, monkeypatch, capsys):
        rc, out = self._run(monkeypatch, capsys, "q" * (MOD.MAX_CHARS * 3))
        assert rc == 0
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "[briefing truncated]" in ctx
        assert len(ctx) < MOD.MAX_CHARS * 2

    def test_json_with_no_renderable_section_falls_back(self, monkeypatch, capsys):
        payload = json.dumps({"workspaceId": "ws", "recentObservations": []})
        rc, out = self._run(monkeypatch, capsys, payload)
        assert rc == 0
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "recentObservations" in ctx  # the raw payload, not an empty heading

    def test_silent_when_the_call_returns_nothing(self, monkeypatch, capsys):
        rc, out = self._run(monkeypatch, capsys, None)
        assert rc == 0
        assert out == ""

    def test_rendered_output_reaches_the_session_as_markdown(self, monkeypatch, capsys):
        rc, out = self._run(monkeypatch, capsys, json.dumps(_full_payload()))
        assert rc == 0
        payload = json.loads(out)
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert ctx.startswith("## omni-mem briefing")
        assert "### Recent observations" in ctx
        assert '"recentObservations"' not in ctx, "raw JSON leaked into the rendered path"


class TestAgainstARealPayload:
    """A synthetic fixture cannot show that the real briefing shape is handled.

    Skips when the capture is absent so the suite stays runnable offline; the
    capture is produced by calling build_briefing for any populated workspace.
    """

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "omni_mem_briefing.json"

    @pytest.mark.skipif(not FIXTURE.exists(), reason="no captured briefing fixture")
    def test_all_sections_survive_where_the_prefix_cut_delivered_one(self):
        raw = self.FIXTURE.read_text(encoding="utf-8")
        data = json.loads(raw)
        populated = [k for k, v in data.items() if isinstance(v, list) and v]
        assert len(populated) > 1, "fixture must have several populated sections to be a test"

        old = raw[: MOD.MAX_CHARS]
        delivered_by_old = [k for k in populated if f'"{k}"' in old]
        assert len(delivered_by_old) < len(populated), "fixture does not reproduce the defect"

        out = MOD.render_briefing(data, MOD.MAX_CHARS)
        labels = {key: heading for key, heading, _ in MOD.SECTION_ORDER}
        # Checked before the loop, not inside it: skipping sections absent from
        # SECTION_ORDER would let a section be deleted from the renderer without
        # a single test noticing.
        unhandled = [key for key in populated if key not in labels]
        assert not unhandled, f"populated briefing sections with no renderer: {unhandled}"
        for key in populated:
            assert labels[key] in out, f"{key} still missing from the rendered briefing"
