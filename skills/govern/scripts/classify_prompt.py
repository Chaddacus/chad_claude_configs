#!/usr/bin/env python3
"""Lightweight prompt classifier for UserPromptSubmit hook.

Thin adapter: reads the hook's stdin JSON payload, classifies the prompt via
the SHARED policy module (~/.claude/bin/route_classifier.py — single source
of classification truth for this hook AND auto_runtime_common.classify_route),
writes the session route file + durable decision record, and emits the
route-gated policy injection blocks.

Must complete in <100ms — no network calls, no heavy imports.
Output: JSON envelope with additionalContext (route status + policy blocks).
"""

# PEP 604 annotations (`str | None`) are evaluated at def time on the 3.9
# interpreter this hook runs under, so they raise TypeError without this
# import — and a hook that raises produces no envelope at all, silently
# disabling classification for every prompt. route_classifier.py carries the
# same import for the same reason.
from __future__ import annotations

import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

# hook_profile and route_classifier live in the runtime bin. CLAUDE_HOME may
# be overridden to redirect STATE (tests isolating route_decisions.jsonl), so
# also add the real runtime home as an import fallback rather than crashing.
sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"))
sys.path.insert(1, os.path.expanduser("~/.claude/bin"))
from hook_profile import should_run
# NOTE: the should_run() profile gate is applied inside main(), not at import
# time. An import-time sys.exit(0) made this module unimportable as a library
# (tests, the shared classifier) and killed host processes silently.
from route_classifier import (
    classify, count_file_mentions, deliverable_kind, is_continuation,
)


def classify_prompt(prompt: str) -> dict:
    """Back-compat entry point — delegates to the shared policy module.

    The tiered keyword sets, file-mention regex, and route thresholds that
    used to live here moved to bin/route_classifier.py on 2026-07-16 (audit
    finding H4: this file and auto_runtime_common had drifted into two
    disagreeing classifiers). Keep this name stable for existing importers.
    """
    result = classify(prompt)
    # Preserve the historical output shape for route-file consumers
    # (stop_gate, hook_profile): evidence is additive, not breaking.
    return result


# ---------------------------------------------------------------------------
# Route-gated policy blocks
#
# These blocks moved out of ~/.claude/CLAUDE.md on 2026-04-17 to save tokens
# on R1/R2 prompts where they don't apply. They are injected into the session
# via UserPromptSubmit additionalContext only when the classified route calls
# for them. CLAUDE.md retains a minimal stub pointing at this file as the
# source of truth.
# ---------------------------------------------------------------------------

ANTI_STOP_PATTERNS = """## Anti-stop patterns (autonomous runs)

These failure modes caused a "fully autonomous" run to stop at 7/10 slices. Disallowed in all future autonomous runs:

1. **"Manual" in the plan is not a stop signal.** Write the code, ship the slice, flag the human-verification step as a pre-merge gate. Do not defer code because verification will eventually need a human.
2. **One failed probe is not an unresolvable external dependency.** Try at least 3 different approaches (running container API, docker-exec, git history, different branch, scaffold-with-placeholder) before declaring blocked.
3. **Output-dependency is not code-dependency.** Write slice B against a placeholder A-artifact if A isn't ready. "Blocked on A" only applies when B cannot be written without A's specific shape.
4. **Plan scope is ALL slices, not the first N easy ones.** No false completion summaries. Every slice must be shipped, explicitly deprecated with justification, or blocked on a genuine boundary.
5. **Budget authorizes spending, it does not require hoarding.** If the slice needs the authorized budget, spend it.
6. **Stop hook is a memory checkpoint, not an exit signal.** After AUTO-SAVE, continue the loop. Only legitimate exits: genuine ambiguity, unresolvable external dependency, authority boundary.

When any of these patterns starts to form ("this needs a browser check so I'll defer", "the corpus isn't in /foo so it must not exist locally", "B is blocked on A so I'll stop"), name the pattern, identify which anti-pattern applies, and take the next step anyway."""


ANTI_OVERRUN_PATTERNS = """## Anti-overrun patterns (all runs)

The mirror image of anti-stop. On 2026-06-10 an advisory request became an unratified implementation sprint under stop-gate pressure. Disallowed:

1. **An agent-authored proposal is not a user instruction.** Implementing your own proposal requires explicit user direction or a filed fork record the user has answered. "Permission to work is implied" covers the work REQUESTED, not adjacent work you invented.
2. **Hook pressure is not user intent.** When a stop-gate block conflicts with the user's request scope, restate the stop as a direction fork without permission-seeking phrasing — restated stops are never re-blocked (recursion guard in stop_gate.py).
3. **Speed does not waive grounding.** Evidence scales with claims: "written/parses" rests on static checks; "works/operational" requires an execution run. A plan naming a config surface must cite the consumer file:line before shipping.
4. **A defensible recommendation is a decision, not a menu.** When the fork is *which approach* for work the user already set in motion and you hold a clear recommendation, take it and continue (state choice + one-line why + reversibility) — do not bounce "A or B?" back. This carve-out does NOT loosen #1: inventing adjacent scope is still a fork you name and do not run."""


