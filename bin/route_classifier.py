#!/usr/bin/env python3
"""route_classifier.py — the single source of route-classification policy.

One classifier, two consumers. Before 2026-07-16 the UserPromptSubmit hook
(skills/govern/scripts/classify_prompt.py) and the track runtime
(bin/auto_runtime_common.classify_route) each vendored their own keyword
logic and drifted: the runtime copy used SUBSTRING matching ("auth" matched
"author", "permission" matched "permissions"), regressing the exact
false-positive class the hook's tiered rewrite had fixed — same input,
different risk attribution (audit finding H4). This module owns the policy;
both consumers import it.

Policy shape:
- Tiered keywords: STRONG always trigger their category; WEAK need compound
  evidence (another active category, deploy context, or 2+ distinct matches).
- Word-boundary matching only — never substring.
- Question carve-out (finding M7): a definitional prompt ("what is a jwt?")
  stays R1 even when it names risk keywords, provided it carries no
  implementation imperative — risk words in a question are a topic, not a
  change surface. Routing is not permissioning; an R1 answer mutates nothing.
- Vague prompts (<5 words, no file mentions, not a simple question) -> R5,
  matching the track runtime's historical is_vague contract.
- Every call returns classification_evidence so all consumers share one set
  of extracted facts.

Fast path requirement: <100ms, stdlib only, no I/O.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Keyword tiers
#
# STRONG keywords always trigger their risk category. WEAK keywords only
# trigger with compound evidence — they appear constantly in non-risk contexts
# ("session-end hook", "add permission to settings.json").
# ---------------------------------------------------------------------------

AUTH_STRONG = {
    "authentication", "authorization", "authorize",
    "jwt", "oauth", "password", "credential", "rbac",
    "api key", "secret", "login", "logout",
}

AUTH_WEAK = {
    "auth",            # common in agent names and config keys
    "authenticate",
    "session",         # session-end, session log, ...
    "token",           # API tokens, rate-limit tokens, ...
    "permission",      # "permission to do X", settings.json permissions
    "permissions",
    "role",            # agent role, task role, ...
    "access control",
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

# Simple/trivial indicators that push toward R1 (prompt must START with one).
SIMPLE_INDICATORS = {
    "what is", "what's", "how does", "explain", "show me", "list",
    "tell me", "describe", "define", "meaning of", "difference between",
    "translate",
}

# File path pattern. The trailing \b prevents prefix phantom-matches
# (".json" inside ".jsonl" — the bug that inflated file_count on 99.9% of
# production prompts while the hook was classifying its own JSON envelope).
FILE_PATH_RE = re.compile(
    r"(?:[\w./\\-]+\.(?:ts|tsx|js|jsx|py|go|rs|java|rb|css|html|json|yaml|yml|toml|md|sql|sh)\b)"
)

# ---------------------------------------------------------------------------
# Deliverable kind (advice vs artifact) — read by stop_gate.py via the route
# file. On an advisory prompt the correct final turn IS a recommendation, so
# the stop gate must not treat recommendation-shaped phrasing as a stall.
# ---------------------------------------------------------------------------

ADVISORY_RE = re.compile(
    r"\b("
    r"what (would|do) you think|how would you|would you suggest|suggest(ion)?s?|"
    r"critique|review|research|compare|evaluate|assess|analy[sz]e|explain|"
    r"thoughts on|your opinion|opinion on|recommend(ation)?s?|"
    r"should we|what'?s the best way|how should (we|i)|adversarial(ly)?"
    r")\b",
    re.IGNORECASE,
)

IMPLEMENTATION_IMPERATIVE_RE = re.compile(
    r"\b("
    r"implement|fix|build|apply|ship|create|write|add|refactor|make|update|"
    r"change|install|deploy|run|set ?up|go with|do it|proceed|merge|"
    r"commit|push|delete|remove|rename|migrate|"
    r"patch|repair|debug|resolve|correct|rewrite|optimi[sz]e|configure|"
    r"integrate|upgrade|edit"
    r")\b",
    re.IGNORECASE,
)


def deliverable_kind(text: str) -> str:
    """Return "advice" or "artifact". Advice requires an advisory signal AND
    no implementation imperative; everything else is artifact so the stop
    gate stays strict by default."""
    if not text or not text.strip():
        return "artifact"
    if ADVISORY_RE.search(text) and not IMPLEMENTATION_IMPERATIVE_RE.search(text):
        return "advice"
    return "artifact"


# ---------------------------------------------------------------------------
# Signal extraction helpers (word-boundary matching only)
# ---------------------------------------------------------------------------

def count_file_mentions(text: str) -> int:
    """Count unique file path mentions."""
    return len(set(FILE_PATH_RE.findall(text.lower())))


def file_mentions(text: str) -> list[str]:
    """Unique file path mentions, sorted (for evidence records)."""
    return sorted(set(FILE_PATH_RE.findall(text.lower())))


def has_keywords(text: str, keywords: set) -> bool:
    """True if any keyword appears as a whole word/phrase."""
    text_lower = text.lower()
    return any(re.search(r"\b" + re.escape(kw) + r"\b", text_lower) for kw in keywords)


def _count_weak_matches(text: str, keywords: set) -> int:
    """How many distinct weak keywords match as whole words."""
    text_lower = text.lower()
    return sum(
        1 for kw in keywords
        if re.search(r"\b" + re.escape(kw) + r"\b", text_lower)
    )


def is_broad_feature_prompt(text: str) -> bool:
    """Detect broad feature work even when no files are named."""
    text_lower = text.lower()
    implementation_verbs = {
        "add", "build", "create", "implement", "refactor", "redesign",
    }
    feature_scope_signals = {
        "feature", "workflow", "dashboard", "frontend", "backend", "api",
        "persistence", "tests", "integration", "documentation", "app",
    }
    has_verb = any(
        re.search(r"\b" + re.escape(verb) + r"\b", text_lower)
        for verb in implementation_verbs
    )
    has_scope_signal = any(
        re.search(r"\b" + re.escape(signal) + r"\b", text_lower)
        for signal in feature_scope_signals
    )
    has_module_boundary_signal = bool(
        re.search(r"\bmodule\s+boundar(?:y|ies)\b", text_lower)
    )
    return has_verb and (has_scope_signal or has_module_boundary_signal)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(text: str) -> dict:
    """Classify a prompt/task into R1–R5 with shared evidence.

    Returns {route_hint, governance_recommended, reason,
    classification_evidence}. Pure function of the text — no I/O.
    """
    if not text or not text.strip():
        return {
            "route_hint": "R1",
            "governance_recommended": False,
            "reason": "empty prompt",
            "classification_evidence": _evidence("", 0, [], False, False, False, False, False, False),
        }

    text_lower = text.lower().strip()
    mentions = file_mentions(text)
    file_count = len(mentions)
    word_count = len(text.split())

    # --- Risk signals (order matters: acyclic dependencies) ---
    touches_deploy = has_keywords(text, DEPLOY_KEYWORDS)

    # Auth: strong always triggers; weak needs compound evidence. A file-path
    # mention is NOT sufficient compound evidence — "add permission to
    # settings.json" is a config edit, not an auth change.
    touches_auth_strong = has_keywords(text, AUTH_STRONG)
    touches_auth_weak = has_keywords(text, AUTH_WEAK)
    weak_auth_count = _count_weak_matches(text, AUTH_WEAK) if touches_auth_weak else 0
    touches_auth = touches_auth_strong or (
        touches_auth_weak and (touches_deploy or weak_auth_count >= 2)
    )

    # Security: strong always; weak needs compound evidence. Like auth (and
    # unlike the pre-2026-07-16 policy), a bare file-path mention is NOT
    # compound evidence — "audit the logging in api.py" is analysis work, not
    # a security change. The old file_count>0 arm made any single weak word
    # ("audit", "incident", "exposed") + any file mention route R4.
    touches_security_strong = has_keywords(text, SECURITY_STRONG)
    touches_security_weak = has_keywords(text, SECURITY_WEAK)
    weak_security_count = (
        _count_weak_matches(text, SECURITY_WEAK) if touches_security_weak else 0
    )
    touches_security = touches_security_strong or (
        touches_security_weak and (
            touches_auth or touches_deploy or weak_security_count >= 2
        )
    )

    # Migration: strong always; weak needs compound evidence.
    touches_migration_strong = has_keywords(text, MIGRATION_STRONG)
    touches_migration_weak = has_keywords(text, MIGRATION_WEAK)
    touches_migrations = touches_migration_strong or (
        touches_migration_weak and (
            touches_auth or touches_security or touches_deploy or file_count > 0
        )
    )
    if touches_auth_weak and touches_migration_weak:
        touches_auth = True
        touches_migrations = True

    high_risk = touches_auth or touches_security or touches_migrations
    if touches_deploy and (touches_security or touches_auth):
        high_risk = True

    is_simple = any(text_lower.startswith(ind) for ind in SIMPLE_INDICATORS)
    has_imperative = bool(IMPLEMENTATION_IMPERATIVE_RE.search(text))
    is_vague = word_count < 5 and file_count == 0 and not is_simple
    broad_feature = is_broad_feature_prompt(text)

    def _result(route: str, governance: bool, reason: str) -> dict:
        return {
            "route_hint": route,
            "governance_recommended": governance,
            "reason": reason,
            "classification_evidence": _evidence(
                text, file_count, mentions, touches_auth, touches_security,
                touches_migrations, touches_deploy, is_vague, is_simple,
            ),
        }

    # Explicit /govern invocation.
    if text_lower.startswith("/govern"):
        return _result("R3", True, "explicit /govern invocation")

    # R1 — question carve-out (M7): a definitional/simple question with no
    # implementation imperative is a lookup, even when it names risk topics
    # ("what is a jwt?"). With an imperative ("show me how to fix login") it
    # falls through to the risk paths.
    if is_simple and not has_imperative and file_count == 0:
        return _result("R1", False, "simple factual query")

    # R5 — too vague to route ("fix auth", "make it faster"): clarify first.
    if is_vague:
        return _result("R5", True, f"ambiguous/underspecified ({word_count} words)")

    # R4 — high-risk keywords.
    if high_risk:
        reason_parts = []
        if touches_auth:
            reason_parts.append("auth")
        if touches_security:
            reason_parts.append("security")
        if touches_migrations:
            reason_parts.append("migration")
        return _result("R4", True, f"high-risk: {', '.join(reason_parts)}")

    # R3 — broad feature/workflow work requires planning even without files.
    if broad_feature:
        return _result("R3", True, "broad feature/workflow implementation")

    # R2 — small scope, no risk; long/multi-file prompts bump to R3.
    if file_count <= 2 and not touches_deploy:
        if word_count > 50 or file_count > 1:
            return _result(
                "R3", True,
                f"moderate complexity ({word_count} words, {file_count} files)")
        return _result("R2", file_count > 0, "small-scope implementation")

    # R3 — everything else.
    return _result("R3", True, f"non-trivial ({file_count} files mentioned)")


def _evidence(text, file_count, mentions, auth, security, migration, deploy,
              vague, simple) -> dict:
    """Shared evidence record — superset of what both consumers need."""
    return {
        "file_count": file_count,
        "file_mentions": list(mentions),
        "word_count": len(text.split()) if text else 0,
        "touches_auth": auth,
        "touches_security": security,
        "touches_migration": migration,
        "touches_deploy": deploy,
        "is_vague": vague,
        "is_simple": simple,
    }
