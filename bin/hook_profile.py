"""Hook profile gating module.

Determines the active hook profile from the route classification.
The classify_prompt.py hook writes the current route to a session-scoped
temp file; this module reads it to gate downstream hooks.

Route → Profile mapping:
  R1          → minimal
  R2, R5      → standard
  R3, R4      → strict

TRUTH IN GATING (2026-07-16 audit H5): a profile only gates scripts that
actually call should_run("<id>"). Most hook_chain members (stop_gate,
completion_gate, product_truth_auto_dispatch, replan/self-merge checks,
telemetry, recorders) never check — they run in EVERY profile by design.
The ids in PROFILES below are exactly the self-gating scripts; adding an id
here does nothing unless the script also calls should_run.

Fallback: CLAUDE_HOOK_PROFILE env var, or "standard" if nothing is set.
"""
import json
import os
from pathlib import Path

ROUTE_TO_PROFILE = {
    "R1": "minimal",
    "R2": "standard",
    "R3": "strict",
    "R4": "strict",
    "R5": "standard",
}

PROFILES = {
    # Self-gating scripts only — each id below has a script calling
    # should_run with it. CAUTION: two call shapes are DYNAMIC and easy to
    # miss in a grep for should_run("<literal>"):
    #   completion_gate.py       -> should_run(f"completion_gate_{_event}")
    #                               (ids completion_gate_stop / _task)
    #   self_merge_check.py      -> should_run(HOOK_PROFILE_ID)
    #   replan_evidence_check.py -> should_run(HOOK_PROFILE_ID)
    # hook_profile_test.py scans all three shapes; keep it that way.
    #
    #   classify_prompt   — the route-file writer; MUST be in every profile.
    #                       Excluding it deadlocks the session in that profile:
    #                       an R1 classification switched to "minimal", gated
    #                       off the classifier, and the route could never
    #                       change again (audit C2, live-demonstrated).
    #   pre_tool_guard, secret_leak_warn, web_search_breaker — safety
    #                       tripwires, enabled in every profile.
    #   what_would_chad_do — self-gates but currently unwired (companion_stop
    #                       has no hook registration); id kept for rewiring.
    # True phantom ids removed 2026-07-16 (audit H5): session_startup,
    # notify_done, codex_review_gate — no script gates itself with them
    # (codex_review_gate.py never existed; session_startup/notify_done are
    # ungated python/bash hooks).
    "minimal": {"pre_tool_guard", "secret_leak_warn", "web_search_breaker",
                "classify_prompt", "completion_gate_stop"},
    "standard": {
        "classify_prompt", "pre_tool_guard", "secret_leak_warn",
        "web_search_breaker", "edit_verify_async", "subagent_verify",
        "tool_failure_context", "what_would_chad_do",
        "completion_gate_stop", "completion_gate_task",
        "replan_evidence_check", "self_merge_check",
    },
    "strict": None,  # None = all hooks enabled
}

_ROUTE_FILE = Path(f"/tmp/claude-route-{os.environ.get('CLAUDE_CODE_SESSION_ID') or os.environ.get('CLAUDE_SESSION_ID') or 'default'}.json")


def _get_profile() -> str:
    """Resolve profile: route file > env var > default."""
    # Try route file first (written by classify_prompt.py)
    try:
        if _ROUTE_FILE.exists():
            data = json.loads(_ROUTE_FILE.read_text())
            route = data.get("route_hint", "")
            if route in ROUTE_TO_PROFILE:
                return ROUTE_TO_PROFILE[route]
    except Exception:
        pass
    # Fallback to env var
    return os.environ.get("CLAUDE_HOOK_PROFILE", "standard")


def should_run(hook_id: str) -> bool:
    profile = _get_profile()
    allowed = PROFILES.get(profile)
    if allowed is None:
        return True
    return hook_id in allowed
