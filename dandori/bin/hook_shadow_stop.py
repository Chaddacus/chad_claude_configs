#!/usr/bin/env python3
"""Stop adapter for the dandori streaming gate.

In shadow mode (non-authoritative): log what the streaming gate WOULD decide,
never block. In "on" mode (graduated): a streamed FAIL blocks the stop.

Mode transitions are made by graduation.py's deterministic predicates over the
shadow log — graduate at evidence thresholds, demote instantly on FALSE_GREEN.

Records are written with "v": 2. v1 records (before 2026-06-09) compared
against a ledger path that never existed and are excluded from evidence.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/chadsimon/.claude/dandori/bin")
sys.path.insert(0, "/Users/chadsimon/.claude/bin")
LOG = Path("/Users/chadsimon/.claude/state/dandori/shadow_log.jsonl")


def real_verdict(payload):
    """Read completion_gate's verdict ledger (it runs earlier in the Stop
    chain) from state/verify-ledgers/ via case_file — the same resolution
    completion_gate itself uses. Returns PASS / FAIL / SKIP / UNKNOWN."""
    try:
        from case_file import resolve_session_id, verify_ledger_path
        sid = resolve_session_id(payload)
        if not sid:
            return "UNKNOWN"
        p = verify_ledger_path(sid)
        if not p.exists():
            return "UNKNOWN"
        led = json.loads(p.read_text())
    except Exception:
        return "UNKNOWN"
    last_edit = led.get("last_edit_at", 0)
    last_ver = led.get("last_verified_at", 0)
    if not last_edit:
        return "SKIP"  # no code edits -> completion_gate would not block
    if last_ver >= last_edit:
        return "PASS" if led.get("verified_clean", False) else "FAIL"
    return "UNKNOWN"  # edits present but no verification ran


def agreement(streamed, real):
    """Classify streamed-vs-real. The only dangerous class is FALSE_GREEN."""
    if streamed in ("INCONCLUSIVE", "MISS", None):
        return "inconclusive"          # gate not ready by stop — feasibility miss
    if real in ("SKIP", "UNKNOWN"):
        return "no_real_signal"
    if streamed == real:
        return "agree"
    if streamed == "PASS" and real == "FAIL":
        return "FALSE_GREEN"           # MUST be zero before flipping to authoritative
    return "false_alarm"               # streamed FAIL, real PASS — safe but noisy


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    try:
        import stream_gates
        session = str(payload.get("session_id") or "default")
        files = stream_gates.recorded_files(session)
        if not files:
            return 0  # nothing edited this session
        res = stream_gates.evaluate(session, files)
        if res is None:
            return 0  # streaming disabled
        streamed = res.get("decision")
        real = real_verdict(payload)
        rec = {"v": 2, "ts": time.time(), "session": session,
               "n_files": len(files),
               "streamed_decision": streamed,
               "real_decision": real,
               "agreement": agreement(streamed, real),
               "authoritative": res.get("authoritative", False),
               "verdicts": res.get("verdicts", {})}
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")

        import graduation
        graduation.maybe_transition(rec)

        # Authoritative path: graduated gate blocks on streamed FAIL.
        # stop_hook_active guard prevents block loops on repeated stops.
        if (res.get("authoritative")
                and streamed == "FAIL"
                and not payload.get("stop_hook_active")):
            failing = {r: v for r, v in rec["verdicts"].items() if v == "FAIL"}
            print(json.dumps({
                "decision": "block",
                "reason": (
                    "Dandori streaming gate (authoritative): project "
                    f"verification FAILED for {failing or rec['verdicts']}. "
                    "Fix the failures before stopping."),
            }))
            return 0
    except Exception:
        pass  # never break a stop on internal error
    return 0


if __name__ == "__main__":
    sys.exit(main())
