"""Hook profile gating module.

Determines the active hook profile from the route classification.
The classify_prompt.py hook writes the current route to a session-scoped
temp file; this module reads it to gate downstream hooks automatically.

Route → Profile mapping:
  R1          → minimal  (health check + notify only)
  R2          → standard (all governance hooks)
  R3, R4, R5  → strict   (all hooks + observation capture)

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
    "minimal": {"session_startup", "notify_done", "completion_gate_stop", "pre_tool_guard"},
    "standard": {
        "session_startup", "notify_done", "completion_gate_stop", "pre_tool_guard",
        "classify_prompt", "edit_verify_async", "completion_gate_task",
        "subagent_verify", "tool_failure_context", "what_would_chad_do",
        "codex_review_gate", "replan_evidence_check", "self_merge_check",
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
