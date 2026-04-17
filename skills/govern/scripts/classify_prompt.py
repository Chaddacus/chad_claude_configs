#!/usr/bin/env python3
"""Lightweight prompt classifier for UserPromptSubmit hook.

Reads the user prompt from the CLAUDE_USER_PROMPT environment variable
(set by the hook system) and produces a fast heuristic classification.

Must complete in <100ms — no network calls, no heavy imports.
Output: JSON with route_hint, governance_recommended, reason.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"))
from hook_profile import should_run
if not should_run("classify_prompt"):
    sys.exit(0)

# ---------------------------------------------------------------------------
# Keyword tiers
#
# STRONG keywords always trigger their risk category.
# WEAK keywords only trigger when compound evidence is present:
#   - another risk category is active, OR
#   - 2+ distinct weak keywords appear (for SECURITY_WEAK), OR
#   - file paths are mentioned.
# ---------------------------------------------------------------------------

AUTH_KEYWORDS = {
    "auth", "authenticate", "authentication", "authorization", "authorize",
    "login", "logout", "session", "token",
    "jwt", "oauth", "password", "credential", "rbac",
    "permission", "permissions",
    "role", "access control", "api key", "secret",
}

SECURITY_STRONG = {
    "vulnerability", "cve", "xss", "csrf", "injection",
    "encrypt", "encrypted", "encryption", "decrypt", "decrypted", "decryption",
    "certificate", "tls", "ssl",
    "firewall", "cors", "helmet", "rate limit",
    "data breach", "data loss", "data-loss", "pii", "gdpr", "hipaa",
}

SECURITY_WEAK = {
    "security", "audit", "compliance", "incident",
    "exposed", "exposure", "sanitize", "audit log",
}

MIGRATION_STRONG = {
    "schema", "alter table", "drop table",
    "add column", "prisma migrate", "knex migrate",
    "typeorm migration", "sequelize migration",
}

MIGRATION_WEAK = {
    "migration", "migrate",
}

DEPLOY_KEYWORDS = {
    "deploy", "production", "release", "rollback", "ci/cd", "pipeline",
    "dockerfile", "kubernetes", "k8s", "terraform", "cloudformation",
}

# Simple/trivial indicators that push toward R1
SIMPLE_INDICATORS = {
    "what is", "what's", "how does", "explain", "show me", "list",
    "tell me", "describe", "define", "meaning of", "difference between",
    "translate",
}

# File path pattern
FILE_PATH_RE = re.compile(
    r"(?:[\w./\\-]+\.(?:ts|tsx|js|jsx|py|go|rs|java|rb|css|html|json|yaml|yml|toml|md|sql|sh))"
)


def count_file_mentions(text: str) -> int:
    """Count unique file path mentions in the prompt."""
    matches = FILE_PATH_RE.findall(text.lower())
    return len(set(matches))


def has_keywords(text: str, keywords: set) -> bool:
    """Check if any keyword appears in the text as a whole word."""
    text_lower = text.lower()
    return any(re.search(r'\b' + re.escape(kw) + r'\b', text_lower) for kw in keywords)


def _count_weak_matches(text: str, keywords: set) -> int:
    """Count how many distinct weak keywords match as whole words."""
    text_lower = text.lower()
    return sum(
        1 for kw in keywords
        if re.search(r'\b' + re.escape(kw) + r'\b', text_lower)
    )


def classify_prompt(prompt: str) -> dict:
    """Fast heuristic classification of a user prompt."""
    if not prompt or not prompt.strip():
        return {
            "route_hint": "R1",
            "governance_recommended": False,
            "reason": "empty prompt",
        }

    prompt_lower = prompt.lower().strip()
    file_count = count_file_mentions(prompt)

    # --- Compute risk signals (order matters: acyclic dependencies) ---

    touches_auth = has_keywords(prompt, AUTH_KEYWORDS)
    touches_deploy = has_keywords(prompt, DEPLOY_KEYWORDS)

    # Security: strong always triggers; weak needs compound evidence
    touches_security_strong = has_keywords(prompt, SECURITY_STRONG)
    touches_security_weak = has_keywords(prompt, SECURITY_WEAK)
    weak_security_count = (
        _count_weak_matches(prompt, SECURITY_WEAK) if touches_security_weak else 0
    )
    touches_security = touches_security_strong or (
        touches_security_weak and (
            touches_auth or touches_deploy or file_count > 0
            or weak_security_count >= 2
        )
    )

    # Migration: strong always triggers; weak needs compound evidence
    touches_migration_strong = has_keywords(prompt, MIGRATION_STRONG)
    touches_migration_weak = has_keywords(prompt, MIGRATION_WEAK)
    touches_migrations = touches_migration_strong or (
        touches_migration_weak and (
            touches_auth or touches_security or touches_deploy or file_count > 0
        )
    )

    high_risk = touches_auth or touches_security or touches_migrations
    # Production incidents with data/security signals are R4
    if touches_deploy and (touches_security or touches_auth):
        high_risk = True

    # Check for simple/factual queries
    is_simple = any(prompt_lower.startswith(ind) for ind in SIMPLE_INDICATORS)

    # Check for explicit /govern invocation
    if prompt_lower.startswith("/govern"):
        return {
            "route_hint": "R3",
            "governance_recommended": True,
            "reason": "explicit /govern invocation",
        }

    # R1: simple questions, no file mentions
    if is_simple and file_count == 0 and not high_risk:
        return {
            "route_hint": "R1",
            "governance_recommended": False,
            "reason": "simple factual query",
        }

    # R4: high-risk keywords detected
    if high_risk:
        reason_parts = []
        if touches_auth:
            reason_parts.append("auth")
        if touches_security:
            reason_parts.append("security")
        if touches_migrations:
            reason_parts.append("migration")
        return {
            "route_hint": "R4",
            "governance_recommended": True,
            "reason": f"high-risk: {', '.join(reason_parts)}",
        }

    # R2: small scope, no risk
    if file_count <= 2 and not high_risk and not touches_deploy:
        # But if the prompt is long/complex, bump to R3
        word_count = len(prompt.split())
        if word_count > 50 or file_count > 1:
            return {
                "route_hint": "R3",
                "governance_recommended": True,
                "reason": f"moderate complexity ({word_count} words, {file_count} files)",
            }
        return {
            "route_hint": "R2",
            "governance_recommended": file_count > 0,
            "reason": "small-scope implementation",
        }

    # R3: everything else
    return {
        "route_hint": "R3",
        "governance_recommended": True,
        "reason": f"non-trivial ({file_count} files mentioned)",
    }


def main():
    # Hook system passes prompt via environment variable
    prompt = os.environ.get("CLAUDE_USER_PROMPT", "")

    # Also try stdin as fallback for testing
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read()

    result = classify_prompt(prompt)

    # Write route to session-scoped temp file for downstream hook profile gating
    route_file = f"/tmp/claude-route-{os.environ.get('CLAUDE_SESSION_ID', 'default')}.json"
    try:
        with open(route_file, "w") as f:
            json.dump(result, f)
    except OSError:
        pass

    context = (
        f"[route-classifier] route_hint={result['route_hint']} "
        f"governance_recommended={result['governance_recommended']} "
        f"reason={result['reason']}"
    )
    envelope = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    print(json.dumps(envelope))


if __name__ == "__main__":
    main()