R3_R4_GOVERNANCE_GATES = """## R3/R4 governed-lanes gates

- Use the governed path: run omni-mem retrieval, use planning-gate skill, satisfy prompt-contract requirements, run validation before closeout.
- R3/R4 require planning-gate and evidence-backed track closure (auto_runtime update-node --evidence, cycle to OBJECTIVE_COMPLETE). The legacy Ralph postflight chain runs only under claude_run — it is NOT a live gate on this path; do not defer to it.
- Align before broad execution: explore repo facts first, then resolve only the product/authority ambiguity that cannot be discovered locally.
- Convert broad work into PRD/story-shaped slices when useful, but revalidate old PRDs, plans, and issue text against current code before trusting them.
- Prefer vertical slices/tracer bullets over horizontal database-then-API-then-UI phases unless dependencies force a horizontal step.
- Keep architecture explicit: identify modules/interfaces expected to change, prefer deep modules with simple testable boundaries, and avoid scattering behavior across shallow helpers.
- R3 defaults to execution_shape=single_lane; bounded_swarm requires explicit justification and reuse-first proof.
- R4 may use a reviewer-centered bounded_swarm with the same justification requirements.
- Plans must include a solution ladder (L1_patch, L2_abstraction, L3_operating_surface) and select the highest useful layer.
- Plans must record existing_primitives_considered, reuse_first_decision, estimated_files_touched, estimated_loc.
- Plans that exceed the simplicity budget or introduce a new runtime surface without proof must fail closed.
- finalize_gate.py must return ok=true before R3/R4 work is treated as approved.
- For R3/R4, produce a manual enterprise scorecard if no automated rubric tool exists."""


# Product-trigger keywords for product-orchestrator nudge. Mirrored in
# ~/.claude/bin/product_truth_auto_dispatch.py — keep in sync.
PRODUCT_TRIGGER_RE = re.compile(
    r"\b("
    r"product"
    r"|launch(?:ed|ing)?"
    r"|release"
    r"|landing\s+page"
    r"|positioning"
    r"|pitch"
    r"|marketing"
    r"|claim"
    r"|differentiation"
    r"|wedge"
    r"|truth\s+layer"
    r"|prove[\s-]?it"
    r")\b",
    re.IGNORECASE,
)
PRODUCT_TRIGGER_HITS_REQUIRED = 2

PRODUCT_TRIGGER_NUDGE = """[product-trigger] This prompt looks product-shaped. The Stop hook at \
~/.claude/bin/product_truth_auto_dispatch.py will block close unless \
~/.claude/state/product_truth/<slug>.json exists and product_truth_check.py passes. \
Dispatch the product-orchestrator agent via Task tool to scaffold/update the truth layer. \
Bypass for this session: export OMNI_MEM_PRODUCT_TRUTH_BYPASS=1."""


def product_trigger_block(prompt: str) -> str:
    """Return the product-trigger nudge string if prompt has ≥2 keyword hits."""
    if not prompt:
        return ""
    hits = len(PRODUCT_TRIGGER_RE.findall(prompt))
    if hits >= PRODUCT_TRIGGER_HITS_REQUIRED:
        return PRODUCT_TRIGGER_NUDGE
    return ""


def route_policy_block(result: dict) -> str:
    """Build the route-specific policy block to inject into the session.

    Returns empty string for R1/R2 (no extra policy beyond always-loaded
    CLAUDE.md). Returns anti-stop + R3/R4 gates for R3/R4/R5 prompts.
    """
    route = result.get("route_hint", "R1")
    if route in ("R1", "R2"):
        return ""
    return ANTI_STOP_PATTERNS + "\n\n" + ANTI_OVERRUN_PATTERNS + "\n\n" + R3_R4_GOVERNANCE_GATES


