#!/usr/bin/env python3
"""Stop hook: refuse to let the agent stop in a report-and-wait shape.

The failure mode: agent finishes a slice, then writes "I recommend X / next
step would be Y / should I continue?" and stops — waiting for permission to
do work that permission was already granted for.

Detection is purely lexical. The agent's own word choice is the signal.
Three legitimate stops do NOT use these phrases naturally:
  - completion: "Done. Tests pass: X"
  - direction conflict: "Two paths: A or B — which?"
  - authority boundary: "About to rm -rf X — confirm?"

Anything matching the permission-seeking regex AND containing no completion
or authority markers gets blocked back into the loop.

Wire as a Stop hook AFTER memory-save / telemetry, so checkpoints fire even
when this gate blocks.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# Local imports (case-file library)
sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"))
try:
    from case_file import (
        read_summary,
        read_completion,
        is_zero_test_output,
    )
except Exception:  # pragma: no cover - degraded mode if module missing
    read_summary = lambda: {}  # type: ignore
    read_completion = lambda: None  # type: ignore
    is_zero_test_output = lambda _t: False  # type: ignore

# L2 config — log-only by default until calibrated.
CONFIG_PATH = Path(os.path.expanduser("~/.claude/state/stop_gate_config.json"))


def audit_log_path(session_id: str) -> Path:
    sid = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "") or "unknown"
    return Path(os.path.expanduser(f"~/.claude/state/stop_gate_audit-{sid}.jsonl"))

DEFAULT_CONFIG = {
    "lexical": "block",         # block | log | off
    "evidentiary": "log",       # block | log | off  (start log-only)
    "rules": {
        "verification_claims": True,
        "scope_claims": True,
        "state_claims": False,
        "edit_without_verify": True,
        "slice_reconciliation": True,
        "empty_diff_completion": True,
        "functional_claims": True,  # "operational/working/live" needs an execution run
        "completion_record_required": False,  # set True after agent is trained to file
    },
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg["rules"] = dict(DEFAULT_CONFIG["rules"])
    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text())
            if isinstance(user_cfg, dict):
                for k, v in user_cfg.items():
                    if k == "rules" and isinstance(v, dict):
                        cfg["rules"].update(v)
                    else:
                        cfg[k] = v
        except Exception:
            pass
    return cfg

# Permission-seeking / report-and-wait shapes. Case-insensitive.
#
# Two pools (2026-06-10 redesign). STALL: mid-task permission begging —
# gated on every prompt kind. DELIVERABLE: recommendation-shaped phrasing —
# on an advisory prompt (deliverable_kind == "advice" in the route file
# written by classify_prompt.py) a recommendation IS the completed
# deliverable, so this pool is gated only when the prompt asked for an
# artifact. Root cause being fixed: the gate punished the correct shape of
# a finished advisory answer and pushed unrequested implementation.
STALL_SEEKING = re.compile(
    r"\b("
    r"should i\b|"
    r"shall i\b|"
    r"would you like\b|"
    r"let me know (if|whether|when)\b|"
    r"ready to (proceed|continue|move on)\b|"
    r"i'?ll (now|proceed|continue|go ahead)\b|"
    r"do you want (me to|to)\b|"
    r"want me to\b|"
    r"if you'?d like\b|"
    r"happy to (continue|proceed|do)\b"
    r")",
    re.IGNORECASE,
)

# Recommendation phrasing only. Offer-shaped begging ("want me to") is a
# stall on every prompt kind and lives in STALL_SEEKING (Codex review
# finding #1, 2026-06-10).
DELIVERABLE_SEEKING = re.compile(
    r"\b("
    r"i recommend\b|"
    r"my recommendation\b|"
    r"next steps? (would|will) be\b|"
    r"the next step (is|would be)\b"
    r")",
    re.IGNORECASE,
)

# Escape hatch markers — if present, allow the stop even if a permission
# phrase appears (e.g., a genuine either/or question, or explicit destructive
# confirmation request).
ALLOW_MARKERS = re.compile(
    r"("
    r"\bconfirm\b.*\b(delete|destroy|drop|rm -rf|force[- ]push|push --force)\b|"
    r"\b(delete|destroy|drop|rm -rf|force[- ]push|push --force)\b.*\bconfirm\b|"
    r"\bauthoriz(e|ation)\b.*\b(spend|cost|charge|\$)|"
    r"\bspend\b.*\bauthoriz(e|ation)\b|"
    # Explicit fork: "A or B — which"
    r"\bwhich (do you|would you|of these)\b|"
    r"\b(option a|option b|path a|path b)\b|"
    # Ratification-shaped stops: a delivered decision awaiting the user's word
    r"\byour call\b|"
    r"\bdecision (needed|required)\b|"
    r"\bawaiting (your )?direction\b|"
    r"\bratif(y|ied|ication)\b|"
    r"\bsay the word\b|"
    r"\bgreen-?light\b"
    r")",
    re.IGNORECASE,
)

BLOCK_REASON = (
    "Stop-gate (matched: {match!r}). Classify this stop BEFORE reacting: "
    "(a) completion with evidence, (b) direction fork between named "
    "incompatible outcomes, (c) destructive/external action needing "
    "authorization. If one applies, RESTATE the stop without the "
    "permission-seeking phrasing — restated stops are never re-blocked "
    "(recursion guard). If none applies, take the next step OF THE "
    "REQUESTED WORK. An agent-authored proposal is not a user instruction: "
    "if the only remaining step is implementing your own proposal, that is "
    "a direction fork — state it as one, do not start the work."
)


def read_input() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


ROUTE_FILE_MAX_AGE_S = 2 * 3600  # stale advice must not relax later prompts


def read_deliverable_kind(session_id: str) -> str:
    """Read deliverable_kind from the route file classify_prompt.py writes
    (classify_prompt.py main(): /tmp/claude-route-<session>.json). Missing,
    stale (>2h), wrong-owner, world-writable, or malformed -> "artifact"
    (strict: preserves pre-redesign behavior when the channel is absent).

    Trust note: this file can only RELAX the DELIVERABLE pool, never the
    STALL pool. Same-user tampering is the same trust domain as editing this
    gate itself; the ownership/mode/freshness checks kill cross-user and
    stale-file paths (Codex review findings #3/#4, 2026-06-10).

    The candidate list covers "the payload omitted session_id", so ONLY a
    missing file advances to the next candidate. A file that exists but fails
    a trust check ends the search at "artifact": advancing past it let the
    ambient CLAUDE_CODE_SESSION_ID's route file stand in for the rejected
    one, which relaxed the DELIVERABLE pool via exactly the stale and
    world-writable paths #3/#4 exist to close (fixed 2026-07-27)."""
    candidates = [
        session_id,
        os.environ.get("CLAUDE_CODE_SESSION_ID"),
        os.environ.get("CLAUDE_SESSION_ID"),
    ]
    for sid in candidates:
        if not sid:
            continue
        sid = re.sub(r"[^A-Za-z0-9_-]", "_", sid)
        path = Path(f"/tmp/claude-route-{sid}.json")
        try:
            st = path.stat()
        except OSError:
            continue  # no channel for this candidate -> try the next one
        # From here the channel for this session is decided, pass or fail.
        if st.st_uid != os.getuid():
            return "artifact"
        if st.st_mode & 0o002:  # world-writable
            return "artifact"
        if time.time() - st.st_mtime > ROUTE_FILE_MAX_AGE_S:
            return "artifact"
        try:
            kind = json.loads(path.read_text()).get("deliverable_kind")
        except Exception:
            return "artifact"
        return kind if kind in ("advice", "artifact") else "artifact"
    return "artifact"


# Strip code/quoted spans so meta-mentions of the regex don't trigger it.
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
# Single-star italics, max 3 words: meta-mentions of gate phrases are
# phrase-length (*should I*). Longer italic spans stay scanned so a real
# stall sentence cannot hide in emphasis (*Should I continue with this?*
# still matches). Bold (**…**) is never stripped.
_ITALIC = re.compile(r"(?<!\*)\*[^*\n ]+(?: [^*\n ]+){0,2}\*(?!\*)")
_DOUBLE_QUOTED = re.compile(r"\"[^\"\n]{1,200}\"")
_SINGLE_QUOTED = re.compile(r"'[^'\n]{1,200}'")
_BLOCKQUOTE = re.compile(r"^>.*$", re.MULTILINE)
_TABLE_ROW = re.compile(r"^\|.*\|.*$", re.MULTILINE)


def strip_non_prose(text: str) -> str:
    """Remove regions that don't count as the agent's own speech:
    code fences, inline code, quoted strings, blockquotes, table rows.
    These are common false-positive surfaces (the agent describes the
    rule and the rule's own phrases appear verbatim)."""
    text = _CODE_FENCE.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _ITALIC.sub(" ", text)
    text = _DOUBLE_QUOTED.sub(" ", text)
    text = _SINGLE_QUOTED.sub(" ", text)
    text = _BLOCKQUOTE.sub(" ", text)
    text = _TABLE_ROW.sub(" ", text)
    return text


def last_assistant_text(transcript_path: str) -> str:
    """Return concatenated text from the most recent assistant message in the
    transcript JSONL. Empty string if not found or unreadable."""
    if not transcript_path:
        return ""
    p = Path(transcript_path).expanduser()
    if not p.exists() or not p.is_file():
        return ""

    last_text = ""
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                msg = entry.get("message") if isinstance(entry.get("message"), dict) else entry
                role = msg.get("role") if isinstance(msg, dict) else None
                if role != "assistant":
                    continue
                content = msg.get("content")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            t = block.get("text", "")
                            if isinstance(t, str):
                                parts.append(t)
                    text = "\n".join(parts)
                if text.strip():
                    last_text = text  # keep overwriting; final value = last
    except Exception:
        return ""
    return last_text


# === L2: Evidentiary checks =================================================

# Falsifiable verification claims (claim text → required tool kind).
VERIFICATION_CLAIMS = [
    (re.compile(r"\b(\d+\s*)?tests?\s+(pass(es|ed|ing)?|green)\b", re.I), "test"),
    (re.compile(r"\ball\s+tests?\s+(pass(es|ed|ing)?|green)\b", re.I), "test"),
    (re.compile(r"\btypecheck(s|ing)?\s+(clean|pass(es|ed|ing)?|green)\b", re.I), "typecheck"),
    (re.compile(r"\b(lint(er|ing)?|build)\s+(clean|green|pass(es|ed|ing)?)\b", re.I), "lint_or_build"),
]

SCOPE_COMPLETION = re.compile(
    r"\b(task complete|all slices done|fully done|all done|ship it|ready to ship)\b",
    re.I,
)

IMPLEMENTATION_CLAIM = re.compile(
    r"\b(implemented|added|fixed|refactored|wrote|built|created)\s+",
    re.I,
)

STATE_CLAIMS = [
    (re.compile(r"\b(merged|pr merged|merged to (main|master))\b", re.I), "pr_merge"),
    (re.compile(r"\b(pushed|branch pushed)\b", re.I), "git_push"),
    (re.compile(r"\bcommitted\b", re.I), "git_commit"),
]

# Functional-status claims: stronger than "written", weaker than a named
# test claim. "Operational" with only a syntax check is the failure mode
# this lexicon exists to catch (2026-06-10). Deliberately conservative:
# bare "working"/"works"/"live" false-positive on "working on it" /
# "how it works" — only unambiguous functional-status phrasings listed.
FUNCTIONAL_CLAIMS = re.compile(
    r"\b(operational|production[- ]ready|fully (working|functional)|"
    r"up and running|ready to use|live now|confirmed working|"
    r"works end[- ]to[- ]end)\b",
    re.I,
)


def _has_verification(summary: dict, kind: str) -> bool:
    """Did this session run a verification of given kind that passed?"""
    target_kinds = {kind} if kind != "lint_or_build" else {"lint", "build"}
    for v in summary.get("verifications", []):
        if v.get("kind") in target_kinds and v.get("exit") == 0 and not is_zero_test_output(v.get("summary", "")):
            return True
    return False


def _has_state_mutation(summary: dict, kind: str) -> bool:
    for m in summary.get("state_mutations", []):
        if m.get("kind") == kind and m.get("exit") == 0:
            return True
    return False


def evidentiary_check(prose: str, cfg: dict, session_id: str | None = None) -> list[dict]:
    """Return list of findings (empty = pass). Each finding:
       {rule, claim, missing_evidence}.
    Evidence is read from THIS session's case file only — binding claims to
    the current session's recorded tool activity (P0.2, 2026-06-09)."""
    findings: list[dict] = []
    rules = cfg.get("rules", {})
    try:
        summary = read_summary(session_id) if session_id else read_summary()
        completion = read_completion(session_id) if session_id else read_completion()
    except Exception:
        return findings  # Fail open on infrastructure error

    # Rule: verification_claims
    if rules.get("verification_claims"):
        for pat, kind in VERIFICATION_CLAIMS:
            m = pat.search(prose)
            if m and not _has_verification(summary, kind):
                findings.append({
                    "rule": "verification_claims",
                    "claim": m.group(0),
                    "missing_evidence": f"no passing {kind} run in this session's case file",
                })

    # Rule: functional_claims — "operational/working/live" requires at least
    # one passing execution (any verification kind) recorded this session.
    if rules.get("functional_claims"):
        m = FUNCTIONAL_CLAIMS.search(prose)
        if m:
            has_exec = any(
                v.get("exit") == 0 for v in summary.get("verifications", [])
            )
            if not has_exec:
                findings.append({
                    "rule": "functional_claims",
                    "claim": m.group(0),
                    "missing_evidence": "functional-status claim with no passing execution recorded this session",
                })

    # Rule: state_claims (off by default — opt-in)
    if rules.get("state_claims"):
        for pat, kind in STATE_CLAIMS:
            m = pat.search(prose)
            if m and not _has_state_mutation(summary, kind):
                findings.append({
                    "rule": "state_claims",
                    "claim": m.group(0),
                    "missing_evidence": f"no successful {kind} call recorded",
                })

    # Rule: edit_without_verify
    if rules.get("edit_without_verify"):
        last_edit = summary.get("last_edit_at", 0)
        last_pass = summary.get("last_passing_verify_at", 0)
        if last_edit and last_edit > last_pass + 1:  # +1s slack
            # only fire if a verification claim or scope claim is present
            has_claim = any(pat.search(prose) for pat, _ in VERIFICATION_CLAIMS) or \
                        SCOPE_COMPLETION.search(prose) is not None
            if has_claim:
                findings.append({
                    "rule": "edit_without_verify",
                    "claim": "code edited after last passing verification",
                    "missing_evidence": f"last_edit_at={last_edit:.0f} > last_passing_verify_at={last_pass:.0f}",
                })

    # Rule: empty_diff_completion
    if rules.get("empty_diff_completion"):
        if IMPLEMENTATION_CLAIM.search(prose):
            if not summary.get("files_touched"):
                findings.append({
                    "rule": "empty_diff_completion",
                    "claim": "implementation claim with no file edits this session",
                    "missing_evidence": "summary.files_touched is empty",
                })

    # Rule: completion_record_required (opt-in once agent is trained)
    if rules.get("completion_record_required"):
        if SCOPE_COMPLETION.search(prose) and completion is None:
            findings.append({
                "rule": "completion_record_required",
                "claim": "task-complete claim with no completion.json filed",
                "missing_evidence": "run claim_complete.py before stopping",
            })

    # Rule: slice_reconciliation — only fires when a track exists. Reading
    # auto_runtime state is out of scope for v1; this rule is a placeholder
    # for when the track integration lands.
    return findings


def write_audit(turn_id: str, mode: str, findings: list[dict], blocked: bool) -> None:
    try:
        log_path = audit_log_path(turn_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.time(),
                "turn_id": turn_id,
                "mode": mode,
                "blocked": blocked,
                "findings": findings,
            }) + "\n")
    except Exception:
        pass


