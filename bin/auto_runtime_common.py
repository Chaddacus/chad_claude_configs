#!/usr/bin/env python3
"""Claude Auto Runtime — Core State Machine Library.

Event-sourced autonomous task orchestrator for Claude Code.
Provides behavioral parity with Codex auto_runtime_common.py,
adapted for Claude's model profiles, route_manifest.json schema,
and omni-mem MCP integration.

State directory: ~/.claude/state/autonomy/{track_id}/
Event log: objective.events.jsonl (authoritative, replayable)
Materialized views: objective.{state,graph,frontier,policy,metrics,summary,closure,governance,maintenance,memory}.json
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omni_mem_route import container_for_cwd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude"))
AUTONOMY_DIR = CLAUDE_HOME / "state" / "autonomy"
ROUTE_MANIFEST_PATH = CLAUDE_HOME / "state" / "route_manifest.json"
CONTROL_PLANE_PATH = CLAUDE_HOME / "state" / "control_plane.json"

OBJECTIVE_STATE_SCHEMA = "auto-objective-state.v1"
GOVERNANCE_SCHEMA = "auto-governance.v2"
MAINTENANCE_SCHEMA = "auto-maintenance.v1"
MEMORY_SCHEMA = "auto-memory.v1"
MANAGER_SCHEMA = "auto-manager.v1"
MANAGER_TASK_RUN_SCHEMA = "auto-manager-task-run.v1"
POLICY_ENVELOPE_SCHEMA = "auto-policy-envelope.v1"

OBJECTIVE_TERMINAL_STATES = {
    "OBJECTIVE_COMPLETE",
    "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK",
    "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED",
}
OBJECTIVE_SUCCESS_STATES = {
    "OBJECTIVE_COMPLETE",
    "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK",
}
OBJECTIVE_BLOCKED_STATES = {
    "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED",
    "OBJECTIVE_BLOCKED_MIGRATION_DEFECT",
}

GRAPH_NODE_STATES = {
    "queued", "ready", "in_progress", "awaiting_verification",
    "accepted", "rework", "blocked", "deferred", "cancelled",
}

DISPATCH_CYCLE_MAX_BY_ROUTE = {"R1": 6, "R2": 12, "R3": 24, "R4": 40, "R5": 4}
ADVISOR_CHECKPOINT_MAX_BY_ROUTE = {"R3": 12, "R4": 20}

SAME_STRATEGY_RETRY_BUDGET = 2
NO_FRONTIER_MOVEMENT_MAX = 2
AWAITING_VERIFICATION_SYNC_LIMIT = 2
TRACK_LOCK_TIMEOUT_SECONDS = 30
MIN_ACCEPTANCE_CRITERIA_R3_R4 = 3
EVALUATOR_VERDICT_SCHEMA = "evaluator-verdict.v1"

REPLAYABLE_EVENTS = {
    "objective_initialized",
    "node_state_updated",
    "inline_dispatched",
    "route_promoted",
    "governed_dispatched",
    "governed_sync_blocked",
    "governed_synced",
    "governed_node_transition",
    "dispatch_blocked",
    "track_lock_timeout",
    "memory_lifecycle_written",
    "memory_lifecycle_failed",
    "memory_lifecycle_skipped",
    "escalation_reset",
    "effort_escalated",
    "maintenance_reconciled",
    "anticipation_recorded",
    "cycle_completed",
    "manager_action_selected",
    "manager_cycle_completed",
    "task_run_final_review_recorded",
    "git_checkpoint_recorded",
    "omnimem_checkpoint_recorded",
    "wake_ceremony_completed",
    "evaluator_dispatched",
    "evaluator_verdict",
    "phase_changed",
    "question_selection",
    "decision_record",
    "cycle_summary",
    "track_summary",
    "baseline_captured",
    "baseline_unavailable",
    "verifier_matrix_started",
    "verifier_run",
    "verifier_classified",
    "verifier_matrix_completed",
    "phase_transition_blocked",
    "shadow_decision",
}
AUDIT_ONLY_EVENTS = {
    "frontier_refreshed",
    "transition_rejected",
    "governed_advisor_checkpoint",
    "memory_lifecycle_reconciled",
    "git_checkpoint_skipped",
    "omnimem_checkpoint_skipped",
}

UI_KEYWORDS = {
    "frontend", "ui", "ux", "component", "page", "button", "form",
    "modal", "dialog", "layout", "css", "style", "theme", "responsive",
    "react", "vue", "angular", "svelte", "next", "nuxt", "remix",
    "html", "dom", "render", "template", "jsx", "tsx", "tailwind",
    "widget", "navigation", "menu", "sidebar", "header", "footer",
    "dashboard", "chart", "table", "grid", "input", "select",
    "dropdown", "tooltip", "animation", "transition", "hover",
    "browser", "web app", "webapp", "website", "landing page",
    "screenshot", "visual", "pixel", "viewport",
}
UI_FILE_EXTENSIONS = {
    ".tsx", ".jsx", ".vue", ".svelte", ".html", ".css", ".scss",
    ".less", ".sass", ".styl",
}

PREFLIGHT_WARN_SCORE_MAX = 74
PREFLIGHT_BLOCK_SCORE_MAX = 59


# ---------------------------------------------------------------------------
# Phase register (stage-aware orchestrator loop — Slice 1a)
# Plan ref: ~/.codex-spar/stage-aware-orchestrator-loop/plan-final.md §1
# ---------------------------------------------------------------------------

PHASE_ENUM = ("discovery", "design", "build", "verify", "closeout")
PHASE_INITIAL = "discovery"

# Monotonic phase transitions only (plan-final §1, post-v3 revision).
# verify -> build is a bounded retry edge, NOT a phase regression.
# Each entry: (from, to) -> required_evidence_keys
PHASE_TRANSITION_TABLE: dict[tuple[str, str], frozenset[str]] = {
    ("discovery", "design"):  frozenset({"repo_facts", "scope", "constraints"}),
    ("design", "build"):      frozenset({"plan_approved", "owned_files", "validation_plan"}),
    ("build", "verify"):      frozenset({"patch_applied", "lint_pass"}),
    ("verify", "closeout"):   frozenset({"tests_pass", "no_introduced_regressions"}),
    ("verify", "build"):      frozenset({"introduced_failure"}),  # retry edge
}
PHASE_RETRY_BUDGET = {("verify", "build"): 3}

# Route strictness ordering for the generic stricter-route rule (plan-final §1).
# R5 is unordered until resolved; handled by special-case rows.
ROUTE_STRICTNESS = {"R1": 1, "R2": 2, "R3": 3, "R4": 4}

# Required-evidence sets per route (used by route-change reconciliation).
# A phase's required evidence is its inbound transition's required set,
# plus any route-level requirements stacked on top.
ROUTE_REQUIRED_EVIDENCE = {
    "R1": frozenset(),
    "R2": frozenset(),
    "R3": frozenset(),
    "R4": frozenset({"threat_model", "security_review"}),
}

# Special-case route-change rows (override the generic rule).
# (from_route, to_route) -> {target_phase, invalidate, required_backfill, dispatch}
ROUTE_CHANGE_SPECIAL_CASES: dict[tuple[str, str], dict[str, Any]] = {
    ("R5", "R2"): {
        "target_phase": "build",
        "invalidate": ["r5_ambiguity_resolution"],
        "required_backfill": [],
        "dispatch": "resume",
    },
    ("R5", "R3"): {
        "target_phase": "discovery",
        "invalidate": ["r5_ambiguity_resolution"],
        "required_backfill": ["scope", "owned_files", "validation_plan"],
        "dispatch": "PAUSE",
    },
    ("R5", "R4"): {
        "target_phase": "discovery",
        "invalidate": ["r5_ambiguity_resolution"],
        "required_backfill": ["scope", "owned_files", "validation_plan",
                              "threat_model", "security_review"],
        "dispatch": "PAUSE",
    },
}


# ---------------------------------------------------------------------------
# Question registry + decision_record (Slice 1b)
# Plan ref: ~/.codex-spar/stage-aware-orchestrator-loop/plan-final.md §3
# Inline map; YAML externalization in Slice 2.
# Observable decision_kinds only (per Slice 1b grounding):
#   phase, route, next_action, owned_files
# ---------------------------------------------------------------------------

# decision_kind enum restricted to observable kinds.
# Adding scope/validation_plan/tool_invocation requires state plumbing —
# separate proposal, see plan-final §3 + Slice 1b grounding notes.
OBSERVABLE_DECISION_KINDS = ("phase", "route", "next_action", "owned_files")

# ---------------------------------------------------------------------------
# Verifier baseline capture (Slice 1c)
# Plan ref: ~/.codex-spar/stage-aware-orchestrator-loop/plan-final.md §2
# Deviation from §2: dropped `matrix_cell` from baseline_key. A verifier's
# output against (cwd, base_git_sha, owned_files_hash) is the same regardless
# of which transition checks it; Slice 3 selects which command per cell.
# Baseline key: sha256({track_id, route, command_id, owned_files_hash, base_git_sha})
# ---------------------------------------------------------------------------

# Static allowlist (per Codex M3: no auto-shelling to arbitrary commands).
# Project-level override via .claude/verifiers.yaml is Slice 3 work.
VERIFIER_ALLOWLIST: dict[str, dict[str, Any]] = {
    "ruff_check": {
        "argv": ["ruff", "check", "."],
        "project_markers": ["pyproject.toml", "ruff.toml", ".ruff.toml"],
        "timeout_ms": 30000,
    },
    "mypy_check": {
        "argv": ["python3", "-m", "mypy", "--no-incremental", "."],
        "project_markers": ["mypy.ini", "pyproject.toml"],
        "timeout_ms": 60000,
    },
    "pytest_smoke": {
        "argv": ["python3", "-m", "pytest", "-q", "-x", "--tb=no"],
        "project_markers": ["pytest.ini", "pyproject.toml", "conftest.py"],
        "timeout_ms": 60000,
    },
    "eslint_check": {
        "argv": ["npx", "--no-install", "eslint", "."],
        "project_markers": [".eslintrc", ".eslintrc.js", ".eslintrc.json",
                            ".eslintrc.cjs", "eslint.config.js"],
        "timeout_ms": 30000,
    },
    "tsc_noemit": {
        "argv": ["npx", "--no-install", "tsc", "--noEmit"],
        "project_markers": ["tsconfig.json"],
        "timeout_ms": 60000,
    },
}

# Per-route total baseline budget. R1 and R5-unresolved skip entirely.
BASELINE_BUDGET_MS_BY_ROUTE = {
    "R1": 0,
    "R2": 1500,
    "R3": 8000,
    "R4": 30000,
    "R5": 0,
}

# Baseline status enum (Slice 1c emits; Slice 3 acts on these).
BASELINE_STATUS = (
    "pass",
    "preexisting_failure",
    "infra_error",
    "timeout",
    "budget_exhausted",
    "not_applicable",
    "baseline_unavailable",
)

BASELINE_OUTPUT_EXCERPT_BYTES = 4096


QUESTION_REGISTRY_INLINE: dict[str, Any] = {
    "registry_version": "inline-slice-1b",
    "phases": {
        "discovery": {
            "questions": [
                {
                    "id": "prior_art",
                    "question": "How do existing components in this repo solve this?",
                    "any_evidence_required": ["repo_search", "memory_lookup"],
                    "targets_decision_kind": "next_action",
                    "skip_when": {"route_in": ["R1"]},
                },
            ],
        },
        "build": {
            "questions": [
                {
                    "id": "simplest_path",
                    "question": "Is this still the simplest path?",
                    "any_evidence_required": ["repo_search"],
                    "targets_decision_kind": "owned_files",
                    "skip_when": {"route_in": ["R1"]},
                },
            ],
        },
    },
    "loop_invariant": {
        "triggers": {
            "event_count_since_last": 5,
            "on_route_promotion": True,
        },
        "max_invariant_tokens": 400,
        "questions": [
            {
                "id": "premise_check",
                "question": "Did anything we just learned invalidate the plan?",
                "any_evidence_required": ["memory_lookup", "event_log_diff"],
                "targets_decision_kind": "next_action",
                "skip_when": {"route_in": ["R1"]},
            },
        ],
    },
}


# ---------------------------------------------------------------------------
# External question registry loader (Slice 2)
# ---------------------------------------------------------------------------
#
# Default path: ~/.claude/policy/phase_questions.yaml
# Falls back to QUESTION_REGISTRY_INLINE if the file is missing, unreadable,
# or fails parse. On load failure we emit a one-shot stderr warning (the
# inline copy keeps the runtime working). The cache is keyed by (path, mtime).
#
# Schema is enforced by ~/.claude/bin/registry_lint.py — not enforced at
# load time so a malformed external registry can't crash the loop.

PHASE_QUESTIONS_REGISTRY_PATH = Path.home() / ".claude" / "policy" / "phase_questions.yaml"

_REGISTRY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_REGISTRY_LOAD_WARNED: set[str] = set()


def _warn_registry_load(path: str, reason: str) -> None:
    """Stderr-warn once per (path, reason). Never raises."""
    key = f"{path}|{reason}"
    if key in _REGISTRY_LOAD_WARNED:
        return
    _REGISTRY_LOAD_WARNED.add(key)
    try:
        sys.stderr.write(
            f"[auto_runtime] phase-questions registry load fallback "
            f"({path}): {reason}\n"
        )
    except Exception:
        pass


def load_question_registry(
    path: Path | str | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Load the external YAML registry; fall back to inline on any error.

    Caches by (path, mtime). Pass force=True to bypass cache (test hook).
    """
    p = Path(path) if path is not None else PHASE_QUESTIONS_REGISTRY_PATH
    key = str(p)
    try:
        if not p.exists():
            _warn_registry_load(key, "file_not_found")
            return QUESTION_REGISTRY_INLINE
        mtime = p.stat().st_mtime
        if not force and key in _REGISTRY_CACHE:
            cached_mtime, cached_data = _REGISTRY_CACHE[key]
            if cached_mtime == mtime:
                return cached_data
        try:
            import yaml  # type: ignore
        except ImportError:
            _warn_registry_load(key, "pyyaml_unavailable")
            return QUESTION_REGISTRY_INLINE
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as e:
            _warn_registry_load(key, f"parse_error:{type(e).__name__}")
            return QUESTION_REGISTRY_INLINE
        if not isinstance(data, dict) or "registry_version" not in data:
            _warn_registry_load(key, "invalid_shape")
            return QUESTION_REGISTRY_INLINE
        _REGISTRY_CACHE[key] = (mtime, data)
        return data
    except Exception as e:  # noqa: BLE001 — defensive: loader must never raise
        _warn_registry_load(key, f"unexpected:{type(e).__name__}")
        return QUESTION_REGISTRY_INLINE


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_track_id(task: str, cwd: str) -> str:
    """Deterministic track ID from task + cwd."""
    raw = f"{task.strip().lower()}|{os.path.realpath(cwd)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def track_dir(track_id: str) -> Path:
    return AUTONOMY_DIR / track_id


def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_json(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


def _append_event(track_id: str, event: dict[str, Any]) -> None:
    """Append a single event to the track's event log."""
    td = track_dir(track_id)
    _ensure_dir(td)
    event.setdefault("timestamp", now_iso())
    event.setdefault("track_id", track_id)
    with open(td / "objective.events.jsonl", "a") as f:
        f.write(json.dumps(event, default=str) + "\n")


def _read_events(track_id: str) -> list[dict[str, Any]]:
    """Read all events from the track's event log, skipping malformed."""
    path = track_dir(track_id) / "objective.events.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _append_phase_event(
    track_id: str,
    *,
    from_phase: str | None,
    to_phase: str,
    evidence: dict[str, Any] | None = None,
    triggered_by: str = "transition",
) -> None:
    """Append a phase_changed event.

    Slice 1a primitive: records the transition without enforcing it.
    Enforcement and prompt-side effects come in Slices 1b and 3.

    Args:
        track_id: target track.
        from_phase: previous phase, or None for initial entry.
        to_phase: new phase (must be in PHASE_ENUM).
        evidence: optional dict of {evidence_key: ref-or-bool} for audit.
        triggered_by: free-text reason ("transition", "route_change",
            "retry", "initial"); used by analysis tools.
    """
    if to_phase not in PHASE_ENUM:
        raise ValueError(f"unknown phase: {to_phase!r}; expected one of {PHASE_ENUM}")
    _append_event(track_id, {
        "event": "phase_changed",
        "from_phase": from_phase,
        "to_phase": to_phase,
        "triggered_by": triggered_by,
        "evidence": evidence or {},
    })


def current_phase(events: list[dict[str, Any]]) -> str:
    """Fold the event log to determine the current phase.

    Returns PHASE_INITIAL if no phase_changed events have been recorded.
    Ignores malformed phase_changed events (missing/invalid to_phase).
    """
    for event in reversed(events):
        if event.get("event") != "phase_changed":
            continue
        to_phase = event.get("to_phase")
        if to_phase in PHASE_ENUM:
            return to_phase
    return PHASE_INITIAL


# ---------------------------------------------------------------------------
# Decision record canonical state (Slice 1b)
# ---------------------------------------------------------------------------

def canonical_state(kind: str, source: Any) -> Any:
    """Return the canonical state payload for a decision kind.

    Observable kinds only. Raises for non-observable kinds — those
    require state plumbing not in Slice 1b's scope.
    """
    if kind == "phase":
        return source if source in PHASE_ENUM else None
    if kind == "route":
        return source if source in ROUTE_STRICTNESS or source == "R5" else None
    if kind == "next_action":
        if source is None:
            return None
        return {
            "action_kind": source.get("action_kind", ""),
            "target_ref": source.get("target_ref", ""),
        }
    if kind == "owned_files":
        if source is None:
            return []
        return sorted(source) if isinstance(source, (list, tuple, set)) else []
    raise ValueError(f"non-observable decision_kind: {kind!r}; observable: {OBSERVABLE_DECISION_KINDS}")


def state_hash(state: Any) -> str:
    """SHA-256 of canonical JSON payload."""
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, default=str).encode()
    ).hexdigest()


