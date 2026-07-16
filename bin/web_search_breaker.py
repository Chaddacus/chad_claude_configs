#!/usr/bin/env python3
"""web_search_breaker.py — PreToolUse/PostToolUse circuit breaker for the NATIVE
WebSearch/WebFetch tools. Thin hook wrapper over web_budget.py (the shared policy, also
used by the web_search.py curl helper — one source of truth, no drift).

Wired twice in settings.json on matcher "WebSearch|WebFetch":
  PreToolUse  -> web_budget.check(): allow (exit 0) or block (exit 2, reason on stderr).
  PostToolUse -> web_budget.record(): a success resets the failure streak + closes the
                 circuit; a failure opens it after MAX_FAILS consecutive.

Registered in hook_profile.py PROFILES (minimal + standard; strict = all) — a hook NOT in
the active profile's allowlist silently no-ops. FAILS OPEN on any internal error.
"""
import json
import os
import sys

CLAUDE_HOME = os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude"))
sys.path.insert(0, os.path.join(CLAUDE_HOME, "bin"))
try:
    from hook_profile import should_run
    if not should_run("web_search_breaker"):
        sys.exit(0)
except Exception:
    pass  # fail open if the profile gate is unavailable

import web_budget  # shared budget/circuit policy

WEB_TOOLS = {"WebSearch", "WebFetch"}

# Hard failure tokens — narrow so a long legitimate result that merely says "error" is not
# flagged; only an explicit error field or a SHORT error-shaped response counts.
FAIL_TOKENS = ("bot-challenge", "captcha", "challenge", "blocked", "forbidden",
               "timed out", "timeout", "no results", "429", "403", "503",
               "rate limit", "quota")


def _is_failure(resp):
    """Heuristic: did this native WebSearch/WebFetch call fail?"""
    if resp is None:
        return False
    if isinstance(resp, dict):
        if resp.get("is_error") or resp.get("error"):
            return True
        text = json.dumps(resp).lower()
    else:
        text = str(resp).lower()
    return len(text) < 400 and any(tok in text for tok in FAIL_TOKENS)


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        sys.exit(0)  # unparseable -> fail open

    if hook_input.get("tool_name", "") not in WEB_TOOLS:
        sys.exit(0)

    event = hook_input.get("hook_event_name") or (
        "PostToolUse" if "tool_response" in hook_input else "PreToolUse"
    )
    sid = web_budget.resolve_sid(hook_input.get("session_id"))

    if event == "PreToolUse":
        allowed, reason = web_budget.check(sid)
        if not allowed:
            print(f"🛑 {reason}", file=sys.stderr)
            sys.exit(2)
        sys.exit(0)

    # PostToolUse — record the outcome.
    web_budget.record(sid, ok=not _is_failure(hook_input.get("tool_response")))
    sys.exit(0)


if __name__ == "__main__":
    main()
