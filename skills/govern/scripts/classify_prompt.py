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
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

# hook_profile lives in the runtime bin. CLAUDE_HOME may be overridden to
# redirect STATE (tests isolating route_decisions.jsonl), so also add the
# real runtime home as an import fallback rather than crashing.
sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"))
sys.path.insert(1, os.path.expanduser("~/.claude/bin"))
from hook_profile import should_run
# NOTE: the should_run() profile gate is applied inside main(), not at import
# time. An import-time sys.exit(0) made this module unimportable as a library
# (tests, the shared classifier) and killed host processes silently.

# ---------------------------------------------------------------------------
# Keyword tiers
#
# STRONG keywords always trigger their risk category.
# WEAK keywords only trigger when compound evidence is present:
#   - another risk category is active, OR
#   - 2+ distinct weak keywords appear (for SECURITY_WEAK), OR
#   - file paths are mentioned.
# ---------------------------------------------------------------------------

# AUTH_STRONG: always triggers R4 on single match. These are the auth-primitive
# keywords — naming one of these is explicit intent to touch auth mechanics.
AUTH_STRONG = {
    "authentication", "authorization", "authorize",
    "jwt", "oauth", "password", "credential", "rbac",
    "api key", "secret", "login", "logout",
}

# AUTH_WEAK: needs compound evidence (another risk signal active, file path
# mentioned, or 2+ weak matches). These keywords appear in non-auth contexts
# constantly ("session-end hook", "set permission", "check role of agent", etc.)
# and firing R4 on a single match produces false-positives that escalate cost
# without proportionate safety gain.
AUTH_WEAK = {
    "auth",            # common in agent names and config keys
    "authenticate",    # narrow — but appears in MCP/tool names sometimes
    "session",         # many non-auth usages (session-end, session log)
    "token",           # API tokens, rate-limit tokens, etc.
    "permission",      # "permission to do X", "add permission", settings.json
    "permissions",
    "role",            # many non-auth usages (agent role, task role)
    "access control",  # narrow but still worth compounding
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

# File path pattern. The trailing \b prevents prefix phantom-matches:
# without it, ".json" matched inside ".jsonl", so the hook payload's
# transcript_path counted as a file mention on EVERY prompt (2026-07-16
# audit: file_count>=1 on 99.9% of 2,550 production rows purely from this).
FILE_PATH_RE = re.compile(
    r"(?:[\w./\\-]+\.(?:ts|tsx|js|jsx|py|go|rs|java|rb|css|html|json|yaml|yml|toml|md|sql|sh)\b)"
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
        re.search(r'\b' + re.escape(verb) + r'\b', text_lower)
        for verb in implementation_verbs
    )
    has_scope_signal = any(
        re.search(r'\b' + re.escape(signal) + r'\b', text_lower)
        for signal in feature_scope_signals
    )
    has_module_boundary_signal = bool(
        re.search(r"\bmodule\s+boundar(?:y|ies)\b", text_lower)
    )
    return has_verb and (has_scope_signal or has_module_boundary_signal)


# ---------------------------------------------------------------------------
# Deliverable kind (advice vs artifact)
#
# Read by stop_gate.py via the route file this script writes. On an advisory
# prompt the correct final turn IS a recommendation, so the stop gate must not
# treat recommendation-shaped phrasing as a stall. Deterministic predicates
# only; defaults to "artifact" (strict gate) on any ambiguity.
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


def deliverable_kind(prompt: str) -> str:
    """Return "advice" or "artifact". Advice requires an advisory signal AND
    the absence of any implementation imperative; everything else is artifact
    so the stop gate stays strict by default."""
    if not prompt or not prompt.strip():
        return "artifact"
    if ADVISORY_RE.search(prompt) and not IMPLEMENTATION_IMPERATIVE_RE.search(prompt):
        return "advice"
    return "artifact"


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

    touches_deploy = has_keywords(prompt, DEPLOY_KEYWORDS)

    # Auth: strong keywords always trigger; weak keywords need compound evidence.
    # Fixes the earlier false-positive problem where single words like
    # "permission" or "session" hit R4 on prompts that were routine config
    # edits, not auth changes.
    #
    # File-path mention is NOT sufficient compound evidence for auth —
    # "add permission to settings.json" is a config edit, not an auth change.
    # Require either a deploy context, another risk category active, or
    # multiple weak auth matches.
    touches_auth_strong = has_keywords(prompt, AUTH_STRONG)
    touches_auth_weak = has_keywords(prompt, AUTH_WEAK)
    weak_auth_count = (
        _count_weak_matches(prompt, AUTH_WEAK) if touches_auth_weak else 0
    )
    touches_auth = touches_auth_strong or (
        touches_auth_weak and (
            touches_deploy or weak_auth_count >= 2
        )
    )

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
    if touches_auth_weak and touches_migration_weak:
        touches_auth = True
        touches_migrations = True

    high_risk = touches_auth or touches_security or touches_migrations
    # Production incidents with data/security signals are R4
    if touches_deploy and (touches_security or touches_auth):
        high_risk = True

    # Check for simple/factual queries
    is_simple = any(prompt_lower.startswith(ind) for ind in SIMPLE_INDICATORS)
    broad_feature = is_broad_feature_prompt(prompt)

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

    # R3: broad feature/workflow work requires planning even without file mentions.
    if broad_feature:
        return {
            "route_hint": "R3",
            "governance_recommended": True,
            "reason": "broad feature/workflow implementation",
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
- R3/R4 require planning-gate and Ralph postflight.
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
    env_prompt = os.environ.get("CLAUDE_USER_PROMPT")
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


def main():
    # Profile gate (moved from import time so the module stays importable).
    if not should_run("classify_prompt"):
        return

    prompt, session_id = _read_hook_payload()

    result = classify_prompt(prompt)
    result["deliverable_kind"] = deliverable_kind(prompt)

    # --- Slice 5: bandit enabler (additive instrumentation) -------------------
    # decision_id is the per-prompt join-key tying this routing decision to the
    # session's stop outcome (read by stop_reason_telemetry.py). word_count and
    # file_count are already computed inside classify_prompt(); recompute here
    # (O(n), trivial on hook budget) so they land in the durable record.
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
