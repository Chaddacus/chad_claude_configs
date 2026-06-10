#!/usr/bin/env python3
"""Deterministic self-graduation for the dandori streaming gate.

Closes the shadow→authoritative loop with explicit predicates over recorded
facts (shadow_log.jsonl), per global policy: the model never makes the flip
decision — these predicates do.

Lifecycle:
  shadow --[ready()]--> on --[FALSE_GREEN observed]--> shadow (demoted)

GRADUATE when, over v2 samples newer than the baseline:
  - false_green == 0                     (hard safety: streamed PASS, real FAIL)
  - decisive >= MIN_DECISIVE (20)        (agree + false_alarm; real signal present)
  - distinct sessions among decisive >= MIN_SESSIONS (5)
  - inconclusive rate < MAX_INCONCLUSIVE_RATE (0.30)

DEMOTE immediately when mode == "on" and a FALSE_GREEN lands. Demotion resets
the baseline: re-graduation requires entirely fresh evidence.

Only "v": 2 records count — records written before the 2026-06-09 real_verdict
fix compared against a ledger path that never existed (668/668 no_real_signal)
and are evidence of nothing.

State surfaces (both under state/dandori/):
  shadow_log.jsonl      — input facts (written by hook_shadow_stop)
  graduation_log.jsonl  — transition events; baseline = ts of last demotion

Usage: imported by hook_shadow_stop (maybe_transition) and shadow_report
(status). CLI: `python3 graduation.py` prints status JSON.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

DANDORI = Path("/Users/chadsimon/.claude/dandori")
STATE = Path("/Users/chadsimon/.claude/state/dandori")
CONFIG = DANDORI / "config.json"
SHADOW_LOG = STATE / "shadow_log.jsonl"
GRAD_LOG = STATE / "graduation_log.jsonl"
NOTIFY = Path("/Users/chadsimon/.claude/bin/notify_done.sh")

MIN_DECISIVE = 20
MIN_SESSIONS = 5
MAX_INCONCLUSIVE_RATE = 0.30


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _mode() -> str:
    try:
        return json.loads(CONFIG.read_text()).get("streaming_gates", "shadow")
    except Exception:
        return "shadow"


def _set_mode(mode: str) -> None:
    cfg = {}
    try:
        cfg = json.loads(CONFIG.read_text())
    except Exception:
        pass
    cfg["streaming_gates"] = mode
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")


def baseline_ts(grad_rows: list) -> float:
    """Evidence baseline: ts of the most recent demotion (0.0 if never)."""
    ts = 0.0
    for r in grad_rows:
        if r.get("event") == "demotion":
            ts = max(ts, float(r.get("ts", 0)))
    return ts


def evaluate(shadow_rows: list, grad_rows: list) -> dict:
    """Pure predicate evaluation — no side effects."""
    base = baseline_ts(grad_rows)
    rows = [r for r in shadow_rows
            if r.get("v") == 2 and float(r.get("ts", 0)) > base]
    decisive_rows = [r for r in rows
                     if r.get("agreement") in ("agree", "false_alarm")]
    false_green = sum(1 for r in rows if r.get("agreement") == "FALSE_GREEN")
    inconclusive = sum(1 for r in rows if r.get("agreement") == "inconclusive")
    total = len(rows)
    sessions = {r.get("session") for r in decisive_rows}
    inconclusive_rate = (inconclusive / total) if total else 1.0
    checks = {
        "no_false_greens": false_green == 0,
        "enough_decisive": len(decisive_rows) >= MIN_DECISIVE,
        "enough_sessions": len(sessions) >= MIN_SESSIONS,
        "inconclusive_rate_ok": inconclusive_rate < MAX_INCONCLUSIVE_RATE,
    }
    return {
        "mode": _mode(),
        "baseline_ts": base,
        "v2_samples": total,
        "decisive": len(decisive_rows),
        "decisive_sessions": len(sessions),
        "false_green": false_green,
        "inconclusive_rate": round(inconclusive_rate, 3),
        "checks": checks,
        "ready": all(checks.values()),
    }


def _append_event(event: dict) -> None:
    event.setdefault("ts", time.time())
    GRAD_LOG.parent.mkdir(parents=True, exist_ok=True)
    with GRAD_LOG.open("a") as fh:
        fh.write(json.dumps(event) + "\n")


def _notify(detail: str) -> None:
    try:
        subprocess.Popen(
            ["bash", str(NOTIFY), "--status", "success",
             "--task", "dandori streaming gate", "--details", detail],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def maybe_transition(latest_record: dict) -> str | None:
    """Called after each shadow record append. Returns the transition taken
    ('graduation' | 'demotion') or None. Never raises."""
    try:
        mode = _mode()
        grad_rows = _read_jsonl(GRAD_LOG)
        if mode == "on":
            if latest_record.get("agreement") == "FALSE_GREEN":
                _set_mode("shadow")
                _append_event({
                    "event": "demotion",
                    "reason": "FALSE_GREEN observed while authoritative",
                    "record": latest_record,
                })
                _notify("DEMOTED to shadow: FALSE_GREEN observed. "
                        "Re-graduation requires fresh evidence.")
                return "demotion"
            return None
        if mode == "shadow":
            status = evaluate(_read_jsonl(SHADOW_LOG), grad_rows)
            if status["ready"]:
                _set_mode("on")
                _append_event({"event": "graduation", "status": status})
                _notify(
                    f"GRADUATED to authoritative: {status['decisive']} decisive "
                    f"samples across {status['decisive_sessions']} sessions, "
                    f"0 false-greens, inconclusive rate "
                    f"{status['inconclusive_rate']}.")
                return "graduation"
        return None
    except Exception:
        return None  # never break the stop path


if __name__ == "__main__":
    print(json.dumps(
        evaluate(_read_jsonl(SHADOW_LOG), _read_jsonl(GRAD_LOG)), indent=2))