def _append_decision_record(
    track_id: str,
    *,
    decision_kind: str,
    before_state: Any,
    after_state: Any,
    evidence_refs: list[str] | None = None,
    triggered_by_question_id: str | None = None,
    no_change_reason: str | None = None,
) -> dict[str, Any]:
    """Emit a decision_record event with canonical-state hashes.

    Returns the emitted event dict (for test inspection).
    Validates: changed=False requires no_change_reason when a question
    targeted this decision (per plan-final §3 disposition validity).
    """
    if decision_kind not in OBSERVABLE_DECISION_KINDS:
        raise ValueError(
            f"non-observable decision_kind: {decision_kind!r}"
        )
    bhash = state_hash(before_state)
    ahash = state_hash(after_state)
    changed = bhash != ahash
    # Disposition validity: question fired AND changed=False => must explain.
    if (not changed) and triggered_by_question_id and not no_change_reason:
        raise ValueError(
            "decision_record with changed=False and a triggering question "
            "must include no_change_reason (plan-final §3)"
        )
    event = {
        "event": "decision_record",
        "decision_kind": decision_kind,
        "before_state": before_state,
        "after_state": after_state,
        "before_state_hash": bhash,
        "after_state_hash": ahash,
        "changed": changed,
        "no_change_reason": no_change_reason,
        "triggered_by_question_id": triggered_by_question_id,
        "evidence_refs": evidence_refs or [],
    }
    _append_event(track_id, event)
    return event


# ---------------------------------------------------------------------------
# Question selection (Slice 1b)
# ---------------------------------------------------------------------------

def _question_applies(question: dict[str, Any], route: str) -> bool:
    """Apply skip_when.route_in filter."""
    skip_when = question.get("skip_when", {}) or {}
    if route in (skip_when.get("route_in") or []):
        return False
    return True


def select_phase_questions(
    phase: str,
    route: str,
    *,
    registry: dict[str, Any] | None = None,
    fire_invariant: bool = True,
) -> list[dict[str, Any]]:
    """Select questions applicable to (phase, route).

    R1 bypasses entirely. R5 (unresolved) gets only loop_invariant questions
    that don't skip on R5.
    """
    if route == "R1":
        return []
    reg = registry if registry is not None else load_question_registry()
    selected: list[dict[str, Any]] = []
    if route != "R5":
        phase_block = reg.get("phases", {}).get(phase, {})
        for q in phase_block.get("questions", []):
            if _question_applies(q, route):
                selected.append(q)
    if fire_invariant:
        inv_block = reg.get("loop_invariant", {})
        for q in inv_block.get("questions", []):
            if _question_applies(q, route):
                selected.append(q)
    return selected


def _append_question_selection(
    track_id: str,
    *,
    phase: str,
    route: str,
    questions: list[dict[str, Any]],
    trigger: str = "cycle_start",
) -> None:
    """Emit a question_selection event recording what was asked this cycle."""
    _append_event(track_id, {
        "event": "question_selection",
        "phase": phase,
        "route": route,
        "trigger": trigger,
        "question_ids": [q["id"] for q in questions],
        "targets_decision_kinds": sorted({
            q.get("targets_decision_kind") for q in questions
            if q.get("targets_decision_kind")
        }),
    })