def _read_hook_payload() -> tuple[str, str]:
    """Return (prompt, session_id) for this hook invocation.

    Claude Code delivers UserPromptSubmit input as JSON on stdin —
    {"hook_event_name": "UserPromptSubmit", "prompt": "...", "session_id":
    "...", "transcript_path": "...", ...} (verified against the shipped
    v2.1.211 dispatch source). CLAUDE_USER_PROMPT is NOT a product interface;
    it is kept only as an explicit test override.

    2026-07-16 regression note: this hook previously read CLAUDE_USER_PROMPT
    (never set by the product) and fell back to classifying the RAW stdin
    envelope — i.e. every real prompt for months was classified as JSON text
    (route_decisions.jsonl: zero R1 rows, 99.9% phantom file_count). Parse the
    JSON; never classify the envelope.
    """
    # "" means unset — callers export CLAUDE_USER_PROMPT="" to force the
    # stdin path (pre-existing test contract).
    env_prompt = os.environ.get("CLAUDE_USER_PROMPT") or None
    payload_session = ""
    prompt = ""
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                prompt = str(data.get("prompt") or "")
                payload_session = str(data.get("session_id") or "")
            elif env_prompt is None:
                # Plain-text stdin (manual invocation / piping in a prompt).
                prompt = raw
    if env_prompt is not None:
        prompt = env_prompt
    session_id = (
        payload_session
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
        or "default"
    )
    return prompt, session_id


def _last_session_route(session_id: str) -> str | None:
    """Most recent non-continuation route recorded for this session.

    Reads the tail of route_decisions.jsonl rather than the whole file — the
    log is ~1MB and this runs on the prompt hot path. Returns None when the
    session has no prior decision (first prompt of a session).
    """
    if not session_id or session_id == "default":
        return None
    log = Path(
        os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude"))
    ) / "state" / "route_decisions.jsonl"
    try:
        with log.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            window = min(size, 256 * 1024)
            f.seek(size - window)
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # partial first line from the window cut
        if row.get("session_id") != session_id:
            continue
        if row.get("inherited_from_continuation"):
            continue  # do not chain inheritance off another continuation
        route = row.get("route_hint")
        if route:
            return str(route)
    return None


def main():
    # Profile gate (moved from import time so the module stays importable).
    if not should_run("classify_prompt"):
        return

    prompt, session_id = _read_hook_payload()

    result = classify_prompt(prompt)
    result["deliverable_kind"] = deliverable_kind(prompt)

    # Continuation handling. A bare "yes"/"both"/"go ahead" is the user
    # answering the turn already in flight, not a new ambiguous request — but
    # is_vague (<5 words, no files) cannot see that and routed it R5, pulling
    # the full governance block AND telling the agent to clarify ambiguity the
    # user had just resolved. Inherit the route the session was already on so
    # a continuation authorizing R4 work keeps its R4 gates; fall back to R1
    # when there is no prior turn to continue.
    inherited = False
    if is_continuation(prompt):
        prior = _last_session_route(session_id)
        result["route_hint"] = prior or "R1"
        result["governance_recommended"] = (result["route_hint"] in ("R3", "R4"))
        result["reason"] = (
            f"continuation of {prior} turn" if prior
            else "continuation with no prior turn"
        )
        inherited = True

    # --- Slice 5: bandit enabler (additive instrumentation) -------------------
    # decision_id is the per-prompt join-key tying this routing decision to the
    # session's stop outcome (read by stop_reason_telemetry.py).
    decision_id = secrets.token_hex(8)   # 16-char hex, 64-bit entropy
    word_count = len(prompt.split()) if prompt else 0
    file_count = count_file_mentions(prompt)
    result["decision_id"] = decision_id
    result["word_count"] = word_count
    result["file_count"] = file_count
    # --------------------------------------------------------------------------

    # Write route to session-scoped temp file for downstream hook profile
    # gating (session_id resolved from the hook payload, env as fallback).
    route_file = f"/tmp/claude-route-{session_id}.json"
    try:
        with open(route_file, "w") as f:
            json.dump(result, f)
    except OSError:
        pass

    # --- Slice 5: durable decision record (bandit training data) --------------
    # Append to route_decisions.jsonl. Silent on any I/O error — hot path.
    decision_log = Path(
        os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude"))
    ) / "state" / "route_decisions.jsonl"
    try:
        decision_log.parent.mkdir(parents=True, exist_ok=True)
        with decision_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "decision_id": decision_id,
                "route_hint": result["route_hint"],
                "governance_recommended": result["governance_recommended"],
                "reason": result["reason"],
                "deliverable_kind": result["deliverable_kind"],
                "word_count": word_count,
                "file_count": file_count,
                "inherited_from_continuation": inherited,
            }, sort_keys=True) + "\n")
    except OSError:
        pass
    # --------------------------------------------------------------------------

    status = (
        f"[route-classifier] route_hint={result['route_hint']} "
        f"governance_recommended={result['governance_recommended']} "
        f"reason={result['reason']}"
    )
    policy = route_policy_block(result)
    product_nudge = product_trigger_block(prompt)

    pieces = [status]
    if policy:
        pieces.append(policy)
    if product_nudge:
        pieces.append(product_nudge)
    context = "\n\n".join(pieces)

    envelope = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    print(json.dumps(envelope))


if __name__ == "__main__":
    main()
