#!/usr/bin/env python3
"""PostToolUse (Bash) — detect credential material in tool output.

PostToolUse cannot redact output that already entered context; what it CAN
do is (a) make the agent immediately aware a secret surfaced so it isn't
echoed into memory/journals/commits, and (b) leave an audit record.

High-confidence token shapes only — this is a tripwire, not a DLP system.
Detection writes ~/.claude/state/secret-leaks.jsonl (shape metadata only,
NEVER the matched value) and emits additionalContext.

Justification for a new script (anti-overengineering gate): pre_tool_guard
is input-side; no existing primitive inspects tool output for leaked
credentials.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"))
from hook_profile import should_run
if not should_run("secret_leak_warn"):
    sys.exit(0)

LOG = os.path.expanduser("~/.claude/state/secret-leaks.jsonl")

PATTERNS = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws_access_key_id"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "github_pat"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), "github_fine_grained_pat"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "openai_style_secret_key"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"), "anthropic_api_key"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "slack_token"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "pem_private_key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "jwt"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "google_api_key"),
]


def extract_text(tool_response) -> str:
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        parts = [str(tool_response.get(k, "")) for k in ("stdout", "stderr", "output")]
        return "\n".join(p for p in parts if p)
    return ""


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, IOError):
        return 0

    if hook_input.get("tool_name") != "Bash":
        return 0

    text = extract_text(hook_input.get("tool_response", ""))
    if not text:
        return 0

    hits = sorted({label for pat, label in PATTERNS if pat.search(text)})
    if not hits:
        return 0

    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.time(),
                "session_id": hook_input.get("session_id", ""),
                "kinds": hits,
                "command": str(hook_input.get("tool_input", {}).get("command", ""))[:200],
            }) + "\n")
    except IOError:
        pass

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                f"🔑 Credential-shaped material detected in command output ({', '.join(hits)}). "
                "Do NOT echo it into files, memory, journals, commits, or responses. "
                "If it is a live secret, tell the user it surfaced so they can rotate it."
            ),
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