def _capture_observable_states(
    track_id: str,
    state: dict[str, Any],
    *,
    anticipation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture current values for the 4 observable decision kinds.

    Used to compute before/after deltas in decision_record events.
    `anticipation` provides next_action data when available (post-anticipate).
    """
    events = _read_events(track_id)
    policy = state.get("views", {}).get("policy", {})
    graph = state.get("views", {}).get("graph", {})
    frontier = state.get("views", {}).get("frontier", {})

    # Find focus node = anticipation's slice OR frontier's next_slice_id
    focus_slice_id = None
    if anticipation:
        ed = anticipation.get("evaluator_dispatch", {}) or {}
        focus_slice_id = ed.get("slice_id")
    if not focus_slice_id:
        focus_slice_id = frontier.get("next_slice_id")
    focus_node = (graph.get("nodes") or {}).get(focus_slice_id, {}) if focus_slice_id else {}

    next_action_source = None
    if anticipation:
        next_action_source = {
            "action_kind": anticipation.get("recommended_action", ""),
            "target_ref": focus_slice_id or "",
        }

    return {
        "phase": canonical_state("phase", current_phase(events)),
        "route": canonical_state("route", policy.get("hard_policy", {}).get("route_id", "")),
        "next_action": canonical_state("next_action", next_action_source),
        "owned_files": canonical_state("owned_files", focus_node.get("owned_scope", [])),
    }


def _emit_cycle_summary(
    track_id: str,
    *,
    cycle_idx: int,
    route: str,
    recommended_action: str,
    action_status: str,
    questions_fired: list[str] | None = None,
    decisions_recorded: list[dict[str, Any]] | None = None,
    phase_at_start: str | None = None,
    phase_at_end: str | None = None,
) -> None:
    """Emit cycle_summary event for Layer-2 validation (plan-final §7).

    Token/wall-clock counts come from the Claude Code layer; this layer
    cannot observe them, so they remain null. analyze.py merges in any
    upstream-supplied measurements at validation time.
    """
    _append_event(track_id, {
        "event": "cycle_summary",
        "cycle": cycle_idx,
        "route": route,
        "phase_at_start": phase_at_start,
        "phase_at_end": phase_at_end,
        "questions_fired": questions_fired or [],
        "decisions_recorded": decisions_recorded or [],
        "recommended_action": recommended_action,
        "action_status": action_status,
        "tokens_in": None,        # supplied by Claude Code layer
        "tokens_out": None,
        "wall_clock_ms": None,
    })


# ---------------------------------------------------------------------------
# Baseline capture (Slice 1c)
# ---------------------------------------------------------------------------

def _baseline_key(
    *,
    track_id: str,
    route: str,
    command_id: str,
    owned_files_hash: str,
    base_git_sha: str,
) -> str:
    """Deterministic baseline key (per plan-final §2 with matrix_cell dropped)."""
    payload = {
        "track_id": track_id,
        "route": route,
        "command_id": command_id,
        "owned_files_hash": owned_files_hash,
        "base_git_sha": base_git_sha,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()


def _owned_files_hash(owned_scope: list[str] | None) -> str:
    """sha256 of sorted owned-files set; empty set → known empty hash."""
    return hashlib.sha256(
        json.dumps(sorted(owned_scope or []), default=str).encode()
    ).hexdigest()


def _detect_applicable_verifiers(cwd: str) -> list[str]:
    """Return command_ids whose project_markers exist in cwd.

    Allowlist-only — no auto-detection of arbitrary commands (Codex M3).
    Empty list if cwd doesn't exist.
    """
    if not cwd or not os.path.isdir(cwd):
        return []
    applicable: list[str] = []
    for command_id, spec in VERIFIER_ALLOWLIST.items():
        for marker in spec["project_markers"]:
            if os.path.exists(os.path.join(cwd, marker)):
                applicable.append(command_id)
                break
    return applicable


def _capture_baseline_command(
    *,
    command_id: str,
    cwd: str,
    remaining_budget_ms: int,
) -> dict[str, Any]:
    """Run one verifier command and return its baseline result.

    Does NOT touch the worktree — the caller is responsible for ensuring
    we're against a clean ref (see `capture_baselines`).
    Returns: {status, exit_code, output_excerpt, duration_ms}.
    """
    spec = VERIFIER_ALLOWLIST[command_id]
    timeout_s = min(spec["timeout_ms"], remaining_budget_ms) / 1000.0
    if timeout_s <= 0:
        return {
            "status": "budget_exhausted", "exit_code": None,
            "output_excerpt": "", "duration_ms": 0,
        }
    start = time.monotonic()
    try:
        result = subprocess.run(
            spec["argv"], cwd=cwd,
            capture_output=True, text=True, timeout=timeout_s,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        combined = (result.stdout + result.stderr)[-BASELINE_OUTPUT_EXCERPT_BYTES:]
        status = "pass" if result.returncode == 0 else "preexisting_failure"
        return {
            "status": status, "exit_code": result.returncode,
            "output_excerpt": combined, "duration_ms": duration_ms,
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout", "exit_code": None,
            "output_excerpt": "", "duration_ms": int((time.monotonic() - start) * 1000),
        }
    except (FileNotFoundError, OSError) as e:
        return {
            "status": "infra_error", "exit_code": None,
            "output_excerpt": f"{type(e).__name__}: {e}",
            "duration_ms": int((time.monotonic() - start) * 1000),
        }


def _get_base_git_sha(cwd: str) -> str | None:
    """Return HEAD sha or None when not in a git repo / git missing."""
    if not _is_git_repo(cwd):
        return None
    rc, out = _run_git(["rev-parse", "HEAD"], cwd)
    if rc != 0 or not out.strip():
        return None
    return out.strip()


def capture_baselines(
    track_id: str,
    *,
    cwd: str,
    route: str,
    owned_files: list[str] | None,
) -> dict[str, Any]:
    """Capture verifier baselines pre-dispatch (Slice 1c entry point).

    Emits one `baseline_captured` or `baseline_unavailable` event per
    detected verifier command. Stores per-command JSON under
    `~/.claude/state/autonomy/{track_id}/baselines/{key}.json`.

    Does NOT block dispatch (Slice 3 wires that). Returns a summary dict
    for the caller's telemetry.
    """
    summary = {
        "route": route,
        "captured": [],
        "unavailable": [],
        "budget_ms_total": BASELINE_BUDGET_MS_BY_ROUTE.get(route, 0),
        "budget_ms_used": 0,
    }

    budget = BASELINE_BUDGET_MS_BY_ROUTE.get(route, 0)
    if budget <= 0:
        # Route bypasses baseline capture (R1, R5-unresolved).
        return summary

    base_git_sha = _get_base_git_sha(cwd)
    if not base_git_sha:
        # No clean ref available → emit baseline_unavailable for every
        # detected verifier so Slice 3 can classify as unknown_failure later.
        for command_id in _detect_applicable_verifiers(cwd):
            _append_event(track_id, {
                "event": "baseline_unavailable",
                "command_id": command_id,
                "reason": "no_git_repo_or_no_head",
                "cwd": cwd,
            })
            summary["unavailable"].append({
                "command_id": command_id, "reason": "no_git_repo_or_no_head",
            })
        return summary

    applicable = _detect_applicable_verifiers(cwd)
    if not applicable:
        return summary

    owned_files_hash = _owned_files_hash(owned_files)
    baselines_dir = track_dir(track_id) / "baselines"
    _ensure_dir(baselines_dir)

    # Try stash for clean ref. If stash fails (e.g., nothing to stash, or
    # stash command unavailable), proceed against HEAD directly — base_git_sha
    # still pins the ref-identity for the baseline key.
    stash_active = False
    rc, stash_out = _run_git(
        ["stash", "push", "--include-untracked", "-m", f"baseline-{track_id}"],
        cwd, timeout=10,
    )
    if rc == 0 and "No local changes" not in stash_out:
        stash_active = True

    try:
        budget_used_ms = 0
        for command_id in applicable:
            remaining = budget - budget_used_ms
            if remaining <= 0:
                _append_event(track_id, {
                    "event": "baseline_unavailable",
                    "command_id": command_id,
                    "reason": "budget_exhausted",
                    "budget_ms_total": budget,
                    "budget_ms_used": budget_used_ms,
                })
                summary["unavailable"].append({
                    "command_id": command_id, "reason": "budget_exhausted",
                })
                continue
            result = _capture_baseline_command(
                command_id=command_id, cwd=cwd, remaining_budget_ms=remaining,
            )
            budget_used_ms += result["duration_ms"]

            key = _baseline_key(
                track_id=track_id, route=route, command_id=command_id,
                owned_files_hash=owned_files_hash, base_git_sha=base_git_sha,
            )
            baseline_record = {
                "key": key,
                "track_id": track_id,
                "route": route,
                "command_id": command_id,
                "owned_files_hash": owned_files_hash,
                "base_git_sha": base_git_sha,
                "status": result["status"],
                "exit_code": result["exit_code"],
                "output_excerpt": result["output_excerpt"],
                "duration_ms": result["duration_ms"],
                "captured_at": now_iso(),
            }
            (baselines_dir / f"{key}.json").write_text(
                json.dumps(baseline_record, indent=2) + "\n"
            )
            _append_event(track_id, {
                "event": "baseline_captured",
                "command_id": command_id,
                "status": result["status"],
                "key": key,
                "duration_ms": result["duration_ms"],
            })
            summary["captured"].append({
                "command_id": command_id, "status": result["status"], "key": key,
            })
        summary["budget_ms_used"] = budget_used_ms
    finally:
        if stash_active:
            _run_git(["stash", "pop"], cwd, timeout=10)

    return summary


def _emit_track_summary(track_id: str, *, closure_state: str) -> None:
    """Emit track_summary event at track close (plan-final §7)."""
    events = _read_events(track_id)
    phases_visited: list[str] = []
    for e in events:
        if e.get("event") == "phase_changed":
            tp = e.get("to_phase")
            if tp in PHASE_ENUM and (not phases_visited or phases_visited[-1] != tp):
                phases_visited.append(tp)
    question_events = [e for e in events if e.get("event") == "question_selection"]
    decision_events = [e for e in events if e.get("event") == "decision_record"]
    cycle_events = [e for e in events if e.get("event") == "cycle_summary"]
    distinct_kinds_changed = sorted({
        e.get("decision_kind") for e in decision_events
        if e.get("changed") and e.get("decision_kind")
    })
    _append_event(track_id, {
        "event": "track_summary",
        "closure_state": closure_state,
        "cycle_count": len(cycle_events),
        "phases_visited": phases_visited,
        "question_selection_count": len(question_events),
        "decision_record_count": len(decision_events),
        "decision_kinds_changed": distinct_kinds_changed,
    })


# ---------------------------------------------------------------------------
# Slice 3 — phase-aware verifier matrix + baseline consumer + transition gate
# ---------------------------------------------------------------------------
#
# The matrix declares which verifier commands are `required` vs `advisory`
# per (route, transition). A transition's matrix is the union across keys
# matched from most-specific (route, from→to) to most-generic (route, "*").
# Missing entries default to no verifiers (R1, R5-unresolved).
#
# Required + introduced_failure  → blocks transition (fail-closed).
# Required + unknown_failure     → policy decides per UNKNOWN_FAILURE_POLICY_BY_ROUTE.
# Required + preexisting_failure → does NOT block (baseline says it was already broken).
# Advisory                       → never blocks; recorded for postmortem.

VERIFIER_MATRIX: dict[str, dict[str, dict[str, str]]] = {
    # Route → transition-key → command_id → "required"|"advisory"
    "R2": {
        "build->verify": {
            "ruff_check": "required",
            "pytest_smoke": "advisory",
            "eslint_check": "advisory",
            "tsc_noemit": "advisory",
            "mypy_check": "advisory",
        },
        "verify->closeout": {
            "pytest_smoke": "required",
            "ruff_check": "advisory",
        },
    },
    "R3": {
        "build->verify": {
            "ruff_check": "required",
            "pytest_smoke": "required",
            "eslint_check": "required",
            "tsc_noemit": "required",
            "mypy_check": "advisory",
        },
        "verify->closeout": {
            "pytest_smoke": "required",
            "ruff_check": "required",
            "eslint_check": "required",
            "tsc_noemit": "required",
        },
    },
    "R4": {
        "build->verify": {
            "ruff_check": "required",
            "pytest_smoke": "required",
            "eslint_check": "required",
            "tsc_noemit": "required",
            "mypy_check": "required",
        },
        "verify->closeout": {
            "ruff_check": "required",
            "pytest_smoke": "required",
            "eslint_check": "required",
            "tsc_noemit": "required",
            "mypy_check": "required",
        },
    },
    # R1 + R5: empty — no verifier matrix runs.
}

# Per-route total transition-time budget. Distinct from BASELINE_BUDGET_MS_BY_ROUTE.
VERIFIER_MATRIX_BUDGET_MS_BY_ROUTE: dict[str, int] = {
    "R1": 0,
    "R2": 5000,
    "R3": 30000,
    "R4": 120000,
    "R5": 0,
}

# Per-route policy for unknown_failure on required verifiers (plan-final §2).
# R2 = advisory (do not block); R3/R4 = block (fail-closed).
UNKNOWN_FAILURE_POLICY_BY_ROUTE: dict[str, str] = {
    "R1": "advisory",
    "R2": "advisory",
    "R3": "block",
    "R4": "block",
    "R5": "advisory",
}

VERIFIER_RESULT_CLASS = (
    "pass",
    "introduced_failure",
    "preexisting_failure",
    "unknown_failure",
)


def matrix_for_transition(route: str, from_phase: str, to_phase: str) -> dict[str, str]:
    """Return the {command_id: requiredness} map for this (route, transition).

    Empty dict means no verifiers run for this cell.
    """
    route_block = VERIFIER_MATRIX.get(route, {})
    key = f"{from_phase}->{to_phase}"
    return dict(route_block.get(key, {}))


def _load_baseline(
    track_id: str,
    *,
    route: str,
    command_id: str,
    owned_files_hash: str,
    base_git_sha: str,
) -> dict[str, Any] | None:
    """Load a previously-captured baseline record by exact key match."""
    if not base_git_sha:
        return None
    key = _baseline_key(
        track_id=track_id, route=route, command_id=command_id,
        owned_files_hash=owned_files_hash, base_git_sha=base_git_sha,
    )
    path = track_dir(track_id) / "baselines" / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def classify_verifier_result(current_status: str, baseline_status: str | None) -> str:
    """Compare a fresh verifier run to its baseline.

    Truth table:
      baseline    | current                  | classification
      ----------- | ------------------------ | ------------------
      None        | any                      | unknown_failure (only if current != pass)
      None        | pass                     | pass
      pass        | pass                     | pass
      pass        | preexisting_failure/etc  | introduced_failure
      preexisting | pass                     | pass (fixed!)
      preexisting | preexisting_failure      | preexisting_failure
      timeout/etc | any non-pass             | unknown_failure
      any         | infra_error              | unknown_failure
      any         | timeout/budget_exhausted | unknown_failure
    """
    if current_status == "pass":
        return "pass"
    # current is some kind of failure/non-pass
    if current_status in ("infra_error", "timeout", "budget_exhausted"):
        return "unknown_failure"
    if baseline_status is None:
        return "unknown_failure"
    if baseline_status == "pass":
        return "introduced_failure"
    if baseline_status == "preexisting_failure":
        return "preexisting_failure"
    # baseline was infra_error/timeout/etc — can't compare
    return "unknown_failure"


def run_verifier_matrix(
    track_id: str,
    *,
    cwd: str,
    route: str,
    from_phase: str,
    to_phase: str,
    owned_files: list[str] | None,
    shadow: bool = False,
) -> dict[str, Any]:
    """Run verifiers required/advisory for this transition; classify vs baseline.

    Returns:
      {
        "transition_allowed": bool,
        "block_reasons": [{"command_id", "classification", "requiredness"}],
        "results": [{command_id, requiredness, current_status, classification, duration_ms}],
        "budget_ms_total": int, "budget_ms_used": int,
        "shadow": bool,
      }
    """
    matrix = matrix_for_transition(route, from_phase, to_phase)
    summary = {
        "transition_allowed": True,
        "block_reasons": [],
        "results": [],
        "budget_ms_total": VERIFIER_MATRIX_BUDGET_MS_BY_ROUTE.get(route, 0),
        "budget_ms_used": 0,
        "shadow": shadow,
    }
    if not matrix:
        return summary

    budget = VERIFIER_MATRIX_BUDGET_MS_BY_ROUTE.get(route, 0)
    if budget <= 0:
        return summary

    _append_event(track_id, {
        "event": "verifier_matrix_started",
        "route": route, "from_phase": from_phase, "to_phase": to_phase,
        "command_ids": sorted(matrix.keys()),
        "shadow": shadow,
    })

    applicable_in_repo = set(_detect_applicable_verifiers(cwd))
    base_git_sha = _get_base_git_sha(cwd) or ""
    owned_files_hash = _owned_files_hash(owned_files)
    unknown_policy = UNKNOWN_FAILURE_POLICY_BY_ROUTE.get(route, "advisory")

    budget_used_ms = 0
    for command_id, requiredness in matrix.items():
        # Skip commands the project doesn't actually have markers for.
        if command_id not in applicable_in_repo:
            summary["results"].append({
                "command_id": command_id, "requiredness": requiredness,
                "current_status": "not_applicable",
                "classification": "pass",  # not applicable → can't block
                "duration_ms": 0,
            })
            _append_event(track_id, {
                "event": "verifier_classified",
                "command_id": command_id, "requiredness": requiredness,
                "current_status": "not_applicable", "classification": "pass",
                "shadow": shadow,
            })
            continue

        remaining = budget - budget_used_ms
        if remaining <= 0:
            current_status = "budget_exhausted"
        else:
            run_result = _capture_baseline_command(
                command_id=command_id, cwd=cwd, remaining_budget_ms=remaining,
            )
            current_status = run_result["status"]
            budget_used_ms += run_result["duration_ms"]
            _append_event(track_id, {
                "event": "verifier_run",
                "command_id": command_id, "requiredness": requiredness,
                "status": current_status, "duration_ms": run_result["duration_ms"],
                "shadow": shadow,
            })

        baseline = _load_baseline(
            track_id, route=route, command_id=command_id,
            owned_files_hash=owned_files_hash, base_git_sha=base_git_sha,
        )
        classification = classify_verifier_result(
            current_status, baseline["status"] if baseline else None,
        )
        result_entry = {
            "command_id": command_id, "requiredness": requiredness,
            "current_status": current_status, "classification": classification,
            "duration_ms": 0 if remaining <= 0 else run_result["duration_ms"],
        }
        summary["results"].append(result_entry)
        _append_event(track_id, {
            "event": "verifier_classified",
            "command_id": command_id, "requiredness": requiredness,
            "current_status": current_status, "classification": classification,
            "baseline_present": baseline is not None,
            "shadow": shadow,
        })

        # Blocking decision: only `required` verifiers can block.
        if requiredness != "required":
            continue
        if classification == "introduced_failure":
            summary["transition_allowed"] = False
            summary["block_reasons"].append({
                "command_id": command_id,
                "classification": classification,
                "requiredness": requiredness,
            })
        elif classification == "unknown_failure" and unknown_policy == "block":
            summary["transition_allowed"] = False
            summary["block_reasons"].append({
                "command_id": command_id,
                "classification": classification,
                "requiredness": requiredness,
                "policy": "block_on_unknown",
            })

    summary["budget_ms_used"] = budget_used_ms
    _append_event(track_id, {
        "event": "verifier_matrix_completed",
        "transition_allowed": summary["transition_allowed"],
        "block_reasons": summary["block_reasons"],
        "budget_ms_used": budget_used_ms,
        "budget_ms_total": budget,
        "shadow": shadow,
    })
    return summary


# Map cycle_track's recommended_action → the phase the loop is moving into
# when that action runs. Used by _maybe_auto_advance_phase to wire phase
# advancement into the cycle flow.
_ACTION_TO_PHASE_INTENT: dict[str, str] = {
    "dispatch": "build",     # dispatching code work → enter build
    "evaluate": "verify",    # evaluator run → enter verify
    "close": "closeout",     # close → enter closeout
}


def _collect_evidence_keys(track_id: str) -> set[str]:
    """Derive phase-transition evidence keys from the event log.

    The phase transition table requires keys like `repo_facts`, `patch_applied`,
    `tests_pass`. The cycle flow doesn't directly emit those tags — they're
    implicit in observable events:
      - dispatch (inline or governed) implies discovery + design work was done
        plus the patch surface was opened (patch_applied)
      - verifier_matrix_completed with transition_allowed=True implies
        lint/tests passed cleanly (no introduced regressions)
      - verifier_classified with introduced_failure implies the retry edge
    """
    events = _read_events(track_id)
    ev: set[str] = set()
    for e in events:
        et = e.get("event")
        if et in ("inline_dispatched", "governed_dispatched"):
            ev.update({
                "repo_facts", "scope", "constraints",
                "plan_approved", "owned_files", "validation_plan",
                "patch_applied",
            })
        elif et == "verifier_matrix_completed" and e.get("transition_allowed"):
            ev.update({"lint_pass", "tests_pass", "no_introduced_regressions"})
        elif et == "verifier_classified" and e.get("classification") == "introduced_failure":
            ev.add("introduced_failure")
    return ev


def _active_slice_owned_files(
    state: dict[str, Any],
    *,
    action: str,
    action_result: dict[str, Any],
    anticipation: dict[str, Any],
) -> list[str]:
    """Find the owned_scope list for the slice the cycle is operating on.

    Resolution order:
      - dispatch  → action_result.dispatch.slice_id (the just-dispatched slice)
      - evaluate  → anticipation.evaluator_dispatch.slice_id
      - other     → frontier.next_slice_id (current focus)
    Returns [] when no slice can be resolved (e.g., close, or empty graph).
    """
    graph = state.get("views", {}).get("graph", {}) or {}
    frontier = state.get("views", {}).get("frontier", {}) or {}
    nodes = graph.get("nodes") or {}

    slice_id: str | None = None
    if action == "dispatch":
        slice_id = (action_result.get("dispatch") or {}).get("slice_id")
    elif action == "evaluate":
        slice_id = (anticipation.get("evaluator_dispatch") or {}).get("slice_id")
    if not slice_id:
        slice_id = frontier.get("next_slice_id")

    if not slice_id:
        return []
    node = nodes.get(slice_id) or {}
    owned = node.get("owned_scope", []) or []
    # Defensive: ensure list[str].
    return [str(x) for x in owned if x]


def _maybe_auto_advance_phase(
    track_id: str,
    *,
    target_phase: str | None,
    route: str,
    cwd: str,
    owned_files: list[str] | None,
    shadow: bool = False,
) -> list[dict[str, Any]]:
    """Walk phase transitions toward target_phase using collected evidence.

    Returns a list of decisions (one per attempted transition). Stops at the
    first blocked transition. Returns [] if target_phase is None, cwd missing,
    or already at/past target.
    """
    if not target_phase or not cwd:
        return []
    phase_order = list(PHASE_ENUM)
    if target_phase not in phase_order:
        return []
    decisions: list[dict[str, Any]] = []
    # Cap iterations at len(PHASE_ENUM) to prevent any pathological loop.
    for _ in range(len(phase_order)):
        current = current_phase(_read_events(track_id))
        if current == target_phase:
            break
        cur_idx = phase_order.index(current)
        tgt_idx = phase_order.index(target_phase)
        if cur_idx >= tgt_idx:
            break  # don't walk backward
        next_phase = phase_order[cur_idx + 1]
        decision = attempt_phase_transition(
            track_id, to_phase=next_phase, route=route,
            cwd=cwd, owned_files=owned_files,
            evidence_keys=_collect_evidence_keys(track_id),
            shadow=shadow,
        )
        decisions.append(decision)
        if not decision["allowed"]:
            break
    return decisions


def attempt_phase_transition(
    track_id: str,
    *,
    to_phase: str,
    route: str,
    cwd: str,
    owned_files: list[str] | None,
    evidence_keys: set[str] | None = None,
    shadow: bool = False,
) -> dict[str, Any]:
    """Public entry point: try to transition to `to_phase`.

    Steps:
      1. Compute current phase via fold over events.
      2. Check phase_transition_allowed (Slice 1a guard on enum + evidence).
      3. Run the verifier matrix for (route, from→to).
      4. If transition is allowed AND matrix doesn't block → emit phase_changed
         (live mode only). In shadow mode emit shadow_decision with the
         would-have-been transition + verifier summary; do NOT emit phase_changed.

    Returns the full decision dict for the caller.
    """
    events = _read_events(track_id)
    from_phase = current_phase(events)
    evidence = evidence_keys or set()

    guard = phase_transition_allowed(from_phase, to_phase, route, evidence)
    if not guard["allowed"]:
        decision = {
            "allowed": False,
            "from_phase": from_phase, "to_phase": to_phase,
            "reason": guard.get("reason", "transition_not_allowed"),
            "missing_evidence": guard.get("missing_evidence", []),
            "verifier_summary": None,
            "shadow": shadow,
        }
        if shadow:
            _append_event(track_id, {
                "event": "shadow_decision",
                "decision_kind": "phase_transition",
                "would_emit_phase_changed": False,
                "from_phase": from_phase, "to_phase": to_phase,
                "reason": decision["reason"],
            })
        else:
            _append_event(track_id, {
                "event": "phase_transition_blocked",
                "from_phase": from_phase, "to_phase": to_phase,
                "reason": decision["reason"],
                "missing_evidence": decision["missing_evidence"],
            })
        return decision

    matrix_summary = run_verifier_matrix(
        track_id, cwd=cwd, route=route,
        from_phase=from_phase, to_phase=to_phase,
        owned_files=owned_files, shadow=shadow,
    )

    if not matrix_summary["transition_allowed"]:
        decision = {
            "allowed": False,
            "from_phase": from_phase, "to_phase": to_phase,
            "reason": "verifier_matrix_blocked",
            "verifier_summary": matrix_summary,
            "shadow": shadow,
        }
        if shadow:
            _append_event(track_id, {
                "event": "shadow_decision",
                "decision_kind": "phase_transition",
                "would_emit_phase_changed": False,
                "from_phase": from_phase, "to_phase": to_phase,
                "reason": "verifier_matrix_blocked",
                "block_reasons": matrix_summary["block_reasons"],
            })
        else:
            _append_event(track_id, {
                "event": "phase_transition_blocked",
                "from_phase": from_phase, "to_phase": to_phase,
                "reason": "verifier_matrix_blocked",
                "block_reasons": matrix_summary["block_reasons"],
            })
        return decision

    decision = {
        "allowed": True,
        "from_phase": from_phase, "to_phase": to_phase,
        "verifier_summary": matrix_summary,
        "shadow": shadow,
    }
    if shadow:
        _append_event(track_id, {
            "event": "shadow_decision",
            "decision_kind": "phase_transition",
            "would_emit_phase_changed": True,
            "from_phase": from_phase, "to_phase": to_phase,
            "verifier_budget_ms_used": matrix_summary["budget_ms_used"],
        })
    else:
        _append_phase_event(
            track_id, from_phase=from_phase, to_phase=to_phase,
            evidence=sorted(evidence),
        )
    return decision


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _run_git(args: list[str], cwd: str, *, timeout: float = 5) -> tuple[int, str]:
    """Run a git command, returning (returncode, output)."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 1, ""


def _is_git_repo(cwd: str) -> bool:
    rc, _ = _run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    return rc == 0


def _last_checkpoint_sha(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("event") == "git_checkpoint_recorded":
            return event.get("commit_sha")
    return None


def _git_context(cwd: str, *, since_sha: str | None = None) -> dict[str, Any]:
    """Collect git state for briefings."""
    if not _is_git_repo(cwd):
        return {"available": False, "reason": "not_a_git_repo"}
    _, branch = _run_git(["branch", "--show-current"], cwd)
    _, status_short = _run_git(["status", "--short"], cwd)
    _, log_oneline = _run_git(["log", "--oneline", "-15"], cwd)
    diff_stat = ""
    if since_sha:
        rc, out = _run_git(["diff", "--stat", f"{since_sha}..HEAD"], cwd)
        if rc == 0:
            diff_stat = out
    changed = [l.strip() for l in status_short.splitlines() if l.strip()]
    return {
        "available": True,
        "branch": branch,
        "dirty": bool(changed),
        "changed_files": changed[:20],
        "changed_file_count": len(changed),
        "recent_commits": log_oneline.splitlines()[:15],
        "diff_stat_since_checkpoint": diff_stat or None,
    }


def _git_checkpoint_on_acceptance(
    track_id: str, node_id: str, node: dict[str, Any], cwd: str,
) -> dict[str, Any]:
    """Create a git commit when a slice is accepted, if there are changes."""
    if not cwd or not os.path.isdir(cwd):
        return {"status": "skipped", "reason": "cwd_not_found"}
    if not _is_git_repo(cwd):
        return {"status": "skipped", "reason": "not_a_git_repo"}
    rc, status_out = _run_git(["status", "--porcelain"], cwd)
    if rc != 0 or not status_out.strip():
        return {"status": "skipped", "reason": "no_changes"}
    _, current_branch = _run_git(["branch", "--show-current"], cwd)
    if current_branch in ("main", "master"):
        return {"status": "skipped", "reason": "on_protected_branch", "branch": current_branch}
    title = node.get("title", node_id)[:72]
    msg = f"[auto-runtime] {node_id} accepted: {title}"
    owned = node.get("owned_scope", [cwd])
    for scope in owned:
        if os.path.exists(os.path.realpath(scope)):
            _run_git(["add", os.path.realpath(scope)], cwd, timeout=10)
    rc, staged = _run_git(["diff", "--cached", "--stat"], cwd)
    if not staged.strip():
        _run_git(["add", "-u"], cwd, timeout=10)
        _, staged = _run_git(["diff", "--cached", "--stat"], cwd)
        if not staged.strip():
            return {"status": "skipped", "reason": "no_stageable_changes"}
    rc, out = _run_git(["commit", "-m", msg], cwd, timeout=30)
    if rc != 0:
        return {"status": "error", "reason": "git_commit_failed", "output": out[:500]}
    _, sha = _run_git(["rev-parse", "HEAD"], cwd)
    return {"status": "committed", "commit_sha": sha, "commit_message": msg, "branch": current_branch}


def _omni_mem_checkpoint(
    track_id: str, node_id: str, node: dict[str, Any], cwd: str, boundary: str,
) -> dict[str, Any]:
    """Emit an omni-mem checkpoint at a slice boundary (best-effort, never raises).

    Boundary detection is deterministic and lives in the caller (accepted slice ->
    slice_complete, blocked slice -> escalation); the summary is produced by
    omni-mem's own summarizer over the raw observation span. Gated by
    OMNI_MEM_CHECKPOINTS_ENABLED (default off) so existing tracks are unchanged
    until the hub opts in. Targets the omni-mem container by default (durable DB at
    /data/omni-mem.db); OMNI_MEM_CONTAINER="" falls back to a local CLI.
    """
    if os.environ.get("OMNI_MEM_CHECKPOINTS_ENABLED", "").lower() not in ("1", "true", "yes"):
        return {"status": "skipped", "reason": "disabled"}
    workspace_id = os.path.basename(os.path.normpath(cwd)) if cwd else "global"
    cli = os.environ.get("OMNI_MEM_CLI_BIN", "omni-mem")
    # Unset -> vault routed by cwd (~/chad_personal -> omni-mem-personal, else
    # omni-mem). Explicit "" keeps the documented local-CLI fallback.
    container = os.environ.get("OMNI_MEM_CONTAINER")
    if container is None:
        container = container_for_cwd(cwd)
    args = [
        cli, "generate_checkpoint",
        "--workspaceId", workspace_id,
        "--sessionId", track_id,
        "--boundary", boundary,
    ]
    cmd = (["docker", "exec", container] + args) if container else args
    try:
        timeout = int(os.environ.get("OMNI_MEM_CHECKPOINT_TIMEOUT", "20"))
    except ValueError:
        timeout = 20
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return {"status": "error", "reason": type(exc).__name__}
    if result.returncode != 0:
        return {"status": "error", "reason": "cli_failed", "output": (result.stderr or result.stdout)[:300]}
    return {"status": "recorded", "workspace_id": workspace_id, "boundary": boundary}


# ---------------------------------------------------------------------------
# UI detection
# ---------------------------------------------------------------------------

def _detect_ui_work(task: str, graph: dict[str, Any]) -> dict[str, Any]:
    """Detect whether the task involves frontend/UI changes."""
    task_lower = task.lower()
    signals = []
    score = 0.0
    matched = [kw for kw in UI_KEYWORDS if kw in task_lower]
    if matched:
        signals.append(f"task_keywords:{','.join(matched[:5])}")
        score += min(len(matched) * 0.15, 0.6)
    file_mentions = FILE_PATH_RE.findall(task) if 'FILE_PATH_RE' in dir() else []
    ui_files = [f for f in file_mentions if Path(f).suffix.lower() in UI_FILE_EXTENSIONS]
    if ui_files:
        signals.append(f"ui_files:{','.join(ui_files[:5])}")
        score += min(len(ui_files) * 0.2, 0.4)
    for nid, node in graph.get("nodes", {}).items():
        text = (node.get("title", "") + " " + node.get("description", "")).lower()
        if any(kw in text for kw in UI_KEYWORDS):
            signals.append(f"graph_node:{nid}")
            score += 0.1
            break
    return {"is_ui_work": score >= 0.3, "confidence": round(min(score, 1.0), 2), "signals": signals}


# ---------------------------------------------------------------------------
# Sprint contract validation
# ---------------------------------------------------------------------------

def validate_sprint_contract(node: dict[str, Any], route_id: str) -> dict[str, Any]:
    """Validate a slice node's sprint contract against route requirements."""
    if route_id in ("R1", "R2"):
        return {"valid": True, "missing": []}
    contract = node.get("slice_contract", {})
    criteria = contract.get("acceptance_criteria", [])
    missing = []
    if not criteria:
        missing.append("missing_acceptance_criteria")
    elif len(criteria) < MIN_ACCEPTANCE_CRITERIA_R3_R4:
        missing.append(f"acceptance_criteria_below_minimum:need_{MIN_ACCEPTANCE_CRITERIA_R3_R4}_have_{len(criteria)}")
    return {"valid": len(missing) == 0, "missing": missing}


# Track-level reviewer ack (S7 fleet-hardening). planner.md/reviewer.md always
# CLAIMED "no execution until the reviewer acks the sprint contract" but no
# code checked it — a dead-letter contract. This is the enforcement: R3/R4
# dispatch blocks until an ack is recorded via `auto_runtime.py record-ack`.
REVIEWER_ACK_KEY = "reviewer_ack"


def validate_reviewer_ack(state: dict[str, Any], route_id: str) -> dict[str, Any]:
    """R3/R4 gate: dispatch may not start until the sprint contract carries a
    recorded reviewer ack. R1/R2 exempt. Pure predicate over track state."""
    if route_id in ("R1", "R2"):
        return {"valid": True}
    ack = state.get("views", {}).get("governance", {}).get(REVIEWER_ACK_KEY)
    if isinstance(ack, dict) and ack.get("acked_by") and ack.get("at"):
        return {"valid": True, "ack": ack}
    return {"valid": False, "reason": "missing_reviewer_ack"}


def record_reviewer_ack(track_id: str, *, acked_by: str = "reviewer", ref: str = "") -> dict[str, Any]:
    """Record the reviewer's sprint-contract ack at track level.

    `ref` should identify WHAT was acked (message id, criteria hash, or the
    ack text) so the ack is auditable, not a rubber stamp. Unblocks R3/R4
    dispatch; the gate itself never mutates node state, so recording the ack
    is the complete unblock."""
    td = track_dir(track_id)
    state = _load_json(td / "objective.state.json")
    governance = state["views"].setdefault("governance", {})
    ack = {"acked_by": acked_by, "ref": ref, "at": now_iso()}
    governance[REVIEWER_ACK_KEY] = ack
    _save_json(td / "objective.state.json", state)
    _append_event(track_id, {"event": "reviewer_ack_recorded", **ack})
    return {"track_id": track_id, "reviewer_ack": ack}


def phase_transition_allowed(
    from_phase: str,
    to_phase: str,
    route: str,
    evidence_keys: set[str] | frozenset[str],
    *,
    retry_count: int = 0,
) -> dict[str, Any]:
    """Predicate: is this phase edge legal under the transition table?

    Slice 1a primitive: pure function over the transition table. Does NOT
    enforce — callers in Slices 1b/3 will gate dispatch on the result.

    Returns:
        {
            "allowed": bool,
            "missing": [evidence_key, ...],  # missing required evidence
            "reason": str | None,            # short failure reason if not allowed
        }
    """
    # Initial entry — no prior phase, target must be PHASE_INITIAL
    if from_phase is None:
        if to_phase == PHASE_INITIAL:
            return {"allowed": True, "missing": [], "reason": None}
        return {
            "allowed": False, "missing": [],
            "reason": f"initial_phase_must_be_{PHASE_INITIAL}",
        }

    edge = (from_phase, to_phase)
    if edge not in PHASE_TRANSITION_TABLE:
        return {
            "allowed": False, "missing": [],
            "reason": f"illegal_edge:{from_phase}->{to_phase}",
        }

    # Retry budget for verify->build
    budget = PHASE_RETRY_BUDGET.get(edge)
    if budget is not None and retry_count >= budget:
        return {
            "allowed": False, "missing": [],
            "reason": f"retry_budget_exhausted:{from_phase}->{to_phase}:max_{budget}",
        }

    required = PHASE_TRANSITION_TABLE[edge]
    missing = sorted(required - set(evidence_keys))
    if missing:
        return {
            "allowed": False, "missing": missing,
            "reason": "missing_required_evidence",
        }
    return {"allowed": True, "missing": [], "reason": None}


def route_change_reconcile(
    from_route: str,
    to_route: str,
    current_evidence_keys: set[str] | frozenset[str],
) -> dict[str, Any]:
    """Compute the route-change reconciliation outcome.

    Implements the generic stricter-route rule plus special-case overrides
    from plan-final §1. Slice 1a primitive: returns the decision; callers
    in later slices will emit phase_changed + invalidation events.

    Returns:
        {
            "applies": bool,           # False if no change required
            "target_phase": str | None,
            "invalidate": [str, ...],
            "required_backfill": [str, ...],
            "dispatch": "PAUSE" | "resume" | None,
            "rule": "special_case" | "generic_stricter" | "noop",
        }
    """
    if from_route == to_route:
        return {
            "applies": False, "target_phase": None,
            "invalidate": [], "required_backfill": [],
            "dispatch": None, "rule": "noop",
        }

    # Special-case rows override the generic rule
    special = ROUTE_CHANGE_SPECIAL_CASES.get((from_route, to_route))
    if special is not None:
        return {
            "applies": True,
            "target_phase": special["target_phase"],
            "invalidate": list(special["invalidate"]),
            "required_backfill": list(special["required_backfill"]),
            "dispatch": special["dispatch"],
            "rule": "special_case",
        }

    # Generic stricter-route rule: only applies to upward/corrective changes
    # within the ordered set {R1, R2, R3, R4}.
    if from_route not in ROUTE_STRICTNESS or to_route not in ROUTE_STRICTNESS:
        return {
            "applies": False, "target_phase": None,
            "invalidate": [], "required_backfill": [],
            "dispatch": None, "rule": "noop",
        }
    if ROUTE_STRICTNESS[to_route] <= ROUTE_STRICTNESS[from_route]:
        # Downgrade or sideways — out of scope for Slice 1a.
        return {
            "applies": False, "target_phase": None,
            "invalidate": [], "required_backfill": [],
            "dispatch": None, "rule": "noop",
        }

    # Stricter route: target_phase = earliest phase whose required evidence
    # is missing under new_route. Compute as union of inbound-edge evidence
    # plus route-level required evidence, walking forward from discovery.
    new_route_required = ROUTE_REQUIRED_EVIDENCE.get(to_route, frozenset())
    have = set(current_evidence_keys)
    target_phase = PHASE_INITIAL
    # Walk discovery -> design -> build -> verify -> closeout; first phase
    # whose inbound edge evidence isn't fully satisfied is the target.
    inbound_chain = [
        ("design", PHASE_TRANSITION_TABLE[("discovery", "design")]),
        ("build", PHASE_TRANSITION_TABLE[("design", "build")]),
        ("verify", PHASE_TRANSITION_TABLE[("build", "verify")]),
        ("closeout", PHASE_TRANSITION_TABLE[("verify", "closeout")]),
    ]
    for phase_name, required in inbound_chain:
        if not required.issubset(have):
            target_phase = phase_name
            break
    # Route-level evidence stacks on top — if missing, reset further back.
    if not new_route_required.issubset(have):
        target_phase = "discovery"

    required_backfill = sorted(
        (set().union(*[r for _, r in inbound_chain]) | new_route_required) - have
    )

    return {
        "applies": True,
        "target_phase": target_phase,
        # Invalidate artifacts produced under weaker route and lacking new_route's tags.
        # Slice 1a records the rule; consumer in later slices does the actual GC.
        "invalidate": [f"artifacts_tagged_produced_under:{from_route}_lacking:"
                       + ",".join(sorted(new_route_required))]
                       if new_route_required else [],
        "required_backfill": required_backfill,
        "dispatch": "PAUSE" if required_backfill else "resume",
        "rule": "generic_stricter",
    }


# ---------------------------------------------------------------------------
# Track locking (flock-based)
# ---------------------------------------------------------------------------

class TrackLock:
    """Exclusive flock on track directory. Context manager."""

    def __init__(self, track_id: str, timeout: float = TRACK_LOCK_TIMEOUT_SECONDS):
        self.lock_path = track_dir(track_id) / ".lock"
        self.timeout = timeout
        self._fd = None

    def __enter__(self):
        _ensure_dir(self.lock_path.parent)
        self._fd = open(self.lock_path, "w")
        deadline = time.monotonic() + self.timeout
        delay = 0.05
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (IOError, OSError):
                if time.monotonic() >= deadline:
                    _append_event(
                        self.lock_path.parent.name,
                        {"event": "track_lock_timeout", "timeout": self.timeout},
                    )
                    raise TimeoutError(
                        f"Could not acquire lock on {self.lock_path} "
                        f"within {self.timeout}s"
                    )
                time.sleep(delay)
                delay = min(delay * 1.5, 0.2)

    def __exit__(self, *exc):
        if self._fd:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None


# ---------------------------------------------------------------------------
# Route manifest
# ---------------------------------------------------------------------------

def load_route_manifest() -> dict[str, Any]:
    return _load_json(ROUTE_MANIFEST_PATH)


def load_control_plane() -> dict[str, Any]:
    return _load_json(CONTROL_PLANE_PATH)


def get_route_rule(route_id: str) -> dict[str, Any]:
    manifest = load_route_manifest()
    for rule in manifest.get("rules", []):
        if rule["id"] == route_id:
            return rule
    raise ValueError(f"Unknown route: {route_id}")


# ---------------------------------------------------------------------------
# Route classification
# ---------------------------------------------------------------------------

AUTH_KEYWORDS = {"auth", "login", "session", "token", "oauth", "jwt", "rbac", "permission"}
SECURITY_KEYWORDS = {"security", "vulnerability", "cve", "xss", "injection", "csrf", "encrypt"}
MIGRATION_KEYWORDS = {"migration", "migrate", "schema", "alter table", "drop", "rename column"}
DEPLOY_KEYWORDS = {"deploy", "ci/cd", "pipeline", "release", "rollback"}

FILE_PATH_RE = re.compile(r"(?:^|\s)([a-zA-Z0-9_./-]+\.[a-zA-Z]{1,6})(?:\s|$|[,;:])")


def classify_route(
    task: str,
    *,
    route_override: str | None = None,
    classification_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify a task into R1-R5 route with execution parameters."""
    if route_override:
        rule = get_route_rule(route_override)
        return _build_route_result(rule)

    if classification_payload:
        rid = classification_payload.get("route_hint", "R2")
        rule = get_route_rule(rid)
        return _build_route_result(rule)

    # Inline classification (mirrors classify_prompt.py logic)
    task_lower = task.lower()
    file_mentions = FILE_PATH_RE.findall(task)
    file_count = len(file_mentions)

    touches_auth = any(kw in task_lower for kw in AUTH_KEYWORDS)
    touches_security = any(kw in task_lower for kw in SECURITY_KEYWORDS)
    touches_migration = any(kw in task_lower for kw in MIGRATION_KEYWORDS)
    touches_deploy = any(kw in task_lower for kw in DEPLOY_KEYWORDS)
    high_risk = touches_auth or touches_security or touches_migration

    # Vagueness detection
    word_count = len(task.split())
    is_vague = word_count < 5 and file_count == 0

    if is_vague:
        route_id = "R5"
    elif high_risk:
        route_id = "R4"
    elif file_count > 3 or word_count > 30:
        route_id = "R3"
    elif file_count <= 2 and word_count <= 15:
        route_id = "R2"
    elif word_count <= 8 and "?" in task:
        route_id = "R1"
    else:
        route_id = "R2"

    rule = get_route_rule(route_id)
    result = _build_route_result(rule)
    result["classification_evidence"] = {
        "file_count": file_count,
        "file_mentions": file_mentions,
        "word_count": word_count,
        "touches_auth": touches_auth,
        "touches_security": touches_security,
        "touches_migration": touches_migration,
        "touches_deploy": touches_deploy,
        "is_vague": is_vague,
    }
    return result


def _build_route_result(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "route_id": rule["id"],
        "route_name": rule.get("name", ""),
        "execution_shape": rule.get("execution_shape", "single_lane"),
        "lane_caps": rule.get("lane_caps", {}),
        "frontier_dispatch_order": rule.get("frontier_dispatch_order", []),
        "reviewer_barrier_points": rule.get("reviewer_barrier_points", []),
        "swarm_cap": rule.get("route_swarm_cap", 1),
        "convergence_required": rule.get("convergence_required_for_closure", False),
        "risk_class": rule.get("risk_class", "medium"),
        "packetization_required": rule.get("packetization_required", False),
        "default_parallelism_policy": rule.get("default_parallelism_policy", "serial_only"),
        "profile_overrides": rule.get("profile_overrides", {}),
    }


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def build_preflight_report(
    task: str, cwd: str, route: dict[str, Any],
) -> dict[str, Any]:
    """Score prompt readability and readiness."""
    words = task.split()
    file_mentions = FILE_PATH_RE.findall(task)
    has_verb = any(w.lower() in {
        "add", "fix", "create", "update", "remove", "refactor", "implement",
        "build", "write", "delete", "migrate", "deploy", "test",
    } for w in words[:5])

    score = 50
    if len(words) >= 10:
        score += 15
    if file_mentions:
        score += 10
    if has_verb:
        score += 10
    if route["route_id"] in ("R1", "R2"):
        score += 10
    score = min(score, 100)

    classification = "actionable"
    if score <= PREFLIGHT_BLOCK_SCORE_MAX:
        classification = "blocked"
    elif score <= PREFLIGHT_WARN_SCORE_MAX:
        classification = "warn"

    return {
        "task": task,
        "score": score,
        "classification": classification,
        "local_evidence": {
            "primary_scope": cwd,
            "file_mentions": file_mentions,
        },
        "route_id": route["route_id"],
        "requires_user_clarification": classification == "blocked",
    }


# ---------------------------------------------------------------------------
# Policy envelope
# ---------------------------------------------------------------------------

def compile_policy_envelope(
    task: str,
    cwd: str,
    route: dict[str, Any],
    *,
    soft_policy: dict[str, Any] | None = None,
    preflight: dict[str, Any] | None = None,
    graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the policy envelope that governs execution."""
    manifest = load_route_manifest()
    ui_detection = _detect_ui_work(task, graph or {})
    return {
        "schema_version": POLICY_ENVELOPE_SCHEMA,
        "hard_policy": {
            "route": route,
            "reporting_policy": "terminal_only" if route["route_id"] in ("R1", "R2") else "governed",
            "terminal_states": list(OBJECTIVE_TERMINAL_STATES),
            "destructive_git_forbidden": True,
            "review_priority": "high" if route["risk_class"] == "high" else "normal",
            "closure_requires_evidence": route["route_id"] in ("R3", "R4"),
            "allowed_scope": [cwd],
            "cwd": cwd,
        },
        "route_policy": {
            "dispatch_cycle_max": DISPATCH_CYCLE_MAX_BY_ROUTE.get(route["route_id"], 12),
            "advisor_checkpoint_max": ADVISOR_CHECKPOINT_MAX_BY_ROUTE.get(route["route_id"], 0),
            "same_strategy_retry_budget": SAME_STRATEGY_RETRY_BUDGET,
            "no_frontier_movement_max": NO_FRONTIER_MOVEMENT_MAX,
            "verification_profile": _verification_profile(route["route_id"], ui_detected=ui_detection["is_ui_work"]),
            "ui_detection": ui_detection,
            "worker_runtime": _resolve_worker_runtime(route, manifest),
        },
        "soft_policy": soft_policy or {},
        "preflight": preflight,
        "manifest_version": manifest.get("version", "unknown"),
        "updated_at": now_iso(),
    }


def _verification_profile(route_id: str, *, ui_detected: bool = False) -> str:
    base = {
        "R1": "light",
        "R2": "slice_only",
        "R3": "progressive",
        "R4": "reviewer_first",
        "R5": "light",
    }.get(route_id, "slice_only")
    if ui_detected and base in ("progressive", "reviewer_first"):
        return "browser_e2e"
    return base


# Slice 4 (heterogeneous fleet contract): valid worker_runtime values.
# `claude` = direct claude dispatch (current default); `goose` = goose ACP via
# ~/.claude/bin/goose_dispatch.py (subscription path); `opencode` = anthropic-
# concurrency-system runner.
VALID_WORKER_RUNTIMES = ("claude", "goose", "opencode")
DEFAULT_WORKER_RUNTIME = "claude"


def _resolve_worker_runtime(route: dict[str, Any], manifest: dict[str, Any]) -> str:
    """Resolve worker_runtime for a route using the standard override chain.

    Precedence (most specific first):
      1. route.profile_overrides.worker.worker_runtime
      2. manifest.profiles.worker.worker_runtime
      3. DEFAULT_WORKER_RUNTIME ("claude")

    See ~/.claude/plans/users-chadsimon-thoughts-md-take-a-giggly-moore.md slice 4
    for the heterogeneous fleet contract.
    """
    worker_override = route.get("profile_overrides", {}).get("worker", {})
    candidate = worker_override.get("worker_runtime")
    if candidate in VALID_WORKER_RUNTIMES:
        return candidate
    profile_default = manifest.get("profiles", {}).get("worker", {}).get("worker_runtime")
    if profile_default in VALID_WORKER_RUNTIMES:
        return profile_default
    return DEFAULT_WORKER_RUNTIME


def _build_verification_hints(policy: dict[str, Any], node: dict[str, Any]) -> dict[str, Any] | None:
    """Build verification hints for the executing session.

    When the verification_profile is ``browser_e2e`` (UI work detected on R3/R4), browser-e2e
    coverage is REQUIRED, not advisory. The ``test_breadth_check`` postflight gate (configured in
    route_manifest.json's ``postflight.gate_chain``) enforces breadth presence before slice closure.
    See ``~/.claude/standards/testing-standard.md`` for the full breadth/escalation rules.
    """
    route_policy = policy.get("route_policy", {})
    profile = route_policy.get("verification_profile", "slice_only")
    ui = route_policy.get("ui_detection", {})
    if profile != "browser_e2e" and not ui.get("is_ui_work", False):
        return None
    return {
        # Legacy key kept for backward-compat with skills/drive/SKILL.md and downstream evaluators.
        "playwright_recommended": True,
        # New keys: testing-standard v1.0 enforcement signal.
        "playwright_required": True,
        "required": True,
        "testing_standard": "/Users/chadsimon/.claude/standards/testing-standard.md",
        "enforced_by": "test_breadth_check (route_manifest.json postflight.gate_chain)",
        "required_breadths_hint": ["full", "browser-e2e"],
        "verification_profile": profile,
        "ui_detection": ui,
        "workflow": [
            "Start dev server if not running",
            "browser_navigate to relevant page",
            "browser_snapshot to verify DOM state",
            "browser_take_screenshot for visual evidence",
            "browser_console_messages for JS errors",
            "browser_network_requests for failed API calls",
        ],
        "mcp_tools": [
            "mcp__playwright__browser_navigate",
            "mcp__playwright__browser_snapshot",
            "mcp__playwright__browser_take_screenshot",
            "mcp__playwright__browser_click",
            "mcp__playwright__browser_console_messages",
        ],
    }


# ---------------------------------------------------------------------------
# Evaluator feedback loop
# ---------------------------------------------------------------------------

def _should_evaluate(node: dict[str, Any], policy: dict[str, Any]) -> bool:
    """Check if a slice in awaiting_verification needs an evaluator pass."""
    if node.get("state") != "awaiting_verification":
        return False
    route_id = policy.get("hard_policy", {}).get("route", {}).get("route_id", "R2")
    if route_id not in ("R3", "R4"):
        return False
    profile = policy.get("route_policy", {}).get("verification_profile", "slice_only")
    if profile in ("light", "slice_only"):
        return False
    if any("evaluator_verdict" in str(e) for e in node.get("evidence_refs", [])):
        return False
    return True


def build_evaluator_dispatch(
    track_id: str, slice_id: str, node: dict[str, Any], policy: dict[str, Any],
) -> dict[str, Any]:
    """Build evaluator dispatch payload for Claude session to execute."""
    contract = node.get("slice_contract", {})
    profile = policy.get("route_policy", {}).get("verification_profile", "slice_only")
    ui = policy.get("route_policy", {}).get("ui_detection", {})
    return {
        "action": "evaluate",
        "track_id": track_id,
        "slice_id": slice_id,
        "evaluator_model_hint": "haiku",
        "contract": {
            "objective": contract.get("objective", ""),
            "acceptance_criteria": contract.get("acceptance_criteria", []),
            "verification_commands": contract.get("verification_commands", []),
        },
        "verification_profile": profile,
        "playwright_recommended": profile == "browser_e2e" or ui.get("is_ui_work", False),
        "expected_output_schema": EVALUATOR_VERDICT_SCHEMA,
    }


def record_evaluator_verdict(
    track_id: str, slice_id: str, verdict: dict[str, Any],
) -> dict[str, Any]:
    """Record evaluator verdict and transition slice state."""
    td = track_dir(track_id)
    state = _load_json(td / "objective.state.json")
    graph = state["views"]["graph"]
    if slice_id not in graph["nodes"]:
        raise ValueError(f"Slice {slice_id} not found")
    node = graph["nodes"][slice_id]
    if node["state"] != "awaiting_verification":
        raise ValueError(f"Slice {slice_id} in state '{node['state']}', expected 'awaiting_verification'")

    _append_event(track_id, {
        "event": "evaluator_verdict",
        "slice_id": slice_id,
        "pass": verdict.get("pass", False),
        "criteria_results": verdict.get("criteria_results", []),
        "failure_details": verdict.get("failure_details", []),
        "verification_method": verdict.get("verification_method", "unknown"),
    })

    if verdict.get("pass", False):
        return update_node_state(
            track_id, slice_id, "accepted",
            evidence_refs=[f"evaluator_verdict:{now_iso()}"],
            acceptance_source="evaluator",
        )
    else:
        return update_node_state(
            track_id, slice_id, "rework",
            blockers=verdict.get("failure_details", ["Evaluator rejected"]),
        )


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_initial_graph(
    task: str,
    cwd: str,
    route: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Build the initial work graph with objective + plan + single slice."""
    objective_id = "objective-1"
    plan_id = "plan-1"
    slice_id = "slice-1"

    return {
        "nodes": {
            objective_id: {
                "id": objective_id,
                "kind": "objective",
                "title": task,
                "state": "ready",
                "dependencies": [],
                "owned_scope": [cwd],
            },
            plan_id: {
                "id": plan_id,
                "kind": "plan",
                "title": f"Plan for: {task[:80]}",
                "state": "ready",
                "dependencies": [objective_id],
                "owned_scope": [cwd],
            },
            slice_id: {
                "id": slice_id,
                "kind": "slice",
                "title": f"Execute: {task[:80]}",
                "description": task,
                "goal": task,
                "state": "ready",
                "dependencies": [plan_id],
                "owned_scope": [cwd],
                "blockers": [],
                "evidence_refs": [],
                "acceptance_source": None,
                "metrics": {"retry_count": 0, "escalation_count": 0},
                "planning_source": "initial_graph",
                "slice_contract": {
                    "objective": task,
                    "acceptance_criteria": [],
                    "verification_commands": [],
                },
            },
        },
        "root": objective_id,
        "edges": [
            {"from": plan_id, "to": objective_id, "type": "child_of"},
            {"from": slice_id, "to": plan_id, "type": "child_of"},
        ],
    }


# ---------------------------------------------------------------------------
# Frontier computation
# ---------------------------------------------------------------------------

def compute_frontier(
    graph: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Determine which slices are ready for dispatch."""
    nodes = graph.get("nodes", {})
    route_id = policy["hard_policy"]["route"]["route_id"]

    accepted_ids = {nid for nid, n in nodes.items() if n.get("state") == "accepted"}
    blocked_ids = {nid for nid, n in nodes.items() if n.get("state") in ("blocked", "rework")}

    ready_slices = []
    for nid, node in nodes.items():
        if node.get("kind") != "slice":
            continue
        if node.get("state") not in ("ready", "queued", "rework"):
            continue
        deps = node.get("dependencies", [])
        deps_met = all(
            nodes.get(d, {}).get("state") in ("accepted", "ready")
            or nodes.get(d, {}).get("kind") in ("objective", "plan")
            for d in deps
        )
        if not deps_met:
            continue

        # Score the slice
        score = 0.72
        if node.get("state") == "rework":
            score += 0.08
        retry_count = node.get("metrics", {}).get("retry_count", 0)
        score -= min(retry_count * 0.12, 0.36)
        blockers = node.get("blockers", [])
        score -= min(len(blockers) * 0.08, 0.24)
        if route_id in ("R3", "R4"):
            score += 0.06

        ready_slices.append({"slice_id": nid, "score": round(score, 3)})

    ready_slices.sort(key=lambda s: s["score"], reverse=True)
    next_slice_id = ready_slices[0]["slice_id"] if ready_slices else None

    # Dispatch mode
    if not ready_slices:
        dispatch_mode = "halt"
        stop_reason = "no_ready_slices"
    elif route_id in ("R1", "R2"):
        dispatch_mode = "inline_serial"
        stop_reason = None
    elif route_id == "R5":
        dispatch_mode = "clarify"
        stop_reason = "ambiguous_prompt"
    elif route_id == "R4":
        dispatch_mode = "governed_serial"
        stop_reason = None
    elif route_id == "R3":
        dispatch_mode = "governed_serial"
        stop_reason = None
    else:
        dispatch_mode = "inline_serial"
        stop_reason = None

    needs_replan = bool(blocked_ids) and not ready_slices

    return {
        "ready_slices": ready_slices,
        "next_slice_id": next_slice_id,
        "dispatch_mode": dispatch_mode,
        "confidence": round(ready_slices[0]["score"], 3) if ready_slices else 0.0,
        "needs_replan": needs_replan,
        "stop_reason": stop_reason,
        "why_now": f"{len(ready_slices)} slice(s) ready, route={route_id}",
    }


# ---------------------------------------------------------------------------
# Closure state machine
# ---------------------------------------------------------------------------

def build_closure(
    track_id: str,
    graph: dict[str, Any],
    policy: dict[str, Any],
    governance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate terminal conditions for the objective."""
    nodes = graph.get("nodes", {})
    route_id = policy["hard_policy"]["route"]["route_id"]

    slice_nodes = {nid: n for nid, n in nodes.items() if n.get("kind") == "slice"}
    total = len(slice_nodes)
    accepted = [nid for nid, n in slice_nodes.items() if n.get("state") == "accepted"]
    blocked = [nid for nid, n in slice_nodes.items() if n.get("state") in ("blocked",)]
    in_progress = [nid for nid, n in slice_nodes.items() if n.get("state") in ("in_progress", "ready", "queued", "rework")]

    # Check governance dispatch exhaustion
    dispatch_exhausted = False
    if governance:
        max_dispatch = DISPATCH_CYCLE_MAX_BY_ROUTE.get(route_id, 12)
        dispatch_exhausted = governance.get("dispatch_count", 0) >= max_dispatch

    # Determine closure state
    if total > 0 and len(accepted) == total:
        closure_state = "OBJECTIVE_COMPLETE"
        terminal = True
    elif total > 0 and len(accepted) > 0 and len(blocked) > 0 and not in_progress:
        closure_state = "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK"
        terminal = True
    elif dispatch_exhausted and blocked:
        closure_state = "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED"
        terminal = True
    elif dispatch_exhausted and not accepted:
        closure_state = "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED"
        terminal = True
    elif blocked and not in_progress and not accepted:
        closure_state = "OBJECTIVE_BLOCKED_MIGRATION_DEFECT"
        terminal = False
    else:
        closure_state = "ACTIVE"
        terminal = False

    closure_blockers = []
    for nid in blocked:
        n = slice_nodes[nid]
        closure_blockers.append({
            "slice_id": nid,
            "state": n["state"],
            "blockers": n.get("blockers", []),
        })

    return {
        "track_id": track_id,
        "terminal": terminal,
        "closure_state": closure_state,
        "route_effective": route_id,
        "accepted_slice_ids": accepted,
        "blocked_slice_ids": blocked,
        "in_progress_slice_ids": in_progress,
        "total_slices": total,
        "closure_blockers": closure_blockers,
        "governance_validated": governance is not None,
        # build_closure is the auto_runtime (cycle/refresh/replay) closure path;
        # the route_manifest postflight gate chain runs only under claude_run /
        # ralph_done_loop, never here. Surface that honestly so a reader does not
        # mistake governance_validated (= a governance object was attached) for
        # "the R3/R4 governed postflight gates ran and passed".
        "postflight_required": route_id in ("R3", "R4"),
        "postflight_executed": False,
        "dispatch_exhausted": dispatch_exhausted,
        "updated_at": now_iso(),
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def build_metrics(graph: dict[str, Any], governance: dict[str, Any] | None = None) -> dict[str, Any]:
    nodes = graph.get("nodes", {})
    slices = {nid: n for nid, n in nodes.items() if n.get("kind") == "slice"}
    by_state = {}
    for n in slices.values():
        st = n.get("state", "unknown")
        by_state[st] = by_state.get(st, 0) + 1

    retry_total = sum(n.get("metrics", {}).get("retry_count", 0) for n in slices.values())
    escalation_total = sum(n.get("metrics", {}).get("escalation_count", 0) for n in slices.values())

    return {
        "slice_count": len(slices),
        "slices_by_state": by_state,
        "accepted_slice_count": by_state.get("accepted", 0),
        "frontier_ready_count": by_state.get("ready", 0) + by_state.get("queued", 0) + by_state.get("rework", 0),
        "retry_total": retry_total,
        "escalation_total": escalation_total,
        "dispatch_count": governance.get("dispatch_count", 0) if governance else 0,
    }


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def build_summary(
    track_id: str,
    graph: dict[str, Any],
    frontier: dict[str, Any],
    closure: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "track_id": track_id,
        "closure_state": closure["closure_state"],
        "terminal": closure["terminal"],
        "total_slices": metrics["slice_count"],
        "accepted_slices": metrics["accepted_slice_count"],
        "frontier_ready": metrics["frontier_ready_count"],
        "dispatch_count": metrics["dispatch_count"],
        "dispatch_mode": frontier["dispatch_mode"],
        "next_slice_id": frontier["next_slice_id"],
        "updated_at": now_iso(),
    }


# ---------------------------------------------------------------------------
# Governance state
# ---------------------------------------------------------------------------

def build_governance_default(track_id: str, route: dict[str, Any]) -> dict[str, Any]:
    route_id = route["route_id"]
    return {
        "schema_version": GOVERNANCE_SCHEMA,
        "track_id": track_id,
        "status": "initialized",
        "dispatch_count": 0,
        "dispatch_cycle_max": DISPATCH_CYCLE_MAX_BY_ROUTE.get(route_id, 12),
        "dispatch_cycle_exhausted": False,
        "advisor_enabled": route_id in ("R3", "R4"),
        "advisor_uses": 0,
        "advisor_max_uses": ADVISOR_CHECKPOINT_MAX_BY_ROUTE.get(route_id, 0),
        "advisor_checkpoints": [],
        "slice_escalations": {},
        "memory_context_latest": {"status": "empty", "memory_refs": []},
        "updated_at": now_iso(),
    }


# ---------------------------------------------------------------------------
# Maintenance state
# ---------------------------------------------------------------------------

def build_maintenance_default(track_id: str) -> dict[str, Any]:
    return {
        "schema_version": MAINTENANCE_SCHEMA,
        "track_id": track_id,
        "cleanup_queue": [],
        "classifications": {},
        "updated_at": now_iso(),
    }


# ---------------------------------------------------------------------------
# Memory state
# ---------------------------------------------------------------------------

def build_memory_default(track_id: str) -> dict[str, Any]:
    return {
        "schema_version": MEMORY_SCHEMA,
        "track_id": track_id,
        "gates": {},
        "updated_at": now_iso(),
    }


# ---------------------------------------------------------------------------
# Memory lifecycle gates
# ---------------------------------------------------------------------------

def _memory_gate_coverage(track_id: str, graph: dict[str, Any]) -> dict[str, Any]:
    """Check if all required memory lifecycle gates have been written."""
    td = track_dir(track_id)
    memory = _load_json(td / "objective.memory.json")
    written_gates = set(memory.get("gates", {}).keys())

    required_gates = {"objective:init"}
    nodes = graph.get("nodes", {})
    for nid, node in nodes.items():
        if node.get("kind") == "slice" and node.get("state") in ("accepted", "blocked"):
            required_gates.add(f"slice:{nid}:{node['state']}")

    missing = required_gates - written_gates
    return {
        "complete": not bool(missing),
        "missing_gate_ids": sorted(missing),
        "written_gate_ids": sorted(written_gates),
    }


def _record_memory_lifecycle_gate(
    track_id: str,
    gate_id: str,
    gate: str,
    title: str,
    text: str,
    cwd: str,
) -> dict[str, Any]:
    """Record a memory lifecycle gate via omni-mem CLI or mark as skipped."""
    td = track_dir(track_id)
    memory = _load_json(td / "objective.memory.json") or build_memory_default(track_id)

    # Check idempotency
    if gate_id in memory.get("gates", {}):
        return {"status": "already_written", "gate_id": gate_id}

    # Try omni-mem CLI
    idempotency_key = f"{track_id}:{gate_id}"
    status = "written"
    try:
        result = subprocess.run(
            [
                # Vault routed by cwd: ~/chad_personal -> omni-mem-personal, else omni-mem.
                "docker", "exec", "-i", container_for_cwd(cwd), "omni-mem",
                "save-memory",
                "--title", title,
                "--text", text,
                "--workspaceId", cwd,
                "--taskId", track_id,
                "--idempotencyKey", idempotency_key,
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            status = "failed"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        status = "skipped"

    memory.setdefault("gates", {})[gate_id] = {
        "status": status,
        "gate": gate,
        "idempotency_key": idempotency_key,
        "written_at": now_iso(),
    }
    memory["updated_at"] = now_iso()
    _save_json(td / "objective.memory.json", memory)

    event_type = {
        "written": "memory_lifecycle_written",
        "failed": "memory_lifecycle_failed",
        "skipped": "memory_lifecycle_skipped",
    }[status]
    _append_event(track_id, {"event": event_type, "gate_id": gate_id, "gate": gate, "status": status})

    return {"status": status, "gate_id": gate_id}


# ---------------------------------------------------------------------------
# Scope validation
# ---------------------------------------------------------------------------

def _scope_mismatches(node: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    """Check if node's owned_scope is within policy's allowed_scope."""
    owned = node.get("owned_scope", [])
    allowed = policy.get("hard_policy", {}).get("allowed_scope", [])
    cwd = policy.get("hard_policy", {}).get("cwd", "")

    if not owned:
        return ["missing_owned_scope"]

    mismatches = []
    for scope in owned:
        scope_real = os.path.realpath(scope)
        in_allowed = any(scope_real.startswith(os.path.realpath(a)) for a in allowed)
        in_cwd = scope_real.startswith(os.path.realpath(cwd)) if cwd else False
        if not in_allowed and not in_cwd:
            mismatches.append(f"{scope}:outside_allowed_scope")
    return mismatches


# ---------------------------------------------------------------------------
# Route promotion
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Effort escalation on retry
#
# When a slice enters rework (retry), bump its suggested effort one tier up
# so the next dispatch uses more thinking budget than the previous attempt.
# Independent of route promotion (which changes R2→R3→R4 only after multiple
# retries exceed budget). Effort escalation fires on every rework, giving the
# dispatcher a hint to spend more on the harder-than-expected slice.
#
# Consumers (e.g. /govern, /drive dispatch paths) should call
# get_effective_effort(track_id, slice_id, base_effort) to resolve the
# effort to use when invoking the next agent for that slice.
# ---------------------------------------------------------------------------

EFFORT_LADDER = ["low", "medium", "high", "xhigh"]


def _next_effort_tier(current: str) -> str | None:
    """Return the next effort tier up, or None if already at the ceiling."""
    try:
        idx = EFFORT_LADDER.index(current)
    except ValueError:
        return None
    if idx + 1 >= len(EFFORT_LADDER):
        return None
    return EFFORT_LADDER[idx + 1]


def get_effective_effort(track_id: str, slice_id: str, base_effort: str) -> str:
    """Resolve the effort a dispatcher should use for this slice.

    Reads governance.slice_escalations to find any recorded escalation for
    this slice; returns the escalated effort if present, else base_effort.
    Safe to call for a slice with no escalation history — returns base_effort
    unchanged.
    """
    td = track_dir(track_id)
    try:
        state = _load_json(td / "objective.state.json")
        governance = state["views"].get("governance", {})
        escalations = governance.get("slice_escalations", {}) or {}
        rec = escalations.get(slice_id)
        if rec and rec.get("suggested_effort"):
            return rec["suggested_effort"]
    except (OSError, KeyError):
        pass
    return base_effort


def _apply_route_promotion(
    graph: dict[str, Any],
    policy: dict[str, Any],
    governance: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Promote route if repeated failures suggest escalation needed."""
    nodes = graph.get("nodes", {})
    slices = {nid: n for nid, n in nodes.items() if n.get("kind") == "slice"}

    retry_counts = [n.get("metrics", {}).get("retry_count", 0) for n in slices.values()]
    max_retries = max(retry_counts) if retry_counts else 0

    if max_retries < SAME_STRATEGY_RETRY_BUDGET:
        return policy, None

    current_route = policy["hard_policy"]["route"]["route_id"]
    promotion_map = {"R1": "R2", "R2": "R3", "R3": "R4"}
    new_route_id = promotion_map.get(current_route)

    if not new_route_id:
        return policy, None

    new_rule = get_route_rule(new_route_id)
    new_route = _build_route_result(new_rule)
    policy["hard_policy"]["route"] = new_route
    policy["route_policy"]["dispatch_cycle_max"] = DISPATCH_CYCLE_MAX_BY_ROUTE.get(new_route_id, 12)

    event = {
        "event": "route_promoted",
        "from_route": current_route,
        "to_route": new_route_id,
        "reason": f"max_retries={max_retries} exceeded budget={SAME_STRATEGY_RETRY_BUDGET}",
    }

    return policy, event


# ---------------------------------------------------------------------------
# Anticipation
# ---------------------------------------------------------------------------

def _build_anticipation(
    *,
    track_id: str,
    graph: dict[str, Any],
    policy: dict[str, Any],
    governance: dict[str, Any],
    maintenance: dict[str, Any],
) -> dict[str, Any]:
    """Determine the recommended next action for the track."""
    frontier = compute_frontier(graph, policy)
    memory_coverage = _memory_gate_coverage(track_id, graph)
    closure = build_closure(track_id, graph, policy, governance)
    route_id = policy["hard_policy"]["route"]["route_id"]

    risks = []

    # Check terminal
    if closure["terminal"] and closure["closure_state"] in OBJECTIVE_SUCCESS_STATES:
        if not memory_coverage["complete"]:
            return {
                "recommended_action": "repair_bookkeeping",
                "reason": f"Terminal success but memory gates missing: {memory_coverage['missing_gate_ids']}",
                "risks": ["persist_memory_before_closure"],
                "memory_gate_coverage": memory_coverage,
                "frontier": frontier,
                "closure": closure,
            }
        return {
            "recommended_action": "close",
            "reason": f"Objective complete: {closure['closure_state']}",
            "risks": [],
            "memory_gate_coverage": memory_coverage,
            "frontier": frontier,
            "closure": closure,
        }

    if closure["terminal"] and closure["closure_state"] in OBJECTIVE_BLOCKED_STATES:
        return {
            "recommended_action": "close",
            "reason": f"Objective blocked: {closure['closure_state']}",
            "risks": ["escalation_required"],
            "memory_gate_coverage": memory_coverage,
            "frontier": frontier,
            "closure": closure,
        }

    # Check for slices awaiting evaluation (evaluator feedback loop)
    nodes = graph.get("nodes", {})
    for nid, node in nodes.items():
        if node.get("kind") == "slice" and _should_evaluate(node, policy):
            eval_dispatch = build_evaluator_dispatch(track_id, nid, node, policy)
            return {
                "recommended_action": "evaluate",
                "reason": f"Slice {nid} is awaiting verification — evaluator pass needed",
                "risks": risks,
                "memory_gate_coverage": memory_coverage,
                "frontier": frontier,
                "closure": closure,
                "evaluator_dispatch": eval_dispatch,
            }

    # Non-terminal: check conditions
    if not memory_coverage["complete"]:
        risks.append("persist_memory_before_dispatch")

    cleanup_queue = maintenance.get("cleanup_queue", [])
    if cleanup_queue:
        risks.append("cleanup_pending")

    if not frontier["ready_slices"] and frontier["needs_replan"]:
        return {
            "recommended_action": "replan",
            "reason": "No ready slices and blocked/rework nodes exist",
            "risks": risks + ["replan_or_cleanup_needed"],
            "memory_gate_coverage": memory_coverage,
            "frontier": frontier,
            "closure": closure,
        }

    if frontier["dispatch_mode"] in ("halt", "clarify"):
        return {
            "recommended_action": "halt_for_authority",
            "reason": f"Dispatch halted: {frontier.get('stop_reason', 'unknown')}",
            "risks": risks,
            "memory_gate_coverage": memory_coverage,
            "frontier": frontier,
            "closure": closure,
        }

    # Default: dispatch
    return {
        "recommended_action": "dispatch",
        "reason": f"{len(frontier['ready_slices'])} slice(s) ready for dispatch",
        "risks": risks,
        "memory_gate_coverage": memory_coverage,
        "frontier": frontier,
        "closure": closure,
    }


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def reconcile_track(track_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Reconcile maintenance state: cleanup stale nodes, persist memory."""
    td = track_dir(track_id)
    state = _load_json(td / "objective.state.json")
    graph = state.get("views", {}).get("graph", {})
    policy = state.get("views", {}).get("policy", {})
    governance = state.get("views", {}).get("governance", {})
    maintenance = state.get("views", {}).get("maintenance", build_maintenance_default(track_id))

    actions = []
    changed = False

    # Check memory gates
    mem_coverage = _memory_gate_coverage(track_id, graph)
    if not mem_coverage["complete"]:
        cwd = policy.get("hard_policy", {}).get("cwd", str(Path.home()))
        for gate_id in mem_coverage["missing_gate_ids"]:
            if dry_run:
                actions.append({"action": "persist_memory_gate", "gate_id": gate_id, "dry_run": True})
            else:
                result = _record_memory_lifecycle_gate(
                    track_id, gate_id, gate_id.split(":")[-1],
                    f"Memory gate: {gate_id}", f"Track {track_id} gate {gate_id}",
                    cwd,
                )
                actions.append({"action": "persist_memory_gate", "gate_id": gate_id, "result": result})
                changed = True

    if not dry_run and changed:
        _append_event(track_id, {"event": "maintenance_reconciled", "actions": actions})

    maintenance["updated_at"] = now_iso()

    return {
        "track_id": track_id,
        "dry_run": dry_run,
        "changed": changed,
        "actions": actions,
        "maintenance": maintenance,
    }


# ---------------------------------------------------------------------------
# Materialized views
# ---------------------------------------------------------------------------

def rebuild_materialized_views(track_id: str) -> dict[str, Any]:
    """Rebuild all materialized views from current state."""
    td = track_dir(track_id)
    state = _load_json(td / "objective.state.json")

    graph = state.get("views", {}).get("graph", {})
    policy = state.get("views", {}).get("policy", {})
    governance = state.get("views", {}).get("governance", {})

    frontier = compute_frontier(graph, policy)
    closure = build_closure(track_id, graph, policy, governance)
    metrics = build_metrics(graph, governance)
    summary = build_summary(track_id, graph, frontier, closure, metrics)

    views = {
        "graph": graph,
        "frontier": frontier,
        "policy": policy,
        "metrics": metrics,
        "summary": summary,
        "closure": closure,
        "governance": governance,
        "maintenance": state.get("views", {}).get("maintenance", build_maintenance_default(track_id)),
    }

    state["views"] = views
    state["updated_at"] = now_iso()
    _save_json(td / "objective.state.json", state)

    # Write individual view files
    for name, data in views.items():
        _save_json(td / f"objective.{name}.json", data)

    # Build human-readable progress file
    build_progress_file(track_id)

    return views


# ---------------------------------------------------------------------------
# Progress file
# ---------------------------------------------------------------------------

def _summarize_event(event: dict[str, Any]) -> str:
    """One-line human-readable event summary."""
    etype = event.get("event", "unknown")
    mapping = {
        "objective_initialized": lambda e: f"Initialized (route {e.get('route_id', '?')})",
        "node_state_updated": lambda e: f"{e.get('node_id','?')}: {e.get('old_state','?')} -> {e.get('new_state','?')}",
        "inline_dispatched": lambda e: f"Dispatched {e.get('slice_id','?')} (#{e.get('dispatch_count',0)})",
        "governed_dispatched": lambda e: f"Governed dispatch {e.get('slice_id','?')}",
        "dispatch_blocked": lambda e: f"Dispatch blocked: {e.get('reason','?')}",
        "route_promoted": lambda e: f"Route promoted: {e.get('from_route','?')} -> {e.get('to_route','?')}",
        "cycle_completed": lambda e: f"Cycle {e.get('cycle',0)}: {e.get('recommended_action','?')} ({e.get('action_status','?')})",
        "git_checkpoint_recorded": lambda e: f"Git checkpoint: {e.get('commit_sha','?')[:8]}",
        "evaluator_verdict": lambda e: f"Evaluator: {'PASS' if e.get('pass') else 'FAIL'} on {e.get('slice_id','?')}",
        "memory_lifecycle_written": lambda e: f"Memory gate: {e.get('gate_id','?')}",
        "frontier_refreshed": lambda e: f"Frontier refreshed: {e.get('frontier_ready',0)} ready",
        "wake_ceremony_completed": lambda e: f"Wake: {e.get('closure_state','?')}",
    }
    fn = mapping.get(etype, lambda e: f"{etype}")
    return fn(event)


def build_progress_file(track_id: str) -> str:
    """Build human-readable objective.progress.md."""
    td = track_dir(track_id)
    state = _load_json(td / "objective.state.json")
    if not state:
        return ""
    task = state.get("task", "Unknown")
    cwd = state.get("cwd", "")
    views = state.get("views", {})
    graph = views.get("graph", {})
    policy = views.get("policy", {})
    frontier = views.get("frontier", {})
    closure = views.get("closure", {})
    governance = views.get("governance", {})
    metrics = views.get("metrics", {})
    route_id = policy.get("hard_policy", {}).get("route", {}).get("route_id", "?")

    lines = [
        f"# Objective Progress: {track_id}",
        f"",
        f"> **Task:** {task}",
        f"> **Route:** {route_id}  |  **State:** {closure.get('closure_state', 'UNKNOWN')}",
        f"> **Dispatches:** {governance.get('dispatch_count', 0)}/{governance.get('dispatch_cycle_max', '?')}  |  **Updated:** {state.get('updated_at', '')}",
        f"",
        f"## Slice Status",
        f"| ID | Title | State | Evidence | Criteria |",
        f"|----|-------|-------|----------|----------|",
    ]
    for nid, node in graph.get("nodes", {}).items():
        if node.get("kind") != "slice":
            continue
        title = node.get("title", "")[:50]
        st = node.get("state", "?")
        ev_count = len(node.get("evidence_refs", []))
        cr_count = len(node.get("slice_contract", {}).get("acceptance_criteria", []))
        lines.append(f"| {nid} | {title} | {st} | {ev_count} | {cr_count} |")

    # Blockers
    blockers = []
    for nid, node in graph.get("nodes", {}).items():
        if node.get("blockers"):
            blockers.append((nid, node.get("state", "?"), node["blockers"]))
    lines.append(f"")
    lines.append(f"## Blockers")
    if blockers:
        for nid, st, blist in blockers:
            lines.append(f"- **{nid}** ({st}): {', '.join(str(b) for b in blist[:5])}")
    else:
        lines.append("No active blockers.")

    # Recent events
    events = _read_events(track_id)
    recent = events[-5:] if events else []
    lines.append(f"")
    lines.append(f"## Recent Events (last {len(recent)})")
    lines.append(f"| # | Summary |")
    lines.append(f"|---|---------|")
    for i, ev in enumerate(reversed(recent), 1):
        lines.append(f"| {i} | {_summarize_event(ev)} |")

    lines.append(f"")
    lines.append(f"---")
    lines.append(f"_Auto-generated by auto_runtime. Do not edit._")

    content = "\n".join(lines) + "\n"
    (td / "objective.progress.md").write_text(content)
    return content


# ---------------------------------------------------------------------------
# Wake ceremony
# ---------------------------------------------------------------------------

def _wake_sanity_checks(cwd: str, graph: dict[str, Any]) -> dict[str, Any]:
    """Run lightweight sanity checks on the working directory."""
    checks = {}
    checks["cwd_exists"] = os.path.isdir(cwd) if cwd else False
    if cwd and checks["cwd_exists"]:
        checks["git_repo"] = _is_git_repo(cwd)
    else:
        checks["git_repo"] = False
    missing_scopes = []
    for nid, node in graph.get("nodes", {}).items():
        for scope in node.get("owned_scope", []):
            if not os.path.exists(scope):
                missing_scopes.append(scope)
    checks["owned_scopes_present"] = not bool(missing_scopes)
    if missing_scopes:
        checks["missing_scopes"] = missing_scopes[:10]
    checks["all_ok"] = all(v for k, v in checks.items() if k != "missing_scopes")
    return checks


def wake_ceremony(
    track_id: str,
    *,
    last_n_events: int = 20,
    write_progress: bool = False,
) -> dict[str, Any]:
    """Reconstruct context after session boundary. Produces a structured briefing."""
    td = track_dir(track_id)
    if not (td / "objective.state.json").exists():
        return {"status": "error", "error": f"Track {track_id} not found"}

    events = _read_events(track_id)
    if not events:
        return {"status": "error", "error": f"Track {track_id} has no events"}

    views = rebuild_materialized_views(track_id)
    state = _load_json(td / "objective.state.json")
    graph = views["graph"]
    frontier = views["frontier"]
    closure = views["closure"]
    policy = views["policy"]
    governance = views["governance"]
    metrics = views["metrics"]
    cwd = state.get("cwd") or policy.get("hard_policy", {}).get("cwd", "")

    last_sha = _last_checkpoint_sha(events)
    git = _git_context(cwd, since_sha=last_sha) if cwd else {"available": False}
    recent_events = events[-last_n_events:]

    blocked_slices = []
    for nid, node in graph.get("nodes", {}).items():
        if node.get("kind") == "slice" and node.get("state") in ("blocked", "rework"):
            blocked_slices.append({
                "slice_id": nid, "state": node["state"],
                "title": node.get("title", ""),
                "blockers": node.get("blockers", []),
                "retry_count": node.get("metrics", {}).get("retry_count", 0),
            })

    sanity = _wake_sanity_checks(cwd, graph)
    mem_coverage = _memory_gate_coverage(track_id, graph)

    briefing = {
        "status": "ok",
        "track_id": track_id,
        "task": state.get("task", ""),
        "cwd": cwd,
        "woke_at": now_iso(),
        "closure_state": closure["closure_state"],
        "terminal": closure["terminal"],
        "route_id": policy.get("hard_policy", {}).get("route", {}).get("route_id", ""),
        "frontier": {
            "dispatch_mode": frontier["dispatch_mode"],
            "next_slice_id": frontier["next_slice_id"],
            "ready_count": len(frontier.get("ready_slices", [])),
        },
        "metrics": {
            "total_slices": metrics.get("slice_count", 0),
            "accepted": metrics.get("accepted_slice_count", 0),
            "dispatch_count": metrics.get("dispatch_count", 0),
        },
        "governance": {
            "dispatch_count": governance.get("dispatch_count", 0),
            "dispatch_cycle_max": governance.get("dispatch_cycle_max", 0),
            "dispatch_exhausted": governance.get("dispatch_cycle_exhausted", False),
        },
        "blocked_slices": blocked_slices,
        "total_events": len(events),
        "recent_events": recent_events,
        "git": git,
        "last_checkpoint_sha": last_sha,
        "sanity": sanity,
        "memory_coverage": mem_coverage,
    }

    _append_event(track_id, {
        "event": "wake_ceremony_completed",
        "closure_state": closure["closure_state"],
        "frontier_ready": len(frontier.get("ready_slices", [])),
        "git_dirty": git.get("dirty") if git.get("available") else None,
        "sanity_ok": sanity.get("all_ok", False),
    })

    if write_progress:
        build_progress_file(track_id)

    return briefing


# ---------------------------------------------------------------------------
# Refresh frontier
# ---------------------------------------------------------------------------

def refresh_frontier(track_id: str) -> dict[str, Any]:
    """Recompute frontier, apply route promotion, rebuild views."""
    td = track_dir(track_id)
    state = _load_json(td / "objective.state.json")
    graph = state.get("views", {}).get("graph", {})
    policy = state.get("views", {}).get("policy", {})
    governance = state.get("views", {}).get("governance", {})

    # Route promotion check
    policy, promotion_event = _apply_route_promotion(graph, policy, governance)
    if promotion_event:
        _append_event(track_id, promotion_event)
        state["views"]["policy"] = policy

    views = rebuild_materialized_views(track_id)
    _append_event(track_id, {
        "event": "frontier_refreshed",
        "frontier_ready": len(views["frontier"]["ready_slices"]),
        "dispatch_mode": views["frontier"]["dispatch_mode"],
    })

    return {
        "track_id": track_id,
        "frontier": views["frontier"],
        "summary": views["summary"],
        "metrics": views["metrics"],
        "closure": views["closure"],
        "governance": views["governance"],
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def dispatch_track(track_id: str) -> dict[str, Any]:
    """Dispatch the next ready slice for execution."""
    td = track_dir(track_id)
    state = _load_json(td / "objective.state.json")
    graph = state["views"]["graph"]
    policy = state["views"]["policy"]
    governance = state["views"]["governance"]
    frontier = state["views"]["frontier"]

    route_id = policy["hard_policy"]["route"]["route_id"]
    dispatch_mode = frontier["dispatch_mode"]

    # Check dispatch budget
    dispatch_max = governance.get("dispatch_cycle_max", DISPATCH_CYCLE_MAX_BY_ROUTE.get(route_id, 12))
    if governance["dispatch_count"] >= dispatch_max:
        governance["dispatch_cycle_exhausted"] = True
        state["views"]["governance"] = governance
        _save_json(td / "objective.state.json", state)
        _append_event(track_id, {
            "event": "dispatch_blocked",
            "reason": "dispatch_cycle_cap_exceeded",
            "count": governance["dispatch_count"],
            "max": dispatch_max,
        })
        return {
            "track_id": track_id,
            "status": "blocked",
            "reason": "dispatch_cycle_cap_exceeded",
            "dispatch_mode": dispatch_mode,
        }

    if not frontier["next_slice_id"]:
        return {
            "track_id": track_id,
            "status": "not_dispatchable",
            "reason": "no_ready_slices",
            "dispatch_mode": dispatch_mode,
        }

    slice_id = frontier["next_slice_id"]
    node = graph["nodes"][slice_id]

    # Scope validation
    mismatches = _scope_mismatches(node, policy)
    if mismatches:
        node["state"] = "blocked"
        node["blockers"] = node.get("blockers", []) + [f"scope_mismatch:{m}" for m in mismatches]
        state["views"]["graph"] = graph
        _save_json(td / "objective.state.json", state)
        _append_event(track_id, {
            "event": "dispatch_blocked",
            "reason": "scope_mismatch",
            "slice_id": slice_id,
            "mismatches": mismatches,
        })
        return {
            "track_id": track_id,
            "status": "blocked",
            "reason": "scope_mismatch",
            "slice_id": slice_id,
            "mismatches": mismatches,
        }

    # Sprint contract validation
    contract_check = validate_sprint_contract(node, route_id)
    if not contract_check["valid"]:
        node["state"] = "blocked"
        node["blockers"] = node.get("blockers", []) + [f"contract:{r}" for r in contract_check["missing"]]
        state["views"]["graph"] = graph
        _save_json(td / "objective.state.json", state)
        _append_event(track_id, {
            "event": "dispatch_blocked",
            "reason": "missing_acceptance_criteria",
            "slice_id": slice_id,
            "contract_validation": contract_check,
        })
        return {
            "track_id": track_id,
            "status": "blocked",
            "reason": "missing_acceptance_criteria",
            "slice_id": slice_id,
            "contract_validation": contract_check,
        }

    # Reviewer-ack gate (S7): R3/R4 execution may not start un-acked. The
    # node is NOT marked blocked — the gate is track-level and recoverable;
    # recording the ack makes the very next dispatch succeed.
    ack_check = validate_reviewer_ack(state, route_id)
    if not ack_check["valid"]:
        _append_event(track_id, {
            "event": "dispatch_blocked",
            "reason": "missing_reviewer_ack",
            "slice_id": slice_id,
            "route_id": route_id,
        })
        return {
            "track_id": track_id,
            "status": "blocked",
            "reason": "missing_reviewer_ack",
            "slice_id": slice_id,
            "unblock": (
                "reviewer must ack the sprint contract, then record it: "
                f"python3 ~/.claude/bin/auto_runtime.py record-ack --track-id {track_id} "
                "--by reviewer --ref '<what was acked>'"
            ),
        }

    # Slice 1c: pre-dispatch baseline capture (no enforcement; Slice 3 acts).
    # Wrapped in a try so a baseline failure never blocks dispatch.
    try:
        cwd = policy.get("hard_policy", {}).get("cwd", "")
        if cwd:
            capture_baselines(
                track_id,
                cwd=cwd,
                route=route_id,
                owned_files=node.get("owned_scope", []),
            )
    except Exception as e:  # noqa: BLE001 — defensive; baseline must not block dispatch
        _append_event(track_id, {
            "event": "baseline_unavailable",
            "command_id": None,
            "reason": "capture_exception",
            "error": f"{type(e).__name__}: {e}",
        })

    # Dispatch inline (R1/R2) or mark for governed dispatch (R3/R4)
    node["state"] = "in_progress"
    governance["dispatch_count"] += 1
    governance["status"] = "inline_dispatched" if route_id in ("R1", "R2") else "governed_dispatched"
    governance["updated_at"] = now_iso()

    state["views"]["graph"] = graph
    state["views"]["governance"] = governance
    _save_json(td / "objective.state.json", state)
    rebuild_materialized_views(track_id)

    event_type = "inline_dispatched" if route_id in ("R1", "R2") else "governed_dispatched"
    _append_event(track_id, {
        "event": event_type,
        "slice_id": slice_id,
        "dispatch_count": governance["dispatch_count"],
        "route_id": route_id,
    })

    result = {
        "track_id": track_id,
        "status": "dispatched",
        "dispatch_mode": dispatch_mode,
        "slice_id": slice_id,
        "slice_contract": node.get("slice_contract", {}),
        "governance": governance,
        # Slice 4: heterogeneous fleet — surface worker_runtime so the caller
        # picks the right invocation path (claude direct / goose_dispatch.py /
        # anthropic-concurrency-system).
        "worker_runtime": policy.get("route_policy", {}).get("worker_runtime", DEFAULT_WORKER_RUNTIME),
    }
    if result["worker_runtime"] != DEFAULT_WORKER_RUNTIME:
        result["worker_runtime_invocation"] = _worker_runtime_invocation(result["worker_runtime"])
    verification_hints = _build_verification_hints(policy, node)
    if verification_hints:
        result["verification_hints"] = verification_hints
    return result


def _worker_runtime_invocation(runtime: str) -> dict[str, Any]:
    """Return the canonical invocation hint for a non-default worker_runtime."""
    if runtime == "goose":
        return {
            "runtime": "goose",
            "binary": "/Users/chadsimon/.claude/bin/goose_dispatch.py",
            "via": "goose ACP plugin → Codex/Claude Pro-Max subscription",
            "cost_model": "subscription (no per-token billing)",
            "throttle": "rate-limit-aware (5h Pro/Max windows)",
        }
    if runtime == "opencode":
        return {
            "runtime": "opencode",
            "binary": "opencode",
            "via": "anthropic-concurrency-system runner",
            "cost_model": "subscription (OpenCode Pro/Max)",
            "throttle": "concurrency-tuned (98+ parallel sessions max)",
        }
    return {"runtime": runtime}


# ---------------------------------------------------------------------------
# Node state updates
# ---------------------------------------------------------------------------

def add_slice_node(
    track_id: str,
    title: str,
    *,
    node_id: str | None = None,
    description: str | None = None,
    dependencies: list[str] | None = None,
) -> dict[str, Any]:
    """Add a slice node to an existing track's graph.

    Why: init builds a single auto slice ("slice-1"), so multi-slice plans
    had nowhere to attach per-slice evidence — everything landed in one blob
    (observed on track db6a3338d2ab, 2026-07-04). This lets the operator
    grow the graph to match the plan's real slice decomposition.

    Node id defaults to the next free "slice-N". Dependencies default to the
    plan node when present (mirrors build_initial_graph's shape)."""
    td = track_dir(track_id)
    state = _load_json(td / "objective.state.json")
    graph = state["views"]["graph"]
    nodes = graph["nodes"]

    if node_id is None:
        # Next free slice-N, scanning existing ids so we never collide.
        ns = [int(m.group(1)) for nid in nodes
              if (m := re.match(r"slice-(\d+)$", nid))]
        node_id = f"slice-{(max(ns) + 1) if ns else 1}"
    if node_id in nodes:
        raise ValueError(f"Node {node_id} already exists in graph")

    if dependencies is None:
        dependencies = ["plan-1"] if "plan-1" in nodes else [graph.get("root", "objective-1")]
    missing = [d for d in dependencies if d not in nodes]
    if missing:
        raise ValueError(f"Unknown dependency node(s): {missing}")

    owned_scope = nodes.get(graph.get("root", ""), {}).get("owned_scope", [state.get("cwd", "")])
    nodes[node_id] = {
        "id": node_id,
        "kind": "slice",
        "title": title[:120],
        "description": description or title,
        "goal": description or title,
        "state": "ready",
        "dependencies": dependencies,
        "owned_scope": owned_scope,
        "blockers": [],
        "evidence_refs": [],
        "acceptance_source": None,
        "metrics": {"retry_count": 0, "escalation_count": 0},
        "planning_source": "operator_add_node",
        "slice_contract": {
            "objective": title,
            "acceptance_criteria": [],
            "verification_commands": [],
        },
    }
    graph.setdefault("edges", []).extend(
        {"from": node_id, "to": dep, "type": "child_of"} for dep in dependencies
    )

    state["views"]["graph"] = graph
    _save_json(td / "objective.state.json", state)
    rebuild_materialized_views(track_id)
    _append_event(track_id, {
        "event": "node_added",
        "node_id": node_id,
        "title": title[:120],
        "dependencies": dependencies,
    })
    return {"node_id": node_id, "state": "ready", "dependencies": dependencies}


def update_node_state(
    track_id: str,
    node_id: str,
    new_state: str,
    *,
    evidence_refs: list[str] | None = None,
    acceptance_source: str | None = None,
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    """Update a graph node's state with proper tracking."""
    if new_state not in GRAPH_NODE_STATES:
        raise ValueError(f"Invalid state: {new_state}. Must be one of {GRAPH_NODE_STATES}")

    td = track_dir(track_id)
    state = _load_json(td / "objective.state.json")
    graph = state["views"]["graph"]

    if node_id not in graph["nodes"]:
        raise ValueError(
            f"Node {node_id} not found in graph. "
            f"Valid node ids: {sorted(graph['nodes'])}"
        )

    node = graph["nodes"][node_id]
    old_state = node["state"]

    # Evidence enforcement: block acceptance without evidence
    if new_state == "accepted":
        effective_evidence = list(node.get("evidence_refs", []))
        if evidence_refs:
            effective_evidence.extend(evidence_refs)
        if not effective_evidence:
            _append_event(track_id, {
                "event": "transition_rejected",
                "node_id": node_id,
                "attempted_state": "accepted",
                "reverted_to": old_state,
                "reason": "missing_evidence",
            })
            return {"node_id": node_id, "old_state": old_state, "new_state": old_state, "rejected": True, "reason": "missing_evidence"}

        # ORBIT pattern: if the slice declares an acceptance_check command,
        # require a verify:*:exit=0 token in evidence. Any declared check must
        # have been executed with exit code 0. Slices without an acceptance_check
        # fall back to the presence-only gate above (backward-compatible).
        acceptance_check = node.get("slice_contract", {}).get("acceptance_check")
        if acceptance_check:
            passed = any(
                isinstance(e, str) and e.startswith("verify:") and e.endswith(":exit=0")
                for e in effective_evidence
            )
            if not passed:
                _append_event(track_id, {
                    "event": "transition_rejected",
                    "node_id": node_id,
                    "attempted_state": "accepted",
                    "reverted_to": old_state,
                    "reason": "acceptance_check_unverified",
                    "acceptance_check": acceptance_check,
                })
                return {
                    "node_id": node_id,
                    "old_state": old_state,
                    "new_state": old_state,
                    "rejected": True,
                    "reason": "acceptance_check_unverified",
                    "acceptance_check": acceptance_check,
                }

    node["state"] = new_state

    if evidence_refs:
        node.setdefault("evidence_refs", []).extend(evidence_refs)
    if acceptance_source:
        node["acceptance_source"] = acceptance_source
    if blockers is not None:
        node["blockers"] = blockers

    # Track retries + record effort escalation for dispatch consumers.
    # On rework, bump retry_count AND compute the next-tier effort suggestion.
    # slice_escalations is read by get_effective_effort() during dispatch;
    # a retried slice gets more thinking budget than its previous attempt.
    effort_escalation_event: dict[str, Any] | None = None
    if new_state == "rework":
        metrics = node.setdefault("metrics", {})
        metrics["retry_count"] = metrics.get("retry_count", 0) + 1

        if node.get("kind") == "slice":
            route = state["views"].get("policy", {}).get("hard_policy", {}).get("route", {})
            profile_overrides = route.get("profile_overrides", {})
            # Default role is "worker" — most slices are worker-dispatched.
            # Reviewer/planner slices can still use this escalation; the
            # caller picks the base_effort when invoking get_effective_effort.
            worker_profile = profile_overrides.get("worker", {})
            current_effort = (
                metrics.get("suggested_effort")
                or worker_profile.get("effort")
                or "medium"
            )
            next_tier = _next_effort_tier(current_effort)
            if next_tier:
                metrics["suggested_effort"] = next_tier
                governance = state["views"].setdefault("governance", {})
                escalations = governance.setdefault("slice_escalations", {})
                escalations[node_id] = {
                    "retry_count": metrics["retry_count"],
                    "suggested_effort": next_tier,
                    "previous_effort": current_effort,
                    "escalated_at": now_iso(),
                }
                effort_escalation_event = {
                    "event": "effort_escalated",
                    "node_id": node_id,
                    "from_effort": current_effort,
                    "to_effort": next_tier,
                    "retry_count": metrics["retry_count"],
                }

    state["views"]["graph"] = graph
    _save_json(td / "objective.state.json", state)
    rebuild_materialized_views(track_id)

    if effort_escalation_event is not None:
        _append_event(track_id, effort_escalation_event)

    _append_event(track_id, {
        "event": "node_state_updated",
        "node_id": node_id,
        "old_state": old_state,
        "new_state": new_state,
        "evidence_refs": evidence_refs or [],
        "acceptance_source": acceptance_source,
    })

    # Git checkpoint on slice acceptance
    checkpoint_result = None
    if new_state == "accepted" and node.get("kind") == "slice":
        cwd = state.get("cwd") or state.get("views", {}).get("policy", {}).get("hard_policy", {}).get("cwd", "")
        if cwd:
            checkpoint_result = _git_checkpoint_on_acceptance(track_id, node_id, node, cwd)
            if checkpoint_result.get("status") == "committed":
                sha = checkpoint_result["commit_sha"]
                node.setdefault("evidence_refs", []).append(f"git:{sha}")
                state["views"]["graph"] = graph
                _save_json(td / "objective.state.json", state)
                _append_event(track_id, {
                    "event": "git_checkpoint_recorded",
                    "node_id": node_id,
                    "commit_sha": sha,
                    "commit_message": checkpoint_result.get("commit_message", ""),
                    "branch": checkpoint_result.get("branch", ""),
                })
            else:
                _append_event(track_id, {
                    "event": "git_checkpoint_skipped",
                    "node_id": node_id,
                    "status": checkpoint_result.get("status", "unknown"),
                    "reason": checkpoint_result.get("reason", ""),
                })

    # omni-mem checkpoint on slice boundary: deterministic boundary detection
    # (accepted slice -> slice_complete, blocked slice -> escalation) feeds the
    # checkpoint layer's rollup. The git checkpoint commits code; this rolls up the
    # captain's raw observation thread. Opt-in via OMNI_MEM_CHECKPOINTS_ENABLED.
    omni_checkpoint = None
    if node.get("kind") == "slice" and new_state in ("accepted", "blocked"):
        boundary = "slice_complete" if new_state == "accepted" else "escalation"
        cp_cwd = state.get("cwd") or state.get("views", {}).get("policy", {}).get("hard_policy", {}).get("cwd", "")
        omni_checkpoint = _omni_mem_checkpoint(track_id, node_id, node, cp_cwd, boundary)
        if omni_checkpoint.get("status") == "recorded":
            _append_event(track_id, {
                "event": "omnimem_checkpoint_recorded",
                "node_id": node_id,
                "boundary": boundary,
                "workspace_id": omni_checkpoint.get("workspace_id"),
            })
        elif omni_checkpoint.get("status") != "skipped":
            _append_event(track_id, {
                "event": "omnimem_checkpoint_skipped",
                "node_id": node_id,
                "boundary": boundary,
                "reason": omni_checkpoint.get("reason", "unknown"),
            })

    result = {"node_id": node_id, "old_state": old_state, "new_state": new_state}
    if checkpoint_result:
        result["git_checkpoint"] = checkpoint_result
    if omni_checkpoint and omni_checkpoint.get("status") != "skipped":
        result["omni_mem_checkpoint"] = omni_checkpoint
    return result


# ---------------------------------------------------------------------------
# Cycle track
# ---------------------------------------------------------------------------

def cycle_track(
    track_id: str,
    *,
    max_cycles: int = 1,
    dry_run: bool = False,
    shadow: bool = False,
) -> dict[str, Any]:
    """Run one or more cycle iterations on a track.

    shadow=True (Slice 3): emit shadow_decision events for would-be actions
    instead of dispatching them. Layer-3 validation (analyze.py
    shadow-compare) consumes the resulting event log.
    """
    cycles = []
    if shadow and not dry_run:
        _append_event(track_id, {
            "event": "shadow_decision",
            "decision_kind": "cycle_session_started",
            "max_cycles": max_cycles,
        })

    for cycle_idx in range(max_cycles):
        td = track_dir(track_id)
        state = _load_json(td / "objective.state.json")
        graph = state["views"]["graph"]
        policy = state["views"]["policy"]
        governance = state["views"]["governance"]
        maintenance = state.get("views", {}).get("maintenance", build_maintenance_default(track_id))

        # Reconcile
        reconcile_result = reconcile_track(track_id, dry_run=dry_run)

        # Slice 1b: phase + route observable at cycle start.
        route_id = policy.get("hard_policy", {}).get("route_id", "")
        phase_at_start = current_phase(_read_events(track_id))

        # Slice 1b: question selection (R1 bypassed inside selector).
        selected_questions = select_phase_questions(phase_at_start, route_id)
        if not dry_run and selected_questions:
            _append_question_selection(
                track_id,
                phase=phase_at_start,
                route=route_id,
                questions=selected_questions,
            )

        # Anticipate
        anticipation = _build_anticipation(
            track_id=track_id,
            graph=graph,
            policy=policy,
            governance=governance,
            maintenance=maintenance,
        )

        # Slice 1b: capture before-state for observable kinds (post-anticipate
        # so next_action source has its target_ref; pre-action so we can diff).
        before_states = _capture_observable_states(
            track_id, state, anticipation=anticipation,
        ) if not dry_run else None

        action = anticipation["recommended_action"]
        action_result = {"action": action, "status": "planned" if dry_run else "pending"}

        if dry_run:
            cycles.append({
                "cycle": cycle_idx,
                "anticipation": anticipation,
                "action_result": action_result,
                "reconcile": reconcile_result,
            })
            break

        # Execute action
        if action == "close":
            cwd = policy.get("hard_policy", {}).get("cwd", str(Path.home()))
            closure = anticipation["closure"]
            _record_memory_lifecycle_gate(
                track_id,
                f"objective:closure:{closure['closure_state']}",
                "closure",
                f"Objective closure: {closure['closure_state']}",
                f"Track {track_id} reached {closure['closure_state']}",
                cwd,
            )
            action_result = {"action": "close", "status": "closed", "closure": closure}

        elif action == "dispatch":
            refresh_frontier(track_id)
            dispatch_result = dispatch_track(track_id)
            action_result = {"action": "dispatch", "status": dispatch_result["status"], "dispatch": dispatch_result}

        elif action == "repair_bookkeeping":
            reconcile_track(track_id)
            action_result = {"action": "repair_bookkeeping", "status": "repaired"}

        elif action == "replan":
            action_result = {"action": "replan", "status": "blocked", "reason": "replan_required"}

        elif action == "halt_for_authority":
            action_result = {"action": "halt_for_authority", "status": "blocked", "reason": anticipation.get("reason", "")}

        elif action == "evaluate":
            eval_dispatch = anticipation.get("evaluator_dispatch", {})
            if eval_dispatch.get("slice_id"):
                _append_event(track_id, {
                    "event": "evaluator_dispatched",
                    "slice_id": eval_dispatch["slice_id"],
                    "verification_profile": eval_dispatch.get("verification_profile", ""),
                    "playwright_recommended": eval_dispatch.get("playwright_recommended", False),
                })
            action_result = {"action": "evaluate", "status": "evaluator_dispatched", "evaluator_dispatch": eval_dispatch}

        else:
            action_result = {"action": action, "status": "blocked", "reason": f"unhandled_action: {action}"}

        _append_event(track_id, {
            "event": "cycle_completed",
            "cycle": cycle_idx,
            "recommended_action": action,
            "action_status": action_result["status"],
        })

        # Slice 1b: post-action decision_record emission for selected questions.
        # Re-load state to pick up any mutations from the action.
        decisions_recorded: list[dict[str, Any]] = []
        if before_states is not None:
            state_after = _load_json(td / "objective.state.json")
            # Anticipation values represent the action just dispatched, so
            # next_action's after-state equals the pre-state for this slice
            # (the action has been launched, not re-anticipated). Capture
            # what's observable now.
            after_states = _capture_observable_states(
                track_id, state_after, anticipation=anticipation,
            )
            # For each selected question with a targets_decision_kind, emit
            # a decision_record. Auto-generate no_change_reason when unchanged.
            for q in selected_questions:
                kind = q.get("targets_decision_kind")
                if kind not in OBSERVABLE_DECISION_KINDS:
                    continue
                before = before_states[kind]
                after = after_states[kind]
                changed = state_hash(before) != state_hash(after)
                no_change_reason = (
                    None if changed
                    else f"state_unchanged_after_{action}"
                )
                event = _append_decision_record(
                    track_id,
                    decision_kind=kind,
                    before_state=before,
                    after_state=after,
                    triggered_by_question_id=q["id"],
                    no_change_reason=no_change_reason,
                )
                decisions_recorded.append({
                    "kind": kind, "changed": event["changed"],
                    "triggered_by_question_id": q["id"],
                })

        # Slice 3-wire: auto-advance phase based on the action just executed.
        # Synthesizes evidence keys from observable events and walks the
        # phase chain toward the intent target. Transitions that fail the
        # phase-guard or verifier matrix are recorded (phase_transition_blocked)
        # and the walk stops; nothing throws.
        cwd_for_advance = policy.get("hard_policy", {}).get("cwd", "")
        target_intent = _ACTION_TO_PHASE_INTENT.get(action)
        if target_intent and cwd_for_advance:
            # Re-load state after the action — dispatch may have mutated the
            # frontier and node states.
            state_after_action = _load_json(td / "objective.state.json")
            slice_owned = _active_slice_owned_files(
                state_after_action,
                action=action, action_result=action_result,
                anticipation=anticipation,
            )
            _maybe_auto_advance_phase(
                track_id,
                target_phase=target_intent,
                route=route_id,
                cwd=cwd_for_advance,
                owned_files=slice_owned,
                shadow=shadow,
            )

        # Slice 1b: cycle_summary event (additive to cycle_completed).
        # Token / wall-clock counts come from Claude Code layer; null here.
        phase_at_end = current_phase(_read_events(track_id))
        _emit_cycle_summary(
            track_id,
            cycle_idx=cycle_idx,
            route=route_id,
            recommended_action=action,
            action_status=action_result["status"],
            questions_fired=[q["id"] for q in selected_questions],
            decisions_recorded=decisions_recorded,
            phase_at_start=phase_at_start,
            phase_at_end=phase_at_end,
        )

        # Slice 1b: track_summary on close.
        if action_result["status"] == "closed":
            closure = action_result.get("closure", {})
            _emit_track_summary(
                track_id,
                closure_state=closure.get("closure_state", "closed"),
            )

        cycles.append({
            "cycle": cycle_idx,
            "anticipation": anticipation,
            "action_result": action_result,
            "reconcile": reconcile_result,
        })

        # Break conditions
        if action_result["status"] in ("blocked", "closed", "not_dispatchable"):
            break

    # Reload final state
    td = track_dir(track_id)
    state = _load_json(td / "objective.state.json")

    return {
        "track_id": track_id,
        "dry_run": dry_run,
        "cycles": cycles,
        "summary": state.get("views", {}).get("summary", {}),
        "frontier": state.get("views", {}).get("frontier", {}),
        "governance": state.get("views", {}).get("governance", {}),
        "maintenance": state.get("views", {}).get("maintenance", {}),
    }


# ---------------------------------------------------------------------------
# Initialize track
# ---------------------------------------------------------------------------

def initialize_track(
    *,
    task: str,
    cwd: str,
    mode: str = "default",
    route_override: str | None = None,
    soft_policy: dict[str, Any] | None = None,
    include_memory: bool = True,
    invoker: str | None = None,
) -> dict[str, Any]:
    """Initialize a new objective track with all state.

    invoker: optional slash-command name that triggered this track
    (e.g., "drive", "build", "govern"). Recorded in state and session index
    to support orchestration-surface usage audits.
    """
    cwd = os.path.realpath(cwd)
    track_id = build_track_id(task, cwd)
    td = track_dir(track_id)

    # If track already exists, return existing state
    if (td / "objective.state.json").exists():
        state = _load_json(td / "objective.state.json")
        return {
            "track_id": track_id,
            "state_dir": str(td),
            "resumed": True,
            "route": state.get("views", {}).get("policy", {}).get("hard_policy", {}).get("route", {}),
            "preflight": state.get("preflight", {}),
            "frontier": state.get("views", {}).get("frontier", {}),
            "summary": state.get("views", {}).get("summary", {}),
            "governance": state.get("views", {}).get("governance", {}),
        }

    _ensure_dir(td)

    # Classify route
    route = classify_route(task, route_override=route_override)

    # Preflight
    preflight = build_preflight_report(task, cwd, route)
    if preflight["classification"] == "blocked":
        route = classify_route(task, route_override="R5")

    # Build state
    policy = compile_policy_envelope(task, cwd, route, soft_policy=soft_policy, preflight=preflight)
    graph = build_initial_graph(task, cwd, route, policy)
    governance = build_governance_default(track_id, route)
    maintenance = build_maintenance_default(track_id)
    memory = build_memory_default(track_id)

    frontier = compute_frontier(graph, policy)
    closure = build_closure(track_id, graph, policy, governance)
    metrics = build_metrics(graph, governance)
    summary = build_summary(track_id, graph, frontier, closure, metrics)

    state = {
        "schema_version": OBJECTIVE_STATE_SCHEMA,
        "track_id": track_id,
        "task": task,
        "cwd": cwd,
        "mode": mode,
        "invoker": invoker,
        "views": {
            "graph": graph,
            "frontier": frontier,
            "policy": policy,
            "metrics": metrics,
            "summary": summary,
            "closure": closure,
            "governance": governance,
            "maintenance": maintenance,
        },
        "preflight": preflight,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }

    # Persist
    with TrackLock(track_id):
        _save_json(td / "objective.state.json", state)
        _save_json(td / "objective.memory.json", memory)
        for name in ("graph", "frontier", "policy", "metrics", "summary", "closure", "governance", "maintenance"):
            _save_json(td / f"objective.{name}.json", state["views"][name])

        _append_event(track_id, {
            "event": "objective_initialized",
            "task": task,
            "cwd": cwd,
            "mode": mode,
            "invoker": invoker,
            "route_id": route["route_id"],
            "preflight_classification": preflight["classification"],
        })

    # Memory lifecycle gate
    if include_memory:
        _record_memory_lifecycle_gate(
            track_id, "objective:init", "objective_initialized",
            f"Objective initialized: {task[:80]}",
            f"Track {track_id} initialized with route {route['route_id']} in {cwd}",
            cwd,
        )

    # Register in session index
    _register_track(track_id, cwd, route["route_id"], invoker=invoker)

    return {
        "track_id": track_id,
        "state_dir": str(td),
        "resumed": False,
        "route": route,
        "preflight": preflight,
        "frontier": frontier,
        "summary": summary,
        "governance": governance,
    }


def _register_track(track_id: str, cwd: str, route_id: str, *, invoker: str | None = None) -> None:
    """Register track in session index."""
    index_path = AUTONOMY_DIR / "session_index.json"
    index = _load_json(index_path) if index_path.exists() else {"tracks": {}}
    index["tracks"][track_id] = {
        "cwd": cwd,
        "route_id": route_id,
        "invoker": invoker,
        "registered_at": now_iso(),
    }
    _save_json(index_path, index)


# ---------------------------------------------------------------------------
# Manager run task (top-level orchestrator)
# ---------------------------------------------------------------------------

def manager_run_task(
    *,
    task: str,
    cwd: str | None = None,
    track_id: str | None = None,
    route_override: str | None = None,
    max_actions: int = 12,
    max_runtime_seconds: float = 900.0,
    dry_run: bool = False,
    include_memory: bool = True,
    soft_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """End-to-end bounded task execution.

    Lifecycle: intake → classify → preflight → init → cycle loop
    → closure evaluation → memory persistence → result payload.
    """
    cwd = os.path.realpath(cwd or os.getcwd())
    started = time.monotonic()
    runs = []

    # Initialize or resume
    if track_id and (track_dir(track_id) / "objective.state.json").exists():
        init_result = initialize_track(task=task, cwd=cwd, route_override=route_override, soft_policy=soft_policy, include_memory=include_memory)
    else:
        init_result = initialize_track(task=task, cwd=cwd, route_override=route_override, soft_policy=soft_policy, include_memory=include_memory)
        track_id = init_result["track_id"]

    if dry_run:
        return {
            "schema_version": MANAGER_TASK_RUN_SCHEMA,
            "status": "planned_only",
            "task": task,
            "cwd": cwd,
            "track_id": track_id,
            "init": init_result,
            "runs": [],
            "dry_run": True,
        }

    status = "running"

    # Main loop
    for action_idx in range(max_actions):
        elapsed = time.monotonic() - started
        if elapsed >= max_runtime_seconds:
            status = "runtime_cap_reached"
            break

        with TrackLock(track_id):
            cycle_result = cycle_track(track_id, max_cycles=1)

        runs.append({
            "action_index": action_idx,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "cycle_result": cycle_result,
        })

        # Check terminal conditions
        summary = cycle_result.get("summary", {})
        closure_state = summary.get("closure_state", "ACTIVE")

        if closure_state in OBJECTIVE_SUCCESS_STATES:
            status = "closed"
            break
        elif closure_state in OBJECTIVE_BLOCKED_STATES:
            status = "blocked"
            break

        # Check cycle action status
        cycles = cycle_result.get("cycles", [])
        if cycles:
            last_action = cycles[-1].get("action_result", {})
            if last_action.get("status") == "blocked":
                action_kind = last_action.get("action", "")
                if action_kind == "halt_for_authority":
                    status = "authority_required"
                    break
                elif action_kind == "replan":
                    status = "blocked"
                    break
            elif last_action.get("status") == "not_dispatchable":
                status = "idle"
                break
            elif last_action.get("action") == "close":
                status = "closed"
                break

    else:
        if status == "running":
            status = "action_cap_reached"

    # Final state
    td = track_dir(track_id)
    final_state = _load_json(td / "objective.state.json")

    # Memory persistence for task run
    if include_memory and status in ("closed", "blocked"):
        cwd_resolved = final_state.get("cwd", cwd)
        _record_memory_lifecycle_gate(
            track_id,
            f"task_run:completion:{status}",
            "task_run_completion",
            f"Task run {status}: {task[:80]}",
            f"Track {track_id} completed with status={status}, "
            f"actions={len(runs)}, elapsed={round(time.monotonic() - started, 1)}s",
            cwd_resolved,
        )

    return {
        "schema_version": MANAGER_TASK_RUN_SCHEMA,
        "status": status,
        "task": task,
        "cwd": cwd,
        "track_id": track_id,
        "init": init_result,
        "runs": runs,
        "final_state": {
            "summary": final_state.get("views", {}).get("summary", {}),
            "closure": final_state.get("views", {}).get("closure", {}),
            "governance": final_state.get("views", {}).get("governance", {}),
            "metrics": final_state.get("views", {}).get("metrics", {}),
        },
        "metrics": {
            "status": status,
            "actions_executed": len(runs),
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "max_actions": max_actions,
            "max_runtime_seconds": max_runtime_seconds,
        },
        "dry_run": False,
    }


# ---------------------------------------------------------------------------
# Readiness check
# ---------------------------------------------------------------------------

def check_readiness() -> dict[str, Any]:
    """Verify auto runtime infrastructure is healthy."""
    checks = {}

    # Route manifest
    checks["route_manifest"] = ROUTE_MANIFEST_PATH.exists()

    # Control plane
    checks["control_plane"] = CONTROL_PLANE_PATH.exists()

    # Autonomy dir
    _ensure_dir(AUTONOMY_DIR)
    checks["autonomy_dir"] = AUTONOMY_DIR.is_dir()

    # omni-mem container
    try:
        result = subprocess.run(
            ["curl", "-s", "http://127.0.0.1:8765/health"],
            capture_output=True, text=True, timeout=5,
        )
        checks["omni_mem"] = '"ok"' in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        checks["omni_mem"] = False

    # Planning-gate scripts
    planning_gate_dir = CLAUDE_HOME / "skills" / "planning-gate" / "scripts"
    checks["planning_gate_scripts"] = planning_gate_dir.is_dir()

    # Classify prompt
    classify_script = CLAUDE_HOME / "skills" / "govern" / "scripts" / "classify_prompt.py"
    checks["classify_prompt"] = classify_script.exists()

    all_ok = all(checks.values())
    return {"ready": all_ok, "checks": checks}


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def replay_events(track_id: str) -> dict[str, Any]:
    """Replay all events for a track and return summary."""
    events = _read_events(track_id)
    replayable = [e for e in events if e.get("event") in REPLAYABLE_EVENTS]
    audit_only = [e for e in events if e.get("event") in AUDIT_ONLY_EVENTS]

    return {
        "track_id": track_id,
        "total_events": len(events),
        "replayable_events": len(replayable),
        "audit_only_events": len(audit_only),
        "event_types": list({e.get("event") for e in events}),
        "first_event": events[0] if events else None,
        "last_event": events[-1] if events else None,
    }


# ---------------------------------------------------------------------------
# Scan active tracks
# ---------------------------------------------------------------------------

def scan_tracks() -> dict[str, Any]:
    """Scan all autonomy tracks and return their status."""
    if not AUTONOMY_DIR.exists():
        return {"tracks": [], "total": 0}

    tracks = []
    for td in sorted(AUTONOMY_DIR.iterdir()):
        if not td.is_dir():
            continue
        state_path = td / "objective.state.json"
        if not state_path.exists():
            continue

        state = _load_json(state_path)
        summary = state.get("views", {}).get("summary", {})
        tracks.append({
            "track_id": td.name,
            "closure_state": summary.get("closure_state", "UNKNOWN"),
            "terminal": summary.get("terminal", False),
            "total_slices": summary.get("total_slices", 0),
            "accepted_slices": summary.get("accepted_slices", 0),
            "dispatch_count": summary.get("dispatch_count", 0),
            "updated_at": state.get("updated_at", ""),
        })

    return {"tracks": tracks, "total": len(tracks)}