def main() -> int:
    data = read_input()

    # Recursion guard: if we already blocked once this stop-cycle, let it go.
    if data.get("stop_hook_active"):
        print("{}")
        return 0

    cfg = load_config()
    session_id = data.get("session_id", "")

    # Prefer explicit field if present; fall back to transcript scan.
    msg = data.get("last_assistant_message") or ""
    if not msg:
        msg = last_assistant_text(data.get("transcript_path", ""))

    msg = msg.strip()
    if not msg:
        # Tool-only turn or empty — never block.
        print("{}")
        return 0

    # Strip code/quoted regions before regex (avoid catching meta-mentions
    # of the rules themselves).
    prose = strip_non_prose(msg)

    # === L1: Lexical (permission-seeking) ===
    # STALL is absolute — no allow-marker bypass ("Decision needed: should I
    # continue?" must still block; Codex review finding #2). ALLOW_MARKERS
    # and the advice deliverable_kind relax only the DELIVERABLE pool.
    if cfg.get("lexical", "block") != "off":
        m = STALL_SEEKING.search(prose)
        if m is None and not ALLOW_MARKERS.search(prose) \
                and read_deliverable_kind(session_id) == "artifact":
            m = DELIVERABLE_SEEKING.search(prose)
        if m:
            if cfg.get("lexical") == "block":
                reason = BLOCK_REASON.format(match=m.group(1))
                write_audit(session_id, "lexical", [{"rule": "permission_seeking", "match": m.group(1)}], True)
                print(json.dumps({"decision": "block", "reason": reason}))
                return 0
            else:  # log
                write_audit(session_id, "lexical-log", [{"rule": "permission_seeking", "match": m.group(1)}], False)

    # === L2: Evidentiary ===
    ev_mode = cfg.get("evidentiary", "log")
    if ev_mode != "off":
        findings = evidentiary_check(prose, cfg, session_id or None)
        if findings:
            if ev_mode == "block":
                reason_lines = ["L2 evidentiary gate found unsupported claims:"]
                for f in findings:
                    reason_lines.append(f"  - [{f['rule']}] {f['claim']} — {f['missing_evidence']}")
                reason_lines.append(
                    "Either run the missing verification, file a completion record via "
                    "claim_complete.py, or restate without the unsupported claim."
                )
                write_audit(session_id, "evidentiary", findings, True)
                print(json.dumps({"decision": "block", "reason": "\n".join(reason_lines)}))
                return 0
            else:  # log
                write_audit(session_id, "evidentiary-log", findings, False)

    print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
