"""Tests for dandori self-graduation: predicates, transitions, fixed
real_verdict, and the authoritative block path."""

from __future__ import annotations

import importlib
import io
import json
import sys
import time
import types
from pathlib import Path

import pytest

DANDORI_BIN = Path("/Users/chadsimon/.claude/dandori/bin")
HOOK_BIN = Path("/Users/chadsimon/.claude/bin")
sys.path.insert(0, str(DANDORI_BIN))
sys.path.insert(0, str(HOOK_BIN))

import graduation  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def v2_row(agreement, session="s1", ts=None):
    return {"v": 2, "ts": ts or time.time(), "session": session,
            "agreement": agreement}


def decisive_set(n_rows=20, n_sessions=5):
    """n_rows decisive v2 rows spread across n_sessions sessions."""
    return [v2_row("agree", session=f"s{i % n_sessions}") for i in range(n_rows)]


@pytest.fixture
def grad_env(tmp_path, monkeypatch):
    """Redirect graduation's state surfaces to a temp dir."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"streaming_gates": "shadow"}))
    shadow = tmp_path / "shadow_log.jsonl"
    grad = tmp_path / "graduation_log.jsonl"
    monkeypatch.setattr(graduation, "CONFIG", cfg)
    monkeypatch.setattr(graduation, "SHADOW_LOG", shadow)
    monkeypatch.setattr(graduation, "GRAD_LOG", grad)
    monkeypatch.setattr(graduation, "_notify", lambda detail: None)
    return types.SimpleNamespace(config=cfg, shadow=shadow, grad=grad)


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


# ===========================================================================
# evaluate — pure predicates
# ===========================================================================

@pytest.mark.unit
class TestEvaluate:
    def test_v1_rows_carry_no_weight(self, grad_env):
        rows = [{"ts": time.time(), "session": "s", "agreement": "agree"}
                for _ in range(50)]  # no "v": 2
        status = graduation.evaluate(rows, [])
        assert status["v2_samples"] == 0
        assert status["ready"] is False

    def test_ready_at_thresholds(self, grad_env):
        status = graduation.evaluate(decisive_set(20, 5), [])
        assert status["checks"] == {
            "no_false_greens": True, "enough_decisive": True,
            "enough_sessions": True, "inconclusive_rate_ok": True}
        assert status["ready"] is True

    def test_one_false_green_blocks(self, grad_env):
        rows = decisive_set(20, 5) + [v2_row("FALSE_GREEN")]
        status = graduation.evaluate(rows, [])
        assert status["false_green"] == 1
        assert status["ready"] is False

    def test_too_few_sessions_blocks(self, grad_env):
        status = graduation.evaluate(decisive_set(20, 2), [])
        assert status["checks"]["enough_sessions"] is False
        assert status["ready"] is False

    def test_high_inconclusive_blocks(self, grad_env):
        rows = decisive_set(20, 5) + [v2_row("inconclusive") for _ in range(10)]
        status = graduation.evaluate(rows, [])  # 10/30 = 0.33 >= 0.30
        assert status["checks"]["inconclusive_rate_ok"] is False
        assert status["ready"] is False

    def test_demotion_resets_baseline(self, grad_env):
        old = decisive_set(20, 5)
        grad_rows = [{"event": "demotion", "ts": time.time() + 1}]
        status = graduation.evaluate(old, grad_rows)
        assert status["v2_samples"] == 0, "pre-demotion evidence must not count"
        assert status["ready"] is False


# ===========================================================================
# maybe_transition — config flips + event log
# ===========================================================================

@pytest.mark.unit
class TestTransitions:
    def test_graduates_when_ready(self, grad_env):
        write_jsonl(grad_env.shadow, decisive_set(20, 5))
        taken = graduation.maybe_transition(v2_row("agree"))
        assert taken == "graduation"
        assert json.loads(grad_env.config.read_text())["streaming_gates"] == "on"
        events = graduation._read_jsonl(grad_env.grad)
        assert events[-1]["event"] == "graduation"

    def test_no_transition_when_not_ready(self, grad_env):
        write_jsonl(grad_env.shadow, decisive_set(5, 2))
        assert graduation.maybe_transition(v2_row("agree")) is None
        assert json.loads(grad_env.config.read_text())["streaming_gates"] == "shadow"

    def test_demotes_on_false_green(self, grad_env):
        grad_env.config.write_text(json.dumps({"streaming_gates": "on"}))
        taken = graduation.maybe_transition(v2_row("FALSE_GREEN"))
        assert taken == "demotion"
        assert json.loads(grad_env.config.read_text())["streaming_gates"] == "shadow"
        events = graduation._read_jsonl(grad_env.grad)
        assert events[-1]["event"] == "demotion"

    def test_regraduation_needs_fresh_evidence(self, grad_env):
        # 20 decisive samples, then demotion, then one new agree: not ready —
        # pre-demotion rows keep their original (pre-demotion) timestamps.
        old_rows = decisive_set(20, 5)
        write_jsonl(grad_env.shadow, old_rows)
        grad_env.config.write_text(json.dumps({"streaming_gates": "on"}))
        graduation.maybe_transition(v2_row("FALSE_GREEN"))
        write_jsonl(grad_env.shadow,
                    old_rows + [v2_row("agree", ts=time.time() + 10)])
        assert graduation.maybe_transition(v2_row("agree")) is None
        assert json.loads(grad_env.config.read_text())["streaming_gates"] == "shadow"

    def test_on_mode_non_false_green_no_demotion(self, grad_env):
        grad_env.config.write_text(json.dumps({"streaming_gates": "on"}))
        assert graduation.maybe_transition(v2_row("agree")) is None
        assert json.loads(grad_env.config.read_text())["streaming_gates"] == "on"


# ===========================================================================
# real_verdict — reads the post-P0 ledger via case_file
# ===========================================================================

@pytest.mark.unit
class TestRealVerdict:
    @pytest.fixture
    def hook(self):
        import hook_shadow_stop
        return importlib.reload(hook_shadow_stop)

    def _with_ledger(self, hook, ledger):
        from case_file import verify_ledger_path
        sid = "dandori-test-verdict"
        p = verify_ledger_path(sid)
        p.write_text(json.dumps(ledger))
        try:
            return hook.real_verdict({"session_id": sid})
        finally:
            p.unlink()

    def test_pass(self, hook):
        assert self._with_ledger(hook, {
            "last_edit_at": 10, "last_verified_at": 20,
            "verified_clean": True}) == "PASS"

    def test_fail(self, hook):
        assert self._with_ledger(hook, {
            "last_edit_at": 10, "last_verified_at": 20,
            "verified_clean": False}) == "FAIL"

    def test_skip_no_edits(self, hook):
        assert self._with_ledger(hook, {
            "last_edit_at": 0, "last_verified_at": 0,
            "verified_clean": True}) == "SKIP"

    def test_unknown_unverified(self, hook):
        assert self._with_ledger(hook, {
            "last_edit_at": 30, "last_verified_at": 20,
            "verified_clean": True}) == "UNKNOWN"

    def test_unknown_no_ledger(self, hook):
        assert hook.real_verdict(
            {"session_id": "dandori-test-no-such-ledger"}) == "UNKNOWN"

    def test_unknown_no_session(self, hook, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        assert hook.real_verdict({}) == "UNKNOWN"


# ===========================================================================
# hook main — authoritative block path (in-process, fake stream_gates)
# ===========================================================================

def run_hook_main(monkeypatch, capsys, *, decision, authoritative,
                  stop_hook_active=False, tmp_path=None):
    fake_sg = types.SimpleNamespace(
        recorded_files=lambda s: ["/tmp/x.py"],
        evaluate=lambda s, f: {
            "mode": "on" if authoritative else "shadow",
            "authoritative": authoritative,
            "decision": decision,
            "verdicts": {"/tmp/proj": decision},
        },
    )
    fake_grad = types.SimpleNamespace(maybe_transition=lambda rec: None)
    monkeypatch.setitem(sys.modules, "stream_gates", fake_sg)
    monkeypatch.setitem(sys.modules, "graduation", fake_grad)

    import hook_shadow_stop
    hook = importlib.reload(hook_shadow_stop)
    if tmp_path is not None:
        monkeypatch.setattr(hook, "LOG", tmp_path / "shadow_log.jsonl")
    payload = {"session_id": "dandori-test-block",
               "stop_hook_active": stop_hook_active}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = hook.main()
    out = capsys.readouterr().out
    return rc, out


@pytest.mark.unit
class TestAuthoritativeBlock:
    def test_blocks_on_fail_when_authoritative(self, monkeypatch, capsys, tmp_path):
        rc, out = run_hook_main(monkeypatch, capsys, decision="FAIL",
                                authoritative=True, tmp_path=tmp_path)
        assert rc == 0
        assert json.loads(out)["decision"] == "block"

    def test_no_block_in_shadow(self, monkeypatch, capsys, tmp_path):
        rc, out = run_hook_main(monkeypatch, capsys, decision="FAIL",
                                authoritative=False, tmp_path=tmp_path)
        assert rc == 0
        assert out.strip() == ""

    def test_no_block_on_pass(self, monkeypatch, capsys, tmp_path):
        rc, out = run_hook_main(monkeypatch, capsys, decision="PASS",
                                authoritative=True, tmp_path=tmp_path)
        assert rc == 0
        assert out.strip() == ""

    def test_no_block_when_stop_hook_active(self, monkeypatch, capsys, tmp_path):
        rc, out = run_hook_main(monkeypatch, capsys, decision="FAIL",
                                authoritative=True, stop_hook_active=True,
                                tmp_path=tmp_path)
        assert rc == 0
        assert out.strip() == ""

    def test_record_written_v2(self, monkeypatch, capsys, tmp_path):
        run_hook_main(monkeypatch, capsys, decision="PASS",
                      authoritative=False, tmp_path=tmp_path)
        rows = [json.loads(l) for l in
                (tmp_path / "shadow_log.jsonl").read_text().splitlines()]
        assert rows[-1]["v"] == 2
        assert rows[-1]["agreement"] in (
            "agree", "false_alarm", "no_real_signal", "inconclusive")
