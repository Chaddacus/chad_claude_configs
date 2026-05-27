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
    container = os.environ.get("OMNI_MEM_CONTAINER", "omni-mem")
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


def _build_verification_hints(policy: dict[str, Any], node: dict[str, Any]) -> dict[str, Any] | None:
    """Build advisory Playwright verification hints for the executing session."""
    route_policy = policy.get("route_policy", {})
    profile = route_policy.get("verification_profile", "slice_only")
    ui = route_policy.get("ui_detection", {})
    if profile != "browser_e2e" and not ui.get("is_ui_work", False):
        return None
    return {
        "playwright_recommended": True,
        "verification_profile": profile,
        "ui_detection": ui,
        "advisory": True,
        "suggested_workflow": [
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
        "evaluator_model_hint": "claude-haiku-4-5",
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
                "docker", "exec", "-i", "omni-mem", "omni-mem",
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
    }
    verification_hints = _build_verification_hints(policy, node)
    if verification_hints:
        result["verification_hints"] = verification_hints
    return result


# ---------------------------------------------------------------------------
# Node state updates
# ---------------------------------------------------------------------------

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
        raise ValueError(f"Node {node_id} not found in graph")

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
) -> dict[str, Any]:
    """Run one or more cycle iterations on a track."""
    cycles = []

    for cycle_idx in range(max_cycles):
        td = track_dir(track_id)
        state = _load_json(td / "objective.state.json")
        graph = state["views"]["graph"]
        policy = state["views"]["policy"]
        governance = state["views"]["governance"]
        maintenance = state.get("views", {}).get("maintenance", build_maintenance_default(track_id))

        # Reconcile
        reconcile_result = reconcile_track(track_id, dry_run=dry_run)

        # Anticipate
        anticipation = _build_anticipation(
            track_id=track_id,
            graph=graph,
            policy=policy,
            governance=governance,
            maintenance=maintenance,
        )

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
) -> dict[str, Any]:
    """Initialize a new objective track with all state."""
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
    _register_track(track_id, cwd, route["route_id"])

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


def _register_track(track_id: str, cwd: str, route_id: str) -> None:
    """Register track in session index."""
    index_path = AUTONOMY_DIR / "session_index.json"
    index = _load_json(index_path) if index_path.exists() else {"tracks": {}}
    index["tracks"][track_id] = {
        "cwd": cwd,
        "route_id": route_id,
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
