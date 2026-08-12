#!/usr/bin/env python3
"""Shared helpers for planning-gate scripts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from objective_scheduler import (
    CLOSURE_STATES,
    DEFAULT_RECOMPUTE_TRIGGERS,
    DEFAULT_TERMINAL_STOP_CONDITIONS,
    FORWARD_MOTION_STATES,
    FRONTIER_MOVEMENT_REASONS,
    OBJECTIVE_CLOSURE_STATES,
    PARALLELISM_POLICIES,
    RUNTIME_STATES,
    STRUCTURAL_STATES,
    VERIFIER_OUTPUTS,
    build_schedule,
    compute_runnable_set,
    dependency_ready,
    evaluate_objective_closure,
    packet_autonomy_ready,
    packet_valid,
    safe_momentum_exists,
    VERDICT_STATES,
    validate_objective_status,
    validate_packet_dag,
    validate_schedule_state,
    validate_scheduler_policy,
)

PLAN_SCHEMA_VERSION = "plan.v1"
IMPLEMENTATION_SCHEMA_VERSION = "implementation.v1"
EXECUTION_PLAN_SCHEMA_VERSION = "execution-plan.v1"
KERNEL_RUNTIME_STATE_SCHEMA_VERSION = "runtime-state.v1"
VERIFICATION_RESULT_SCHEMA_VERSION = "verification-result.v1"
EVIDENCE_REF_SCHEMA_VERSION = "evidence-ref.v1"
TRANSITION_HISTORY_RECORD_SCHEMA_VERSION = "transition-history-record.v1"
FAILED_ATTEMPT_RECORD_SCHEMA_VERSION = "failed-attempt-record.v1"
PLANNING_GATE_PYTHON_ENV_VAR = "PLANNING_GATE_PYTHON_BIN"
PLANNING_GATE_REQUIRED_PYTHON = (3, 11)
# Scope-path tokens that make a packet owe rollback expectations. Shared by the
# packet-quality checker and the validation-packet generator so the two cannot
# disagree about what counts as migration-adjacent: when only the checker knew
# these, the generator emitted a non-rollback fallback for any plan that merely
# had a migration-named FILE, and failed its own gate.
_ROLLBACK_EXPECTATION_TOKENS = ("migration", "schema", ".sql")
OBJECTIVE_INTENT_SCHEMA_VERSION = "objective-intent.v1"
OBJECTIVE_RUNTIME_PACKET_DAG_SCHEMA_VERSION = "objective-packet-dag.v1"
OBJECTIVE_RUNTIME_STATUS_SCHEMA_VERSION = "objective-status.v1"
OBJECTIVE_RUNTIME_SCHEDULE_SCHEMA_VERSION = "objective-schedule.v1"
OBJECTIVE_RUNTIME_CYCLE_STATE_SCHEMA_VERSION = "cycle-state.v1"
OBJECTIVE_RUNTIME_SUMMARY_SCHEMA_VERSION = "objective-summary.v1"
OBJECTIVE_RUNTIME_VALIDATION_PLAN_SCHEMA_VERSION = "objective-validation-plan.v1"
OBJECTIVE_RUNTIME_REPO_CAPABILITIES_SCHEMA_VERSION = "objective-repo-capabilities.v1"
OBJECTIVE_RUNTIME_PACKET_QUALITY_SCHEMA_VERSION = "objective-packet-quality.v1"
OBJECTIVE_RUNTIME_EXECUTION_COVERAGE_SCHEMA_VERSION = "objective-execution-coverage.v1"
OBJECTIVE_RUNTIME_SUPPORT_CONFIDENCE_SCHEMA_VERSION = "objective-support-confidence.v1"
OBJECTIVE_RUNTIME_PACKET_RESULTS_SCHEMA_VERSION = "objective-packet-results.v1"
OBJECTIVE_RUNTIME_EXECUTION_LEDGER_SCHEMA_VERSION = "objective-execution-ledger.v1"
OBJECTIVE_RUNTIME_OPERATOR_VIEW_SCHEMA_VERSION = "objective-operator-view.v1"
OBJECTIVE_RUNTIME_STATE_SCHEMA_VERSION = "objective-runtime-state.v1"
OBJECTIVE_RUNTIME_BENCHMARK_SCHEMA_VERSION = "objective-benchmark.v1"
OBJECTIVE_RUNTIME_CANARY_SCHEMA_VERSION = "objective-canary.v1"
PLAN_INTENT_SCHEMA_VERSION = "plan-intent.v1"
CYCLE_REQUEST_SCHEMA_VERSION = "cycle-request.v1"
CYCLE_RESULT_SCHEMA_VERSION = "cycle-result.v1"
CYCLE_REVIEW_SCHEMA_VERSION = "cycle-review.v1"
CYCLE_STATE_VALUES = ("requested", "executed", "reviewed", "applied")
CAPTURE_SCHEMA_VERSION = "run-cmd-capture.v1"
CAPTURE_PRODUCER = "run_cmd_capture.v1"
TEST_CAPTURE_PREFIXES = ("worker-", "verifier-", "test-")
SMOKE_CAPTURE_PREFIXES = ("smoke-",)
LOG_CAPTURE_PREFIXES = ("log-",)
ROLLBACK_CAPTURE_PREFIXES = ("rollback-",)

_TRANSACTION_PATH_OVERRIDE_STACK: list[dict[Path, Path]] = []

SMOKE_STAGES = ("25%", "50%", "75%", "100%")
PASS_STATUSES = {"pass", "passed", "ok", "done"}
SMOKE_STATUS_VALUES = {"not_run", "pass", "passed", "ok", "done", "fail", "blocked"}

PLAN_REQUIRED_FIELDS = (
    "schema_version",
    "objective",
    "intent_contract",
    "clarification_governor",
    "autonomous_session_readiness",
    "constraints",
    "scope_boundaries",
    "implementation_plan",
    "definition_of_done",
    "objective_requirements",
    "objective_coverage_map",
    "assumptions_ledger",
    "authority_map",
    "integration_map",
    "evidence_plan",
    "dependencies",
    "tests",
    "logging_plan",
    "rollback_plan",
    "risks",
    "approval_gate",
    "non_goals",
    "quality_bar",
    "decomposition_policy",
    "momentum_map",
    "frontier_map",
    "objective_closure_policy",
    "migration_fallback_policy",
    "scheduler_policy",
    "hardening_budget",
    "plan_status",
    "plan_gap_report",
    "pre_delivery_gap_review",
    "plan_sufficiency_report",
    "requirement_risk_rank",
    "failure_mode_matrix",
    "edge_case_matrix",
    "packets",
    "session_harness",
    "required_packets",
    "autonomy_level",
    "existing_primitives_considered",
    "reuse_first_decision",
    "estimated_files_touched",
    "estimated_loc",
    "budget_exception_justification",
    "new_surface_proof",
)

SOLUTION_LADDER_LAYERS = (
    "L1_patch",
    "L2_abstraction",
    "L3_operating_surface",
)
SOLUTION_LAYER_VALUES = set(SOLUTION_LADDER_LAYERS)
FUTURE_REUSE_FREQUENCY_VALUES = {"low", "medium", "high"}
FUTURE_REUSE_SPREAD_VALUES = {"single_flow", "multi_flow", "system_surface"}
FUTURE_REUSE_OPERABILITY_VALUES = {"local_only", "reuse", "operator_surface"}
FUTURE_REUSE_BOUNDEDNESS_VALUES = {"bounded_now", "bounded_follow_on", "unbounded_now"}
SIMPLE_CHANGE_FILE_BUDGET = 3
SIMPLE_CHANGE_LOC_BUDGET = 500
POLICY_PLACEHOLDER_VALUES = {
    "",
    "n/a",
    "na",
    "none",
    "not-applicable",
    "not applicable",
    "not-needed",
    "not needed",
    "within_budget",
}
SURFACE_EXPANSION_KEYWORDS = (
    "new service",
    "service",
    "persistence layer",
    "schema family",
    "schema suite",
    "orchestration engine",
    "orchestration layer",
    "parallel coordination",
    "coordination engine",
    "messaging service",
)
CONTRACT_CLOSURE_REQUIRED_GROUPS = (
    "defined_terms",
    "authority_boundary",
    "repair_boundary",
    "mutator_contracts",
    "read_contracts",
    "frozen_surfaces",
)
OVERENGINEERING_REQUIRED_GROUPS = (
    "minimum_value_loop",
    "surface_budget",
    "reuse_proof",
    "deferred_surfaces",
    "forbidden_growth",
    "simplicity_tripwires",
)
CONTRACT_CLOSURE_DEFINED_TERMS = (
    "authoritative",
    "valid",
    "same_host",
    "occupied_slot",
    "belongs_to_repo",
    "no_mutation",
)
CONTRACT_CLOSURE_TRIGGER_KEYWORDS = (
    "persisted state",
    "persistence",
    "bootstrap",
    "recovery",
    "public api",
    "api surface",
    "file layout",
    "materialized state",
    "runtime surface",
    "control surface",
)
CONTRACT_CLOSURE_MUTATOR_FIELDS = (
    "preconditions",
    "write_set",
    "tx_shape",
    "reject_behavior",
    "quarantine_allowed",
)
CONTRACT_CLOSURE_READ_FIELDS = (
    "read_only",
    "ordering",
    "not_found_behavior",
)
SURFACE_BUDGET_FIELDS = (
    "new_public_apis",
    "new_modules",
    "new_persisted_top_level_paths",
    "new_closed_enum_families",
    "duplicated_state_surfaces",
)
CLOSURE_DRIFT_REPORT_FIELDS = (
    "unexpected_modules",
    "unexpected_public_apis",
    "unexpected_persisted_paths",
    "unexpected_enum_values",
    "unexpected_state_surfaces",
    "repair_boundary_violations",
    "read_only_boundary_violations",
    "overengineering_tripwires_triggered",
)

PLAN_SMOKE_REQUIRED_FIELDS = (
    "stage",
    "status",
    "criteria",
    "commands",
    "expected_output",
    "failure_interpretation",
    "proceed_decision",
    "rollback_decision",
)

DOD_REQUIRED_FIELDS = (
    "id",
    "category",
    "criterion",
    "verification",
)

RUNTIME_COMPATIBILITY_CHECKS = (
    "compiled_contract_schema_compatible",
    "packet_dag_runtime_compatible",
    "scheduler_admission_compatible",
    "verifier_acceptance_compatible",
    "closure_semantics_compatible",
    "intent_schema_compatible",
    "readiness_compatible",
    "autonomy_level_compatible",
)

PLAN_STATUS_VALUES = {
    "draft",
    "hardening",
    "execution_ready_candidate",
    "execution_ready",
    "revise",
    "blocked",
}

ASSUMPTION_CLASSIFICATIONS = {
    "resolved_from_context",
    "reversible_technical",
    "authority_required",
    "blocked",
}

REQUIREMENT_PRIORITIES = {"core", "critical", "optional"}
OBJECTIVE_SHAPE_STATUS_VALUES = {
    "accepted_as_given",
    "accepted_rewritten",
    "revise_required",
    "blocked",
}
AMBIGUITY_CLASSIFICATIONS = {
    "discoverable",
    "technical_reversible",
    "product_authority",
    "blocked",
}
AUTONOMY_LEVEL_VALUES = {
    "L1_guided",
    "L2_supervised_autonomous",
    "L3_supervised_throughput",
    "L4_release_adjacent",
}

PLAN_ARTIFACT_FILES = (
    "objective.intent.json",
    "plan.intent.json",
    "plan.compiler.json",
    "plan.gaps.json",
    "plan.coverage.json",
    "plan.sufficiency.json",
    "plan.readiness.json",
)

SESSION_HARNESS_REQUIRED_FIELDS = (
    "required",
    "route_hint",
    "estimated_packet_count",
    "expected_duration_minutes",
    "checkpoint_interval_minutes",
    "checkpoint_required",
    "context_index_required",
    "bootstrap_commands",
    "validation_commands",
    "clean_state_assertions",
    "ui_evidence_required",
)

SESSION_ROUTE_HINTS = {"R2", "R3", "R4"}
FEATURE_STATUS_VALUES = {"pending", "in_progress", "verified", "blocked", "deferred"}
FRONTIER_EXECUTION_MODES = {
    "parallel_safe",
    "sequence_required",
    "blocked",
    "deferred",
}
CHECKPOINT_REQUIRED_FIELDS = (
    "objective_id",
    "track_id",
    "checkpoint_id",
    "last_verified_packet_ids",
    "current_frontier",
    "bootstrap_commands",
    "validation_commands",
    "repo_state_summary",
    "clean_state_assertions",
    "next_recommended_packet",
    "open_risks",
    "handoff_notes",
    "last_forward_movement",
    "stagnation_risk",
    "escalation_candidates",
    "checkpoint_strategy",
    "checkpoint_attempted_at",
    "rollback_validation_ref",
)
CONTEXT_INDEX_CATEGORIES = (
    "architecture_docs",
    "design_docs",
    "execution_docs",
    "schema_contract_docs",
    "test_runbook_docs",
    "security_policy_docs",
    "active_objective_docs",
)
SESSION_ARTIFACT_FILES = (
    "objective.session.json",
    "objective.feature-list.json",
    "objective.progress.jsonl",
    "objective.checkpoint.json",
    "objective.context-index.json",
    "objective.momentum.json",
    "objective.blockers.json",
)
RUNTIME_ARTIFACT_FILES = (
    "objective.packet-dag.json",
    "objective.status.json",
    "objective.schedule.json",
)
CYCLE_STATE_VALUES = ("requested", "executed", "reviewed", "applied")

DOD_REQUIRED_CATEGORIES = (
    "correctness",
    "tests",
    "security",
    "observability",
    "rollback",
)

DOD_VAGUE_PATTERNS = (
    re.compile(r"(?i)\b(manual(ly)?|looks good|best effort|tbd|todo|as needed)\b"),
    re.compile(r"(?i)\b(ensure quality|validate quality|standard checks)\b"),
)

DOD_EXECUTABLE_PATTERNS = (
    re.compile(
        r"(?i)\b(pytest|python3?(?:\.\d+)?\s+-m|unittest|npm\s+test|pnpm\s+test|yarn\s+test|go\s+test|cargo\s+test|ruff|mypy|playwright|bash|sh|make|gradle|mvn|jq|rg|grep|sha256|checksum)\b"
    ),
    re.compile(r"(?i)\b(validate_(?:plan|impl)\.py|finalize_gate\.py|run_cmd_capture\.py)\b"),
    re.compile(r"(?i)\b[\w./-]+\.(?:json|log|txt|py|sh)\b"),
)

IMPLEMENTATION_REQUIRED_FIELDS = (
    "schema_version",
    "summary",
    "changed_files",
    "tests_run",
    "smoke_results",
    "logging_evidence",
    "rollback_validation",
    "memory_retrieval_evidence",
    "preferences_applied",
    "skill_trigger_eval_results",
    "prompt_contract_used",
    "frontend_roundtrip_evidence",
    "objective_runtime_state",
    "objective_status",
    "objective_summary",
    "validation_plan",
    "support_confidence",
    "schedule_artifact",
    "packet_verdicts",
    "execution_ledger",
    "packet_results_artifact",
    "checkpoint_commit",
    "checkpoint_blocked",
    "checkpoint_block_reason",
    "checkpoint_block_evidence",
    "bootstrap_commands",
    "validation_commands",
    "clean_state_assertions",
    "migration_fallback",
    "budget_outcome",
)

TEST_RUN_REQUIRED_FIELDS = (
    "name",
    "command",
    "status",
    "result",
    "proof_artifact",
    "proof_hash",
)

SMOKE_RESULT_REQUIRED_FIELDS = (
    "stage",
    "status",
    "command",
    "observed_output",
    "decision",
    "proof_artifact",
    "proof_hash",
)

LOG_EVIDENCE_REQUIRED_FIELDS = (
    "event",
    "proof_artifact",
    "proof_hash",
)

ROLLBACK_REQUIRED_FIELDS = (
    "executed",
    "result",
    "evidence",
    "proof_artifact",
    "proof_hash",
)

BUDGET_OUTCOME_REQUIRED_FIELDS = (
    "planned_files_touched",
    "planned_loc",
    "actual_files_touched",
    "actual_loc",
    "exception_used",
    "exception_justification",
    "proof_artifact",
    "proof_hash",
)

SUPPORT_CONFIDENCE_REQUIRED_FIELDS = (
    "schema_version",
    "objective_id",
    "track_id",
    "mode",
    "packet_support",
    "objective_support_status",
    "unsupported_closure_risk",
    "support_gap_reasons",
    "support_remediation_available",
    "external_support_coverage",
    "support_backed_closure",
    "final_gate_recommendation",
)

EXECUTION_PLAN_REQUIRED_FIELDS = (
    "schema_version",
    "objective_id",
    "track_id",
    "units",
)

EXECUTION_PLAN_UNIT_REQUIRED_FIELDS = (
    "unit_id",
    "unit_type",
    "priority",
    "required",
    "candidate_files",
    "allowed_scope",
    "dependencies",
    "max_retries",
    "mutation_budget",
    "verification_scope",
    "acceptance_checks",
    "failure_signals",
    "escalation_on_failure",
    "completion_test",
)

EXECUTION_PLAN_PRIORITY_VALUES = {"high", "medium", "normal", "low"}
EXECUTION_PLAN_VERIFICATION_SCOPES = {"local", "targeted", "broad", "environment"}

KERNEL_RUNTIME_STATE_REQUIRED_FIELDS = (
    "schema_version",
    "objective_id",
    "track_id",
    "state",
    "active_unit_id",
    "active_unit_ids",
    "completed_units",
    "failed_attempts",
    "last_action",
    "last_verification_id",
    "evidence_refs",
    "budget",
    "halt",
    "transition_history",
)

KERNEL_RUNTIME_STATES = {
    "bootstrapped",
    "planning_complete",
    "ready",
    "acting",
    "verifying",
    "repair_pending",
    "blocked",
    "finalize_pending",
    "success",
    "partial",
    "closed_blocked",
    "unsafe",
}

KERNEL_RUNTIME_TERMINAL_STATES = {"success", "partial", "closed_blocked", "unsafe"}
KERNEL_ACTION_KINDS = {
    "inspect",
    "edit",
    "run_command",
    "verify",
    "delegate",
    "repair",
    "finalize",
    "escalate_blocked",
}
KERNEL_HALT_REASONS = {
    "none",
    "accepted_success",
    "accepted_partial",
    "accepted_blocked",
    "needs_human_decision",
    "unsafe_to_continue",
    "no_safe_momentum",
    "invalid_transition",
}
VERIFICATION_RESULT_REQUIRED_FIELDS = (
    "schema_version",
    "verification_id",
    "step_id",
    "unit_id",
    "status",
    "scope",
    "blame",
    "repairability",
    "evidence",
    "suggested_transition",
)
VERIFICATION_RESULT_STATUSES = {"pass", "soft_fail", "hard_fail", "inconclusive"}
VERIFICATION_RESULT_SCOPES = {"local", "targeted", "broad", "environment"}
VERIFICATION_RESULT_BLAME = {"introduced", "pre_existing", "unknown"}
VERIFICATION_RESULT_REPAIRABILITY = {"local_patch", "retryable", "narrow_scope", "blocked"}
EVIDENCE_REF_REQUIRED_FIELDS = ("evidence_id", "kind", "path", "producer", "step_id")
EVIDENCE_REF_KINDS = {
    "command_output",
    "json_artifact",
    "log_ref",
    "policy_violation",
    "diff_snapshot",
    "runtime_artifact",
}
TRANSITION_HISTORY_REQUIRED_FIELDS = (
    "schema_version",
    "step_id",
    "from",
    "to",
    "guard",
    "guard_result",
    "trigger",
    "evidence_refs",
    "timestamp",
)
FAILED_ATTEMPT_REQUIRED_FIELDS = (
    "schema_version",
    "step_id",
    "unit_id",
    "failure_class",
    "verification_id",
    "count_against_retry_budget",
    "timestamp",
)


class ExecutionPlanCompileError(ValueError):
    """Raised when plan.v1 cannot compile into a valid execution-plan.v1 payload."""

OBJECTIVE_RUNTIME_STATE_REQUIRED_FIELDS = (
    "schema_version",
    "objective_id",
    "track_id",
    "route_hint",
    "controller_mode",
    "lifecycle_status",
    "closure_state",
    "required_work_remaining",
    "material_optional_work_remaining",
    "stop_allowed",
    "stop_reason",
    "next_recommended_packet",
    "unsupported_closure_risk",
    "last_verifier_result",
)

CONTROLLER_MODES = {"audit", "enforce"}
RUNTIME_LIFECYCLE_STATUSES = {
    "running",
    "revise",
    "blocked",
    "approved_pending_verify",
    "approved",
    "error",
}

MEMORY_RETRIEVAL_REQUIRED_FIELDS = (
    "tool",
    "query",
    "result_count",
)

PREFERENCE_APPLIED_REQUIRED_FIELDS = (
    "key",
    "decision",
    "rationale",
)

SKILL_TRIGGER_EVAL_REQUIRED_FIELDS = (
    "skill",
    "false_positive_rate",
    "false_negative_rate",
    "threshold_passed",
)

PROMPT_CONTRACT_REQUIRED_FIELDS = (
    "name",
    "required_context",
    "required_constraints",
    "verification_section",
    "done_when",
)

FRONTEND_ROUNDTRIP_REQUIRED_FIELDS = (
    "step",
    "evidence",
)

QUALITY_BAR_REQUIRED_FIELDS = (
    "maintainability",
    "evidence",
    "policy_compliance",
)

INTENT_CONTRACT_REQUIRED_FIELDS = (
    "objective",
    "success_criteria",
    "non_goals",
    "authority_sensitive_decisions",
    "ambiguity_classification",
    "objective_shape_status",
)

CLARIFICATION_GOVERNOR_REQUIRED_FIELDS = (
    "default_batch_limit",
    "allowed_topics",
    "repo_discoverable_questions_forbidden",
    "new_authority_boundary_required_for_mid_execution_clarification",
)

READINESS_REQUIRED_FIELDS = (
    "status",
    "safe_momentum_ready",
    "readiness_gaps",
    "next_executable_frontier",
)

READINESS_STATUS_VALUES = {"execution_ready", "revise", "blocked"}

HARDENING_BUDGET_REQUIRED_FIELDS = (
    "max_hardening_passes",
    "max_repacketization_passes",
    "max_unresolved_gaps_before_revise",
    "max_authority_blockers_before_blocked",
)

SCHEDULER_POLICY_REQUIRED_FIELDS = (
    "max_parallel_packets",
    "parallelism_policy",
    "admission_rule",
    "recompute_triggers",
    "terminal_stop_conditions",
)

OBJECTIVE_CLOSURE_POLICY_REQUIRED_FIELDS = (
    "allowed_states",
    "boundary_shrink_allowed",
)

MIGRATION_FALLBACK_POLICY_REQUIRED_FIELDS = (
    "compat_fallback_allowed",
    "max_fallback_invocations",
    "manifest_rollback_path",
)

OBJECTIVE_REQUIREMENT_REQUIRED_FIELDS = (
    "requirement_id",
    "description",
    "priority",
    "definition_of_done",
)

OBJECTIVE_COVERAGE_REQUIRED_FIELDS = (
    "requirement_id",
    "packet_ids",
    "verification",
    "evidence",
)

ASSUMPTION_REQUIRED_FIELDS = (
    "assumption_id",
    "statement",
    "classification",
    "disposition",
)

AUTHORITY_MAP_REQUIRED_FIELDS = (
    "authority_id",
    "type",
    "scope",
    "resolution",
)

INTEGRATION_TOUCHPOINT_REQUIRED_FIELDS = (
    "integration_id",
    "touchpoint",
    "packet_ids",
    "verification",
    "evidence",
)

EDGE_MATRIX_REQUIRED_FIELDS = (
    "item_id",
    "scenario",
    "handling",
    "verification",
)

PLAN_GAP_REPORT_REQUIRED_FIELDS = (
    "gaps_detected",
    "gaps_auto_fixed",
    "gaps_escalated",
    "gaps_unresolved",
)

PRE_DELIVERY_GAP_REVIEW_REQUIRED_FIELDS = (
    "performed",
    "issues_found",
    "issues_fixed",
    "issues_remaining",
    "ready_to_present",
    "review_summary",
)

PLAN_SUFFICIENCY_REQUIRED_FIELDS = (
    "status",
    "coverage_complete",
    "integration_realism",
    "runtime_compatible",
    "unresolved_gap_count",
    "verifier_notes",
)

REQUIREMENT_RISK_RANK_REQUIRED_FIELDS = (
    "requirement_id",
    "priority",
    "associated_packet_ids",
    "evidence_type",
    "failure_impact",
)

MOMENTUM_MAP_REQUIRED_FIELDS = (
    "packet_id",
    "movement_type",
    "unlocks_packets",
    "resolves_dependency_classification",
    "isolates_blocker",
)

FRONTIER_MAP_REQUIRED_FIELDS = (
    "packet_id",
    "execution_mode",
)

PACKET_DEFINITION_OF_DONE_REQUIRED_FIELDS = (
    "behavior_outcome",
    "acceptance_checks",
    "evidence_requirements",
    "allowed_scope",
    "rollback_or_fallback",
    "verifier_acceptance_condition",
    "objective_linkage",
)

OBJECTIVE_STATUS_REQUIRED_FIELDS = (
    "objective_id",
    "closure_state",
    "completed_packets",
    "pending_packets",
    "blocked_packets",
    "deferred_packets",
    "boundary_shrunk_remainder",
    "artifact_path",
)

SCHEDULE_ARTIFACT_REQUIRED_FIELDS = (
    "objective_id",
    "max_parallel_packets",
    "parallelism_policy",
    "dispatch_history",
    "runnable_set",
    "blocked_set",
    "retry_counters",
    "strategy_switches",
    "repacketization_events",
    "current_frontier",
    "closure_readiness",
    "cycle_log",
    "safe_momentum_available",
    "frontier_movement",
    "frontier_movement_reason",
    "max_same_strategy_retries",
    "max_noop_cycles",
    "max_verifier_return_cycles",
    "max_no_frontier_movement_cycles",
    "admission_failures",
    "total_packet_count",
    "accepted_packet_count",
    "rejected_packet_count",
    "blocked_packet_count",
    "repacketization_count",
    "escalation_count",
    "migration_fallback_used",
    "total_runtime_attempts",
    "runtime_states",
)

PACKET_VERDICT_REQUIRED_FIELDS = (
    "packet_id",
    "strategy_name",
    "runner_kind",
    "runtime_state",
    "verifier_output",
    "allowed_scope_status",
    "artifact_path",
)

MIGRATION_FALLBACK_REQUIRED_FIELDS = (
    "used",
    "reason",
    "artifact_path",
)

SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([^\s]+)"),
    re.compile(r"(?i)(x-api-key\s*:\s*)([^\s]+)"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)([^\s\"']+)"),
    re.compile(r"(?i)(token\s*[=:]\s*)([^\s\"']+)"),
    re.compile(r"(?i)(password\s*[=:]\s*)([^\s\"']+)"),
    re.compile(r"(?i)(secret\s*[=:]\s*)([^\s\"']+)"),
]


class ValidationError(RuntimeError):
    """Raised when input files are invalid."""


@dataclass
class ContractResult:
    missing: list[str]
    blocked: list[str]
    smoke_100_pass: bool
    smoke_quality_score: int = 0
    evidence_quality_score: int = 0
    objective_closure_state: str = ""
    accepted_type: str = ""
    migration_fallback_used: bool = False
    plan_status: str = ""
    runtime_compatible: bool = False
    budget_within_plan: bool = True
    budget_exception_used: bool = False


def ensure_python_3_11() -> None:
    if sys.version_info < PLANNING_GATE_REQUIRED_PYTHON:
        raise RuntimeError(
            "planning-gate requires Python >= 3.11; "
            "rerun with a Python 3.11 interpreter or set "
            f"{PLANNING_GATE_PYTHON_ENV_VAR} to a Python 3.11 executable"
        )


def _python_candidate_supports_required_version(candidate: str) -> bool:
    try:
        proc = subprocess.run(
            [
                candidate,
                "-c",
                (
                    "import sys; "
                    "print(f'{sys.version_info.major}.{sys.version_info.minor}')"
                ),
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    parts = proc.stdout.strip().split(".")
    if len(parts) < 2:
        return False
    try:
        version = (int(parts[0]), int(parts[1]))
    except ValueError:
        return False
    return version >= PLANNING_GATE_REQUIRED_PYTHON


@lru_cache(maxsize=1)
def resolve_python_3_11_bin() -> str:
    candidates: list[str] = []
    override = str(os.environ.get(PLANNING_GATE_PYTHON_ENV_VAR, "")).strip()
    if override:
        candidates.append(override)
    if sys.executable:
        candidates.append(sys.executable)
    discovered = shutil.which("python3.11")
    if discovered:
        candidates.append(discovered)

    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if _python_candidate_supports_required_version(normalized):
            return normalized

    raise RuntimeError(
        "planning-gate could not resolve a canonical Python 3.11 interpreter; "
        f"set {PLANNING_GATE_PYTHON_ENV_VAR} to a valid Python 3.11 executable"
    )


def canonical_python_argv(*args: Any) -> list[str]:
    return [resolve_python_3_11_bin(), *[str(arg) for arg in args]]


def canonical_python_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    if extra:
        env.update(extra)
    env[PLANNING_GATE_PYTHON_ENV_VAR] = resolve_python_3_11_bin()
    return env


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitize_token(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or "unnamed"


def stable_objective_id(track_id: str) -> str:
    return f"objective-{sanitize_token(track_id)}"


def normalize_stage(raw: Any) -> str:
    value = str(raw or "").strip().replace(" ", "")
    if value.endswith("%"):
        value = value[:-1]
    if value in {"25", "50", "75", "100"}:
        return f"{value}%"
    return ""


def to_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        if lines:
            return lines
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if item is None:
                continue
            item_s = str(item).strip()
            if item_s:
                out.append(item_s)
        return out
    value_s = str(value).strip()
    return [value_s] if value_s else []


def _as_string_list(value: Any) -> list[str]:
    return to_string_list(value)


def _normalized_policy_text(value: Any) -> str:
    return str(value or "").strip()


def _has_meaningful_policy_text(value: Any, *, min_length: int = 12) -> bool:
    text = _normalized_policy_text(value)
    if len(text) < min_length:
        return False
    return text.lower() not in POLICY_PLACEHOLDER_VALUES


def _plan_requests_new_surface(plan: dict[str, Any]) -> bool:
    if str(plan.get("chosen_layer", "")).strip() == "L3_operating_surface":
        return True
    probe = json.dumps(
        {
            "objective": plan.get("objective"),
            "constraints": plan.get("constraints"),
            "scope_boundaries": plan.get("scope_boundaries"),
            "implementation_plan": plan.get("implementation_plan"),
            "non_goals": plan.get("non_goals"),
        },
        sort_keys=True,
        default=str,
    ).lower()
    return any(keyword in probe for keyword in SURFACE_EXPANSION_KEYWORDS)


def _plan_route_hint(plan: dict[str, Any]) -> str:
    session_harness = plan.get("session_harness")
    if not isinstance(session_harness, dict):
        return ""
    return str(session_harness.get("route_hint", "")).strip()


def _plan_requires_contract_closure(plan: dict[str, Any], *, route_hint: str) -> bool:
    if route_hint in {"R3", "R4"}:
        return True
    if "contract_closure" in plan or "overengineering_guardrails" in plan:
        return True
    if route_hint != "R2":
        return False
    probe = json.dumps(
        {
            "objective": plan.get("objective"),
            "constraints": plan.get("constraints"),
            "scope_boundaries": plan.get("scope_boundaries"),
            "implementation_plan": plan.get("implementation_plan"),
            "authority_map": plan.get("authority_map"),
            "integration_map": plan.get("integration_map"),
            "non_goals": plan.get("non_goals"),
        },
        sort_keys=True,
        default=str,
    ).lower()
    return any(keyword in probe for keyword in CONTRACT_CLOSURE_TRIGGER_KEYWORDS)


def _has_non_empty_string_items(value: Any, *, minimum_item_len: int = 1) -> bool:
    items = to_string_list(value)
    return bool(items) and all(len(item.strip()) >= minimum_item_len for item in items)


def _validate_contract_closure(
    plan: dict[str, Any],
    *,
    route_hint: str,
    missing: list[str],
    blocked: list[str],
) -> None:
    if not _plan_requires_contract_closure(plan, route_hint=route_hint):
        return

    closure = plan.get("contract_closure")
    if not isinstance(closure, dict):
        missing.append("contract_closure")
        return

    for key in CONTRACT_CLOSURE_REQUIRED_GROUPS:
        if key not in closure:
            missing.append(f"contract_closure:{key}")

    defined_terms = closure.get("defined_terms")
    if not isinstance(defined_terms, dict):
        missing.append("contract_closure:defined_terms:not_object")
        defined_terms = {}
    for term in CONTRACT_CLOSURE_DEFINED_TERMS:
        if not _is_non_empty_string(defined_terms.get(term), 4):
            missing.append(f"contract_closure:defined_terms:{term}")

    authority_boundary = closure.get("authority_boundary")
    if not isinstance(authority_boundary, dict):
        missing.append("contract_closure:authority_boundary:not_object")
        authority_boundary = {}
    if not _is_non_empty_string(authority_boundary.get("source_of_truth"), 8):
        missing.append("contract_closure:authority_boundary:source_of_truth")
    if not _has_non_empty_string_items(authority_boundary.get("materialized_surfaces"), minimum_item_len=3):
        missing.append("contract_closure:authority_boundary:materialized_surfaces")
    if not _is_non_empty_string(authority_boundary.get("rewrite_precedence"), 8):
        missing.append("contract_closure:authority_boundary:rewrite_precedence")
    if not (
        _is_non_empty_string(authority_boundary.get("continuity_rules"), 8)
        or _has_non_empty_string_items(authority_boundary.get("continuity_rules"), minimum_item_len=3)
    ):
        missing.append("contract_closure:authority_boundary:continuity_rules")

    repair_boundary = closure.get("repair_boundary")
    if not isinstance(repair_boundary, dict):
        missing.append("contract_closure:repair_boundary:not_object")
        repair_boundary = {}
    if not _is_non_empty_string(repair_boundary.get("allowed_repair_path"), 8):
        missing.append("contract_closure:repair_boundary:allowed_repair_path")
    forbidden_repair = repair_boundary.get("mutators_forbidden_repair")
    if not isinstance(forbidden_repair, list):
        missing.append("contract_closure:repair_boundary:mutators_forbidden_repair")
    elif any(not _is_non_empty_string(item, 3) for item in forbidden_repair):
        missing.append("contract_closure:repair_boundary:mutators_forbidden_repair:item_invalid")
    if "bootstrap_before_serving" not in repair_boundary:
        missing.append("contract_closure:repair_boundary:bootstrap_before_serving")

    mutator_contracts = closure.get("mutator_contracts")
    if not isinstance(mutator_contracts, dict):
        missing.append("contract_closure:mutator_contracts:not_object")
        mutator_contracts = {}
    if "not_applicable_reason" in mutator_contracts:
        if not _is_non_empty_string(mutator_contracts.get("not_applicable_reason"), 8):
            missing.append("contract_closure:mutator_contracts:not_applicable_reason")
    elif not mutator_contracts:
        missing.append("contract_closure:mutator_contracts:entries_required")
    else:
        for name, contract in mutator_contracts.items():
            if not isinstance(contract, dict):
                missing.append(f"contract_closure:mutator_contracts:{name}:not_object")
                continue
            for field in CONTRACT_CLOSURE_MUTATOR_FIELDS:
                if field not in contract:
                    missing.append(f"contract_closure:mutator_contracts:{name}:{field}")
            if not (
                _is_non_empty_string(contract.get("write_set"), 8)
                or _has_non_empty_string_items(contract.get("write_set"), minimum_item_len=3)
            ):
                missing.append(f"contract_closure:mutator_contracts:{name}:write_set")
            if not _is_non_empty_string(contract.get("reject_behavior"), 8):
                blocked.append(f"contract_closure:mutator_contracts:{name}:reject_behavior_required")

    read_contracts = closure.get("read_contracts")
    if not isinstance(read_contracts, dict):
        missing.append("contract_closure:read_contracts:not_object")
        read_contracts = {}
    if "not_applicable_reason" in read_contracts:
        if not _is_non_empty_string(read_contracts.get("not_applicable_reason"), 8):
            missing.append("contract_closure:read_contracts:not_applicable_reason")
    elif not read_contracts:
        missing.append("contract_closure:read_contracts:entries_required")
    else:
        for name, contract in read_contracts.items():
            if not isinstance(contract, dict):
                missing.append(f"contract_closure:read_contracts:{name}:not_object")
                continue
            for field in CONTRACT_CLOSURE_READ_FIELDS:
                if field not in contract:
                    missing.append(f"contract_closure:read_contracts:{name}:{field}")
            if contract.get("read_only") is not True:
                blocked.append(f"contract_closure:read_contracts:{name}:read_only_required")
            if not _is_non_empty_string(contract.get("ordering"), 3):
                missing.append(f"contract_closure:read_contracts:{name}:ordering")
            if not _is_non_empty_string(contract.get("not_found_behavior"), 3):
                missing.append(f"contract_closure:read_contracts:{name}:not_found_behavior")

    frozen_surfaces = closure.get("frozen_surfaces")
    if not isinstance(frozen_surfaces, dict):
        missing.append("contract_closure:frozen_surfaces:not_object")
        frozen_surfaces = {}
    if not isinstance(frozen_surfaces.get("public_apis"), list):
        missing.append("contract_closure:frozen_surfaces:public_apis")
    if not isinstance(frozen_surfaces.get("modules"), list):
        missing.append("contract_closure:frozen_surfaces:modules")
    if not isinstance(frozen_surfaces.get("persisted_paths"), list):
        missing.append("contract_closure:frozen_surfaces:persisted_paths")
    enum_sets = frozen_surfaces.get("enum_sets")
    if not isinstance(enum_sets, (dict, list)):
        missing.append("contract_closure:frozen_surfaces:enum_sets")
    additions_forbidden = frozen_surfaces.get("additions_forbidden")
    if additions_forbidden not in {True, "true"} and not _is_non_empty_string(additions_forbidden, 8):
        missing.append("contract_closure:frozen_surfaces:additions_forbidden")


def _validate_overengineering_guardrails(
    plan: dict[str, Any],
    *,
    route_hint: str,
    missing: list[str],
    blocked: list[str],
) -> None:
    if not _plan_requires_contract_closure(plan, route_hint=route_hint):
        return

    guardrails = plan.get("overengineering_guardrails")
    if not isinstance(guardrails, dict):
        missing.append("overengineering_guardrails")
        return

    for key in OVERENGINEERING_REQUIRED_GROUPS:
        if key not in guardrails:
            missing.append(f"overengineering_guardrails:{key}")

    minimum_value_loop = guardrails.get("minimum_value_loop")
    if not (
        _is_non_empty_string(minimum_value_loop, 8)
        or _has_non_empty_string_items(minimum_value_loop, minimum_item_len=3)
    ):
        missing.append("overengineering_guardrails:minimum_value_loop")

    surface_budget = guardrails.get("surface_budget")
    if not isinstance(surface_budget, dict):
        missing.append("overengineering_guardrails:surface_budget:not_object")
        surface_budget = {}
    for field in SURFACE_BUDGET_FIELDS:
        if not isinstance(surface_budget.get(field), int) or int(surface_budget.get(field, -1)) < 0:
            missing.append(f"overengineering_guardrails:surface_budget:{field}")

    reuse_proof = guardrails.get("reuse_proof")
    if not isinstance(reuse_proof, dict):
        missing.append("overengineering_guardrails:reuse_proof:not_object")
        reuse_proof = {}
    if not isinstance(reuse_proof.get("reused_primitives"), list):
        missing.append("overengineering_guardrails:reuse_proof:reused_primitives")
    if not isinstance(reuse_proof.get("not_added"), list):
        missing.append("overengineering_guardrails:reuse_proof:not_added")
    if not _is_non_empty_string(reuse_proof.get("why_not_lower"), 8):
        missing.append("overengineering_guardrails:reuse_proof:why_not_lower")
    if not _is_non_empty_string(reuse_proof.get("why_not_higher"), 8):
        missing.append("overengineering_guardrails:reuse_proof:why_not_higher")

    if not _has_non_empty_string_items(guardrails.get("deferred_surfaces"), minimum_item_len=3):
        missing.append("overengineering_guardrails:deferred_surfaces")
    if not _has_non_empty_string_items(guardrails.get("forbidden_growth"), minimum_item_len=3):
        missing.append("overengineering_guardrails:forbidden_growth")
    if not _has_non_empty_string_items(guardrails.get("simplicity_tripwires"), minimum_item_len=3):
        missing.append("overengineering_guardrails:simplicity_tripwires")

    frozen_surfaces = plan.get("contract_closure", {}).get("frozen_surfaces", {}) if isinstance(plan.get("contract_closure"), dict) else {}
    authority_boundary = plan.get("contract_closure", {}).get("authority_boundary", {}) if isinstance(plan.get("contract_closure"), dict) else {}
    public_api_count = len(frozen_surfaces.get("public_apis", [])) if isinstance(frozen_surfaces.get("public_apis"), list) else 0
    module_count = len(frozen_surfaces.get("modules", [])) if isinstance(frozen_surfaces.get("modules"), list) else 0
    persisted_path_count = len(frozen_surfaces.get("persisted_paths", [])) if isinstance(frozen_surfaces.get("persisted_paths"), list) else 0
    enum_family_count = len(frozen_surfaces.get("enum_sets", {})) if isinstance(frozen_surfaces.get("enum_sets"), dict) else len(frozen_surfaces.get("enum_sets", [])) if isinstance(frozen_surfaces.get("enum_sets"), list) else 0
    materialized_surfaces = authority_boundary.get("materialized_surfaces", [])
    duplicated_state_count = max(0, len(materialized_surfaces) - 1) if isinstance(materialized_surfaces, list) else 0
    budget_checks = {
        "new_public_apis": public_api_count,
        "new_modules": module_count,
        "new_persisted_top_level_paths": persisted_path_count,
        "new_closed_enum_families": enum_family_count,
        "duplicated_state_surfaces": duplicated_state_count,
    }
    for field, actual in budget_checks.items():
        budget = surface_budget.get(field)
        if isinstance(budget, int) and actual > budget:
            blocked.append(f"overengineering_guardrails:surface_budget_exceeded:{field}")


def _validate_closure_drift_report(
    value: Any,
    *,
    required: bool,
    missing: list[str],
    blocked: list[str],
) -> None:
    if value is None:
        if required:
            missing.append("implementation:closure_drift_report")
        return
    if not isinstance(value, dict):
        missing.append("implementation:closure_drift_report:not_object")
        return
    for field in CLOSURE_DRIFT_REPORT_FIELDS:
        if field not in value:
            missing.append(f"implementation:closure_drift_report:{field}")
            continue
        items = value.get(field)
        if not isinstance(items, list):
            missing.append(f"implementation:closure_drift_report:{field}:not_list")
            continue
        normalized_items = [str(item).strip() for item in items if str(item).strip()]
        if normalized_items:
            blocked.append(f"implementation:closure_drift_report:{field}")


def _validate_reuse_first_policy(
    plan: dict[str, Any],
    *,
    missing: list[str],
    blocked: list[str],
) -> None:
    primitives = plan.get("existing_primitives_considered")
    if not isinstance(primitives, list) or not primitives:
        missing.append("existing_primitives_considered")
    else:
        for idx, item in enumerate(primitives, start=1):
            if not _is_non_empty_string(item, 3):
                missing.append(f"existing_primitives_considered:{idx}")

    if not _has_meaningful_policy_text(plan.get("reuse_first_decision"), min_length=16):
        missing.append("reuse_first_decision")

    estimated_files = plan.get("estimated_files_touched")
    estimated_loc = plan.get("estimated_loc")
    if not isinstance(estimated_files, int) or estimated_files < 0:
        missing.append("estimated_files_touched")
    if not isinstance(estimated_loc, int) or estimated_loc < 0:
        missing.append("estimated_loc")

    budget_exception = _normalized_policy_text(plan.get("budget_exception_justification"))
    new_surface_proof = _normalized_policy_text(plan.get("new_surface_proof"))
    if not budget_exception:
        missing.append("budget_exception_justification")
    if not new_surface_proof:
        missing.append("new_surface_proof")

    if (
        isinstance(estimated_files, int)
        and isinstance(estimated_loc, int)
        and (estimated_files > SIMPLE_CHANGE_FILE_BUDGET or estimated_loc > SIMPLE_CHANGE_LOC_BUDGET)
        and not _has_meaningful_policy_text(budget_exception, min_length=16)
    ):
        blocked.append("simplicity_budget:exception_required")

    if _plan_requests_new_surface(plan) and not _has_meaningful_policy_text(new_surface_proof, min_length=20):
        blocked.append("new_surface_proof:required_for_surface_expansion")


def redact_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


def resolve_artifacts_root(root: str | None = None, cwd: str | None = None) -> Path:
    base = Path(root) if root else Path(cwd or os.getcwd()) / "planning_artifacts"
    return base.expanduser().resolve()


def resolve_proof_path(path_value: str, artifacts_root: Path, cwd: str | None = None) -> Path:
    p = Path(path_value).expanduser()
    if not p.is_absolute():
        p = Path(cwd or os.getcwd()) / p
    p = p.resolve()
    if artifacts_root not in p.parents and p != artifacts_root:
        raise ValidationError(f"proof artifact outside artifacts root: {p}")
    return p


def resolve_transaction_managed_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    for overrides in reversed(_TRANSACTION_PATH_OVERRIDE_STACK):
        staged_path = overrides.get(resolved)
        if staged_path is not None:
            return staged_path
    return resolved


@contextmanager
def transaction_path_overrides(path_overrides: dict[str | Path, str | Path]):
    normalized: dict[Path, Path] = {}
    for live_path, staged_path in path_overrides.items():
        normalized[Path(live_path).expanduser().resolve()] = Path(staged_path).expanduser().resolve()
    _TRANSACTION_PATH_OVERRIDE_STACK.append(normalized)
    try:
        yield
    finally:
        _TRANSACTION_PATH_OVERRIDE_STACK.pop()


def load_json_file(path: str | Path) -> dict[str, Any]:
    p = resolve_transaction_managed_path(path)
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"missing_json_file:{p}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid_json:{p}:{exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError(f"payload_not_object:{p}")
    return payload


def write_text_atomic(path: str | Path, text: str) -> None:
    p = resolve_transaction_managed_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}-{time.time_ns()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def write_json_file(path: str | Path, payload: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: str | Path, payload: dict[str, Any]) -> None:
    original = Path(path).expanduser().resolve()
    resolved = resolve_transaction_managed_path(path)
    if resolved != original:
        existing = resolved.read_text(encoding="utf-8") if resolved.exists() else ""
        write_text_atomic(resolved, existing + json.dumps(payload, sort_keys=True) + "\n")
        return
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with resolved.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True) + "\n")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
        return digest.hexdigest()


def write_capture_manifest(
    *,
    artifacts_root: Path,
    track_id: str,
    name: str,
    stage: str,
    cwd: str,
    command_argv: list[str],
    exit_code: int,
    stdout_text: str,
    stderr_text: str,
) -> tuple[Path, str]:
    capture_dir = artifacts_root / sanitize_token(track_id) / "captures" / sanitize_token(name)
    capture_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = capture_dir / "stdout.redacted.txt"
    stderr_path = capture_dir / "stderr.redacted.txt"
    stdout_path.write_text(redact_text(stdout_text), encoding="utf-8")
    stderr_path.write_text(redact_text(stderr_text), encoding="utf-8")
    started_at = now_iso()
    ended_at = now_iso()
    manifest = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "producer": CAPTURE_PRODUCER,
        "captured_at": ended_at,
        "track_id": track_id,
        "stage": stage,
        "name": name,
        "cwd": cwd,
        "command_argv": command_argv,
        "exit_code": exit_code,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": 0,
        "stdout_path": str(stdout_path.resolve()),
        "stderr_path": str(stderr_path.resolve()),
        "stdout_sha256": sha256_file(stdout_path),
        "stderr_sha256": sha256_file(stderr_path),
        "safety": {
            "decision": "runtime-generated",
            "override_used": False,
            "override_reason": "",
        },
        "timeout_sec": 0,
        "timeout_exceeded": False,
        "output_truncated": False,
        "env_whitelist": {},
    }
    manifest_path = capture_dir / "manifest.json"
    write_json_file(manifest_path, manifest)
    return manifest_path.resolve(), sha256_file(manifest_path)


def stable_review(
    *,
    gate: str,
    status: str,
    content: str,
    missing_fields: list[str] | None = None,
    blocked_fields: list[str] | None = None,
    risks: list[str] | None = None,
    next_step: str = "Revise and resubmit.",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "planning_gate_review",
        "gate": gate,
        "status": status,
        "missing_fields": sorted(set(missing_fields or [])),
        "blocked_fields": sorted(set(blocked_fields or [])),
        "content": content,
        "risks": risks or [],
        "next_step": next_step,
        "meta": meta or {},
    }


def _validate_smoke_gates(
    smoke_gates: Any,
    required_fields: tuple[str, ...],
    missing: list[str],
    blocked: list[str],
    prefix: str,
) -> tuple[bool, int]:
    if not isinstance(smoke_gates, list):
        missing.append(f"{prefix}:not_list")
        return False, 0

    found: set[str] = set()
    smoke_100_pass = False
    quality_score = 0

    for gate in smoke_gates:
        if not isinstance(gate, dict):
            missing.append(f"{prefix}:item_not_object")
            continue

        stage = normalize_stage(gate.get("stage"))
        if not stage:
            missing.append(f"{prefix}:invalid_stage")
            continue
        found.add(stage)

        for key in required_fields:
            if key not in gate:
                missing.append(f"{prefix}:{stage}:{key}")

        status = str(gate.get("status", "")).strip().lower()
        if status not in SMOKE_STATUS_VALUES:
            missing.append(f"{prefix}:{stage}:invalid_status")

        commands = to_string_list(gate.get("commands", gate.get("command")))
        if "commands" in required_fields and not commands:
            missing.append(f"{prefix}:{stage}:commands")

        if len(commands) >= 1:
            quality_score += 2
        if len(commands) >= 2:
            quality_score += 1
        if len(str(gate.get("criteria", "")).strip()) >= 12:
            quality_score += 1
        if len(str(gate.get("expected_output", "")).strip()) >= 16:
            quality_score += 1
        if len(str(gate.get("failure_interpretation", "")).strip()) >= 16:
            quality_score += 1
        if len(str(gate.get("proceed_decision", "")).strip()) >= 10:
            quality_score += 1
        if len(str(gate.get("rollback_decision", "")).strip()) >= 10:
            quality_score += 1

        if stage == "100%" and status in PASS_STATUSES:
            smoke_100_pass = True

    for required_stage in SMOKE_STAGES:
        if required_stage not in found:
            missing.append(f"{prefix}:missing_stage:{required_stage}")

    if not smoke_100_pass:
        missing.append(f"{prefix}:100% status=pass")

    return smoke_100_pass, quality_score


def _validate_definition_of_done(value: Any, missing: list[str]) -> None:
    def _looks_vague(text: str) -> bool:
        return any(pattern.search(text) for pattern in DOD_VAGUE_PATTERNS)

    def _is_executable_verification(text: str) -> bool:
        return any(pattern.search(text) for pattern in DOD_EXECUTABLE_PATTERNS)

    if not isinstance(value, list):
        missing.append("definition_of_done:not_list")
        return
    if len(value) < len(DOD_REQUIRED_CATEGORIES):
        missing.append("definition_of_done:min_items")

    seen_categories: set[str] = set()
    seen_ids: set[str] = set()
    for idx, item in enumerate(value, start=1):
        prefix = f"definition_of_done:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue

        for required_field in DOD_REQUIRED_FIELDS:
            if required_field not in item:
                missing.append(f"{prefix}:{required_field}")

        raw_id_value = item.get("id")
        if not isinstance(raw_id_value, str):
            missing.append(f"{prefix}:id_not_string")
            raw_id = ""
        else:
            raw_id = raw_id_value.strip()
        if not raw_id:
            missing.append(f"{prefix}:id_blank")
        elif raw_id in seen_ids:
            missing.append(f"{prefix}:id_duplicate")
        else:
            seen_ids.add(raw_id)

        category_value = item.get("category")
        if not isinstance(category_value, str):
            missing.append(f"{prefix}:category_not_string")
            category = ""
        else:
            category = category_value.strip().lower()
        if not category:
            missing.append(f"{prefix}:category_blank")
        elif category not in DOD_REQUIRED_CATEGORIES:
            missing.append(f"{prefix}:category_invalid")
        else:
            seen_categories.add(category)

        criterion_value = item.get("criterion")
        if not isinstance(criterion_value, str):
            missing.append(f"{prefix}:criterion_not_string")
            criterion = ""
        else:
            criterion = criterion_value.strip()

        verification_value = item.get("verification")
        if not isinstance(verification_value, str):
            missing.append(f"{prefix}:verification_not_string")
            verification = ""
        else:
            verification = verification_value.strip()

        if len(criterion) < 12:
            missing.append(f"{prefix}:criterion_too_short")
        if criterion and _looks_vague(criterion):
            missing.append(f"{prefix}:criterion_too_generic")
        if len(verification) < 8:
            missing.append(f"{prefix}:verification_too_short")
        if verification and _looks_vague(verification):
            missing.append(f"{prefix}:verification_too_generic")
        elif verification and not _is_executable_verification(verification):
            missing.append(f"{prefix}:verification_not_executable")

    for category in DOD_REQUIRED_CATEGORIES:
        if category not in seen_categories:
            missing.append(f"definition_of_done:missing_category:{category}")


def _validate_non_goals(value: Any, missing: list[str]) -> None:
    if not isinstance(value, list) or not value:
        missing.append("non_goals")
        return
    for idx, item in enumerate(value, start=1):
        if not _is_non_empty_string(item, 8):
            missing.append(f"non_goals:{idx}")


def _validate_quality_bar(value: Any, missing: list[str]) -> None:
    if not isinstance(value, dict):
        missing.append("quality_bar:not_object")
        return
    for key in QUALITY_BAR_REQUIRED_FIELDS:
        if not _is_non_empty_string(value.get(key), 12):
            missing.append(f"quality_bar:{key}")


def _validate_intent_contract(value: Any, missing: list[str], blocked: list[str]) -> None:
    if not isinstance(value, dict):
        missing.append("intent_contract:not_object")
        return
    for key in INTENT_CONTRACT_REQUIRED_FIELDS:
        if key not in value:
            missing.append(f"intent_contract:{key}")
    if not _is_non_empty_string(value.get("objective"), 12):
        missing.append("intent_contract:objective_invalid")
    success_criteria = to_string_list(value.get("success_criteria"))
    if not success_criteria:
        missing.append("intent_contract:success_criteria_invalid")
    authority_sensitive_decisions = value.get("authority_sensitive_decisions")
    if not isinstance(authority_sensitive_decisions, list):
        missing.append("intent_contract:authority_sensitive_decisions_invalid")
    ambiguity_items = value.get("ambiguity_classification")
    if not isinstance(ambiguity_items, list) or not ambiguity_items:
        missing.append("intent_contract:ambiguity_classification_invalid")
    else:
        for idx, item in enumerate(ambiguity_items, start=1):
            prefix = f"intent_contract:ambiguity_classification:{idx}"
            if not isinstance(item, dict):
                missing.append(f"{prefix}:item_not_object")
                continue
            if not _is_non_empty_string(item.get("item_id"), 1):
                missing.append(f"{prefix}:item_id_invalid")
            if not _is_non_empty_string(item.get("statement"), 8):
                missing.append(f"{prefix}:statement_invalid")
            if str(item.get("classification", "")).strip() not in AMBIGUITY_CLASSIFICATIONS:
                missing.append(f"{prefix}:classification_invalid")
    objective_shape_status = str(value.get("objective_shape_status", "")).strip()
    if objective_shape_status not in OBJECTIVE_SHAPE_STATUS_VALUES:
        missing.append("intent_contract:objective_shape_status_invalid")
    elif objective_shape_status == "blocked":
        blocked.append("intent_contract:objective_shape_status_blocked")


def _validate_clarification_governor(value: Any, missing: list[str]) -> None:
    if not isinstance(value, dict):
        missing.append("clarification_governor:not_object")
        return
    for key in CLARIFICATION_GOVERNOR_REQUIRED_FIELDS:
        if key not in value:
            missing.append(f"clarification_governor:{key}")
    try:
        if int(value.get("default_batch_limit", 0) or 0) <= 0:
            missing.append("clarification_governor:default_batch_limit_invalid")
    except Exception:
        missing.append("clarification_governor:default_batch_limit_invalid")
    allowed_topics = set(to_string_list(value.get("allowed_topics")))
    required_topics = {
        "product_meaning",
        "authority_security_boundaries",
        "missing_success_criteria",
        "non_discoverable_tradeoffs",
    }
    if allowed_topics != required_topics:
        missing.append("clarification_governor:allowed_topics_invalid")
    for key in (
        "repo_discoverable_questions_forbidden",
        "new_authority_boundary_required_for_mid_execution_clarification",
    ):
        if not isinstance(value.get(key), bool):
            missing.append(f"clarification_governor:{key}_invalid")


def _validate_autonomous_session_readiness(value: Any, missing: list[str], blocked: list[str]) -> tuple[str, bool]:
    if not isinstance(value, dict):
        missing.append("autonomous_session_readiness:not_object")
        return "", False
    for key in READINESS_REQUIRED_FIELDS:
        if key not in value:
            missing.append(f"autonomous_session_readiness:{key}")
    status = str(value.get("status", "")).strip()
    if status not in READINESS_STATUS_VALUES:
        missing.append("autonomous_session_readiness:status_invalid")
    safe_momentum_ready = value.get("safe_momentum_ready")
    if not isinstance(safe_momentum_ready, bool):
        missing.append("autonomous_session_readiness:safe_momentum_ready_invalid")
        safe_momentum_ready = False
    if not isinstance(value.get("readiness_gaps"), list):
        missing.append("autonomous_session_readiness:readiness_gaps_invalid")
    if not isinstance(value.get("next_executable_frontier"), list):
        missing.append("autonomous_session_readiness:next_executable_frontier_invalid")
    if status == "execution_ready":
        if to_string_list(value.get("readiness_gaps")):
            missing.append("autonomous_session_readiness:execution_ready_with_gaps")
        if not to_string_list(value.get("next_executable_frontier")):
            missing.append("autonomous_session_readiness:execution_ready_without_frontier")
    if status == "blocked":
        blocked.append("autonomous_session_readiness:blocked")
    return status, safe_momentum_ready


def _validate_momentum_map(
    value: Any,
    *,
    packet_ids: set[str],
    missing: list[str],
    blocked: list[str],
) -> None:
    if not isinstance(value, list) or not value:
        missing.append("momentum_map")
        return
    seen: set[str] = set()
    for idx, item in enumerate(value, start=1):
        prefix = f"momentum_map:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue
        for key in MOMENTUM_MAP_REQUIRED_FIELDS:
            if key not in item:
                missing.append(f"{prefix}:{key}")
        packet_id = str(item.get("packet_id", "")).strip()
        if not packet_id or packet_id not in packet_ids:
            blocked.append(f"{prefix}:packet_id_unknown")
            continue
        seen.add(packet_id)
        movement_type = str(item.get("movement_type", "")).strip()
        if movement_type not in FORWARD_MOTION_STATES - {"invalid_noop"}:
            missing.append(f"{prefix}:movement_type_invalid")
        unlocks_packets = to_string_list(item.get("unlocks_packets"))
        for unlock_id in unlocks_packets:
            if unlock_id not in packet_ids:
                blocked.append(f"{prefix}:unlocks_unknown_packet:{unlock_id}")
        resolves_dependency = bool(item.get("resolves_dependency_classification") is True)
        isolates_blocker = bool(item.get("isolates_blocker") is True)
        if movement_type == "uncertainty_reducing" and not (unlocks_packets or resolves_dependency or isolates_blocker):
            blocked.append(f"{prefix}:uncertainty_reduction_without_frontier_effect")
    for packet_id in sorted(packet_ids - seen):
        missing.append(f"momentum_map:missing_packet:{packet_id}")


def _validate_frontier_map(
    value: Any,
    *,
    packet_ids: set[str],
    missing: list[str],
    blocked: list[str],
) -> None:
    if not isinstance(value, list) or not value:
        missing.append("frontier_map")
        return
    seen: set[str] = set()
    for idx, item in enumerate(value, start=1):
        prefix = f"frontier_map:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue
        for key in FRONTIER_MAP_REQUIRED_FIELDS:
            if key not in item:
                missing.append(f"{prefix}:{key}")
        packet_id = str(item.get("packet_id", "")).strip()
        if not packet_id or packet_id not in packet_ids:
            blocked.append(f"{prefix}:packet_id_unknown")
            continue
        seen.add(packet_id)
        if str(item.get("execution_mode", "")).strip() not in FRONTIER_EXECUTION_MODES:
            missing.append(f"{prefix}:execution_mode_invalid")
    for packet_id in sorted(packet_ids - seen):
        missing.append(f"frontier_map:missing_packet:{packet_id}")


def _validate_evidence_plan(value: Any, missing: list[str]) -> None:
    if not isinstance(value, list) or not value:
        missing.append("evidence_plan")
        return
    for idx, item in enumerate(value, start=1):
        prefix = f"evidence_plan:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue
        if not _is_non_empty_string(item.get("name"), 6):
            missing.append(f"{prefix}:name_invalid")
        if not _is_non_empty_string(item.get("verification"), 8):
            missing.append(f"{prefix}:verification_invalid")
        if not _is_non_empty_string(item.get("evidence"), 8):
            missing.append(f"{prefix}:evidence_invalid")


def _validate_packet_definition_of_done(
    value: Any,
    *,
    prefix: str,
    packet_allowed_scope: list[str],
    missing: list[str],
    blocked: list[str],
) -> None:
    if not isinstance(value, dict):
        missing.append(f"{prefix}:definition_of_done:not_object")
        return
    for key in PACKET_DEFINITION_OF_DONE_REQUIRED_FIELDS:
        if key not in value:
            missing.append(f"{prefix}:definition_of_done:{key}")
    if not _is_non_empty_string(value.get("behavior_outcome"), 8):
        missing.append(f"{prefix}:definition_of_done:behavior_outcome_invalid")
    if not _as_string_list(value.get("acceptance_checks")):
        missing.append(f"{prefix}:definition_of_done:acceptance_checks_invalid")
    if not _as_string_list(value.get("evidence_requirements")):
        missing.append(f"{prefix}:definition_of_done:evidence_requirements_invalid")
    if _as_string_list(value.get("allowed_scope")) != packet_allowed_scope:
        blocked.append(f"{prefix}:definition_of_done:allowed_scope_mismatch")
    if not _is_non_empty_string(value.get("rollback_or_fallback"), 8):
        missing.append(f"{prefix}:definition_of_done:rollback_or_fallback_invalid")
    if not _is_non_empty_string(value.get("verifier_acceptance_condition"), 8):
        missing.append(f"{prefix}:definition_of_done:verifier_acceptance_condition_invalid")
    if not _is_non_empty_string(value.get("objective_linkage"), 3):
        missing.append(f"{prefix}:definition_of_done:objective_linkage_invalid")


def _validate_objective_closure_policy(value: Any, missing: list[str]) -> None:
    if not isinstance(value, dict):
        missing.append("objective_closure_policy:not_object")
        return
    for key in OBJECTIVE_CLOSURE_POLICY_REQUIRED_FIELDS:
        if key not in value:
            missing.append(f"objective_closure_policy:{key}")
    allowed_states = value.get("allowed_states")
    if not isinstance(allowed_states, list) or not allowed_states:
        missing.append("objective_closure_policy:allowed_states")
    else:
        for state in allowed_states:
            if str(state).strip() not in CLOSURE_STATES:
                missing.append(f"objective_closure_policy:allowed_state_invalid:{state}")
    if not isinstance(value.get("boundary_shrink_allowed"), bool):
        missing.append("objective_closure_policy:boundary_shrink_allowed_invalid")


def _validate_migration_fallback_policy(value: Any, missing: list[str]) -> None:
    if not isinstance(value, dict):
        missing.append("migration_fallback_policy:not_object")
        return
    for key in MIGRATION_FALLBACK_POLICY_REQUIRED_FIELDS:
        if key not in value:
            missing.append(f"migration_fallback_policy:{key}")
    if not isinstance(value.get("compat_fallback_allowed"), bool):
        missing.append("migration_fallback_policy:compat_fallback_allowed_invalid")
    if not isinstance(value.get("max_fallback_invocations"), int) or int(value.get("max_fallback_invocations")) < 0:
        missing.append("migration_fallback_policy:max_fallback_invocations_invalid")
    if not _is_non_empty_string(value.get("manifest_rollback_path"), 8):
        missing.append("migration_fallback_policy:manifest_rollback_path_invalid")


def _validate_scheduler_policy(value: Any, missing: list[str], blocked: list[str]) -> None:
    result = validate_scheduler_policy(value)
    missing.extend(result.missing)
    blocked.extend(result.blocked)


def _validate_hardening_budget(value: Any, missing: list[str]) -> None:
    if not isinstance(value, dict):
        missing.append("hardening_budget:not_object")
        return
    for key in HARDENING_BUDGET_REQUIRED_FIELDS:
        raw = value.get(key)
        if not isinstance(raw, int) or raw < 0:
            missing.append(f"hardening_budget:{key}")


def _solution_layer_rank(value: str) -> int:
    order = {
        "L1_patch": 1,
        "L2_abstraction": 2,
        "L3_operating_surface": 3,
    }
    return order.get(value, 0)


def _recommended_solution_layer(
    *,
    frequency: str,
    spread: str,
    operability: str,
    boundedness: str,
) -> str:
    reusable = (
        frequency in {"medium", "high"}
        or spread in {"multi_flow", "system_surface"}
        or operability in {"reuse", "operator_surface"}
    )
    strong_operating = (
        frequency == "high"
        or spread == "system_surface"
        or operability == "operator_surface"
    )
    if strong_operating and boundedness == "bounded_now":
        return "L3_operating_surface"
    if reusable:
        return "L2_abstraction"
    return "L1_patch"


def _validate_solution_ladder(
    plan: dict[str, Any],
    *,
    route_hint: str,
    missing: list[str],
    blocked: list[str],
) -> None:
    if route_hint not in {"R3", "R4"}:
        return

    ladder = plan.get("solution_ladder")
    if not isinstance(ladder, dict):
        missing.append("solution_ladder")
        ladder = {}
    for layer in SOLUTION_LADDER_LAYERS:
        value = ladder.get(layer)
        if not _is_non_empty_string(value, 8) and not isinstance(value, (dict, list)):
            missing.append(f"solution_ladder:{layer}")

    chosen_layer = str(plan.get("chosen_layer", "")).strip()
    if chosen_layer not in SOLUTION_LAYER_VALUES:
        missing.append("chosen_layer")
    if not _is_non_empty_string(plan.get("layer_justification"), 16):
        missing.append("layer_justification")
    if not _is_non_empty_string(plan.get("why_not_lower"), 12):
        missing.append("why_not_lower")
    if not _is_non_empty_string(plan.get("why_not_higher"), 12):
        missing.append("why_not_higher")

    future_reuse_gain = plan.get("future_reuse_gain")
    if not isinstance(future_reuse_gain, dict):
        missing.append("future_reuse_gain")
        return

    frequency = str(future_reuse_gain.get("frequency", "")).strip()
    spread = str(future_reuse_gain.get("spread", "")).strip()
    operability = str(future_reuse_gain.get("operability", "")).strip()
    boundedness = str(future_reuse_gain.get("boundedness", "")).strip()

    if frequency not in FUTURE_REUSE_FREQUENCY_VALUES:
        missing.append("future_reuse_gain:frequency")
    if spread not in FUTURE_REUSE_SPREAD_VALUES:
        missing.append("future_reuse_gain:spread")
    if operability not in FUTURE_REUSE_OPERABILITY_VALUES:
        missing.append("future_reuse_gain:operability")
    if boundedness not in FUTURE_REUSE_BOUNDEDNESS_VALUES:
        missing.append("future_reuse_gain:boundedness")

    if any(
        item in missing
        for item in (
            "chosen_layer",
            "future_reuse_gain:frequency",
            "future_reuse_gain:spread",
            "future_reuse_gain:operability",
            "future_reuse_gain:boundedness",
        )
    ):
        return

    recommended_layer = _recommended_solution_layer(
        frequency=frequency,
        spread=spread,
        operability=operability,
        boundedness=boundedness,
    )
    chosen_rank = _solution_layer_rank(chosen_layer)
    recommended_rank = _solution_layer_rank(recommended_layer)

    if chosen_rank < recommended_rank:
        blocked.append(
            f"solution_ladder:chosen_layer_below_useful:{chosen_layer}:{recommended_layer}"
        )
    if chosen_layer == "L2_abstraction" and recommended_layer == "L1_patch":
        blocked.append("solution_ladder:chosen_layer_above_useful:L2_abstraction")
    if chosen_layer == "L3_operating_surface":
        if boundedness != "bounded_now":
            blocked.append("solution_ladder:l3_requires_bounded_now")
        if recommended_layer != "L3_operating_surface":
            blocked.append("solution_ladder:chosen_layer_above_useful:L3_operating_surface")


def _validate_runtime_definition_of_done(value: Any, missing: list[str], prefix: str) -> None:
    if not isinstance(value, dict):
        missing.append(f"{prefix}:definition_of_done:not_object")
        return

    for field_name in PACKET_DEFINITION_OF_DONE_REQUIRED_FIELDS:
        if field_name not in value:
            missing.append(f"{prefix}:definition_of_done:{field_name}")

    if not _is_non_empty_string(value.get("behavior_outcome"), 12):
        missing.append(f"{prefix}:definition_of_done:behavior_outcome_invalid")
    if not to_string_list(value.get("acceptance_checks")):
        missing.append(f"{prefix}:definition_of_done:acceptance_checks_invalid")
    if not to_string_list(value.get("evidence_requirements")):
        missing.append(f"{prefix}:definition_of_done:evidence_requirements_invalid")
    if not to_string_list(value.get("allowed_scope")):
        missing.append(f"{prefix}:definition_of_done:allowed_scope_invalid")
    if not _is_non_empty_string(value.get("rollback_or_fallback"), 8):
        missing.append(f"{prefix}:definition_of_done:rollback_or_fallback_invalid")
    if not _is_non_empty_string(value.get("verifier_acceptance_condition"), 8):
        missing.append(f"{prefix}:definition_of_done:verifier_acceptance_condition_invalid")
    if not _is_non_empty_string(value.get("objective_linkage"), 3):
        missing.append(f"{prefix}:definition_of_done:objective_linkage_invalid")


def _validate_objective_requirements(value: Any, missing: list[str], blocked: list[str]) -> set[str]:
    if not isinstance(value, list) or not value:
        missing.append("objective_requirements")
        return set()

    seen: set[str] = set()
    for idx, item in enumerate(value, start=1):
        prefix = f"objective_requirements:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue
        for field_name in OBJECTIVE_REQUIREMENT_REQUIRED_FIELDS:
            if field_name not in item:
                missing.append(f"{prefix}:{field_name}")
        requirement_id = str(item.get("requirement_id", "")).strip()
        if not requirement_id:
            missing.append(f"{prefix}:requirement_id_invalid")
            continue
        if requirement_id in seen:
            blocked.append(f"{prefix}:requirement_id_duplicate")
            continue
        seen.add(requirement_id)
        if not _is_non_empty_string(item.get("description"), 12):
            missing.append(f"{prefix}:description_invalid")
        priority = str(item.get("priority", "")).strip()
        if priority not in REQUIREMENT_PRIORITIES:
            missing.append(f"{prefix}:priority_invalid")
        _validate_runtime_definition_of_done(item.get("definition_of_done"), missing, prefix)
    return seen


def _validate_objective_coverage_map(
    value: Any,
    *,
    requirement_ids: set[str],
    packet_ids: set[str],
    missing: list[str],
    blocked: list[str],
) -> None:
    if not isinstance(value, list) or not value:
        missing.append("objective_coverage_map")
        return

    covered: set[str] = set()
    for idx, item in enumerate(value, start=1):
        prefix = f"objective_coverage_map:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue
        for field_name in OBJECTIVE_COVERAGE_REQUIRED_FIELDS:
            if field_name not in item:
                missing.append(f"{prefix}:{field_name}")
        requirement_id = str(item.get("requirement_id", "")).strip()
        if requirement_id not in requirement_ids:
            blocked.append(f"{prefix}:requirement_id_unknown")
        else:
            covered.add(requirement_id)
        mapped_packets = set(to_string_list(item.get("packet_ids")))
        if not mapped_packets:
            missing.append(f"{prefix}:packet_ids_invalid")
        for packet_id in mapped_packets:
            if packet_id not in packet_ids:
                blocked.append(f"{prefix}:packet_id_unknown:{packet_id}")
        if not to_string_list(item.get("verification")):
            missing.append(f"{prefix}:verification_invalid")
        if not to_string_list(item.get("evidence")):
            missing.append(f"{prefix}:evidence_invalid")

    for requirement_id in sorted(requirement_ids - covered):
        missing.append(f"objective_coverage_map:missing_requirement:{requirement_id}")


def _validate_assumptions_ledger(value: Any, missing: list[str]) -> None:
    if not isinstance(value, list) or not value:
        missing.append("assumptions_ledger")
        return
    for idx, item in enumerate(value, start=1):
        prefix = f"assumptions_ledger:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue
        for field_name in ASSUMPTION_REQUIRED_FIELDS:
            if field_name not in item:
                missing.append(f"{prefix}:{field_name}")
        if not _is_non_empty_string(item.get("assumption_id"), 3):
            missing.append(f"{prefix}:assumption_id_invalid")
        if not _is_non_empty_string(item.get("statement"), 8):
            missing.append(f"{prefix}:statement_invalid")
        if str(item.get("classification", "")).strip() not in ASSUMPTION_CLASSIFICATIONS:
            missing.append(f"{prefix}:classification_invalid")
        if not _is_non_empty_string(item.get("disposition"), 8):
            missing.append(f"{prefix}:disposition_invalid")


def _validate_authority_map(value: Any, missing: list[str]) -> None:
    if not isinstance(value, list) or not value:
        missing.append("authority_map")
        return
    for idx, item in enumerate(value, start=1):
        prefix = f"authority_map:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue
        for field_name in AUTHORITY_MAP_REQUIRED_FIELDS:
            if field_name not in item:
                missing.append(f"{prefix}:{field_name}")
        if not _is_non_empty_string(item.get("authority_id"), 3):
            missing.append(f"{prefix}:authority_id_invalid")
        if not _is_non_empty_string(item.get("type"), 3):
            missing.append(f"{prefix}:type_invalid")
        if not _is_non_empty_string(item.get("scope"), 8):
            missing.append(f"{prefix}:scope_invalid")
        if not _is_non_empty_string(item.get("resolution"), 8):
            missing.append(f"{prefix}:resolution_invalid")


def _validate_integration_map(
    value: Any,
    *,
    packet_ids: set[str],
    missing: list[str],
    blocked: list[str],
) -> None:
    if not isinstance(value, list) or not value:
        missing.append("integration_map")
        return
    for idx, item in enumerate(value, start=1):
        prefix = f"integration_map:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue
        for field_name in INTEGRATION_TOUCHPOINT_REQUIRED_FIELDS:
            if field_name not in item:
                missing.append(f"{prefix}:{field_name}")
        if not _is_non_empty_string(item.get("integration_id"), 3):
            missing.append(f"{prefix}:integration_id_invalid")
        if not _is_non_empty_string(item.get("touchpoint"), 8):
            missing.append(f"{prefix}:touchpoint_invalid")
        mapped_packets = set(to_string_list(item.get("packet_ids")))
        if not mapped_packets:
            missing.append(f"{prefix}:packet_ids_invalid")
        for packet_id in mapped_packets:
            if packet_id not in packet_ids:
                blocked.append(f"{prefix}:packet_id_unknown:{packet_id}")
        if not to_string_list(item.get("verification")):
            missing.append(f"{prefix}:verification_invalid")
        if not to_string_list(item.get("evidence")):
            missing.append(f"{prefix}:evidence_invalid")


def _validate_matrix(value: Any, *, name: str, missing: list[str]) -> None:
    if not isinstance(value, list) or not value:
        missing.append(name)
        return
    for idx, item in enumerate(value, start=1):
        prefix = f"{name}:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue
        for field_name in EDGE_MATRIX_REQUIRED_FIELDS:
            if field_name not in item:
                missing.append(f"{prefix}:{field_name}")
        if not _is_non_empty_string(item.get("item_id"), 3):
            missing.append(f"{prefix}:item_id_invalid")
        if not _is_non_empty_string(item.get("scenario"), 8):
            missing.append(f"{prefix}:scenario_invalid")
        if not _is_non_empty_string(item.get("handling"), 8):
            missing.append(f"{prefix}:handling_invalid")
        if not _is_non_empty_string(item.get("verification"), 8):
            missing.append(f"{prefix}:verification_invalid")


def _validate_plan_gap_report(value: Any, missing: list[str]) -> int:
    if not isinstance(value, dict):
        missing.append("plan_gap_report:not_object")
        return 0
    unresolved = 0
    for field_name in PLAN_GAP_REPORT_REQUIRED_FIELDS:
        raw = value.get(field_name)
        if not isinstance(raw, list):
            missing.append(f"plan_gap_report:{field_name}")
            continue
        if field_name == "gaps_unresolved":
            unresolved = len(raw)
    return unresolved


def _validate_pre_delivery_gap_review(
    value: Any,
    *,
    missing: list[str],
    blocked: list[str],
) -> None:
    if not isinstance(value, dict):
        missing.append("pre_delivery_gap_review:not_object")
        return
    for field_name in PRE_DELIVERY_GAP_REVIEW_REQUIRED_FIELDS:
        if field_name not in value:
            missing.append(f"pre_delivery_gap_review:{field_name}")
    if value.get("performed") is not True:
        blocked.append("pre_delivery_gap_review:performed_required")
    for field_name in ("issues_found", "issues_fixed", "issues_remaining"):
        raw = value.get(field_name)
        if not isinstance(raw, list):
            missing.append(f"pre_delivery_gap_review:{field_name}_invalid")
            continue
        for index, item in enumerate(raw, start=1):
            if not _is_non_empty_string(item, 3):
                missing.append(f"pre_delivery_gap_review:{field_name}:{index}")
    if value.get("ready_to_present") is not True:
        blocked.append("pre_delivery_gap_review:ready_to_present_required")
    if not _has_meaningful_policy_text(value.get("review_summary"), min_length=16):
        missing.append("pre_delivery_gap_review:review_summary_invalid")
    issues_remaining = value.get("issues_remaining")
    if isinstance(issues_remaining, list) and issues_remaining:
        blocked.append("pre_delivery_gap_review:issues_remaining")


def _validate_plan_sufficiency_report(
    value: Any,
    *,
    unresolved_gap_count: int,
    missing: list[str],
    blocked: list[str],
) -> tuple[str, bool]:
    if not isinstance(value, dict):
        missing.append("plan_sufficiency_report:not_object")
        return "", False
    for field_name in PLAN_SUFFICIENCY_REQUIRED_FIELDS:
        if field_name not in value:
            missing.append(f"plan_sufficiency_report:{field_name}")

    status = str(value.get("status", "")).strip()
    if status not in PLAN_STATUS_VALUES:
        missing.append("plan_sufficiency_report:status_invalid")
    for field_name in ("coverage_complete", "integration_realism", "runtime_compatible"):
        if not isinstance(value.get(field_name), bool):
            missing.append(f"plan_sufficiency_report:{field_name}_invalid")
    try:
        unresolved = int(value.get("unresolved_gap_count", -1))
    except Exception:
        unresolved = -1
    if unresolved < 0:
        missing.append("plan_sufficiency_report:unresolved_gap_count_invalid")
    elif unresolved != unresolved_gap_count:
        missing.append("plan_sufficiency_report:unresolved_gap_count_mismatch")
    verifier_notes = value.get("verifier_notes")
    if not isinstance(verifier_notes, list):
        missing.append("plan_sufficiency_report:verifier_notes_invalid")

    runtime_compatible = value.get("runtime_compatible") is True
    if status == "execution_ready" and unresolved_gap_count > 0:
        missing.append("plan_sufficiency_report:execution_ready_with_unresolved_gaps")
    if status == "execution_ready" and not runtime_compatible:
        blocked.append("plan_sufficiency_report:execution_ready_without_runtime_compatibility")
    return status, runtime_compatible


def _validate_requirement_risk_rank(
    value: Any,
    *,
    requirement_ids: set[str],
    packet_ids: set[str],
    missing: list[str],
    blocked: list[str],
) -> None:
    if not isinstance(value, list) or not value:
        missing.append("requirement_risk_rank")
        return
    ranked: set[str] = set()
    for idx, item in enumerate(value, start=1):
        prefix = f"requirement_risk_rank:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue
        for field_name in REQUIREMENT_RISK_RANK_REQUIRED_FIELDS:
            if field_name not in item:
                missing.append(f"{prefix}:{field_name}")
        requirement_id = str(item.get("requirement_id", "")).strip()
        if requirement_id not in requirement_ids:
            blocked.append(f"{prefix}:requirement_id_unknown")
        else:
            ranked.add(requirement_id)
        priority = str(item.get("priority", "")).strip()
        if priority not in REQUIREMENT_PRIORITIES:
            missing.append(f"{prefix}:priority_invalid")
        for packet_id in to_string_list(item.get("associated_packet_ids")):
            if packet_id not in packet_ids:
                blocked.append(f"{prefix}:packet_id_unknown:{packet_id}")
        if not _is_non_empty_string(item.get("evidence_type"), 3):
            missing.append(f"{prefix}:evidence_type_invalid")
        if not _is_non_empty_string(item.get("failure_impact"), 8):
            missing.append(f"{prefix}:failure_impact_invalid")

    for requirement_id in sorted(requirement_ids - ranked):
        missing.append(f"requirement_risk_rank:missing_requirement:{requirement_id}")


def _validate_session_harness(
    value: Any,
    *,
    packet_ids: set[str],
    missing: list[str],
    blocked: list[str],
) -> bool:
    if not isinstance(value, dict):
        missing.append("session_harness:not_object")
        return False

    for field_name in SESSION_HARNESS_REQUIRED_FIELDS:
        if field_name not in value:
            missing.append(f"session_harness:{field_name}")

    required = value.get("required")
    if not isinstance(required, bool):
        missing.append("session_harness:required_invalid")
        required = False

    route_hint = str(value.get("route_hint", "")).strip()
    if route_hint not in SESSION_ROUTE_HINTS:
        missing.append("session_harness:route_hint_invalid")

    try:
        estimated_packet_count = int(value.get("estimated_packet_count", -1))
    except Exception:
        estimated_packet_count = -1
    if estimated_packet_count <= 0:
        missing.append("session_harness:estimated_packet_count_invalid")
    elif packet_ids and estimated_packet_count != len(packet_ids):
        missing.append("session_harness:estimated_packet_count_mismatch")

    try:
        expected_duration_minutes = int(value.get("expected_duration_minutes", -1))
    except Exception:
        expected_duration_minutes = -1
    if expected_duration_minutes < 0:
        missing.append("session_harness:expected_duration_minutes_invalid")

    try:
        checkpoint_interval_minutes = int(value.get("checkpoint_interval_minutes", -1))
    except Exception:
        checkpoint_interval_minutes = -1
    if checkpoint_interval_minutes <= 0:
        missing.append("session_harness:checkpoint_interval_minutes_invalid")

    for key in ("checkpoint_required", "context_index_required", "ui_evidence_required"):
        if not isinstance(value.get(key), bool):
            missing.append(f"session_harness:{key}_invalid")

    if not to_string_list(value.get("bootstrap_commands")):
        missing.append("session_harness:bootstrap_commands_invalid")
    if not to_string_list(value.get("validation_commands")):
        missing.append("session_harness:validation_commands_invalid")
    if not to_string_list(value.get("clean_state_assertions")):
        missing.append("session_harness:clean_state_assertions_invalid")

    if route_hint in {"R3", "R4"} and required is not True:
        missing.append("session_harness:required_for_route")
    if route_hint == "R2" and (estimated_packet_count > 1 or expected_duration_minutes > 20) and required is not True:
        missing.append("session_harness:required_for_long_running_r2")
    if required:
        if value.get("checkpoint_required") is not True:
            missing.append("session_harness:checkpoint_required_when_enabled")
        if value.get("context_index_required") is not True:
            missing.append("session_harness:context_index_required_when_enabled")
    return required is True


def _validate_packets(
    packets_value: Any,
    required_packets_value: Any,
    missing: list[str],
    blocked: list[str],
) -> dict[str, Any]:
    packet_missing, packet_blocked, normalized = validate_packet_dag(packets_value, required_packets_value)
    missing.extend(packet_missing)
    blocked.extend(packet_blocked)
    for packet in normalized:
        packet_id = str(packet.get("packet_id", "")).strip() or "unknown"
        prefix = f"packets:{packet_id}"
        _validate_runtime_definition_of_done(packet.get("definition_of_done"), missing, prefix)
    normalized_packets = {
        str(packet.get("packet_id", "")).strip(): packet
        for packet in normalized
        if isinstance(packet, dict) and str(packet.get("packet_id", "")).strip()
    }
    for packet_id, packet in normalized_packets.items():
        _validate_packet_definition_of_done(
            packet.get("definition_of_done"),
            prefix=f"packet:{packet_id}",
            packet_allowed_scope=_as_string_list(packet.get("allowed_scope")),
            missing=missing,
            blocked=blocked,
        )
    return normalized_packets


def _verify_json_artifact_path(
    *,
    path_value: Any,
    field_prefix: str,
    artifacts_root: Path,
    missing: list[str],
    blocked: list[str],
    cwd: str | None = None,
) -> dict[str, Any] | None:
    path_s = str(path_value or "").strip()
    if not path_s:
        missing.append(f"{field_prefix}:artifact_path")
        return None
    try:
        artifact_path = resolve_proof_path(path_s, artifacts_root, cwd)
    except ValidationError as exc:
        blocked.append(f"{field_prefix}:{exc}")
        return None
    if not artifact_path.exists() or not artifact_path.is_file():
        missing.append(f"{field_prefix}:artifact_missing")
        return None
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except Exception:
        blocked.append(f"{field_prefix}:artifact_not_json")
        return None
    if not isinstance(payload, dict):
        blocked.append(f"{field_prefix}:artifact_not_object")
        return None
    return payload


def plan_artifact_paths(*, artifacts_root: Path, track_id: str) -> dict[str, Path]:
    track_token = sanitize_token(track_id)
    base = artifacts_root / track_token
    return {
        "objective_intent": base / "objective.intent.json",
        "intent": base / "plan.intent.json",
        "compiler": base / "plan.compiler.json",
        "gaps": base / "plan.gaps.json",
        "coverage": base / "plan.coverage.json",
        "sufficiency": base / "plan.sufficiency.json",
        "readiness": base / "plan.readiness.json",
    }


def session_artifact_paths(*, artifacts_root: Path, track_id: str) -> dict[str, Path]:
    track_token = sanitize_token(track_id)
    base = artifacts_root / track_token
    return {
        "session": base / "objective.session.json",
        "feature_list": base / "objective.feature-list.json",
        "progress": base / "objective.progress.jsonl",
        "checkpoint": base / "objective.checkpoint.json",
        "context_index": base / "objective.context-index.json",
        "momentum": base / "objective.momentum.json",
        "blockers": base / "objective.blockers.json",
    }


def runtime_artifact_paths(*, artifacts_root: Path, track_id: str) -> dict[str, Path]:
    track_token = sanitize_token(track_id)
    base = artifacts_root / track_token
    return {
        "runtime_state": base / "objective.runtime-state.json",
        "packet_dag": base / "objective.packet-dag.json",
        "status": base / "objective.status.json",
        "schedule": base / "objective.schedule.json",
        "summary": base / "objective.summary.json",
        "execution_plan": base / "objective.execution-plan.json",
        "kernel_runtime_state": base / "objective.kernel-runtime-state.json",
        "transition_history": base / "objective.transition-history.jsonl",
        "verification_results": base / "objective.verification-results.jsonl",
        "invalid_transition": base / "objective.invalid-transition.json",
        "validation_plan": base / "objective.validation-plan.json",
        "repo_capabilities": base / "objective.repo-capabilities.json",
        "packet_quality": base / "objective.packet-quality.json",
        "execution_coverage": base / "objective.execution-coverage.json",
        "support_confidence": base / "objective.support-confidence.json",
        "packet_results": base / "objective.packet-results.jsonl",
        "execution_ledger": base / "objective.execution-ledger.json",
        "adaptation_log": base / "objective.adaptation-log.jsonl",
        "operator_view": base / "objective.operator-view.json",
        "benchmark": base / "objective.benchmark.json",
        "canary": base / "objective.canary.json",
        "transaction_state": base / "objective.transaction-state.json",
        "transaction_log": base / "objective.transaction-log.jsonl",
        "transactions": base / "transactions",
        "lock": base / ".objective-runtime.lock",
}


def transaction_artifact_paths(*, artifacts_root: Path, track_id: str, transaction_id: str) -> dict[str, Path]:
    track_token = sanitize_token(track_id)
    tx_token = sanitize_token(transaction_id)
    base = artifacts_root / track_token / "transactions" / tx_token
    return {
        "root": base,
        "staged": base / "staged",
    }


def _packet_priority(*, packet_id: str, required_packets: set[str], packet: dict[str, Any]) -> str:
    if packet_id in required_packets:
        return "high"
    classification = str(packet.get("classification") or "").strip()
    if classification in {"blocked_authority", "blocked_dependency"}:
        return "medium"
    return "normal"


def _verification_scope_for_packet(packet: dict[str, Any]) -> str:
    strategy = str(packet.get("execution_strategy") or "").strip()
    lane = str(packet.get("packet_lane") or "").strip()
    if strategy in {
        "validation_command",
        "lint_command",
        "typecheck_command",
        "build_command",
        "smoke_command",
        "schema_check_command",
        "test_command",
    }:
        return "targeted"
    if lane in {"reviewer", "validator"}:
        return "broad"
    return "local"


def _escalation_on_failure_for_packet(packet: dict[str, Any]) -> str:
    classification = str(packet.get("classification") or "").strip()
    if classification == "blocked_authority":
        return "needs_human_decision"
    if classification == "blocked_dependency":
        return "accepted_blocked"
    return "retry_then_block"


def _missing_required_fields(record: dict[str, Any], required_fields: tuple[str, ...], prefix: str) -> list[str]:
    missing: list[str] = []
    for field in required_fields:
        if field not in record:
            missing.append(f"{prefix}:{field}")
    return missing


def _execution_plan_compile_error(code: str, detail: str) -> ExecutionPlanCompileError:
    return ExecutionPlanCompileError(f"{code}:{detail}")


def _coerce_non_negative_int(*, value: Any, field_name: str, packet_id: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise _execution_plan_compile_error("invalid_integer", f"{packet_id}:{field_name}") from None
    if parsed < 0:
        raise _execution_plan_compile_error("negative_integer", f"{packet_id}:{field_name}")
    return parsed


def validate_execution_plan_contract(execution_plan: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    errors.extend(_missing_required_fields(execution_plan, EXECUTION_PLAN_REQUIRED_FIELDS, "execution_plan"))
    if execution_plan.get("schema_version") != EXECUTION_PLAN_SCHEMA_VERSION:
        errors.append("execution_plan:schema_version_invalid")
    units = execution_plan.get("units")
    if not isinstance(units, list):
        return [*errors, "execution_plan:units_not_list"]
    for idx, unit in enumerate(units, start=1):
        prefix = f"execution_plan:unit:{idx}"
        if not isinstance(unit, dict):
            errors.append(f"{prefix}:not_object")
            continue
        errors.extend(_missing_required_fields(unit, EXECUTION_PLAN_UNIT_REQUIRED_FIELDS, prefix))
        if str(unit.get("unit_id") or "").strip() == "":
            errors.append(f"{prefix}:unit_id_blank")
        if str(unit.get("unit_type") or "").strip() == "":
            errors.append(f"{prefix}:unit_type_blank")
        if unit.get("priority") not in EXECUTION_PLAN_PRIORITY_VALUES:
            errors.append(f"{prefix}:priority_invalid")
        if not isinstance(unit.get("required"), bool):
            errors.append(f"{prefix}:required_invalid")
        allowed_scope = to_string_list(unit.get("allowed_scope"))
        candidate_files = to_string_list(unit.get("candidate_files"))
        if not allowed_scope:
            errors.append(f"{prefix}:allowed_scope_empty")
        if sorted(candidate_files) != sorted(allowed_scope):
            errors.append(f"{prefix}:candidate_files_scope_mismatch")
        if not to_string_list(unit.get("acceptance_checks")):
            errors.append(f"{prefix}:acceptance_checks_empty")
        if str(unit.get("verification_scope") or "").strip() not in EXECUTION_PLAN_VERIFICATION_SCOPES:
            errors.append(f"{prefix}:verification_scope_invalid")
        if str(unit.get("completion_test") or "").strip() == "":
            errors.append(f"{prefix}:completion_test_blank")
        mutation_budget = unit.get("mutation_budget") if isinstance(unit.get("mutation_budget"), dict) else {}
        if not isinstance(mutation_budget.get("max_files"), int) or mutation_budget.get("max_files", -1) < 0:
            errors.append(f"{prefix}:mutation_budget:max_files_invalid")
        if not isinstance(mutation_budget.get("max_attempts"), int) or mutation_budget.get("max_attempts", -1) < 0:
            errors.append(f"{prefix}:mutation_budget:max_attempts_invalid")
    return errors


def build_execution_plan(*, plan: dict[str, Any], track_id: str) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise _execution_plan_compile_error("invalid_plan", "payload_not_object")
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise _execution_plan_compile_error("invalid_plan_schema", str(plan.get("schema_version") or "missing"))
    track_token = str(track_id or "").strip()
    if not track_token:
        raise _execution_plan_compile_error("invalid_track_id", "blank")
    objective_id = stable_objective_id(track_id)
    required_packets = {
        str(item).strip()
        for item in plan.get("required_packets", [])
        if str(item).strip()
    }
    packets = plan.get("packets")
    if not isinstance(packets, list):
        raise _execution_plan_compile_error("invalid_packets", "packets_not_list")
    units: list[dict[str, Any]] = []
    authored_packet_ids: list[str] = []
    authored_packet_id_set: set[str] = set()
    for idx, packet in enumerate(packets, start=1):
        if not isinstance(packet, dict):
            raise _execution_plan_compile_error("invalid_packet", f"index_{idx}:not_object")
        packet_id = str(packet.get("packet_id") or "").strip()
        if not packet_id:
            raise _execution_plan_compile_error("invalid_packet_id", f"index_{idx}:blank")
        if packet_id in authored_packet_id_set:
            raise _execution_plan_compile_error("duplicate_packet_id", packet_id)
        authored_packet_ids.append(packet_id)
        authored_packet_id_set.add(packet_id)
        allowed_scope = to_string_list(packet.get("allowed_scope"))
        if not allowed_scope:
            raise _execution_plan_compile_error("invalid_allowed_scope", packet_id)
        definition_of_done = packet.get("definition_of_done") if isinstance(packet.get("definition_of_done"), dict) else {}
        dod_allowed_scope = to_string_list(definition_of_done.get("allowed_scope"))
        if dod_allowed_scope and sorted(dod_allowed_scope) != sorted(allowed_scope):
            raise _execution_plan_compile_error("scope_drift", packet_id)
        acceptance_checks = to_string_list(packet.get("acceptance_checks"))
        dod_acceptance_checks = to_string_list(definition_of_done.get("acceptance_checks"))
        if dod_acceptance_checks and acceptance_checks and dod_acceptance_checks != acceptance_checks:
            raise _execution_plan_compile_error("acceptance_drift", packet_id)
        max_retries = _coerce_non_negative_int(value=packet.get("max_retries", 2) or 2, field_name="max_retries", packet_id=packet_id)
        completion_test = str(
            (
                definition_of_done.get("verifier_acceptance_condition")
                if definition_of_done
                else ""
            )
            or packet.get("completion_test")
            or ""
        ).strip()
        if not completion_test:
            completion_test = acceptance_checks[0] if acceptance_checks else packet_id
        if not completion_test:
            raise _execution_plan_compile_error("missing_completion_test", packet_id)
        verification_scope = _verification_scope_for_packet(packet)
        if verification_scope not in EXECUTION_PLAN_VERIFICATION_SCOPES:
            raise _execution_plan_compile_error("invalid_verification_scope", packet_id)
        units.append(
            {
                "unit_id": packet_id,
                "unit_type": str(packet.get("execution_strategy") or packet.get("packet_lane") or "packet").strip(),
                "priority": _packet_priority(packet_id=packet_id, required_packets=required_packets, packet=packet),
                "required": packet_id in required_packets,
                "candidate_files": allowed_scope,
                "allowed_scope": allowed_scope,
                "dependencies": to_string_list(packet.get("dependencies")),
                "max_retries": max_retries,
                "mutation_budget": {
                    "max_files": len(allowed_scope),
                    "max_attempts": max_retries,
                },
                "verification_scope": verification_scope,
                "acceptance_checks": acceptance_checks,
                "failure_signals": to_string_list(packet.get("failure_signals")),
                "escalation_on_failure": _escalation_on_failure_for_packet(packet),
                "completion_test": completion_test,
            }
        )
    missing_required_packets = sorted(required_packets - authored_packet_id_set)
    if missing_required_packets:
        raise _execution_plan_compile_error("required_packet_missing", ",".join(missing_required_packets))
    payload = {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "objective_id": objective_id,
        "track_id": track_id,
        "units": units,
    }
    contract_errors = validate_execution_plan_contract(payload)
    if contract_errors:
        raise _execution_plan_compile_error("contract_invalid", ",".join(contract_errors))
    compiled_unit_ids = [str(unit.get("unit_id") or "").strip() for unit in units if isinstance(unit, dict)]
    if compiled_unit_ids != authored_packet_ids:
        raise _execution_plan_compile_error("identity_drift", "unit_id_sequence_mismatch")
    return payload


def cycle_dir(*, artifacts_root: Path, track_id: str, cycle_id: str) -> Path:
    track_token = sanitize_token(track_id)
    cycle_token = sanitize_token(cycle_id)
    return artifacts_root / track_token / "cycles" / cycle_token


def cycle_artifact_paths(*, artifacts_root: Path, track_id: str, cycle_id: str) -> dict[str, Path]:
    base = cycle_dir(artifacts_root=artifacts_root, track_id=track_id, cycle_id=cycle_id)
    return {
        "request": base / "cycle.request.json",
        "result": base / "cycle.result.json",
        "review": base / "cycle.review.json",
        "state": base / "cycle.state.json",
    }


def packet_definition_path(*, artifacts_root: Path, track_id: str, packet_id: str) -> Path:
    track_token = sanitize_token(track_id)
    packet_token = sanitize_token(packet_id)
    return artifacts_root / track_token / "packets" / f"{packet_token}.json"


def packet_verdict_path(*, artifacts_root: Path, track_id: str, packet_id: str) -> Path:
    track_token = sanitize_token(track_id)
    packet_token = sanitize_token(packet_id)
    return artifacts_root / track_token / "packets" / f"{packet_token}.verdict.json"


def default_clarification_governor() -> dict[str, Any]:
    return {
        "default_batch_limit": 1,
        "allowed_topics": [
            "product_meaning",
            "authority_security_boundaries",
            "missing_success_criteria",
            "non_discoverable_tradeoffs",
        ],
        "repo_discoverable_questions_forbidden": True,
        "new_authority_boundary_required_for_mid_execution_clarification": True,
    }


def _normalize_scope_boundaries(value: Any) -> dict[str, list[str]]:
    payload = value if isinstance(value, dict) else {}
    return {
        "in_scope": to_string_list(payload.get("in_scope")),
        "out_of_scope": to_string_list(payload.get("out_of_scope")),
    }


def _normalize_resolution_log(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, str]] = []
    for idx, item in enumerate(value, start=1):
        if isinstance(item, dict):
            question = str(item.get("question", "")).strip()
            resolution = str(item.get("resolution", "")).strip()
            source = str(item.get("source", "")).strip()
        else:
            question = str(item).strip()
            resolution = ""
            source = ""
        if not question:
            continue
        rows.append(
            {
                "entry_id": f"resolution-{idx:03d}",
                "question": question,
                "resolution": resolution,
                "source": source,
            }
        )
    return rows


def build_objective_intent_payload(
    *,
    track_id: str,
    plan: dict[str, Any] | None = None,
    request_payload: dict[str, Any] | None = None,
    request_text: str | None = None,
) -> dict[str, Any]:
    if plan is not None:
        intent_contract = plan.get("intent_contract") if isinstance(plan.get("intent_contract"), dict) else {}
        clarification_governor = (
            plan.get("clarification_governor")
            if isinstance(plan.get("clarification_governor"), dict)
            else default_clarification_governor()
        )
        scope_boundaries = _normalize_scope_boundaries(plan.get("scope_boundaries"))
        objective = str(intent_contract.get("objective") or plan.get("objective") or "").strip()
        success_criteria = to_string_list(intent_contract.get("success_criteria"))
        authority_sensitive_decisions = to_string_list(intent_contract.get("authority_sensitive_decisions"))
        ambiguity_items = intent_contract.get("ambiguity_classification")
        ambiguity_classification = ambiguity_items if isinstance(ambiguity_items, list) else []
        known_unknowns = [
            str(item.get("statement", "")).strip()
            for item in ambiguity_classification
            if isinstance(item, dict)
            and str(item.get("classification", "")).strip() in {"technical_reversible", "product_authority"}
            and str(item.get("statement", "")).strip()
        ]
        discoverable_unknowns = [
            str(item.get("statement", "")).strip()
            for item in ambiguity_classification
            if isinstance(item, dict)
            and str(item.get("classification", "")).strip() == "discoverable"
            and str(item.get("statement", "")).strip()
        ]
        objective_shape_status = str(intent_contract.get("objective_shape_status", "")).strip() or "revise_required"
        audience = to_string_list(plan.get("audience"))
        raw_request = str(plan.get("objective") or objective).strip()
        source_type = "plan_json"
        normalization_source = "plan_contract"
        clarification_questions: list[str] = []
        clarification_batch_count = 0
        discoverable_resolution_log = [
            {
                "entry_id": f"resolution-{idx:03d}",
                "question": item,
                "resolution": "Recovered from validated plan payload.",
                "source": "plan_json",
            }
            for idx, item in enumerate(discoverable_unknowns, start=1)
        ]
    else:
        payload = request_payload if isinstance(request_payload, dict) else {}
        raw_request = str(
            request_text
            or payload.get("raw_request")
            or payload.get("request")
            or payload.get("objective")
            or ""
        ).strip()
        objective = str(payload.get("objective") or raw_request).strip()
        success_criteria = to_string_list(payload.get("success_criteria"))
        audience = to_string_list(payload.get("audience"))
        authority_sensitive_decisions = to_string_list(payload.get("authority_sensitive_decisions"))
        known_unknowns = to_string_list(payload.get("known_unknowns"))
        discoverable_unknowns = to_string_list(payload.get("discoverable_unknowns"))
        scope_boundaries = _normalize_scope_boundaries(
            payload.get("scope_boundaries")
            or {"in_scope": payload.get("in_scope"), "out_of_scope": payload.get("out_of_scope")}
        )
        clarification_governor = (
            payload.get("clarification_governor")
            if isinstance(payload.get("clarification_governor"), dict)
            else default_clarification_governor()
        )
        source_type = "request_payload" if payload else "request_text"
        normalization_source = str(payload.get("normalization_source") or source_type).strip() or source_type
        objective_shape_status = str(payload.get("objective_shape_status", "")).strip()
        if objective_shape_status not in OBJECTIVE_SHAPE_STATUS_VALUES:
            if objective and success_criteria:
                objective_shape_status = (
                    "accepted_rewritten"
                    if request_text or normalization_source.startswith("model_")
                    else "accepted_as_given"
                )
            else:
                objective_shape_status = "revise_required"
        ambiguity_classification = [
            {
                "item_id": f"discoverable-{idx:03d}",
                "statement": item,
                "classification": "discoverable",
            }
            for idx, item in enumerate(discoverable_unknowns, start=1)
        ] + [
            {
                "item_id": f"known-{idx:03d}",
                "statement": item,
                "classification": "technical_reversible",
            }
            for idx, item in enumerate(known_unknowns, start=1)
        ]
        clarification_questions = to_string_list(payload.get("clarification_questions"))
        try:
            clarification_batch_count = max(0, int(payload.get("clarification_batch_count", 0) or 0))
        except Exception:
            clarification_batch_count = 0
        discoverable_resolution_log = _normalize_resolution_log(payload.get("discoverable_resolution_log"))

    clarification_reasons: list[str] = []
    if not success_criteria:
        clarification_reasons.append("missing_success_criteria")
    if objective_shape_status == "revise_required":
        clarification_reasons.append("objective_shape_revision_required")
    if objective_shape_status == "blocked":
        clarification_reasons.append("authority_or_security_blocked")
    if clarification_questions and "frontloaded_clarification_required" not in clarification_reasons:
        clarification_reasons.append("frontloaded_clarification_required")
    clarification_needed = bool(clarification_reasons or clarification_questions)
    return {
        "schema_version": OBJECTIVE_INTENT_SCHEMA_VERSION,
        "track_id": track_id,
        "track_token": sanitize_token(track_id),
        "objective_id": stable_objective_id(track_id),
        "source_type": source_type,
        "normalization_source": normalization_source,
        "raw_request": raw_request,
        "objective": objective,
        "success_criteria": success_criteria,
        "audience": audience,
        "scope_boundaries": scope_boundaries,
        "authority_sensitive_decisions": authority_sensitive_decisions,
        "known_unknowns": known_unknowns,
        "discoverable_unknowns": discoverable_unknowns,
        "ambiguity_classification": ambiguity_classification,
        "objective_shape_status": objective_shape_status,
        "clarification_governor": clarification_governor,
        "clarification_needed": clarification_needed,
        "clarification_reasons": clarification_reasons,
        "clarification_questions": clarification_questions,
        "clarification_batch_count": clarification_batch_count,
        "discoverable_resolution_log": discoverable_resolution_log,
        "intent_contract": {
            "objective": objective,
            "success_criteria": success_criteria,
            "non_goals": to_string_list(plan.get("non_goals")) if isinstance(plan, dict) else to_string_list((request_payload or {}).get("non_goals")),
            "authority_sensitive_decisions": authority_sensitive_decisions,
            "ambiguity_classification": ambiguity_classification,
            "objective_shape_status": objective_shape_status,
        },
    }


def write_objective_intent_artifact(
    *,
    track_id: str,
    artifacts_root: Path,
    plan: dict[str, Any] | None = None,
    request_payload: dict[str, Any] | None = None,
    request_text: str | None = None,
) -> tuple[dict[str, Any], Path]:
    payload = build_objective_intent_payload(
        track_id=track_id,
        plan=plan,
        request_payload=request_payload,
        request_text=request_text,
    )
    path = plan_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["objective_intent"]
    write_json_file(path, payload)
    return payload, path


def normalize_objective_intent_for_planning(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    intent_contract = payload.get("intent_contract") if isinstance(payload.get("intent_contract"), dict) else {}
    objective_shape_status = str(
        payload.get("objective_shape_status") or intent_contract.get("objective_shape_status") or ""
    ).strip()
    if objective_shape_status in {"accepted_as_given", "accepted_rewritten"}:
        objective_shape_status = "accepted"
    return {
        "objective": str(payload.get("objective") or intent_contract.get("objective") or "").strip(),
        "success_criteria": to_string_list(payload.get("success_criteria") or intent_contract.get("success_criteria")),
        "scope_boundaries": _normalize_scope_boundaries(payload.get("scope_boundaries")),
        "authority_sensitive_decisions": to_string_list(
            payload.get("authority_sensitive_decisions") or intent_contract.get("authority_sensitive_decisions")
        ),
        "objective_shape_status": objective_shape_status,
        "clarification_governor": (
            payload.get("clarification_governor")
            if isinstance(payload.get("clarification_governor"), dict)
            else default_clarification_governor()
        ),
        "intent_contract": {
            "objective": str(intent_contract.get("objective") or payload.get("objective") or "").strip(),
            "success_criteria": to_string_list(intent_contract.get("success_criteria") or payload.get("success_criteria")),
            "non_goals": to_string_list(intent_contract.get("non_goals")),
            "authority_sensitive_decisions": to_string_list(
                intent_contract.get("authority_sensitive_decisions") or payload.get("authority_sensitive_decisions")
            ),
            "objective_shape_status": objective_shape_status,
        },
    }


def objective_intent_matches_plan(*, plan: dict[str, Any], objective_intent: dict[str, Any], track_id: str) -> bool:
    expected = build_objective_intent_payload(track_id=track_id, plan=plan)
    return _matches_plan_value(
        normalize_objective_intent_for_planning(expected),
        normalize_objective_intent_for_planning(objective_intent),
    )


def build_preplan_session_artifacts(
    objective_intent: dict[str, Any],
    *,
    track_id: str,
    cwd: str | None = None,
) -> dict[str, Any]:
    objective_id = stable_objective_id(track_id)
    track_token = sanitize_token(track_id)
    repo_state = _git_repo_state(cwd=cwd)
    context_source, context_source_path, categories = _context_index_from_repo(cwd=cwd, track_id=track_id)
    checkpoint_id = f"{track_token}-checkpoint-000"
    current_frontier: list[str] = []
    progress_events = [
        {
            "schema_version": "objective-progress-event.v1",
            "event_type": "session_initialized",
            "timestamp": now_iso(),
            "objective_id": objective_id,
            "track_id": track_id,
            "checkpoint_id": checkpoint_id,
            "current_frontier": current_frontier,
            "feature_status_summary": {},
        },
        {
            "schema_version": "objective-progress-event.v1",
            "event_type": "checkpoint",
            "timestamp": now_iso(),
            "objective_id": objective_id,
            "track_id": track_id,
            "checkpoint_id": checkpoint_id,
            "last_verified_packet_ids": [],
            "current_frontier": current_frontier,
            "next_recommended_packet": "",
        },
    ]
    checkpoint_payload = {
        "schema_version": "objective-checkpoint.v1",
        "objective_id": objective_id,
        "track_id": track_id,
        "checkpoint_id": checkpoint_id,
        "last_verified_packet_ids": [],
        "current_frontier": current_frontier,
        "bootstrap_commands": [],
        "validation_commands": [],
        "repo_state_summary": repo_state,
        "clean_state_assertions": ["Runtime state is resumable from objective runtime artifacts."],
        "next_recommended_packet": "",
        "open_risks": [],
        "handoff_notes": f"Resume {objective_id} from runtime artifacts without relying on chat history.",
        "last_forward_movement": "session_initialized",
        "stagnation_risk": "low",
        "escalation_candidates": [],
        "checkpoint_strategy": "git_checkpoint_required",
        "checkpoint_attempted_at": "",
        "rollback_validation_ref": "",
        "checkpoint_commit": "",
        "checkpoint_blocked": False,
        "checkpoint_block_reason": "",
        "checkpoint_block_evidence": "",
        "ui_evidence_required": False,
        "feature_ledger_ref": "objective.feature-list.json",
        "progress_log_ref": "objective.progress.jsonl",
        "updated_at": now_iso(),
    }
    return {
        "session": {
            "schema_version": "objective-session.v1",
            "objective_id": objective_id,
            "track_id": track_id,
            "track_token": track_token,
            "route_hint": "",
            "session_harness_required": True,
            "checkpoint_required": True,
            "context_index_required": True,
            "checkpoint_interval_minutes": 20,
            "estimated_packet_count": 0,
            "expected_duration_minutes": 0,
            "ui_evidence_required": False,
            "requirement_ids": [],
            "packet_ids": [],
            "current_frontier": current_frontier,
            "safe_momentum_ready": False,
            "bootstrap_commands": [],
            "validation_commands": [],
            "clean_state_assertions": checkpoint_payload["clean_state_assertions"],
            "repo_state_summary": repo_state,
            "objective_summary": objective_intent.get("objective", ""),
            "objective_shape_status": objective_intent.get("objective_shape_status", ""),
        },
        "checkpoint": checkpoint_payload,
        "context_index": {
            "schema_version": "objective-context-index.v1",
            "objective_id": objective_id,
            "track_id": track_id,
            "source": context_source,
            "source_path": context_source_path,
            "categories": categories,
            "active_objective_docs": sorted(
                set(categories.get("active_objective_docs", []))
                | {f"planning_artifacts/{track_token}/objective.intent.json"}
            ),
        },
        "progress": progress_events,
    }


def write_preplan_session_artifacts(
    objective_intent: dict[str, Any],
    *,
    track_id: str,
    artifacts_root: Path,
    cwd: str | None = None,
) -> dict[str, str]:
    payloads = build_preplan_session_artifacts(objective_intent, track_id=track_id, cwd=cwd)
    paths = session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    write_json_file(paths["session"], payloads["session"])
    write_json_file(paths["checkpoint"], payloads["checkpoint"])
    write_json_file(paths["context_index"], payloads["context_index"])
    write_text_atomic(
        paths["progress"],
        "\n".join(json.dumps(item, sort_keys=True) for item in payloads["progress"]) + "\n",
    )
    return {
        "session": str(paths["session"]),
        "checkpoint": str(paths["checkpoint"]),
        "context_index": str(paths["context_index"]),
        "progress": str(paths["progress"]),
    }


def _git_repo_state(*, cwd: str | None) -> dict[str, Any]:
    root = Path(cwd or os.getcwd()).resolve()

    def run_git(*args: str) -> str:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception:
            return ""
        if proc.returncode != 0:
            return ""
        return proc.stdout.strip()

    git_root = run_git("rev-parse", "--show-toplevel")
    if not git_root:
        return {
            "cwd": str(root),
            "is_git_repo": False,
            "repo_root": "",
            "branch": "",
            "status_short": [],
        }

    status_short = [line.strip() for line in run_git("status", "--short").splitlines() if line.strip()]
    return {
        "cwd": str(root),
        "is_git_repo": True,
        "repo_root": git_root,
        "branch": run_git("rev-parse", "--abbrev-ref", "HEAD"),
        "status_short": status_short,
    }


def _context_index_from_repo(*, cwd: str | None, track_id: str) -> tuple[str, str, dict[str, list[str]]]:
    root = Path(cwd or os.getcwd()).resolve()
    repo_index = root / "docs" / "context.index.json"
    if repo_index.exists() and repo_index.is_file():
        try:
            payload = json.loads(repo_index.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if isinstance(payload, dict):
            categories = payload.get("categories")
            if isinstance(categories, dict):
                normalized = {
                    key: to_string_list(categories.get(key))
                    for key in CONTEXT_INDEX_CATEGORIES
                }
            else:
                normalized = {
                    key: to_string_list(payload.get(key))
                    for key in CONTEXT_INDEX_CATEGORIES
                }
            return "repo_local", str(repo_index), normalized

    docs_dir = root / "docs"
    buckets: dict[str, list[str]] = {key: [] for key in CONTEXT_INDEX_CATEGORIES}
    if docs_dir.exists():
        for path in sorted(p for p in docs_dir.rglob("*") if p.is_file()):
            rel = str(path)
            name = path.name.lower()
            if name in {"prompt.md", "plan.md", "implement.md", "status.md"}:
                buckets["execution_docs"].append(rel)
                if track_id:
                    buckets["active_objective_docs"].append(rel)
            elif any(token in name for token in ("arch", "overview", "system")):
                buckets["architecture_docs"].append(rel)
            elif any(token in name for token in ("design", "adr", "decision")):
                buckets["design_docs"].append(rel)
            elif any(token in name for token in ("schema", "contract", "api")):
                buckets["schema_contract_docs"].append(rel)
            elif any(token in name for token in ("test", "runbook")):
                buckets["test_runbook_docs"].append(rel)
            elif any(token in name for token in ("security", "policy")):
                buckets["security_policy_docs"].append(rel)
    return "generated", "", buckets


def _runtime_feature_items(plan: dict[str, Any]) -> list[dict[str, Any]]:
    feature_items = []
    coverage_by_requirement = {
        str(item.get("requirement_id", "")).strip(): item
        for item in plan.get("objective_coverage_map", [])
        if isinstance(item, dict) and str(item.get("requirement_id", "")).strip()
    }
    for requirement in plan.get("objective_requirements", []):
        if not isinstance(requirement, dict):
            continue
        requirement_id = str(requirement.get("requirement_id", "")).strip()
        if not requirement_id:
            continue
        coverage = coverage_by_requirement.get(requirement_id, {})
        feature_items.append(
            {
                "requirement_id": requirement_id,
                "priority": requirement.get("priority"),
                "status": "pending",
                "packet_ids": to_string_list(coverage.get("packet_ids")),
                "evidence_refs": to_string_list(coverage.get("evidence")),
                "definition_of_done": requirement.get("definition_of_done"),
            }
        )
    return feature_items


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _script_runner(root: Path, package_json: dict[str, Any] | None = None) -> str:
    package_manager = ""
    if isinstance(package_json, dict):
        package_manager = str(package_json.get("packageManager") or "").strip().lower()
    elif (root / "package.json").exists():
        package_json = _read_json_if_exists(root / "package.json")
        package_manager = str(package_json.get("packageManager") or "").strip().lower() if package_json else ""
    if package_manager.startswith("bun@") or (root / "bun.lockb").exists():
        return "bun"
    if package_manager.startswith("pnpm@") or (root / "pnpm-lock.yaml").exists() or (root / "pnpm-workspace.yaml").exists():
        return "pnpm"
    if package_manager.startswith("yarn@") or (root / "yarn.lock").exists():
        return "yarn"
    if package_manager.startswith("npm@"):
        return "npm"
    return "npm"


def _first_script_command(root: Path, scripts: dict[str, Any], *names: str, package_json: dict[str, Any] | None = None) -> str:
    runner = _script_runner(root, package_json)
    for name in names:
        value = scripts.get(name)
        if isinstance(value, str) and value.strip():
            if runner == "yarn":
                return f"yarn {name}"
            if runner == "bun":
                return f"bun run {name}"
            return f"{runner} run {name}"
    return ""


def _known_config_exists(root: Path, *names: str) -> str:
    for name in names:
        path = root / name
        if path.exists():
            return str(path)
    return ""


def _workspace_patterns_from_package_json(package_json: dict[str, Any] | None) -> list[str]:
    if not isinstance(package_json, dict):
        return []
    workspaces = package_json.get("workspaces")
    if isinstance(workspaces, list):
        return [str(item).strip() for item in workspaces if str(item).strip()]
    if isinstance(workspaces, dict):
        packages = workspaces.get("packages")
        if isinstance(packages, list):
            return [str(item).strip() for item in packages if str(item).strip()]
    return []


def _workspace_patterns_from_pnpm_workspace(root: Path) -> list[str]:
    workspace_file = root / "pnpm-workspace.yaml"
    if not workspace_file.exists():
        return []
    patterns: list[str] = []
    for line in workspace_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        match = re.match(r"^-\s*['\"]?([^'\"]+)['\"]?$", stripped)
        if match:
            patterns.append(match.group(1).strip())
    return [item for item in patterns if item]


def _iter_workspace_package_jsons(root: Path, package_json: dict[str, Any] | None) -> list[Path]:
    patterns = [
        * _workspace_patterns_from_package_json(package_json),
        * _workspace_patterns_from_pnpm_workspace(root),
    ]
    package_paths: list[Path] = []
    for pattern in patterns:
        for candidate in root.glob(pattern):
            manifest = candidate if candidate.name == "package.json" else candidate / "package.json"
            if manifest.is_file():
                package_paths.append(manifest.resolve())
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in sorted(package_paths):
        if path == (root / "package.json").resolve():
            continue
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def _script_command_for_manifest(root: Path, manifest_dir: Path, script_name: str, package_json: dict[str, Any] | None = None) -> str:
    runner = _script_runner(root, package_json)
    if manifest_dir.resolve() == root.resolve():
        if runner == "yarn":
            return f"yarn {script_name}"
        if runner == "bun":
            return f"bun run {script_name}"
        return f"{runner} run {script_name}"
    rel_path = manifest_dir.resolve().relative_to(root.resolve()).as_posix()
    quoted_rel = shlex.quote(rel_path)
    if runner == "pnpm":
        return f"pnpm --filter ./{rel_path} run {script_name}"
    if runner == "yarn":
        return f"yarn --cwd {quoted_rel} {script_name}"
    if runner == "bun":
        return f"bun --cwd {quoted_rel} run {script_name}"
    return f"npm --prefix {quoted_rel} run {script_name}"


def _workflow_files(root: Path) -> list[Path]:
    workflows = root / ".github" / "workflows"
    if not workflows.exists():
        return []
    return sorted(
        [
            path
            for path in workflows.iterdir()
            if path.is_file() and path.suffix.lower() in {".yml", ".yaml"}
        ]
    )


def _infer_commands_from_ci_text(ci_text: str, *, root: Path, package_json: dict[str, Any] | None = None) -> dict[str, list[str]]:
    lower = ci_text.lower()
    commands: dict[str, list[str]] = {
        "tests": [],
        "lint": [],
        "typecheck": [],
        "build": [],
        "smoke_e2e": [],
        "schema_check": [],
    }
    runner = _script_runner(root, package_json)
    patterns = {
        "tests": [r"\bpython(?:3(?:\.\d+)?)?\s+-m\s+pytest\b", r"\bpytest\b", r"\bgo\s+test\b", rf"\b{runner}\s+run\s+test\b", r"\bmake\s+test\b", r"\btox\b", r"\bnox\b"],
        "lint": [r"\bruff\s+check\b", r"\bflake8\b", r"\bpylint\b", rf"\b{runner}\s+run\s+lint\b", r"\beslint\b", r"\bmake\s+lint\b"],
        "typecheck": [r"\bmypy\b", r"\bpyright\b", rf"\b{runner}\s+run\s+typecheck\b", r"\btsc\s+--noemit\b", r"\bmake\s+typecheck\b"],
        "build": [rf"\b{runner}\s+run\s+build\b", r"\bvite\s+build\b", r"\bnext\s+build\b", r"\bgo\s+build\b", r"\bmake\s+build\b"],
        "smoke_e2e": [r"\bplaywright\s+test\b", r"\bcypress\s+run\b", rf"\b{runner}\s+run\s+(?:e2e|test:e2e|smoke)\b", r"\bmake\s+(?:e2e|smoke)\b"],
        "schema_check": [r"\balembic\s+check\b", r"manage\.py\s+makemigrations\s+--check\s+--dry-run\b", r"\bmake\s+(?:schema-check|migration-check)\b"],
    }
    for lane, lane_patterns in patterns.items():
        for pattern in lane_patterns:
            match = re.search(pattern, ci_text, re.IGNORECASE)
            if match:
                commands[lane].append(match.group(0).strip())
    return {key: list(dict.fromkeys(value)) for key, value in commands.items() if value}


def _append_lane_discovery(
    *,
    capabilities: dict[str, bool],
    lane_commands: dict[str, list[str]],
    confidence_by_lane: dict[str, str],
    source_refs: dict[str, list[str]],
    capability_key: str,
    commands: list[str],
    confidence: str,
    source_path: Path,
) -> None:
    resolved_commands = [str(item).strip() for item in commands if str(item).strip()]
    if not resolved_commands:
        return
    capabilities[capability_key] = True
    lane_commands[capability_key] = list(dict.fromkeys([*lane_commands.get(capability_key, []), *resolved_commands]))
    existing_confidence = str(confidence_by_lane.get(capability_key, "none") or "none").strip()
    ranking = {"none": 0, "low": 1, "medium": 2, "high": 3}
    if ranking.get(confidence, 0) >= ranking.get(existing_confidence, 0):
        confidence_by_lane[capability_key] = confidence
    source_refs.setdefault(capability_key, [])
    source_refs[capability_key].append(str(source_path))


def discover_repo_capabilities(*, cwd: str | None = None) -> dict[str, Any]:
    root = Path(cwd or os.getcwd()).resolve()
    detectors_run: list[str] = []
    capabilities: dict[str, bool] = {
        "tests": False,
        "typecheck": False,
        "lint": False,
        "build": False,
        "smoke_e2e": False,
        "schema_check": False,
        "security_sensitive_review": True,
    }
    lane_commands: dict[str, list[str]] = {key: [] for key in capabilities}
    confidence_by_lane: dict[str, str] = {key: "none" for key in capabilities}
    source_refs: dict[str, list[str]] = {key: [] for key in capabilities}
    missing_capabilities: dict[str, str] = {}

    package_json = _read_json_if_exists(root / "package.json")
    runner = _script_runner(root, package_json)
    if package_json:
        detectors_run.append("node_package_json")
        scripts = package_json.get("scripts") if isinstance(package_json.get("scripts"), dict) else {}
        if _first_script_command(root, scripts, "test", "test:unit", "test:integration", package_json=package_json):
            capabilities["tests"] = True
            lane_commands["tests"] = list(
                dict.fromkeys(
                    [
                        command
                        for command in [
                            _first_script_command(root, scripts, "test", package_json=package_json),
                            _first_script_command(root, scripts, "test:unit", package_json=package_json),
                            _first_script_command(root, scripts, "test:integration", package_json=package_json),
                        ]
                        if command
                    ]
                )
            )
            confidence_by_lane["tests"] = "high"
            source_refs["tests"].append(str(root / "package.json"))
        if _first_script_command(root, scripts, "typecheck", "check-types", package_json=package_json):
            capabilities["typecheck"] = True
            lane_commands["typecheck"] = [
                command
                for command in [
                    _first_script_command(root, scripts, "typecheck", package_json=package_json),
                    _first_script_command(root, scripts, "check-types", package_json=package_json),
                ]
                if command
            ]
            confidence_by_lane["typecheck"] = "high"
            source_refs["typecheck"].append(str(root / "package.json"))
        if _first_script_command(root, scripts, "lint", "lint:check", package_json=package_json):
            capabilities["lint"] = True
            lane_commands["lint"] = [
                command
                for command in [
                    _first_script_command(root, scripts, "lint", package_json=package_json),
                    _first_script_command(root, scripts, "lint:check", package_json=package_json),
                ]
                if command
            ]
            confidence_by_lane["lint"] = "high"
            source_refs["lint"].append(str(root / "package.json"))
        if _first_script_command(root, scripts, "build", package_json=package_json):
            capabilities["build"] = True
            lane_commands["build"] = [_first_script_command(root, scripts, "build", package_json=package_json)]
            confidence_by_lane["build"] = "high"
            source_refs["build"].append(str(root / "package.json"))
        if _first_script_command(root, scripts, "test:e2e", "e2e", "smoke", "playwright", package_json=package_json):
            capabilities["smoke_e2e"] = True
            lane_commands["smoke_e2e"] = [
                command
                for command in [
                    _first_script_command(root, scripts, "test:e2e", package_json=package_json),
                    _first_script_command(root, scripts, "e2e", package_json=package_json),
                    _first_script_command(root, scripts, "smoke", package_json=package_json),
                    _first_script_command(root, scripts, "playwright", package_json=package_json),
                ]
                if command
            ]
            confidence_by_lane["smoke_e2e"] = "high"
            source_refs["smoke_e2e"].append(str(root / "package.json"))
        workspace_package_jsons = _iter_workspace_package_jsons(root, package_json)
        if workspace_package_jsons:
            detectors_run.append("node_workspace_manifests")
            for manifest_path in workspace_package_jsons:
                child_package_json = _read_json_if_exists(manifest_path)
                child_scripts = child_package_json.get("scripts") if isinstance(child_package_json.get("scripts"), dict) else {}
                manifest_dir = manifest_path.parent
                _append_lane_discovery(
                    capabilities=capabilities,
                    lane_commands=lane_commands,
                    confidence_by_lane=confidence_by_lane,
                    source_refs=source_refs,
                    capability_key="tests",
                    commands=[
                        _script_command_for_manifest(root, manifest_dir, "test", package_json),
                        _script_command_for_manifest(root, manifest_dir, "test:unit", package_json),
                        _script_command_for_manifest(root, manifest_dir, "test:integration", package_json),
                    ]
                    if any(name in child_scripts for name in ("test", "test:unit", "test:integration"))
                    else [],
                    confidence="high",
                    source_path=manifest_path,
                )
                _append_lane_discovery(
                    capabilities=capabilities,
                    lane_commands=lane_commands,
                    confidence_by_lane=confidence_by_lane,
                    source_refs=source_refs,
                    capability_key="typecheck",
                    commands=[
                        _script_command_for_manifest(root, manifest_dir, "typecheck", package_json),
                        _script_command_for_manifest(root, manifest_dir, "check-types", package_json),
                    ]
                    if any(name in child_scripts for name in ("typecheck", "check-types"))
                    else [],
                    confidence="high",
                    source_path=manifest_path,
                )
                _append_lane_discovery(
                    capabilities=capabilities,
                    lane_commands=lane_commands,
                    confidence_by_lane=confidence_by_lane,
                    source_refs=source_refs,
                    capability_key="lint",
                    commands=[
                        _script_command_for_manifest(root, manifest_dir, "lint", package_json),
                        _script_command_for_manifest(root, manifest_dir, "lint:check", package_json),
                    ]
                    if any(name in child_scripts for name in ("lint", "lint:check"))
                    else [],
                    confidence="high",
                    source_path=manifest_path,
                )
                _append_lane_discovery(
                    capabilities=capabilities,
                    lane_commands=lane_commands,
                    confidence_by_lane=confidence_by_lane,
                    source_refs=source_refs,
                    capability_key="build",
                    commands=[_script_command_for_manifest(root, manifest_dir, "build", package_json)]
                    if "build" in child_scripts
                    else [],
                    confidence="high",
                    source_path=manifest_path,
                )
                _append_lane_discovery(
                    capabilities=capabilities,
                    lane_commands=lane_commands,
                    confidence_by_lane=confidence_by_lane,
                    source_refs=source_refs,
                    capability_key="smoke_e2e",
                    commands=[
                        _script_command_for_manifest(root, manifest_dir, "test:e2e", package_json),
                        _script_command_for_manifest(root, manifest_dir, "e2e", package_json),
                        _script_command_for_manifest(root, manifest_dir, "smoke", package_json),
                        _script_command_for_manifest(root, manifest_dir, "playwright", package_json),
                    ]
                    if any(name in child_scripts for name in ("test:e2e", "e2e", "smoke", "playwright"))
                    else [],
                    confidence="high",
                    source_path=manifest_path,
                )

    if not capabilities["tests"]:
        config = _known_config_exists(root, "vitest.config.ts", "vitest.config.js", "jest.config.ts", "jest.config.js")
        if config:
            detectors_run.append("node_test_config")
            capabilities["tests"] = True
            lane_commands["tests"] = ["npx vitest run"] if "vitest" in config else ["npx jest --runInBand"]
            confidence_by_lane["tests"] = "medium"
            source_refs["tests"].append(config)
    if not capabilities["build"]:
        build_config = _known_config_exists(root, "vite.config.ts", "vite.config.js", "next.config.js", "next.config.mjs", "turbo.json")
        if build_config:
            detectors_run.append("node_build_config")
            capabilities["build"] = True
            lane_commands["build"] = ["npx vite build"] if "vite" in build_config else ["npx next build"] if "next.config" in build_config else [f"{runner} run build"]
            confidence_by_lane["build"] = "medium"
            source_refs["build"].append(build_config)

    smoke_config = _known_config_exists(
        root,
        "playwright.config.ts",
        "playwright.config.js",
        "playwright.config.mts",
        "playwright.config.cts",
        "cypress.config.ts",
        "cypress.config.js",
        "cypress.config.mjs",
    )
    if smoke_config and not lane_commands["smoke_e2e"]:
        detectors_run.append("ui_smoke_config")
        capabilities["smoke_e2e"] = True
        lane_commands["smoke_e2e"] = ["npx playwright test"] if "playwright" in smoke_config else ["npx cypress run"]
        confidence_by_lane["smoke_e2e"] = "medium"
        source_refs["smoke_e2e"].append(smoke_config)

    pyproject_text = _read_text_if_exists(root / "pyproject.toml")
    pytest_ini = _read_text_if_exists(root / "pytest.ini")
    setup_cfg = _read_text_if_exists(root / "setup.cfg")
    tox_ini = _read_text_if_exists(root / "tox.ini")
    mypy_ini = _read_text_if_exists(root / "mypy.ini")
    pyright_config = _read_text_if_exists(root / "pyrightconfig.json")
    ruff_toml = _read_text_if_exists(root / "ruff.toml")
    dot_ruff_toml = _read_text_if_exists(root / ".ruff.toml")
    noxfile = _read_text_if_exists(root / "noxfile.py")
    if pyproject_text or pytest_ini or setup_cfg or tox_ini or mypy_ini or pyright_config or ruff_toml or dot_ruff_toml or noxfile:
        detectors_run.append("python_project")
        combined = "\n".join(
            item
            for item in (pyproject_text, pytest_ini, setup_cfg, tox_ini, mypy_ini, pyright_config, ruff_toml, dot_ruff_toml, noxfile)
            if item
        )
        if "pytest" in combined.lower() and not capabilities["tests"]:
            capabilities["tests"] = True
            lane_commands["tests"] = ["python -m pytest"]
            confidence_by_lane["tests"] = "medium"
            source_refs["tests"].extend(
                [
                    str(path)
                    for path in (
                        root / "pyproject.toml",
                        root / "pytest.ini",
                        root / "setup.cfg",
                        root / "tox.ini",
                        root / "noxfile.py",
                    )
                    if path.exists()
                ]
            )
        if "nox" in combined.lower() and not capabilities["tests"]:
            capabilities["tests"] = True
            lane_commands["tests"] = ["python -m nox -s tests"]
            confidence_by_lane["tests"] = "medium"
            source_refs["tests"].append(str(root / "noxfile.py"))
        if any(token in combined.lower() for token in ("mypy", "pyright")) and not capabilities["typecheck"]:
            capabilities["typecheck"] = True
            lane_commands["typecheck"] = ["python -m mypy ."] if "mypy" in combined.lower() else ["pyright"]
            confidence_by_lane["typecheck"] = "medium"
            source_refs["typecheck"].append(
                str(root / "pyproject.toml" if (root / "pyproject.toml").exists() else root / "pyrightconfig.json" if (root / "pyrightconfig.json").exists() else root / "setup.cfg")
            )
        if any(token in combined.lower() for token in ("ruff", "flake8", "pylint")) and not capabilities["lint"]:
            capabilities["lint"] = True
            if "ruff" in combined.lower():
                lane_commands["lint"] = ["python -m ruff check ."]
            elif "flake8" in combined.lower():
                lane_commands["lint"] = ["python -m flake8 ."]
            else:
                lane_commands["lint"] = ["python -m pylint ."]
            confidence_by_lane["lint"] = "medium"
            source_refs["lint"].append(str(root / "pyproject.toml" if (root / "pyproject.toml").exists() else root / "setup.cfg"))
        if any(token in combined.lower() for token in ("alembic", "django", "sqlalchemy", "migrations")):
            capabilities["schema_check"] = True
            command = ""
            if "alembic" in combined.lower():
                command = "alembic check"
            elif "django" in combined.lower():
                command = "python manage.py makemigrations --check --dry-run"
            if command:
                lane_commands["schema_check"] = [command]
                confidence_by_lane["schema_check"] = "medium"
            else:
                confidence_by_lane["schema_check"] = "low"
            source_refs["schema_check"].extend(
                [str(path) for path in (root / "pyproject.toml", root / "alembic.ini", root / "manage.py") if path.exists()]
            )
    migrations_dir = next((path for path in (root / "migrations", root / "alembic", root / "db" / "migrations") if path.exists()), None)
    if migrations_dir and not capabilities["schema_check"]:
        detectors_run.append("migration_layout")
        capabilities["schema_check"] = True
        confidence_by_lane["schema_check"] = "low"
        missing_capabilities["schema_check"] = "Schema or migration layout detected but no deterministic schema-check command was discovered."
        source_refs["schema_check"].append(str(migrations_dir))
    if (root / "manage.py").exists() and not capabilities["schema_check"]:
        detectors_run.append("django_manage")
        capabilities["schema_check"] = True
        lane_commands["schema_check"] = ["python manage.py makemigrations --check --dry-run"]
        confidence_by_lane["schema_check"] = "medium"
        source_refs["schema_check"].append(str(root / "manage.py"))

    workflow_files = _workflow_files(root)
    if workflow_files:
        detectors_run.append("ci_config")
        ci_commands: dict[str, list[str]] = {}
        for workflow_file in workflow_files:
            ci_text = _read_text_if_exists(workflow_file)
            discovered = _infer_commands_from_ci_text(ci_text, root=root, package_json=package_json)
            for lane_name, commands in discovered.items():
                ci_commands.setdefault(lane_name, [])
                ci_commands[lane_name].extend(commands)
        ci_commands = {key: list(dict.fromkeys(value)) for key, value in ci_commands.items() if value}
        for lane_name, capability_key in (
            ("tests", "tests"),
            ("lint", "lint"),
            ("typecheck", "typecheck"),
            ("build", "build"),
            ("smoke_e2e", "smoke_e2e"),
            ("schema_check", "schema_check"),
        ):
            if ci_commands.get(lane_name) and not capabilities[capability_key]:
                capabilities[capability_key] = True
                lane_commands[capability_key] = ci_commands[lane_name]
                confidence_by_lane[capability_key] = "medium"
        for capability_key, lane_name in (
            ("tests", "tests"),
            ("lint", "lint"),
            ("typecheck", "typecheck"),
            ("build", "build"),
            ("smoke_e2e", "smoke_e2e"),
            ("schema_check", "schema_check"),
        ):
            if ci_commands.get(lane_name):
                lane_commands[capability_key] = list(dict.fromkeys([*lane_commands[capability_key], *ci_commands[lane_name]]))
                source_refs[capability_key].extend(str(path) for path in workflow_files)

    for lane, enabled in capabilities.items():
        source_refs[lane] = sorted(set(source_refs.get(lane, [])))
        if not enabled and lane != "security_sensitive_review":
            missing_capabilities[lane] = f"No deterministic repo capability discovered for {lane}."

    return {
        "schema_version": OBJECTIVE_RUNTIME_REPO_CAPABILITIES_SCHEMA_VERSION,
        "workspace_root": str(root),
        "detectors_run": sorted(set(detectors_run)),
        "capabilities": capabilities,
        "lane_commands": lane_commands,
        "confidence_by_lane": confidence_by_lane,
        "source_refs": source_refs,
        "missing_capabilities": missing_capabilities,
    }


def _repo_validation_lanes_for_paths(
    paths: list[str],
    *,
    repo_capabilities: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    lane_specs: dict[str, dict[str, Any]] = {
        "tests": {"required": False, "reasons": [], "paths": [], "risk_trigger": "", "capability_key": "tests"},
        "types_build": {"required": False, "reasons": [], "paths": [], "risk_trigger": "", "capability_key": "typecheck"},
        "lint": {"required": False, "reasons": [], "paths": [], "risk_trigger": "", "capability_key": "lint"},
        "smoke_e2e": {"required": False, "reasons": [], "paths": [], "risk_trigger": "", "capability_key": "smoke_e2e"},
        "migration_schema": {"required": False, "reasons": [], "paths": [], "risk_trigger": "", "capability_key": "schema_check"},
        "security_sensitive_review": {"required": False, "reasons": [], "paths": [], "risk_trigger": "", "capability_key": "security_sensitive_review"},
    }
    discovered_caps = repo_capabilities.get("capabilities") if isinstance(repo_capabilities.get("capabilities"), dict) else {}
    for raw_path in paths:
        path = str(raw_path or "").strip()
        if not path:
            continue
        lower = path.lower()
        ext = Path(path).suffix.lower()
        code_like = ext in {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb", ".php"}
        ui_like = ext in {".tsx", ".jsx", ".css", ".scss", ".html"} or any(token in lower for token in ("frontend", "ui/", "/ui", "web/", "/web", "app/", "/app"))
        migration_like = any(token in lower for token in ("migration", "migrations", "schema", ".sql", "alembic"))
        security_like = any(token in lower for token in ("auth", "permission", "secret", "token", "billing", "payment", "security", "oauth"))
        if code_like:
            for lane_name in ("tests", "types_build", "lint"):
                lane_specs[lane_name]["required"] = True
                lane_specs[lane_name]["reasons"].append(f"code path touched: {path}")
                lane_specs[lane_name]["paths"].append(path)
        if ui_like:
            lane_specs["smoke_e2e"]["required"] = True
            lane_specs["smoke_e2e"]["reasons"].append(f"ui surface touched: {path}")
            lane_specs["smoke_e2e"]["paths"].append(path)
            lane_specs["smoke_e2e"]["risk_trigger"] = "frontend_ui"
        if migration_like:
            lane_specs["migration_schema"]["required"] = True
            lane_specs["migration_schema"]["reasons"].append(f"schema or migration surface touched: {path}")
            lane_specs["migration_schema"]["paths"].append(path)
            lane_specs["migration_schema"]["risk_trigger"] = "schema_migration"
        if security_like:
            lane_specs["security_sensitive_review"]["required"] = True
            lane_specs["security_sensitive_review"]["reasons"].append(f"security-sensitive surface touched: {path}")
            lane_specs["security_sensitive_review"]["paths"].append(path)
            lane_specs["security_sensitive_review"]["risk_trigger"] = "security_sensitive_surface"
    if not paths:
        for lane_name, capability_key in (("tests", "tests"), ("types_build", "typecheck"), ("lint", "lint")):
            if discovered_caps.get(capability_key):
                lane_specs[lane_name]["required"] = True
                lane_specs[lane_name]["reasons"].append(f"repo capability discovered for {capability_key}")
    for lane in lane_specs.values():
        lane["reasons"] = sorted(set(lane["reasons"]))
        lane["paths"] = sorted(set(lane["paths"]))
    return lane_specs


def _validation_strategy_for_lane(lane_name: str) -> str:
    return {
        "tests": "test_command",
        "types_build": "typecheck_command",
        "lint": "lint_command",
        "smoke_e2e": "smoke_command",
        "migration_schema": "schema_check_command",
        "security_sensitive_review": "review_evidence_packet",
    }.get(lane_name, "validation_command")


def build_packet_quality_report(
    *,
    plan: dict[str, Any],
    route_hint: str,
    packets: list[dict[str, Any]],
) -> dict[str, Any]:
    warning_thresholds = {"R2": 0.5, "R3": 0.2, "R4": 0.1}
    fail_thresholds = {"R2": 0.8, "R3": 0.35, "R4": 0.2}
    rows: list[dict[str, Any]] = []
    fallback_count = 0
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        packet_id = str(packet.get("packet_id") or "").strip()
        hard_fail_checks: list[str] = []
        warning_checks: list[str] = []
        acceptance_checks = to_string_list(packet.get("acceptance_checks"))
        if not acceptance_checks or any(len(item.strip()) < 8 for item in acceptance_checks):
            hard_fail_checks.append("acceptance_checks_weak")
        scope = to_string_list(packet.get("allowed_scope"))
        packet_class = str(packet.get("packet_class") or packet.get("classification") or "").strip()
        if packet_class in {"implementation", "ready"} and len(scope) > 8:
            hard_fail_checks.append("allowed_scope_oversized")
        if packet_class == "review" and scope:
            warning_checks.append("review_scope_should_be_minimal")
        evidence_destination = str(packet.get("evidence_destination") or "").strip()
        if not evidence_destination:
            hard_fail_checks.append("missing_evidence_destination")
        strategy_name = str(packet.get("execution_strategy") or "").strip()
        support_expectations = packet.get("support_expectations") if isinstance(packet.get("support_expectations"), dict) else {}
        external_support_required = packet.get("external_support_required") is True
        support_remediation_mode = str(packet.get("support_remediation_mode") or "").strip()
        alternate_strategies = to_string_list(packet.get("alternate_strategies"))
        if alternate_strategies and not str(packet.get("adaptation_policy") or "").strip():
            hard_fail_checks.append("adaptation_policy_missing")
        if alternate_strategies and packet.get("max_adaptations") is None:
            hard_fail_checks.append("max_adaptations_missing")
        if strategy_name == "codex_prompt_worker":
            fallback_count += 1
            if not str(packet.get("fallback_reason") or "").strip():
                hard_fail_checks.append("fallback_reason_missing")
            if packet_class == "validation":
                hard_fail_checks.append("fallback_not_allowed_for_validation")
            if not support_expectations:
                hard_fail_checks.append("fallback_support_plan_missing")
        elif packet.get("fallback_reason"):
            warning_checks.append("fallback_reason_present_on_non_fallback_packet")
        if packet_class == "validation" and strategy_name == "review_evidence_packet" and "security" not in " ".join(to_string_list(packet.get("shared_surface_categories"))).lower():
            warning_checks.append("review_strategy_on_non_security_validation")
        if strategy_name == "review_evidence_packet" and not support_expectations.get("expected_evidence_artifacts"):
            hard_fail_checks.append("review_support_expectations_missing")
        if strategy_name == "multi_command_pipeline" and not support_expectations.get("required_step_evidence"):
            hard_fail_checks.append("pipeline_support_expectations_missing")
        if packet_class == "validation" and not any(
            any(token in item.lower() for token in ("evidence", "artifact", "capture", "manifest", "proof"))
            for item in acceptance_checks
        ):
            hard_fail_checks.append("validation_acceptance_not_evidence_driven")
        if external_support_required and not support_remediation_mode:
            hard_fail_checks.append("support_remediation_mode_missing")
        if any(token in " ".join(scope).lower() for token in _ROLLBACK_EXPECTATION_TOKENS) and "rollback" not in str(packet.get("fallback_or_rollback") or "").lower():
            hard_fail_checks.append("rollback_expectation_missing")
        dependencies = to_string_list(packet.get("dependencies"))
        if len(dependencies) > 4:
            warning_checks.append("dependency_complexity_high")
        rows.append(
            {
                "packet_id": packet_id,
                "score": max(0, 100 - (25 * len(hard_fail_checks)) - (10 * len(warning_checks))),
                "hard_fail_checks": hard_fail_checks,
                "warning_checks": warning_checks,
                "scope_size": len(scope),
                "fallback_class": "fallback" if strategy_name == "codex_prompt_worker" else "deterministic",
                "evidence_completeness": bool(evidence_destination),
                "dependency_complexity": len(dependencies),
            }
        )
    packet_count = max(len(rows), 1)
    fallback_ratio = fallback_count / packet_count
    budget = {
        "route_hint": route_hint,
        "fallback_count": fallback_count,
        "packet_count": packet_count,
        "fallback_ratio": round(fallback_ratio, 4),
        "warning_threshold": warning_thresholds.get(route_hint, 0.5),
        "fail_threshold": fail_thresholds.get(route_hint, 0.8),
    }
    if fallback_ratio >= budget["fail_threshold"]:
        budget["status"] = "hard_fail"
    elif fallback_ratio >= budget["warning_threshold"]:
        budget["status"] = "warning"
    else:
        budget["status"] = "pass"
    return {
        "schema_version": OBJECTIVE_RUNTIME_PACKET_QUALITY_SCHEMA_VERSION,
        "route_hint": route_hint,
        "rows": rows,
        "budget": budget,
        "hard_fail_packet_ids": sorted(row["packet_id"] for row in rows if row["hard_fail_checks"]),
    }


def build_repo_validation_plan(
    plan: dict[str, Any],
    *,
    track_id: str,
    cwd: str | None = None,
    repo_capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    objective_id = stable_objective_id(track_id)
    packets = [
        packet
        for packet in plan.get("packets", [])
        if isinstance(packet, dict) and str(packet.get("packet_id", "")).strip()
    ]
    changed_files = sorted(
        {
            path
            for packet in packets
            for path in to_string_list(packet.get("allowed_scope"))
            if path
        }
    )
    resolved_capabilities = repo_capabilities or discover_repo_capabilities(cwd=cwd)
    lanes = _repo_validation_lanes_for_paths(changed_files, repo_capabilities=resolved_capabilities)
    tests = plan.get("tests") if isinstance(plan.get("tests"), dict) else {}
    session_harness = plan.get("session_harness") if isinstance(plan.get("session_harness"), dict) else {}
    route_hint = str(session_harness.get("route_hint") or "").strip()
    required_packets = {str(packet_id).strip() for packet_id in to_string_list(plan.get("required_packets"))}
    validation_lanes: list[dict[str, Any]] = []
    generated_packets: list[dict[str, Any]] = []
    combined_r2 = route_hint == "R2" and len(changed_files) <= 2
    for lane_name, lane in lanes.items():
        commands: list[str] = []
        capability_key = str(lane.get("capability_key") or "").strip()
        discovered_commands = (
            resolved_capabilities.get("lane_commands", {}).get(capability_key, [])
            if isinstance(resolved_capabilities.get("lane_commands"), dict)
            else []
        )
        if lane_name == "tests":
            commands = to_string_list(tests.get("unit")) + to_string_list(tests.get("integration")) + to_string_list(tests.get("regression"))
        elif lane_name == "smoke_e2e":
            commands = [
                command
                for gate in tests.get("smoke_gates", [])
                if isinstance(gate, dict)
                for command in to_string_list(gate.get("commands"))
            ]
        elif lane_name in {"types_build", "lint"}:
            commands = to_string_list(session_harness.get("validation_commands"))
        if discovered_commands:
            commands = list(dict.fromkeys([*commands, *to_string_list(discovered_commands)]))
        generated_packet_ids: list[str] = []
        manual_only_blocker = ""
        if lane["required"]:
            if lane_name in {"security_sensitive_review"}:
                packet_id = f"packet-validation-{lane_name.replace('_', '-')}"
                generated_packets.append(
                    {
                        "packet_id": packet_id,
                        "primary_behavior": "Produce explicit reviewer-verifier evidence for security-sensitive runtime surfaces.",
                        "execution_strategy": "review_evidence_packet",
                        "strategy_inputs": {
                            "review_focus": "Security-sensitive changed surfaces require reviewer-grade evidence.",
                            "expected_artifacts": [f"{packet_id}.review.json"],
                        },
                        "execution_mode": "sequence_required",
                        "allowed_scope": lane["paths"],
                        "dependencies": sorted(required_packets),
                        "dependency_mode": "accepted_upstream",
                        "acceptance_checks": ["review evidence emitted", "security-sensitive concerns adjudicated"],
                        "failure_signals": ["missing review evidence", "security concerns unresolved"],
                        "constraints": ["Stay within validation surfaces."],
                        "fallback_or_rollback": "Escalate with explicit blocker evidence.",
                        "verifier_mapping": f"cycle-verifier:{packet_id}",
                        "evidence_destination": f"planning_artifacts/<track-id>/packets/{packet_id}.verdict.json",
                        "shared_surface_categories": ["security-sensitive-review"],
                        "classification": "ready",
                        "packet_class": "review",
                        "support_expectations": {
                            "expected_evidence_artifacts": [f"{packet_id}.review.json"],
                            "support_kind": "review_evidence",
                        },
                        "external_support_required": True,
                        "support_remediation_mode": "review_evidence_packet",
                        "product_meaning_resolved": True,
                        "automatable_acceptance": True,
                        "prohibited_action_required": False,
                        "maintainable_completion_path": True,
                        "definition_of_done": {
                            "behavior_outcome": "Security-sensitive reviewer evidence exists for changed high-risk surfaces.",
                            "acceptance_checks": ["review evidence emitted"],
                            "evidence_requirements": [f"{packet_id}.review.json"],
                            "allowed_scope": lane["paths"],
                            "rollback_or_fallback": "Escalate with explicit blocker evidence.",
                            "verifier_acceptance_condition": "Reviewer evidence is attached and non-empty.",
                            "objective_linkage": "generated-validation",
                        },
                    }
                )
                generated_packet_ids.append(packet_id)
            elif commands and combined_r2:
                packet_id = "packet-validation-combined"
                if not any(packet["packet_id"] == packet_id for packet in generated_packets):
                    generated_packets.append(
                        {
                            "packet_id": packet_id,
                            "primary_behavior": "Run combined validation lanes for a low-risk compact objective.",
                            "execution_strategy": "multi_command_pipeline",
                            "strategy_inputs": {
                                "commands": commands,
                                "validation_lane": "combined",
                                "cwd": str(Path(cwd or os.getcwd()).resolve()),
                            },
                            "execution_mode": "sequence_required",
                            "allowed_scope": changed_files,
                            "dependencies": sorted(required_packets),
                            "dependency_mode": "accepted_upstream",
                            "acceptance_checks": ["combined validation capture manifests prove all required lanes pass"],
                            "failure_signals": ["combined validation fails"],
                            "constraints": ["Validation only; no source edits."],
                            "fallback_or_rollback": "Stop on failed validation and preserve evidence.",
                            "verifier_mapping": "cycle-verifier:packet-validation-combined",
                            "evidence_destination": "planning_artifacts/<track-id>/packets/packet-validation-combined.verdict.json",
                            "shared_surface_categories": ["validation-lane-combined"],
                            "classification": "ready",
                            "packet_class": "validation",
                            "support_expectations": {
                                "required_step_evidence": [f"command://packet-validation-combined:{idx}" for idx, _ in enumerate(commands, start=1)],
                                "support_kind": "validation_pipeline",
                            },
                            "external_support_required": True,
                            "support_remediation_mode": "validation_packet",
                            "product_meaning_resolved": True,
                            "automatable_acceptance": True,
                            "prohibited_action_required": False,
                            "maintainable_completion_path": True,
                            "definition_of_done": {
                                "behavior_outcome": "Required low-risk validation lanes completed successfully.",
                                "acceptance_checks": ["combined validation capture manifests prove all required lanes pass"],
                                "evidence_requirements": ["validation capture manifests"],
                                "allowed_scope": changed_files,
                                "rollback_or_fallback": "Stop on failed validation and preserve evidence.",
                                "verifier_acceptance_condition": "Combined validation command exits successfully.",
                                "objective_linkage": "generated-validation",
                            },
                        }
                )
                generated_packet_ids.append(packet_id)
            elif commands:
                packet_id = f"packet-validation-{lane_name.replace('_', '-')}"
                strategy_name = "multi_command_pipeline" if len(commands) > 1 and lane_name != "security_sensitive_review" else _validation_strategy_for_lane(lane_name)
                rollback_or_fallback = (
                    "Stop on failed schema validation, preserve evidence, and require rollback evidence before closure."
                    # The lane test alone is not enough: the packet-quality checker keys on the
                    # SCOPE PATHS, not the lane, so a lint/tests/types_build packet whose
                    # allowed_scope merely contains a migration-named file trips
                    # rollback_expectation_missing. Reuse the checker's own tokens so the two
                    # halves cannot drift apart again. This only ever selects the STRICTER
                    # wording, so it can never weaken the gate.
                    if lane_name == "migration_schema"
                    or any(
                        token in " ".join(changed_files).lower()
                        for token in _ROLLBACK_EXPECTATION_TOKENS
                    )
                    else "Stop on failed validation and preserve evidence."
                )
                strategy_inputs = {
                    "commands": commands,
                    "cwd": str(Path(cwd or os.getcwd()).resolve()),
                } if strategy_name == "multi_command_pipeline" else {
                    "command": commands[0],
                    "commands": commands,
                    "test_lane": lane_name if strategy_name == "test_command" else "",
                    "validation_lane": "" if strategy_name == "test_command" else lane_name,
                    "cwd": str(Path(cwd or os.getcwd()).resolve()),
                }
                generated_packets.append(
                    {
                        "packet_id": packet_id,
                        "primary_behavior": f"Run {lane_name.replace('_', ' ')} validation for the current objective.",
                        "execution_strategy": strategy_name,
                        "strategy_inputs": strategy_inputs,
                        "execution_mode": "parallel_safe" if lane_name not in {"migration_schema"} else "sequence_required",
                        "allowed_scope": lane["paths"] or changed_files,
                        "dependencies": sorted(required_packets),
                        "dependency_mode": "accepted_upstream",
                        "acceptance_checks": [f"{lane_name} validation capture manifests prove the lane passes"],
                        "failure_signals": [f"{lane_name} validation fails"],
                        "constraints": ["Validation only; no source edits."],
                        "fallback_or_rollback": rollback_or_fallback,
                        "verifier_mapping": f"cycle-verifier:{packet_id}",
                        "evidence_destination": f"planning_artifacts/<track-id>/packets/{packet_id}.verdict.json",
                        "shared_surface_categories": [f"validation-lane-{lane_name}"],
                        "classification": "ready",
                        "packet_class": "validation",
                        "support_expectations": {
                            "expected_evidence_artifacts": ["validation capture manifests"],
                            "required_step_evidence": (
                                [f"command://{packet_id}:pipeline:{idx}" for idx, _ in enumerate(commands, start=1)]
                                if strategy_name == "multi_command_pipeline"
                                else []
                            ),
                            "support_kind": "validation_lane",
                        },
                        "external_support_required": True,
                        "support_remediation_mode": "validation_packet",
                        "product_meaning_resolved": True,
                        "automatable_acceptance": True,
                        "prohibited_action_required": False,
                        "maintainable_completion_path": True,
                        "alternate_strategies": ["multi_command_pipeline"] if strategy_name not in {"multi_command_pipeline", "review_evidence_packet"} else [],
                        "adaptation_policy": "bounded_retry_then_alternate",
                        "max_adaptations": 1,
                        "definition_of_done": {
                            "behavior_outcome": f"{lane_name} validation completed with evidence.",
                            "acceptance_checks": [f"{lane_name} validation capture manifests prove the lane passes"],
                            "evidence_requirements": ["validation capture manifests"],
                            "allowed_scope": lane["paths"] or changed_files,
                            "rollback_or_fallback": rollback_or_fallback,
                            "verifier_acceptance_condition": "Validation command exits successfully.",
                            "objective_linkage": "generated-validation",
                        },
                    }
                )
                generated_packet_ids.append(packet_id)
            else:
                manual_only_blocker = f"required lane {lane_name} has no deterministic validation command"
        validation_lanes.append(
            {
                "lane": lane_name,
                "required": lane["required"],
                "reasons": lane["reasons"],
                "paths": lane["paths"],
                "commands": commands,
                "capability_source": (
                    resolved_capabilities.get("source_refs", {}).get(capability_key, [])
                    if isinstance(resolved_capabilities.get("source_refs"), dict)
                    else []
                ),
                "capability_confidence": (
                    resolved_capabilities.get("confidence_by_lane", {}).get(capability_key, "none")
                    if isinstance(resolved_capabilities.get("confidence_by_lane"), dict)
                    else "none"
                ),
                "generated_packet_ids": generated_packet_ids,
                "manual_only_blocker": manual_only_blocker,
                "missing_capability_reason": (
                    resolved_capabilities.get("missing_capabilities", {}).get(capability_key, "")
                    if isinstance(resolved_capabilities.get("missing_capabilities"), dict)
                    else ""
                ),
                "risk_trigger": str(lane.get("risk_trigger") or "").strip(),
            }
        )
    return {
        "schema_version": OBJECTIVE_RUNTIME_VALIDATION_PLAN_SCHEMA_VERSION,
        "objective_id": objective_id,
        "track_id": track_id,
        "route_hint": route_hint,
        "workspace_root": str(Path(cwd or os.getcwd()).resolve()),
        "changed_files": changed_files,
        "required_packets": sorted(required_packets),
        "lanes": validation_lanes,
        "generated_packets": generated_packets,
        "coverage": {
            "required_lane_count": sum(1 for lane in validation_lanes if lane["required"]),
            "generated_lane_count": sum(1 for lane in validation_lanes if lane["generated_packet_ids"]),
            "manual_blocker_count": sum(1 for lane in validation_lanes if lane["manual_only_blocker"]),
        },
        "escalated_review_required": any(
            lane["required"] for lane in validation_lanes if lane["lane"] in {"migration_schema", "security_sensitive_review"}
        ),
    }


def build_objective_summary(
    *,
    objective_id: str,
    track_id: str,
    route_hint: str,
    closure_state: str,
    frontier: list[str],
    blocked_reasons: list[str],
    accepted_packet_count: int,
    next_action: str,
    execution_shape: str = "single_lane",
    swarm_status: str = "single_lane",
    lane_queue_depths: dict[str, int] | None = None,
    active_packets_by_lane: dict[str, list[str]] | None = None,
    convergence_status: str = "",
) -> dict[str, Any]:
    return {
        "schema_version": OBJECTIVE_RUNTIME_SUMMARY_SCHEMA_VERSION,
        "objective_id": objective_id,
        "track_id": track_id,
        "route_hint": route_hint,
        "closure_state": closure_state,
        "current_frontier": sorted(set(frontier)),
        "blocked_reasons": sorted(set(reason for reason in blocked_reasons if reason)),
        "accepted_packet_count": accepted_packet_count,
        "next_action": next_action,
        "execution_shape": execution_shape,
        "swarm_status": swarm_status,
        "lane_queue_depths": lane_queue_depths or {},
        "active_packets_by_lane": active_packets_by_lane or {},
        "convergence_status": convergence_status,
        "updated_at": now_iso(),
    }


def build_runtime_bootstrap_artifacts(
    plan: dict[str, Any],
    *,
    track_id: str,
    cwd: str | None = None,
) -> dict[str, Any]:
    objective_id = stable_objective_id(track_id)
    packets = {
        str(packet.get("packet_id", "")).strip(): json.loads(json.dumps(packet))
        for packet in plan.get("packets", [])
        if isinstance(packet, dict) and str(packet.get("packet_id", "")).strip()
    }
    _, packet_ids = _plan_ids(plan)
    session_harness = plan.get("session_harness") if isinstance(plan.get("session_harness"), dict) else {}
    bootstrap_commands = to_string_list(session_harness.get("bootstrap_commands"))
    validation_commands = to_string_list(session_harness.get("validation_commands"))
    clean_state_assertions = to_string_list(session_harness.get("clean_state_assertions"))
    scheduler_policy = plan.get("scheduler_policy") if isinstance(plan.get("scheduler_policy"), dict) else {}
    max_parallel_packets = int(scheduler_policy.get("max_parallel_packets", 1) or 1)
    parallelism_policy = str(scheduler_policy.get("parallelism_policy", "bounded_parallel")).strip() or "bounded_parallel"
    execution_shape = str(plan.get("execution_shape") or scheduler_policy.get("execution_shape") or "single_lane").strip() or "single_lane"
    lane_caps = scheduler_policy.get("lane_caps") if isinstance(scheduler_policy.get("lane_caps"), dict) else {}
    route_swarm_cap = scheduler_policy.get("route_swarm_cap")
    frontier_dispatch_order = to_string_list(scheduler_policy.get("frontier_dispatch_order"))
    reviewer_barrier_points = to_string_list(scheduler_policy.get("reviewer_barrier_points"))
    convergence_required_for_closure = scheduler_policy.get("convergence_required_for_closure") is True
    current_frontier = compute_runnable_set(
        packets=packets,
        accepted_packets=set(),
        active_packets=set(),
        retry_counters={},
        max_parallel_packets=max_parallel_packets,
        parallelism_policy=parallelism_policy,
        execution_shape=execution_shape,
        lane_caps=lane_caps,
        route_swarm_cap=int(route_swarm_cap) if route_swarm_cap is not None else None,
        frontier_dispatch_order=frontier_dispatch_order,
        reviewer_barrier_points=reviewer_barrier_points,
    )
    safe_momentum_ready = safe_momentum_exists(
        packets=packets,
        accepted_packets=set(),
        active_packets=set(),
        retry_counters={},
        max_parallel_packets=max_parallel_packets,
        parallelism_policy=parallelism_policy,
        execution_shape=execution_shape,
        lane_caps=lane_caps,
        route_swarm_cap=int(route_swarm_cap) if route_swarm_cap is not None else None,
        frontier_dispatch_order=frontier_dispatch_order,
        reviewer_barrier_points=reviewer_barrier_points,
    )
    feature_items = _runtime_feature_items(plan)
    momentum_entries = []
    blockers = []
    momentum_by_packet = {
        str(item.get("packet_id", "")).strip(): item
        for item in plan.get("momentum_map", [])
        if isinstance(item, dict) and str(item.get("packet_id", "")).strip()
    }
    frontier_by_packet = {
        str(item.get("packet_id", "")).strip(): item
        for item in plan.get("frontier_map", [])
        if isinstance(item, dict) and str(item.get("packet_id", "")).strip()
    }
    for packet_id in packet_ids:
        packet = packets[packet_id]
        packet["runtime_state"] = "queued"
        packet["last_verifier_output"] = ""
        packet["last_cycle_id"] = ""
        packet["cancelled_by"] = ""
        momentum_item = momentum_by_packet.get(packet_id, {})
        frontier_item = frontier_by_packet.get(packet_id, {})
        momentum_entries.append(
            {
                "packet_id": packet_id,
                "movement_type": momentum_item.get("movement_type"),
                "unlocks_packets": to_string_list(momentum_item.get("unlocks_packets")),
                "resolves_dependency_classification": momentum_item.get("resolves_dependency_classification") is True,
                "isolates_blocker": momentum_item.get("isolates_blocker") is True,
                "execution_mode": frontier_item.get("execution_mode", ""),
            }
        )
        if str(frontier_item.get("execution_mode", "")).strip() == "blocked":
            blockers.append(
                {
                    "packet_id": packet_id,
                    "reason": "blocked_by_frontier_classification",
                    "authority_sensitive": str(packet.get("classification", "")).strip() == "blocked_authority",
                }
            )
    repo_capabilities = discover_repo_capabilities(cwd=cwd)
    validation_plan = build_repo_validation_plan(plan, track_id=track_id, cwd=cwd, repo_capabilities=repo_capabilities)
    generated_packets = [
        packet
        for packet in validation_plan.get("generated_packets", [])
        if isinstance(packet, dict) and str(packet.get("packet_id", "")).strip()
    ]
    for packet in generated_packets:
        packets[str(packet.get("packet_id", "")).strip()] = json.loads(json.dumps(packet))
    all_packet_ids = sorted(packets)
    schedule = build_schedule(
        objective_id=objective_id,
        packets=packets,
        accepted_packets=set(),
        active_packets=set(),
        retry_counters={},
        max_parallel_packets=max_parallel_packets,
        parallelism_policy=parallelism_policy,
        execution_shape=execution_shape,
        lane_caps=lane_caps,
        route_swarm_cap=int(route_swarm_cap) if route_swarm_cap is not None else None,
        frontier_dispatch_order=frontier_dispatch_order,
        reviewer_barrier_points=reviewer_barrier_points,
        convergence_required_for_closure=convergence_required_for_closure,
        blocked_set=[item["packet_id"] for item in blockers],
    )
    route_hint = str(session_harness.get("route_hint") or "").strip()
    execution_plan = build_execution_plan(plan=plan, track_id=track_id)
    bootstrap_transition_history = [
        {
            "schema_version": TRANSITION_HISTORY_RECORD_SCHEMA_VERSION,
            "step_id": "bootstrap-000",
            "from": "bootstrapped",
            "to": "planning_complete",
            "guard": "execution_plan_compiled",
            "guard_result": True,
            "trigger": "runtime_bootstrap",
            "evidence_refs": [],
            "timestamp": now_iso(),
        },
        {
            "schema_version": TRANSITION_HISTORY_RECORD_SCHEMA_VERSION,
            "step_id": "bootstrap-001",
            "from": "planning_complete",
            "to": "ready",
            "guard": "exists_actionable_unit || stop_allowed",
            "guard_result": True,
            "trigger": "runtime_bootstrap",
            "evidence_refs": [],
            "timestamp": now_iso(),
        },
    ]
    summary = build_objective_summary(
        objective_id=objective_id,
        track_id=track_id,
        route_hint=route_hint,
        closure_state="",
        frontier=current_frontier,
        blocked_reasons=[str(item.get("reason") or "").strip() for item in blockers],
        accepted_packet_count=0,
        next_action=current_frontier[0] if current_frontier else "escalate_or_finalize",
        execution_shape=execution_shape,
        swarm_status=str(schedule.get("swarm_status") or execution_shape),
        lane_queue_depths=schedule.get("lane_queue_depths") if isinstance(schedule.get("lane_queue_depths"), dict) else {},
        active_packets_by_lane=schedule.get("active_packets_by_lane") if isinstance(schedule.get("active_packets_by_lane"), dict) else {},
        convergence_status=str(schedule.get("convergence_status") or ""),
    )
    packet_quality = build_packet_quality_report(
        plan=plan,
        route_hint=route_hint,
        packets=[packets[packet_id] for packet_id in all_packet_ids],
    )
    return {
        "feature_list": {
            "schema_version": "objective-feature-list.v1",
            "objective_id": objective_id,
            "track_id": track_id,
            "features": feature_items,
        },
        "momentum": {
            "schema_version": "objective-momentum.v1",
            "objective_id": objective_id,
            "track_id": track_id,
            "safe_momentum_ready": safe_momentum_ready,
            "current_frontier": current_frontier,
            "entries": momentum_entries,
        },
        "blockers": {
            "schema_version": "objective-blockers.v1",
            "objective_id": objective_id,
            "track_id": track_id,
            "items": blockers,
        },
        "packet_dag": {
            "schema_version": OBJECTIVE_RUNTIME_PACKET_DAG_SCHEMA_VERSION,
            "objective_id": objective_id,
            "track_id": track_id,
            "packets": [packets[packet_id] for packet_id in all_packet_ids],
        },
        "status": {
            "schema_version": OBJECTIVE_RUNTIME_STATUS_SCHEMA_VERSION,
            "objective_id": objective_id,
            "track_id": track_id,
            "closure_state": "",
            "completed_packets": [],
            "pending_packets": all_packet_ids,
            "blocked_packets": [item["packet_id"] for item in blockers],
            "deferred_packets": [
                packet_id
                for packet_id, packet in packets.items()
                if str(packet.get("execution_mode", "")).strip() == "deferred"
            ],
            "boundary_shrunk_remainder": [],
        },
        "schedule": {
            **schedule,
            "schema_version": OBJECTIVE_RUNTIME_SCHEDULE_SCHEMA_VERSION,
            "track_id": track_id,
        },
        "summary": summary,
        "execution_plan": execution_plan,
        "kernel_runtime_state": {
            "schema_version": KERNEL_RUNTIME_STATE_SCHEMA_VERSION,
            "objective_id": objective_id,
            "track_id": track_id,
            "state": "ready",
            "active_unit_id": current_frontier[0] if current_frontier else None,
            "active_unit_ids": [current_frontier[0]] if current_frontier else [],
            "completed_units": [],
            "failed_attempts": [],
            "last_action": None,
            "last_verification_id": None,
            "evidence_refs": [],
            "budget": {
                "remaining_steps": max(len(all_packet_ids) * 3, 1),
                "remaining_mutations": sum(len(to_string_list(packet.get("allowed_scope"))) for packet in packets.values()),
                "remaining_retries": sum(
                    int(packet.get("max_retries", 2) or 2)
                    for packet in packets.values()
                    if isinstance(packet, dict)
                ),
            },
            "halt": {
                "terminal": False,
                "reason": "none",
            },
            "transition_history": list(bootstrap_transition_history),
        },
        "transition_history": list(bootstrap_transition_history),
        "verification_results": [],
        "repo_capabilities": repo_capabilities,
        "validation_plan": validation_plan,
        "packet_quality": packet_quality,
        "checkpoint_updates": {
            "last_verified_packet_ids": [],
            "current_frontier": current_frontier,
            "bootstrap_commands": bootstrap_commands,
            "validation_commands": validation_commands,
            "clean_state_assertions": clean_state_assertions,
            "next_recommended_packet": current_frontier[0] if current_frontier else "",
            "open_risks": to_string_list(plan.get("risks")),
            "last_forward_movement": "runtime_bootstrap",
            "stagnation_risk": "low" if current_frontier else "elevated",
            "escalation_candidates": [item["packet_id"] for item in blockers if item.get("authority_sensitive")],
            "checkpoint_strategy": "git_checkpoint_required",
            "checkpoint_attempted_at": "",
            "rollback_validation_ref": "",
            "updated_at": now_iso(),
        },
        "progress_events": [
            {
                "schema_version": "objective-progress-event.v1",
                "event_type": "runtime_bootstrap",
                "timestamp": now_iso(),
                "objective_id": objective_id,
                "track_id": track_id,
                "checkpoint_id": f"{sanitize_token(track_id)}-checkpoint-000",
                "current_frontier": current_frontier,
                "feature_status_summary": {item["requirement_id"]: item["status"] for item in feature_items},
            },
            {
                "schema_version": "objective-progress-event.v1",
                "event_type": "checkpoint",
                "timestamp": now_iso(),
                "objective_id": objective_id,
                "track_id": track_id,
                "checkpoint_id": f"{sanitize_token(track_id)}-checkpoint-000",
                "last_verified_packet_ids": [],
                "current_frontier": current_frontier,
                "next_recommended_packet": current_frontier[0] if current_frontier else "",
            },
        ],
    }


def write_session_harness_artifacts(
    objective_intent: dict[str, Any],
    *,
    track_id: str,
    artifacts_root: Path,
    cwd: str | None = None,
) -> dict[str, str]:
    return write_preplan_session_artifacts(
        objective_intent,
        track_id=track_id,
        artifacts_root=artifacts_root,
        cwd=cwd,
    )


def build_compiled_contract(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "compiled-contract.v2.7",
        "objective": plan.get("objective"),
        "intent_contract": plan.get("intent_contract"),
        "clarification_governor": plan.get("clarification_governor"),
        "autonomous_session_readiness": plan.get("autonomous_session_readiness"),
        "objective_requirements": plan.get("objective_requirements"),
        "non_goals": plan.get("non_goals"),
        "constraints": plan.get("constraints"),
        "dependencies": plan.get("dependencies"),
        "quality_bar": plan.get("quality_bar"),
        "decomposition_policy": plan.get("decomposition_policy"),
        "momentum_map": plan.get("momentum_map"),
        "frontier_map": plan.get("frontier_map"),
        "scheduler_policy": plan.get("scheduler_policy"),
        "session_harness": plan.get("session_harness"),
        "objective_closure_policy": plan.get("objective_closure_policy"),
        "migration_fallback_policy": plan.get("migration_fallback_policy"),
        "autonomy_level": plan.get("autonomy_level"),
        "authority_map": plan.get("authority_map"),
        "integration_map": plan.get("integration_map"),
        "evidence_plan": plan.get("evidence_plan"),
        "rollback_plan": plan.get("rollback_plan"),
        "definition_of_done": plan.get("definition_of_done"),
        "packets": plan.get("packets"),
        "required_packets": plan.get("required_packets"),
    }


def _plan_ids(plan: dict[str, Any]) -> tuple[list[str], list[str]]:
    requirement_ids = sorted(
        str(item.get("requirement_id", "")).strip()
        for item in plan.get("objective_requirements", [])
        if isinstance(item, dict) and str(item.get("requirement_id", "")).strip()
    )
    packet_ids = sorted(
        str(item.get("packet_id", "")).strip()
        for item in plan.get("packets", [])
        if isinstance(item, dict) and str(item.get("packet_id", "")).strip()
    )
    return requirement_ids, packet_ids


def build_runtime_compatibility_checks(plan: dict[str, Any]) -> dict[str, bool]:
    normalized_packets = {
        str(packet.get("packet_id", "")).strip(): packet
        for packet in plan.get("packets", [])
        if isinstance(packet, dict) and str(packet.get("packet_id", "")).strip()
    }
    required_packet_ids = set(_as_string_list(plan.get("required_packets")))
    packet_dag_missing, packet_dag_blocked, _ = validate_packet_dag(plan.get("packets"), plan.get("required_packets"))
    compiled_contract_schema_compatible = plan.get("schema_version") == PLAN_SCHEMA_VERSION
    packet_dag_runtime_compatible = not packet_dag_missing and not packet_dag_blocked and required_packet_ids.issubset(set(normalized_packets))

    scheduler_admission_compatible = True
    accepted_seed = {
        packet_id
        for packet_id, packet in normalized_packets.items()
        if str(packet.get("classification", "")).strip() == "ready" and packet.get("dependency_mode") == "explicit_stub"
    }
    for packet in normalized_packets.values():
        classification = str(packet.get("classification", "")).strip()
        if classification != "ready":
            continue
        if not packet_valid(packet) or not packet_autonomy_ready(packet):
            scheduler_admission_compatible = False
            break
        if packet.get("dependency_mode") == "accepted_upstream" and not dependency_ready(packet, required_packet_ids | accepted_seed):
            scheduler_admission_compatible = False
            break
        retry_budget = packet.get("retry_budget")
        if retry_budget is not None:
            if not isinstance(retry_budget, dict):
                scheduler_admission_compatible = False
                break
            for key in ("same_method_attempts", "alternate_strategy_attempts"):
                try:
                    if int(retry_budget.get(key, -1)) < 0:
                        scheduler_admission_compatible = False
                        break
                except Exception:
                    scheduler_admission_compatible = False
                    break
        if not isinstance(packet.get("definition_of_done"), dict):
            scheduler_admission_compatible = False
            break

    verifier_acceptance_compatible = True
    for packet in normalized_packets.values():
        if not isinstance(packet.get("definition_of_done"), dict):
            verifier_acceptance_compatible = False
            break
    for requirement in plan.get("objective_requirements", []):
        if not isinstance(requirement, dict) or not isinstance(requirement.get("definition_of_done"), dict):
            verifier_acceptance_compatible = False
            break

    closure_policy = plan.get("objective_closure_policy")
    closure_semantics_compatible = isinstance(closure_policy, dict) and set(_as_string_list(closure_policy.get("allowed_states"))).issubset(CLOSURE_STATES)
    intent_schema_compatible = isinstance(plan.get("intent_contract"), dict) and str(
        plan.get("intent_contract", {}).get("objective_shape_status", "")
    ).strip() in OBJECTIVE_SHAPE_STATUS_VALUES
    readiness = plan.get("autonomous_session_readiness")
    readiness_compatible = isinstance(readiness, dict) and str(readiness.get("status", "")).strip() == "execution_ready"
    autonomy_level_compatible = str(plan.get("autonomy_level", "")).strip() in AUTONOMY_LEVEL_VALUES

    return {
        "compiled_contract_schema_compatible": compiled_contract_schema_compatible,
        "packet_dag_runtime_compatible": packet_dag_runtime_compatible,
        "scheduler_admission_compatible": scheduler_admission_compatible,
        "verifier_acceptance_compatible": verifier_acceptance_compatible,
        "closure_semantics_compatible": closure_semantics_compatible,
        "intent_schema_compatible": intent_schema_compatible,
        "readiness_compatible": readiness_compatible,
        "autonomy_level_compatible": autonomy_level_compatible,
    }


def build_plan_frontloaded_artifacts(plan: dict[str, Any], *, track_id: str) -> dict[str, dict[str, Any]]:
    requirement_ids, packet_ids = _plan_ids(plan)
    runtime_checks = build_runtime_compatibility_checks(plan)
    unresolved_gap_count = len(plan.get("plan_gap_report", {}).get("gaps_unresolved", []) or [])
    plan_status = str(plan.get("plan_status", "")).strip()
    objective_intent = build_objective_intent_payload(track_id=track_id, plan=plan)
    verifier_status = (
        "approved"
        if plan_status == "execution_ready" and unresolved_gap_count == 0 and all(runtime_checks.values())
        else "rejected"
    )
    return {
        "objective_intent": objective_intent,
        "intent": {
            "schema_version": PLAN_INTENT_SCHEMA_VERSION,
            "track_id": track_id,
            "objective_ref": sanitize_token(track_id),
            "objective_intent_ref": "objective.intent.json",
            "intent_contract": objective_intent.get("intent_contract"),
            "clarification_governor": objective_intent.get("clarification_governor"),
            "autonomy_level": plan.get("autonomy_level"),
        },
        "compiler": {
            "schema_version": "compiled-contract.v2.7",
            "track_id": track_id,
            "objective_ref": sanitize_token(track_id),
            "compiled_contract": build_compiled_contract(plan),
            "packet_ids": packet_ids,
            "requirement_ids": requirement_ids,
            "contract_refs": {
                "plan_schema_version": plan.get("schema_version"),
                "plan_status": plan_status,
                "session_harness_required": bool(plan.get("session_harness", {}).get("required"))
                if isinstance(plan.get("session_harness"), dict)
                else False,
            },
        },
        "coverage": {
            "schema_version": "plan-coverage.v1",
            "track_id": track_id,
            "objective_ref": sanitize_token(track_id),
            "objective_coverage_map": plan.get("objective_coverage_map"),
            "requirement_risk_rank": plan.get("requirement_risk_rank"),
            "integration_map": plan.get("integration_map"),
            "packet_ids": packet_ids,
            "requirement_ids": requirement_ids,
        },
        "gaps": {
            "schema_version": "plan-gap-report.v1",
            "track_id": track_id,
            "objective_ref": sanitize_token(track_id),
            "plan_gap_report": plan.get("plan_gap_report"),
            "pre_delivery_gap_review": plan.get("pre_delivery_gap_review"),
            "hardening_budget": plan.get("hardening_budget"),
            "unresolved_gap_count": unresolved_gap_count,
        },
        "sufficiency": {
            "schema_version": "plan-sufficiency.v1",
            "track_id": track_id,
            "objective_ref": sanitize_token(track_id),
            "plan_status": plan_status,
            "plan_verifier_status": verifier_status,
            "runtime_compatible": all(runtime_checks.values()) and plan.get("plan_sufficiency_report", {}).get("runtime_compatible") is True,
            "runtime_compatibility_checks": runtime_checks,
            "pre_delivery_gap_review": plan.get("pre_delivery_gap_review"),
            "plan_sufficiency_report": plan.get("plan_sufficiency_report"),
            "unresolved_gap_count": unresolved_gap_count,
        },
        "readiness": {
            "schema_version": "plan-readiness.v1",
            "track_id": track_id,
            "objective_ref": sanitize_token(track_id),
            "autonomous_session_readiness": plan.get("autonomous_session_readiness"),
            "momentum_map": plan.get("momentum_map"),
            "frontier_map": plan.get("frontier_map"),
            "required_packets": plan.get("required_packets"),
        },
    }


def write_plan_frontloaded_artifacts(
    plan: dict[str, Any],
    *,
    track_id: str,
    artifacts_root: Path,
    include: tuple[str, ...] = ("objective_intent", "intent", "compiler", "coverage", "gaps", "sufficiency", "readiness"),
) -> dict[str, str]:
    payloads = build_plan_frontloaded_artifacts(plan, track_id=track_id)
    paths = plan_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    written: dict[str, str] = {}
    for key in include:
        path = paths[key]
        write_json_file(path, payloads[key])
        written[key] = str(path)
    return written


def _matches_plan_value(expected: Any, actual: Any) -> bool:
    return json.dumps(expected, sort_keys=True) == json.dumps(actual, sort_keys=True)


def _validate_plan_artifacts(
    *,
    plan: dict[str, Any],
    track_id: str,
    artifacts_root: Path,
    missing: list[str],
    blocked: list[str],
    runtime_compatible: list[bool],
) -> tuple[str, bool]:
    paths = plan_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    payloads: dict[str, dict[str, Any]] = {}
    for key, path in paths.items():
        if not path.exists() or not path.is_file():
            missing.append(f"plan_artifacts:{path.name}:missing")
            continue
        try:
            payload = load_json_file(path)
        except Exception as exc:
            blocked.append(f"plan_artifacts:{path.name}:{exc}")
            continue
        payloads[key] = payload

    compiler = payloads.get("compiler")
    objective_intent = payloads.get("objective_intent")
    intent = payloads.get("intent")
    if objective_intent is not None:
        if objective_intent.get("schema_version") != OBJECTIVE_INTENT_SCHEMA_VERSION:
            blocked.append("plan_artifacts:objective_intent:schema_version")
        if not objective_intent_matches_plan(plan=plan, objective_intent=objective_intent, track_id=track_id):
            blocked.append("plan_artifacts:objective_intent:payload_mismatch")
    if intent is not None:
        if intent.get("schema_version") != PLAN_INTENT_SCHEMA_VERSION:
            blocked.append("plan_artifacts:intent:schema_version")
        expected_intent_contract = (
            objective_intent.get("intent_contract")
            if isinstance(objective_intent, dict)
            else build_objective_intent_payload(track_id=track_id, plan=plan).get("intent_contract")
        )
        expected_clarification_governor = (
            objective_intent.get("clarification_governor")
            if isinstance(objective_intent, dict)
            else build_objective_intent_payload(track_id=track_id, plan=plan).get("clarification_governor")
        )
        if str(intent.get("objective_intent_ref", "")).strip() != "objective.intent.json":
            blocked.append("plan_artifacts:intent:objective_intent_ref")
        if not _matches_plan_value(expected_intent_contract, intent.get("intent_contract")):
            blocked.append("plan_artifacts:intent:intent_contract_mismatch")
        if not _matches_plan_value(expected_clarification_governor, intent.get("clarification_governor")):
            blocked.append("plan_artifacts:intent:clarification_governor_mismatch")
        plan_autonomy_level = str(plan.get("autonomy_level", "")).strip()
        if plan_autonomy_level and str(intent.get("autonomy_level", "")).strip() != plan_autonomy_level:
            blocked.append("plan_artifacts:intent:autonomy_level_mismatch")

    if compiler is not None:
        if compiler.get("schema_version") != "compiled-contract.v2.7":
            blocked.append("plan_artifacts:compiler:schema_version")
        expected_contract = build_compiled_contract(plan)
        if not _matches_plan_value(expected_contract, compiler.get("compiled_contract")):
            blocked.append("plan_artifacts:compiler:compiled_contract_mismatch")
        _, expected_packet_ids = _plan_ids(plan)
        if _as_string_list(compiler.get("packet_ids")) != expected_packet_ids:
            blocked.append("plan_artifacts:compiler:packet_ids_mismatch")
        expected_requirement_ids, _ = _plan_ids(plan)
        if _as_string_list(compiler.get("requirement_ids")) != expected_requirement_ids:
            blocked.append("plan_artifacts:compiler:requirement_ids_mismatch")

    coverage = payloads.get("coverage")
    if coverage is not None:
        if coverage.get("schema_version") != "plan-coverage.v1":
            blocked.append("plan_artifacts:coverage:schema_version")
        if not _matches_plan_value(plan.get("objective_coverage_map"), coverage.get("objective_coverage_map")):
            blocked.append("plan_artifacts:coverage:coverage_map_mismatch")
        if not _matches_plan_value(plan.get("requirement_risk_rank"), coverage.get("requirement_risk_rank")):
            blocked.append("plan_artifacts:coverage:requirement_risk_rank_mismatch")
        if not _matches_plan_value(plan.get("integration_map"), coverage.get("integration_map")):
            blocked.append("plan_artifacts:coverage:integration_map_mismatch")

    gaps = payloads.get("gaps")
    if gaps is not None:
        if gaps.get("schema_version") != "plan-gap-report.v1":
            blocked.append("plan_artifacts:gaps:schema_version")
        if not _matches_plan_value(plan.get("plan_gap_report"), gaps.get("plan_gap_report")):
            blocked.append("plan_artifacts:gaps:gap_report_mismatch")
        if not _matches_plan_value(plan.get("pre_delivery_gap_review"), gaps.get("pre_delivery_gap_review")):
            blocked.append("plan_artifacts:gaps:pre_delivery_gap_review_mismatch")
        if not _matches_plan_value(plan.get("hardening_budget"), gaps.get("hardening_budget")):
            blocked.append("plan_artifacts:gaps:hardening_budget_mismatch")

    sufficiency = payloads.get("sufficiency")
    plan_status = str(plan.get("plan_status", "")).strip()
    sufficiency_status = ""
    sufficiency_runtime_compatible = False
    if sufficiency is not None:
        if sufficiency.get("schema_version") != "plan-sufficiency.v1":
            blocked.append("plan_artifacts:sufficiency:schema_version")
        sufficiency_status = str(sufficiency.get("plan_status", "")).strip()
        if sufficiency_status != plan_status:
            blocked.append("plan_artifacts:sufficiency:plan_status_mismatch")
        if not _matches_plan_value(plan.get("pre_delivery_gap_review"), sufficiency.get("pre_delivery_gap_review")):
            blocked.append("plan_artifacts:sufficiency:pre_delivery_gap_review_mismatch")
        if not _matches_plan_value(plan.get("plan_sufficiency_report"), sufficiency.get("plan_sufficiency_report")):
            blocked.append("plan_artifacts:sufficiency:plan_sufficiency_report_mismatch")
        expected_runtime_checks = build_runtime_compatibility_checks(plan)
        expected_runtime_compatible = (
            all(expected_runtime_checks.values())
            and plan.get("plan_sufficiency_report", {}).get("runtime_compatible") is True
        )
        expected_verifier_status = (
            "approved"
            if plan_status == "execution_ready"
            and len(plan.get("plan_gap_report", {}).get("gaps_unresolved", []) or []) == 0
            and all(expected_runtime_checks.values())
            else "rejected"
        )
        verifier_status = str(sufficiency.get("plan_verifier_status", "")).strip()
        if verifier_status != expected_verifier_status:
            blocked.append("plan_artifacts:sufficiency:plan_verifier_status_mismatch")
        runtime_compatibility_checks = sufficiency.get("runtime_compatibility_checks")
        if not isinstance(runtime_compatibility_checks, dict):
            blocked.append("plan_artifacts:sufficiency:runtime_compatibility_checks")
        else:
            for key in RUNTIME_COMPATIBILITY_CHECKS:
                if runtime_compatibility_checks.get(key) is not expected_runtime_checks.get(key):
                    blocked.append(f"plan_artifacts:sufficiency:runtime_check_mismatch:{key}")
        sufficiency_runtime_compatible = sufficiency.get("runtime_compatible") is True
        if sufficiency_runtime_compatible != expected_runtime_compatible:
            blocked.append("plan_artifacts:sufficiency:runtime_compatible_mismatch")

    readiness = payloads.get("readiness")
    if readiness is not None:
        if readiness.get("schema_version") != "plan-readiness.v1":
            blocked.append("plan_artifacts:readiness:schema_version")
        if not _matches_plan_value(plan.get("autonomous_session_readiness"), readiness.get("autonomous_session_readiness")):
            blocked.append("plan_artifacts:readiness:autonomous_session_readiness_mismatch")
        if not _matches_plan_value(plan.get("momentum_map"), readiness.get("momentum_map")):
            blocked.append("plan_artifacts:readiness:momentum_map_mismatch")
        if not _matches_plan_value(plan.get("frontier_map"), readiness.get("frontier_map")):
            blocked.append("plan_artifacts:readiness:frontier_map_mismatch")
    runtime_compatible[0] = sufficiency_runtime_compatible
    return plan_status, sufficiency_runtime_compatible


def _load_jsonl_artifact(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValidationError("jsonl_item_not_object")
        items.append(payload)
    return items


def _load_fixed_json_artifact(path: Path, *, field_prefix: str, missing: list[str], blocked: list[str]) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        missing.append(f"{field_prefix}:artifact_missing")
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        blocked.append(f"{field_prefix}:artifact_not_json")
        return None
    if not isinstance(payload, dict):
        blocked.append(f"{field_prefix}:artifact_not_object")
        return None
    return payload


def _validate_session_artifacts(
    *,
    plan: dict[str, Any],
    track_id: str,
    artifacts_root: Path,
    missing: list[str],
    blocked: list[str],
    cwd: str | None = None,
) -> None:
    paths = session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    objective_intent = build_objective_intent_payload(track_id=track_id, plan=plan)
    expected = build_preplan_session_artifacts(objective_intent, track_id=track_id, cwd=cwd)
    runtime_expected = build_runtime_bootstrap_artifacts(plan, track_id=track_id, cwd=cwd)
    objective_id = stable_objective_id(track_id)

    session_payload = _load_fixed_json_artifact(
        paths["session"],
        field_prefix="session_artifacts:session",
        missing=missing,
        blocked=blocked,
    )
    if session_payload is not None:
        if session_payload.get("schema_version") != "objective-session.v1":
            blocked.append("session_artifacts:session:schema_version")
        if str(session_payload.get("objective_id", "")).strip() != objective_id:
            blocked.append("session_artifacts:session:objective_id_mismatch")
        for key in ("bootstrap_commands", "validation_commands", "clean_state_assertions"):
            actual = tuple(to_string_list(session_payload.get(key)))
            if actual != tuple(to_string_list(expected["session"].get(key))):
                blocked.append(f"session_artifacts:session:{key}_mismatch")
        packet_ids = to_string_list(session_payload.get("packet_ids"))
        requirement_ids = to_string_list(session_payload.get("requirement_ids"))
        if packet_ids != []:
            blocked.append("session_artifacts:session:packet_ids_mismatch")
        if requirement_ids != []:
            blocked.append("session_artifacts:session:requirement_ids_mismatch")

    checkpoint_payload = _load_fixed_json_artifact(
        paths["checkpoint"],
        field_prefix="session_artifacts:checkpoint",
        missing=missing,
        blocked=blocked,
    )
    if checkpoint_payload is not None:
        if checkpoint_payload.get("schema_version") != "objective-checkpoint.v1":
            blocked.append("session_artifacts:checkpoint:schema_version")
        for key in CHECKPOINT_REQUIRED_FIELDS:
            if key not in checkpoint_payload:
                missing.append(f"session_artifacts:checkpoint:{key}")
        if str(checkpoint_payload.get("objective_id", "")).strip() != objective_id:
            blocked.append("session_artifacts:checkpoint:objective_id_mismatch")
        for key in ("bootstrap_commands", "validation_commands", "clean_state_assertions"):
            actual = tuple(to_string_list(checkpoint_payload.get(key)))
            if actual not in {
                tuple(to_string_list(expected["checkpoint"].get(key))),
                tuple(to_string_list(runtime_expected["checkpoint_updates"].get(key))),
            }:
                blocked.append(f"session_artifacts:checkpoint:{key}_mismatch")

    context_payload = _load_fixed_json_artifact(
        paths["context_index"],
        field_prefix="session_artifacts:context_index",
        missing=missing,
        blocked=blocked,
    )
    if context_payload is not None:
        if context_payload.get("schema_version") != "objective-context-index.v1":
            blocked.append("session_artifacts:context_index:schema_version")
        categories = context_payload.get("categories")
        if not isinstance(categories, dict):
            missing.append("session_artifacts:context_index:categories")
        else:
            for key in CONTEXT_INDEX_CATEGORIES:
                if not isinstance(categories.get(key), list):
                    missing.append(f"session_artifacts:context_index:{key}")

    progress_path = paths["progress"]
    if not progress_path.exists() or not progress_path.is_file():
        missing.append("session_artifacts:progress:artifact_missing")
    else:
        try:
            progress_events = _load_jsonl_artifact(progress_path)
        except Exception as exc:
            blocked.append(f"session_artifacts:progress:{exc}")
        else:
            if not progress_events:
                missing.append("session_artifacts:progress:empty")
            else:
                if str(progress_events[0].get("objective_id", "")).strip() != objective_id:
                    blocked.append("session_artifacts:progress:objective_id_mismatch")


def _validate_objective_status(
    value: Any,
    *,
    required_packets: set[str],
    artifacts_root: Path,
    missing: list[str],
    blocked: list[str],
    cwd: str | None = None,
) -> tuple[str, str]:
    if not isinstance(value, dict):
        missing.append("implementation:objective_status:not_object")
        return "", ""
    for key in OBJECTIVE_STATUS_REQUIRED_FIELDS:
        if key not in value:
            missing.append(f"implementation:objective_status:{key}")
    if not _is_non_empty_string(value.get("objective_id"), 3):
        missing.append("implementation:objective_status:objective_id_invalid")
    closure_state = str(value.get("closure_state", "")).strip()
    if closure_state not in CLOSURE_STATES:
        missing.append("implementation:objective_status:closure_state_invalid")
    for key in ("completed_packets", "pending_packets", "blocked_packets", "deferred_packets", "boundary_shrunk_remainder"):
        if not isinstance(value.get(key), list):
            missing.append(f"implementation:objective_status:{key}_not_list")
    payload = _verify_json_artifact_path(
        path_value=value.get("artifact_path"),
        field_prefix="implementation:objective_status",
        artifacts_root=artifacts_root,
        missing=missing,
        blocked=blocked,
        cwd=cwd,
    )
    if payload is not None:
        result = validate_objective_status(payload, required_packets)
        missing.extend([f"implementation:{item}" for item in result.missing])
        blocked.extend([f"implementation:{item}" for item in result.blocked])
    accepted_type = _accepted_type_for_closure_state(closure_state)
    return closure_state, accepted_type


def _accepted_type_for_closure_state(closure_state: str) -> str:
    if closure_state == "OBJECTIVE_COMPLETE":
        return "ACCEPTED_SUCCESS"
    if closure_state in {
        "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK",
        "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED",
        "OBJECTIVE_BLOCKED_MIGRATION_DEFECT",
    }:
        return "ACCEPTED_BLOCKED"
    return ""


def _validate_objective_runtime_state(
    value: Any,
    *,
    artifacts_root: Path,
    objective_status: dict[str, Any] | None,
    support_confidence: dict[str, Any] | None,
    track_id: str | None,
    missing: list[str],
    blocked: list[str],
    evidence_score: list[int],
    cwd: str | None = None,
) -> tuple[dict[str, Any], str, str, bool]:
    if not isinstance(value, dict):
        missing.append("implementation:objective_runtime_state:not_object")
        return {}, "", "", False
    for key in OBJECTIVE_RUNTIME_STATE_REQUIRED_FIELDS:
        if key not in value:
            missing.append(f"implementation:objective_runtime_state:{key}")
    payload = _verify_json_artifact_path(
        path_value=value.get("artifact_path"),
        field_prefix="implementation:objective_runtime_state",
        artifacts_root=artifacts_root,
        missing=missing,
        blocked=blocked,
        cwd=cwd,
    )
    if payload is None:
        return {}, "", "", False

    if payload.get("schema_version") != OBJECTIVE_RUNTIME_STATE_SCHEMA_VERSION:
        missing.append("implementation:objective_runtime_state:schema_version_invalid")
    if not _is_non_empty_string(payload.get("objective_id"), 3):
        missing.append("implementation:objective_runtime_state:objective_id_invalid")
    runtime_track_id = str(payload.get("track_id", "")).strip()
    if not runtime_track_id:
        missing.append("implementation:objective_runtime_state:track_id_invalid")
    elif track_id and runtime_track_id != track_id:
        blocked.append("implementation:objective_runtime_state:track_id_mismatch")
    route_hint = str(payload.get("route_hint", "")).strip()
    if route_hint not in {"R1", "R2", "R3", "R4", "R5"}:
        missing.append("implementation:objective_runtime_state:route_hint_invalid")
    controller_mode = str(payload.get("controller_mode", "")).strip().lower()
    if controller_mode not in CONTROLLER_MODES:
        missing.append("implementation:objective_runtime_state:controller_mode_invalid")
    lifecycle_status = str(payload.get("lifecycle_status", "")).strip()
    if lifecycle_status not in RUNTIME_LIFECYCLE_STATUSES:
        missing.append("implementation:objective_runtime_state:lifecycle_status_invalid")
    closure_state = str(payload.get("closure_state", "")).strip()
    if closure_state not in CLOSURE_STATES:
        missing.append("implementation:objective_runtime_state:closure_state_invalid")
    for key in ("required_work_remaining", "material_optional_work_remaining", "stop_allowed"):
        if not isinstance(payload.get(key), bool):
            missing.append(f"implementation:objective_runtime_state:{key}_invalid")
    if not isinstance(payload.get("last_verifier_result"), dict):
        missing.append("implementation:objective_runtime_state:last_verifier_result_not_object")

    runtime_objective_id = str(payload.get("objective_id", "")).strip()
    objective_payload = objective_status if isinstance(objective_status, dict) else {}
    if objective_payload:
        status_objective_id = str(objective_payload.get("objective_id", "")).strip()
        if runtime_objective_id and status_objective_id and runtime_objective_id != status_objective_id:
            blocked.append("implementation:objective_runtime_state:objective_id_mismatch")
        status_closure_state = str(objective_payload.get("closure_state", "")).strip()
        if closure_state and status_closure_state and closure_state != status_closure_state:
            blocked.append("implementation:objective_runtime_state:closure_state_mismatch")

    support_payload = support_confidence if isinstance(support_confidence, dict) else {}
    if support_payload:
        support_objective_id = str(support_payload.get("objective_id", "")).strip()
        support_track_id = str(support_payload.get("track_id", "")).strip()
        if runtime_objective_id and support_objective_id and runtime_objective_id != support_objective_id:
            blocked.append("implementation:objective_runtime_state:support_confidence_objective_id_mismatch")
        if runtime_track_id and support_track_id and runtime_track_id != support_track_id:
            blocked.append("implementation:objective_runtime_state:support_confidence_track_id_mismatch")
        support_risk = str(support_payload.get("unsupported_closure_risk") or "none").strip()
        runtime_risk = str(payload.get("unsupported_closure_risk") or "none").strip()
        if runtime_risk != support_risk:
            blocked.append("implementation:objective_runtime_state:unsupported_closure_risk_mismatch")
        if payload.get("stop_allowed") is True and runtime_risk not in {"", "none"}:
            blocked.append("implementation:objective_runtime_state:unsupported_closure_risk_present")

    if payload.get("stop_allowed") is True and not _accepted_type_for_closure_state(closure_state):
        blocked.append("implementation:objective_runtime_state:stop_allowed_requires_accepted_closure")
    if isinstance(payload.get("stop_allowed"), bool) and payload.get("stop_allowed") is not True:
        blocked.append("implementation:objective_runtime_state:stop_not_allowed")
    if payload.get("lifecycle_status") == "approved" and payload.get("required_work_remaining") is True:
        blocked.append("implementation:objective_runtime_state:approved_with_required_work_remaining")
    if payload.get("lifecycle_status") == "approved" and payload.get("material_optional_work_remaining") is True:
        blocked.append("implementation:objective_runtime_state:approved_with_material_optional_work_remaining")

    verifier_result = payload.get("last_verifier_result") if isinstance(payload.get("last_verifier_result"), dict) else {}
    verifier_status = str(verifier_result.get("status", "")).strip().lower()
    if route_hint in {"R3", "R4"} and verifier_status and verifier_status != "approve" and payload.get("stop_allowed") is True:
        blocked.append("implementation:objective_runtime_state:last_verifier_result_conflict")

    evidence_score[0] += 2
    return payload, closure_state, _accepted_type_for_closure_state(closure_state), payload.get("stop_allowed") is True


def _validate_schedule_artifact(
    value: Any,
    *,
    artifacts_root: Path,
    packets: dict[str, Any],
    objective_status: dict[str, Any] | None,
    missing: list[str],
    blocked: list[str],
    evidence_score: list[int],
    cwd: str | None = None,
) -> None:
    payload = _verify_json_artifact_path(
        path_value=value,
        field_prefix="implementation:schedule_artifact",
        artifacts_root=artifacts_root,
        missing=missing,
        blocked=blocked,
        cwd=cwd,
    )
    if payload is None:
        return
    for key in SCHEDULE_ARTIFACT_REQUIRED_FIELDS:
        if key not in payload:
            missing.append(f"implementation:schedule_artifact:{key}")
    if int(payload.get("total_packet_count", -1) or -1) != len(packets):
        missing.append("implementation:schedule_artifact:total_packet_count_mismatch")
    if objective_status is not None:
        result = validate_schedule_state(payload, packets, objective_status)
        missing.extend([f"implementation:{item}" for item in result.missing])
        blocked.extend([f"implementation:{item}" for item in result.blocked])
    evidence_score[0] += 3


def _validate_supporting_runtime_artifact(
    value: Any,
    *,
    field_name: str,
    artifacts_root: Path,
    missing: list[str],
    blocked: list[str],
    evidence_score: list[int],
    cwd: str | None = None,
) -> None:
    if not isinstance(value, dict):
        missing.append(f"implementation:{field_name}:not_object")
        return
    _verify_json_artifact_path(
        path_value=value.get("artifact_path"),
        field_prefix=f"implementation:{field_name}",
        artifacts_root=artifacts_root,
        missing=missing,
        blocked=blocked,
        cwd=cwd,
    )
    evidence_score[0] += 1


def _validate_support_confidence_artifact(
    value: Any,
    *,
    artifacts_root: Path,
    objective_closure_state: str,
    missing: list[str],
    blocked: list[str],
    evidence_score: list[int],
    cwd: str | None = None,
) -> None:
    if not isinstance(value, dict):
        missing.append("implementation:support_confidence:not_object")
        return
    for key in SUPPORT_CONFIDENCE_REQUIRED_FIELDS:
        if key not in value:
            missing.append(f"implementation:support_confidence:{key}")
    payload = _verify_json_artifact_path(
        path_value=value.get("artifact_path"),
        field_prefix="implementation:support_confidence",
        artifacts_root=artifacts_root,
        missing=missing,
        blocked=blocked,
        cwd=cwd,
    )
    if payload is None:
        return
    if payload.get("schema_version") != OBJECTIVE_RUNTIME_SUPPORT_CONFIDENCE_SCHEMA_VERSION:
        missing.append("implementation:support_confidence:schema_version_invalid")
    if not isinstance(payload.get("packet_support"), list):
        missing.append("implementation:support_confidence:packet_support_not_list")
    if not isinstance(payload.get("support_gap_reasons"), list):
        missing.append("implementation:support_confidence:support_gap_reasons_not_list")
    if not isinstance(payload.get("external_support_coverage"), dict):
        missing.append("implementation:support_confidence:external_support_coverage_not_object")
    if not isinstance(payload.get("support_remediation_available"), bool):
        missing.append("implementation:support_confidence:support_remediation_available_invalid")
    if objective_closure_state in {"OBJECTIVE_COMPLETE", "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK"}:
        if payload.get("support_backed_closure") is not True:
            blocked.append("implementation:support_confidence:unsupported_closure")
        if str(payload.get("unsupported_closure_risk") or "none").strip() not in {"", "none"}:
            blocked.append("implementation:support_confidence:unsupported_closure_risk_present")
    evidence_score[0] += 2


def _validate_packet_verdicts(
    value: Any,
    *,
    packet_ids: set[str],
    artifacts_root: Path,
    missing: list[str],
    blocked: list[str],
    evidence_score: list[int],
    cwd: str | None = None,
) -> None:
    if not isinstance(value, list) or not value:
        missing.append("implementation:packet_verdicts")
        return
    seen: set[str] = set()
    for idx, item in enumerate(value, start=1):
        prefix = f"implementation:packet_verdicts:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue
        for key in PACKET_VERDICT_REQUIRED_FIELDS:
            if key not in item:
                missing.append(f"{prefix}:{key}")
        packet_id = str(item.get("packet_id", "")).strip()
        if not packet_id or packet_id not in packet_ids:
            blocked.append(f"{prefix}:packet_id_unknown")
        else:
            seen.add(packet_id)
        if str(item.get("runtime_state", "")).strip() not in RUNTIME_STATES:
            missing.append(f"{prefix}:runtime_state_invalid")
        if str(item.get("verifier_output", "")).strip() not in VERIFIER_OUTPUTS:
            missing.append(f"{prefix}:verifier_output_invalid")
        if not _is_non_empty_string(item.get("allowed_scope_status"), 3):
            missing.append(f"{prefix}:allowed_scope_status_invalid")
        if not _is_non_empty_string(item.get("strategy_name"), 3):
            missing.append(f"{prefix}:strategy_name_invalid")
        if not _is_non_empty_string(item.get("runner_kind"), 3):
            missing.append(f"{prefix}:runner_kind_invalid")
        _verify_json_artifact_path(
            path_value=item.get("artifact_path"),
            field_prefix=prefix,
            artifacts_root=artifacts_root,
            missing=missing,
            blocked=blocked,
            cwd=cwd,
        )
        evidence_score[0] += 1
    missing_packet_ids = packet_ids.difference(seen)
    for packet_id in sorted(missing_packet_ids):
        missing.append(f"implementation:packet_verdicts:missing:{packet_id}")


def _validate_supporting_artifact_path(
    value: Any,
    *,
    field_name: str,
    artifacts_root: Path,
    missing: list[str],
    blocked: list[str],
    evidence_score: list[int],
    cwd: str | None = None,
) -> None:
    path_s = str(value or "").strip()
    if not path_s:
        missing.append(f"implementation:{field_name}:artifact_path")
        return
    try:
        artifact_path = resolve_proof_path(path_s, artifacts_root, cwd)
    except ValidationError as exc:
        blocked.append(f"implementation:{field_name}:{exc}")
        return
    if not artifact_path.exists() or not artifact_path.is_file():
        missing.append(f"implementation:{field_name}:artifact_missing")
    evidence_score[0] += 1


def _validate_migration_fallback(
    value: Any,
    *,
    artifacts_root: Path,
    missing: list[str],
    blocked: list[str],
    cwd: str | None = None,
) -> bool:
    if not isinstance(value, dict):
        missing.append("implementation:migration_fallback:not_object")
        return False
    for key in MIGRATION_FALLBACK_REQUIRED_FIELDS:
        if key not in value:
            missing.append(f"implementation:migration_fallback:{key}")
    used = value.get("used")
    if not isinstance(used, bool):
        missing.append("implementation:migration_fallback:used_invalid")
        return False
    if not _is_non_empty_string(value.get("reason"), 3):
        missing.append("implementation:migration_fallback:reason_invalid")
    artifact_path = value.get("artifact_path")
    if used:
        _verify_json_artifact_path(
            path_value=artifact_path,
            field_prefix="implementation:migration_fallback",
            artifacts_root=artifacts_root,
            missing=missing,
            blocked=blocked,
            cwd=cwd,
        )
    return used


def validate_plan_contract(
    plan: dict[str, Any],
    *,
    artifacts_root: Path | None = None,
    track_id: str | None = None,
    cwd: str | None = None,
) -> ContractResult:
    missing: list[str] = []
    blocked: list[str] = []
    runtime_compatible = [False]

    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        blocked.append(f"schema_version:{plan.get('schema_version')}")

    for key in PLAN_REQUIRED_FIELDS:
        if key not in plan:
            missing.append(key)

    tests = plan.get("tests")
    if not isinstance(tests, dict):
        missing.append("tests:not_object")
        tests = {}

    smoke_gates = tests.get("smoke_gates")
    if not isinstance(smoke_gates, list):
        top_level = plan.get("smoke_gates")
        if isinstance(top_level, list):
            smoke_gates = top_level
            missing.append("tests.smoke_gates:preferred_location")
        else:
            smoke_gates = []

    smoke_100_pass, smoke_quality = _validate_smoke_gates(
        smoke_gates,
        PLAN_SMOKE_REQUIRED_FIELDS,
        missing,
        blocked,
        "tests.smoke_gates",
    )
    _validate_definition_of_done(plan.get("definition_of_done"), missing)
    _validate_non_goals(plan.get("non_goals"), missing)
    _validate_quality_bar(plan.get("quality_bar"), missing)
    requirement_ids = _validate_objective_requirements(plan.get("objective_requirements"), missing, blocked)
    normalized_packets = _validate_packets(plan.get("packets"), plan.get("required_packets"), missing, blocked)
    packet_ids = set(normalized_packets)
    _validate_intent_contract(plan.get("intent_contract"), missing, blocked)
    _validate_clarification_governor(plan.get("clarification_governor"), missing)
    readiness_status, readiness_safe_momentum = _validate_autonomous_session_readiness(
        plan.get("autonomous_session_readiness"),
        missing,
        blocked,
    )
    _validate_momentum_map(
        plan.get("momentum_map"),
        packet_ids=packet_ids,
        missing=missing,
        blocked=blocked,
    )
    _validate_frontier_map(
        plan.get("frontier_map"),
        packet_ids=packet_ids,
        missing=missing,
        blocked=blocked,
    )
    if not _is_non_empty_string(plan.get("constraints"), 8) and not isinstance(plan.get("constraints"), (list, dict)):
        missing.append("constraints")
    _validate_objective_coverage_map(
        plan.get("objective_coverage_map"),
        requirement_ids=requirement_ids,
        packet_ids=packet_ids,
        missing=missing,
        blocked=blocked,
    )
    _validate_assumptions_ledger(plan.get("assumptions_ledger"), missing)
    _validate_authority_map(plan.get("authority_map"), missing)
    _validate_integration_map(
        plan.get("integration_map"),
        packet_ids=packet_ids,
        missing=missing,
        blocked=blocked,
    )
    _validate_evidence_plan(plan.get("evidence_plan"), missing)
    _validate_matrix(plan.get("failure_mode_matrix"), name="failure_mode_matrix", missing=missing)
    _validate_matrix(plan.get("edge_case_matrix"), name="edge_case_matrix", missing=missing)
    _validate_hardening_budget(plan.get("hardening_budget"), missing)
    _validate_reuse_first_policy(plan, missing=missing, blocked=blocked)
    plan_status = str(plan.get("plan_status", "")).strip()
    if plan_status not in PLAN_STATUS_VALUES:
        missing.append("plan_status")
    unresolved_gap_count = _validate_plan_gap_report(plan.get("plan_gap_report"), missing)
    _validate_pre_delivery_gap_review(
        plan.get("pre_delivery_gap_review"),
        missing=missing,
        blocked=blocked,
    )
    sufficiency_status, sufficiency_runtime_compatible = _validate_plan_sufficiency_report(
        plan.get("plan_sufficiency_report"),
        unresolved_gap_count=unresolved_gap_count,
        missing=missing,
        blocked=blocked,
    )
    _validate_requirement_risk_rank(
        plan.get("requirement_risk_rank"),
        requirement_ids=requirement_ids,
        packet_ids=packet_ids,
        missing=missing,
        blocked=blocked,
    )
    harness_required = _validate_session_harness(
        plan.get("session_harness"),
        packet_ids=packet_ids,
        missing=missing,
        blocked=blocked,
    )
    session_harness = plan.get("session_harness")
    route_hint = (
        str(session_harness.get("route_hint", "")).strip()
        if isinstance(session_harness, dict)
        else ""
    )
    _validate_contract_closure(
        plan,
        route_hint=route_hint,
        missing=missing,
        blocked=blocked,
    )
    _validate_overengineering_guardrails(
        plan,
        route_hint=route_hint,
        missing=missing,
        blocked=blocked,
    )
    execution_shape = str(plan.get("execution_shape", "")).strip()
    _validate_solution_ladder(
        plan,
        route_hint=route_hint,
        missing=missing,
        blocked=blocked,
    )
    if route_hint in {"R3", "R4"}:
        if execution_shape not in {"single_lane", "bounded_swarm"}:
            missing.append("execution_shape")
        elif execution_shape == "bounded_swarm":
            if not _has_meaningful_policy_text(plan.get("swarm_justification"), min_length=20):
                blocked.append("execution_shape:bounded_swarm_requires_swarm_justification")
            scheduler_policy = plan.get("scheduler_policy") if isinstance(plan.get("scheduler_policy"), dict) else {}
            if not isinstance(scheduler_policy.get("lane_caps"), dict):
                missing.append("scheduler_policy:lane_caps")
            if not scheduler_policy.get("route_swarm_cap"):
                missing.append("scheduler_policy:route_swarm_cap")
            if len(to_string_list(scheduler_policy.get("frontier_dispatch_order"))) == 0:
                missing.append("scheduler_policy:frontier_dispatch_order")
            if len(to_string_list(scheduler_policy.get("reviewer_barrier_points"))) == 0:
                missing.append("scheduler_policy:reviewer_barrier_points")
            if scheduler_policy.get("convergence_required_for_closure") is not True:
                missing.append("scheduler_policy:convergence_required_for_closure")
            swarm_packets = [
                packet for packet in normalized_packets.values()
                if packet.get("swarm_eligible") is True
            ]
            if len(swarm_packets) < 2:
                blocked.append("execution_shape:bounded_swarm_insufficient_frontier")
            elif all(
                str(packet.get("execution_mode", "")).strip() == "sequence_required"
                or str(packet.get("parallelism_class", "")).strip() == "serial"
                for packet in swarm_packets
            ):
                blocked.append("execution_shape:bounded_swarm_effectively_serial")
            if any(not str(packet.get("packet_lane", "")).strip() for packet in swarm_packets):
                missing.append("packets:packet_lane")
    elif execution_shape and execution_shape != "single_lane":
        blocked.append("execution_shape:single_lane_required")
    _validate_objective_closure_policy(plan.get("objective_closure_policy"), missing)
    _validate_migration_fallback_policy(plan.get("migration_fallback_policy"), missing)
    _validate_scheduler_policy(plan.get("scheduler_policy"), missing, blocked)
    autonomy_level = str(plan.get("autonomy_level", "")).strip()
    if autonomy_level not in AUTONOMY_LEVEL_VALUES:
        missing.append("autonomy_level")
    if plan_status != "execution_ready":
        missing.append("plan_status:execution_ready_required")
    if sufficiency_status and sufficiency_status != "execution_ready":
        missing.append("plan_sufficiency_report:execution_ready_required")
    if readiness_status and readiness_status != "execution_ready":
        missing.append("autonomous_session_readiness:execution_ready_required")
    if plan_status == "execution_ready" and not sufficiency_runtime_compatible:
        blocked.append("plan_sufficiency_report:runtime_compatible_required")
    if plan_status == "execution_ready" and not readiness_safe_momentum:
        missing.append("autonomous_session_readiness:safe_momentum_required")
    if track_id and artifacts_root is not None:
        artifact_plan_status, artifact_runtime_compatible = _validate_plan_artifacts(
            plan=plan,
            track_id=track_id,
            artifacts_root=artifacts_root,
            missing=missing,
            blocked=blocked,
            runtime_compatible=runtime_compatible,
        )
        if artifact_plan_status and artifact_plan_status != "execution_ready":
            missing.append("plan_artifacts:sufficiency:execution_ready_required")
        if artifact_plan_status == "execution_ready" and not artifact_runtime_compatible:
            blocked.append("plan_artifacts:sufficiency:runtime_compatible_required")
        if harness_required:
            _validate_session_artifacts(
                plan=plan,
                track_id=track_id,
                artifacts_root=artifacts_root,
                missing=missing,
                blocked=blocked,
                cwd=cwd,
            )

    return ContractResult(
        missing=sorted(set(missing)),
        blocked=sorted(set(blocked)),
        smoke_100_pass=smoke_100_pass,
        smoke_quality_score=smoke_quality,
        plan_status=plan_status,
        runtime_compatible=sufficiency_runtime_compatible and runtime_compatible[0] if track_id and artifacts_root is not None else sufficiency_runtime_compatible,
    )


def _verify_proof_reference(
    *,
    payload: dict[str, Any],
    field_prefix: str,
    artifacts_root: Path,
    missing: list[str],
    blocked: list[str],
    expected_track_id: str | None = None,
    expected_stage: str | None = None,
    expected_name_prefixes: tuple[str, ...] = (),
    require_success_exit: bool = False,
    cwd: str | None = None,
) -> None:
    proof_artifact = str(payload.get("proof_artifact", "")).strip()
    proof_hash = str(payload.get("proof_hash", "")).strip()

    if not proof_artifact:
        missing.append(f"{field_prefix}:proof_artifact")
        return
    if not proof_hash:
        missing.append(f"{field_prefix}:proof_hash")
        return

    try:
        artifact_path = resolve_proof_path(proof_artifact, artifacts_root, cwd)
    except ValidationError as exc:
        blocked.append(f"{field_prefix}:{exc}")
        return

    if not artifact_path.exists() or not artifact_path.is_file():
        missing.append(f"{field_prefix}:proof_artifact_missing")
        return

    actual_hash = sha256_file(artifact_path)
    if actual_hash != proof_hash:
        blocked.append(f"{field_prefix}:proof_hash_mismatch")
        return

    try:
        manifest = json.loads(artifact_path.read_text(encoding="utf-8"))
    except Exception:
        blocked.append(f"{field_prefix}:proof_artifact_not_json")
        return

    if not isinstance(manifest, dict):
        blocked.append(f"{field_prefix}:proof_artifact_not_object")
        return

    if manifest.get("schema_version") != CAPTURE_SCHEMA_VERSION:
        blocked.append(f"{field_prefix}:proof_schema_version")
    if manifest.get("producer") != CAPTURE_PRODUCER:
        blocked.append(f"{field_prefix}:proof_producer")
    if expected_track_id and str(manifest.get("track_id", "")).strip() != expected_track_id:
        blocked.append(f"{field_prefix}:proof_track_id_mismatch")
    if expected_stage:
        manifest_stage = normalize_stage(manifest.get("stage"))
        if manifest_stage != expected_stage:
            blocked.append(f"{field_prefix}:proof_stage_mismatch")
    if expected_name_prefixes:
        manifest_name = str(manifest.get("name", "")).strip().lower()
        if not any(manifest_name.startswith(prefix.lower()) for prefix in expected_name_prefixes):
            blocked.append(f"{field_prefix}:proof_name_prefix_mismatch")
    if require_success_exit:
        try:
            exit_code = int(manifest.get("exit_code", 1))
        except Exception:
            blocked.append(f"{field_prefix}:proof_exit_code_invalid")
        else:
            if exit_code != 0:
                blocked.append(f"{field_prefix}:proof_exit_code_nonzero")


def _validate_budget_outcome(
    value: Any,
    *,
    plan: dict[str, Any] | None,
    changed_files: list[str],
    artifacts_root: Path,
    missing: list[str],
    blocked: list[str],
    expected_track_id: str | None,
    cwd: str | None,
) -> tuple[bool, bool]:
    if not isinstance(value, dict):
        missing.append("implementation:budget_outcome:not_object")
        return False, False

    for key in BUDGET_OUTCOME_REQUIRED_FIELDS:
        if key not in value:
            missing.append(f"implementation:budget_outcome:{key}")

    _verify_proof_reference(
        payload=value,
        field_prefix="implementation:budget_outcome",
        artifacts_root=artifacts_root,
        missing=missing,
        blocked=blocked,
        expected_track_id=expected_track_id,
        expected_name_prefixes=("budget-",),
        cwd=cwd,
    )

    planned_files = value.get("planned_files_touched")
    planned_loc = value.get("planned_loc")
    actual_files = value.get("actual_files_touched")
    actual_loc = value.get("actual_loc")
    exception_used = value.get("exception_used")
    exception_justification = value.get("exception_justification")

    if not isinstance(planned_files, int) or planned_files < 0:
        missing.append("implementation:budget_outcome:planned_files_touched_invalid")
    if not isinstance(planned_loc, int) or planned_loc < 0:
        missing.append("implementation:budget_outcome:planned_loc_invalid")
    if not isinstance(actual_files, int) or actual_files < 0:
        missing.append("implementation:budget_outcome:actual_files_touched_invalid")
    if not isinstance(actual_loc, int) or actual_loc < 0:
        missing.append("implementation:budget_outcome:actual_loc_invalid")
    if not isinstance(exception_used, bool):
        missing.append("implementation:budget_outcome:exception_used_invalid")
    if not _normalized_policy_text(exception_justification):
        missing.append("implementation:budget_outcome:exception_justification")

    if any(
        item in missing
        for item in (
            "implementation:budget_outcome:planned_files_touched_invalid",
            "implementation:budget_outcome:planned_loc_invalid",
            "implementation:budget_outcome:actual_files_touched_invalid",
            "implementation:budget_outcome:actual_loc_invalid",
            "implementation:budget_outcome:exception_used_invalid",
        )
    ):
        return False, bool(exception_used)

    plan_estimated_files = plan.get("estimated_files_touched") if isinstance(plan, dict) else None
    plan_estimated_loc = plan.get("estimated_loc") if isinstance(plan, dict) else None
    if isinstance(plan_estimated_files, int) and planned_files != plan_estimated_files:
        blocked.append("implementation:budget_outcome:planned_files_mismatch")
    if isinstance(plan_estimated_loc, int) and planned_loc != plan_estimated_loc:
        blocked.append("implementation:budget_outcome:planned_loc_mismatch")

    changed_count = len([item for item in changed_files if str(item).strip()])
    if actual_files != changed_count:
        blocked.append("implementation:budget_outcome:actual_files_mismatch")

    budget_within_plan = actual_files <= planned_files and actual_loc <= planned_loc
    plan_exception = _normalized_policy_text(plan.get("budget_exception_justification")) if isinstance(plan, dict) else ""
    exception_is_meaningful = _has_meaningful_policy_text(exception_justification, min_length=16)
    plan_exception_is_meaningful = _has_meaningful_policy_text(plan_exception, min_length=16)
    if not budget_within_plan and not (exception_used is True and exception_is_meaningful and plan_exception_is_meaningful):
        blocked.append("implementation:budget_outcome:unapproved_budget_drift")

    return budget_within_plan, bool(exception_used)


def _is_non_empty_string(value: Any, minimum: int = 1) -> bool:
    return isinstance(value, str) and len(value.strip()) >= minimum


def _is_frontend_scope(
    changed_files: list[Any],
    prompt_contract_used: list[Any],
    frontend_roundtrip_evidence: list[Any],
) -> bool:
    frontend_extensions = (".tsx", ".jsx", ".css", ".scss", ".sass", ".less", ".html", ".vue", ".svelte")
    frontend_signals = ("frontend", "ui", "figma", "design")

    for path in changed_files:
        path_text = str(path).strip().lower()
        if not path_text:
            continue
        if path_text.endswith(frontend_extensions):
            return True
        if any(token in path_text for token in ("/frontend/", "/ui/", "/components/", "/styles/", "/pages/")):
            return True

    for item in prompt_contract_used:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip().lower()
        if name and any(signal in name for signal in frontend_signals):
            return True

    if frontend_roundtrip_evidence:
        return True

    return False


def _validate_memory_retrieval_evidence(
    value: Any,
    *,
    missing: list[str],
    evidence_score: list[int],
) -> None:
    if not isinstance(value, list) or not value:
        missing.append("implementation:memory_retrieval_evidence")
        return

    for idx, item in enumerate(value, start=1):
        prefix = f"implementation:memory_retrieval_evidence:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue

        for key in MEMORY_RETRIEVAL_REQUIRED_FIELDS:
            if key not in item:
                missing.append(f"{prefix}:{key}")

        if not _is_non_empty_string(item.get("tool"), 3):
            missing.append(f"{prefix}:tool_invalid")
        if not _is_non_empty_string(item.get("query"), 3):
            missing.append(f"{prefix}:query_invalid")

        result_count = item.get("result_count")
        if not isinstance(result_count, int) or result_count < 0:
            missing.append(f"{prefix}:result_count_invalid")
        elif result_count > 0:
            evidence_score[0] += 1

        evidence_score[0] += 1


def _validate_preferences_applied(
    value: Any,
    *,
    missing: list[str],
    evidence_score: list[int],
) -> None:
    if not isinstance(value, list):
        missing.append("implementation:preferences_applied:not_list")
        return
    if not value:
        missing.append("implementation:preferences_applied:empty")
        return

    for idx, item in enumerate(value, start=1):
        prefix = f"implementation:preferences_applied:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue

        for key in PREFERENCE_APPLIED_REQUIRED_FIELDS:
            if key not in item:
                missing.append(f"{prefix}:{key}")

        if not _is_non_empty_string(item.get("key"), 3):
            missing.append(f"{prefix}:key_invalid")
        if not _is_non_empty_string(item.get("decision"), 3):
            missing.append(f"{prefix}:decision_invalid")
        if not _is_non_empty_string(item.get("rationale"), 10):
            missing.append(f"{prefix}:rationale_too_short")
        else:
            evidence_score[0] += 1


def _validate_skill_trigger_eval_results(
    value: Any,
    *,
    missing: list[str],
    blocked: list[str],
    evidence_score: list[int],
) -> None:
    if not isinstance(value, list) or not value:
        missing.append("implementation:skill_trigger_eval_results")
        return

    for idx, item in enumerate(value, start=1):
        prefix = f"implementation:skill_trigger_eval_results:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue

        for key in SKILL_TRIGGER_EVAL_REQUIRED_FIELDS:
            if key not in item:
                missing.append(f"{prefix}:{key}")

        if not _is_non_empty_string(item.get("skill"), 3):
            missing.append(f"{prefix}:skill_invalid")

        fp = item.get("false_positive_rate")
        fn = item.get("false_negative_rate")
        passed = item.get("threshold_passed")

        if not isinstance(fp, (int, float)) or fp < 0 or fp > 1:
            missing.append(f"{prefix}:false_positive_rate_invalid")
        if not isinstance(fn, (int, float)) or fn < 0 or fn > 1:
            missing.append(f"{prefix}:false_negative_rate_invalid")
        if not isinstance(passed, bool):
            missing.append(f"{prefix}:threshold_passed_invalid")

        if isinstance(fp, (int, float)) and fp > 0.10:
            blocked.append(f"{prefix}:false_positive_rate_above_threshold")
        if isinstance(fn, (int, float)) and fn > 0.10:
            blocked.append(f"{prefix}:false_negative_rate_above_threshold")
        if passed is False:
            blocked.append(f"{prefix}:threshold_failed")

        if isinstance(fp, (int, float)) and isinstance(fn, (int, float)) and fp <= 0.10 and fn <= 0.10:
            evidence_score[0] += 2


def _validate_prompt_contract_used(
    value: Any,
    *,
    missing: list[str],
    evidence_score: list[int],
) -> None:
    if not isinstance(value, list) or not value:
        missing.append("implementation:prompt_contract_used")
        return

    for idx, item in enumerate(value, start=1):
        prefix = f"implementation:prompt_contract_used:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue

        for key in PROMPT_CONTRACT_REQUIRED_FIELDS:
            if key not in item:
                missing.append(f"{prefix}:{key}")

        if not _is_non_empty_string(item.get("name"), 3):
            missing.append(f"{prefix}:name_invalid")
        if not _is_non_empty_string(item.get("required_context"), 12):
            missing.append(f"{prefix}:required_context_too_short")
        if not _is_non_empty_string(item.get("required_constraints"), 12):
            missing.append(f"{prefix}:required_constraints_too_short")
        if not _is_non_empty_string(item.get("verification_section"), 12):
            missing.append(f"{prefix}:verification_section_too_short")
        if not _is_non_empty_string(item.get("done_when"), 12):
            missing.append(f"{prefix}:done_when_too_short")
        else:
            evidence_score[0] += 1


def _validate_frontend_roundtrip(
    value: Any,
    *,
    require_non_empty: bool,
    missing: list[str],
    evidence_score: list[int],
) -> None:
    if not isinstance(value, list):
        missing.append("implementation:frontend_roundtrip_evidence:not_list")
        return

    if require_non_empty and not value:
        missing.append("implementation:frontend_roundtrip_evidence:required_for_frontend_scope")
        return

    for idx, item in enumerate(value, start=1):
        prefix = f"implementation:frontend_roundtrip_evidence:{idx}"
        if not isinstance(item, dict):
            missing.append(f"{prefix}:item_not_object")
            continue

        for key in FRONTEND_ROUNDTRIP_REQUIRED_FIELDS:
            if key not in item:
                missing.append(f"{prefix}:{key}")

        if not _is_non_empty_string(item.get("step"), 3):
            missing.append(f"{prefix}:step_invalid")
        if not _is_non_empty_string(item.get("evidence"), 8):
            missing.append(f"{prefix}:evidence_too_short")
        else:
            evidence_score[0] += 1


def _validate_string_list_field(
    value: Any,
    *,
    field_name: str,
    minimum_item_len: int,
    missing: list[str],
    evidence_score: list[int] | None = None,
) -> list[str]:
    items = to_string_list(value)
    if not items:
        missing.append(f"implementation:{field_name}")
        return []
    for idx, item in enumerate(items, start=1):
        if len(item.strip()) < minimum_item_len:
            missing.append(f"implementation:{field_name}:{idx}_too_short")
    if evidence_score is not None:
        evidence_score[0] += min(len(items), 3)
    return items


def _validate_checkpoint_metadata(
    impl: dict[str, Any],
    *,
    plan: dict[str, Any] | None,
    artifacts_root: Path,
    track_id: str | None,
    packet_ids: set[str],
    accepted_packet_ids: set[str],
    missing: list[str],
    blocked: list[str],
    evidence_score: list[int],
    frontend_roundtrip_evidence: Any,
    cwd: str | None = None,
) -> None:
    session_harness = plan.get("session_harness") if isinstance(plan, dict) and isinstance(plan.get("session_harness"), dict) else {}
    harness_required = session_harness.get("required") is True
    if not harness_required:
        return

    bootstrap_commands = _validate_string_list_field(
        impl.get("bootstrap_commands"),
        field_name="bootstrap_commands",
        minimum_item_len=3,
        missing=missing,
        evidence_score=evidence_score,
    )
    validation_commands = _validate_string_list_field(
        impl.get("validation_commands"),
        field_name="validation_commands",
        minimum_item_len=3,
        missing=missing,
        evidence_score=evidence_score,
    )
    clean_state_assertions = _validate_string_list_field(
        impl.get("clean_state_assertions"),
        field_name="clean_state_assertions",
        minimum_item_len=8,
        missing=missing,
        evidence_score=evidence_score,
    )

    checkpoint_blocked = impl.get("checkpoint_blocked")
    if not isinstance(checkpoint_blocked, bool):
        missing.append("implementation:checkpoint_blocked_invalid")
        checkpoint_blocked = False

    checkpoint_commit = str(impl.get("checkpoint_commit", "")).strip()
    checkpoint_block_reason = str(impl.get("checkpoint_block_reason", "")).strip()
    checkpoint_block_evidence = str(impl.get("checkpoint_block_evidence", "")).strip()
    rollback_validation = impl.get("rollback_validation") if isinstance(impl.get("rollback_validation"), dict) else {}
    rollback_proof_ref = str(rollback_validation.get("proof_artifact", "")).strip()

    if checkpoint_blocked:
        if not checkpoint_block_reason:
            missing.append("implementation:checkpoint_block_reason")
        if not checkpoint_block_evidence:
            missing.append("implementation:checkpoint_block_evidence")
    elif accepted_packet_ids and not checkpoint_commit:
        missing.append("implementation:checkpoint_commit_required")

    if not track_id:
        missing.append("implementation:track_id_required_for_checkpoint")
        return

    paths = session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    checkpoint_payload = _load_fixed_json_artifact(
        paths["checkpoint"],
        field_prefix="implementation:objective_checkpoint",
        missing=missing,
        blocked=blocked,
    )
    feature_payload = _load_fixed_json_artifact(
        paths["feature_list"],
        field_prefix="implementation:feature_list",
        missing=missing,
        blocked=blocked,
    )
    context_payload = _load_fixed_json_artifact(
        paths["context_index"],
        field_prefix="implementation:context_index",
        missing=missing,
        blocked=blocked,
    )
    momentum_payload = _load_fixed_json_artifact(
        paths["momentum"],
        field_prefix="implementation:momentum",
        missing=missing,
        blocked=blocked,
    )
    blockers_payload = _load_fixed_json_artifact(
        paths["blockers"],
        field_prefix="implementation:blockers",
        missing=missing,
        blocked=blocked,
    )

    if checkpoint_payload is not None:
        if checkpoint_payload.get("schema_version") != "objective-checkpoint.v1":
            blocked.append("implementation:objective_checkpoint:schema_version")
        for key in CHECKPOINT_REQUIRED_FIELDS:
            if key not in checkpoint_payload:
                missing.append(f"implementation:objective_checkpoint:{key}")
        if str(checkpoint_payload.get("objective_id", "")).strip() != stable_objective_id(track_id):
            blocked.append("implementation:objective_checkpoint:objective_id_mismatch")
        last_verified_packet_ids = set(to_string_list(checkpoint_payload.get("last_verified_packet_ids")))
        if not accepted_packet_ids.issubset(last_verified_packet_ids):
            missing.append("implementation:objective_checkpoint:last_verified_packet_ids_incomplete")
        if bootstrap_commands and bootstrap_commands != to_string_list(checkpoint_payload.get("bootstrap_commands")):
            blocked.append("implementation:objective_checkpoint:bootstrap_commands_mismatch")
        if validation_commands and validation_commands != to_string_list(checkpoint_payload.get("validation_commands")):
            blocked.append("implementation:objective_checkpoint:validation_commands_mismatch")
        if clean_state_assertions and clean_state_assertions != to_string_list(checkpoint_payload.get("clean_state_assertions")):
            blocked.append("implementation:objective_checkpoint:clean_state_assertions_mismatch")
        repo_state_summary = checkpoint_payload.get("repo_state_summary")
        if not isinstance(repo_state_summary, dict):
            missing.append("implementation:objective_checkpoint:repo_state_summary")
        checkpoint_strategy = str(checkpoint_payload.get("checkpoint_strategy", "")).strip()
        if checkpoint_strategy != "git_checkpoint_required":
            blocked.append("implementation:objective_checkpoint:checkpoint_strategy_invalid")
        if accepted_packet_ids and not str(checkpoint_payload.get("checkpoint_attempted_at", "")).strip():
            missing.append("implementation:objective_checkpoint:checkpoint_attempted_at")
        if checkpoint_blocked:
            if checkpoint_payload.get("checkpoint_blocked") is not True:
                blocked.append("implementation:objective_checkpoint:checkpoint_blocked_mismatch")
        else:
            if checkpoint_commit and str(checkpoint_payload.get("checkpoint_commit", "")).strip() != checkpoint_commit:
                blocked.append("implementation:objective_checkpoint:checkpoint_commit_mismatch")
            rollback_ref = str(checkpoint_payload.get("rollback_validation_ref", "")).strip()
            if not rollback_ref:
                missing.append("implementation:objective_checkpoint:rollback_validation_ref")
            elif rollback_proof_ref:
                try:
                    rollback_ref_resolved = str(Path(rollback_ref).expanduser().resolve())
                    rollback_proof_ref_resolved = str(Path(rollback_proof_ref).expanduser().resolve())
                except Exception:
                    rollback_ref_resolved = rollback_ref
                    rollback_proof_ref_resolved = rollback_proof_ref
                if rollback_ref_resolved != rollback_proof_ref_resolved:
                    blocked.append("implementation:objective_checkpoint:rollback_validation_ref_mismatch")
        if not _is_non_empty_string(checkpoint_payload.get("last_forward_movement"), 3):
            missing.append("implementation:objective_checkpoint:last_forward_movement_invalid")
        if not _is_non_empty_string(checkpoint_payload.get("stagnation_risk"), 3):
            missing.append("implementation:objective_checkpoint:stagnation_risk_invalid")
        escalation_candidates = set(to_string_list(checkpoint_payload.get("escalation_candidates")))
        if not escalation_candidates.issubset(packet_ids):
            blocked.append("implementation:objective_checkpoint:escalation_candidates_unknown")
        next_recommended_packet = str(checkpoint_payload.get("next_recommended_packet", "")).strip()
        frontier = set(to_string_list(checkpoint_payload.get("current_frontier")))
        if next_recommended_packet and next_recommended_packet not in packet_ids:
            blocked.append("implementation:objective_checkpoint:next_recommended_packet_unknown")
        if not frontier and packet_ids and not accepted_packet_ids:
            missing.append("implementation:objective_checkpoint:current_frontier_empty")

    progress_path = paths["progress"]
    if not progress_path.exists() or not progress_path.is_file():
        missing.append("implementation:objective_progress:artifact_missing")
    else:
        try:
            progress_events = _load_jsonl_artifact(progress_path)
        except Exception as exc:
            blocked.append(f"implementation:objective_progress:{exc}")
        else:
            if not progress_events:
                missing.append("implementation:objective_progress:empty")
            else:
                last_event = progress_events[-1]
                if str(last_event.get("event_type", "")).strip() != "checkpoint":
                    missing.append("implementation:objective_progress:last_event_not_checkpoint")
                if checkpoint_payload is not None:
                    if str(last_event.get("checkpoint_id", "")).strip() != str(checkpoint_payload.get("checkpoint_id", "")).strip():
                        blocked.append("implementation:objective_progress:checkpoint_id_mismatch")
                    if set(to_string_list(last_event.get("last_verified_packet_ids"))) != set(
                        to_string_list(checkpoint_payload.get("last_verified_packet_ids"))
                    ):
                        blocked.append("implementation:objective_progress:last_verified_packet_ids_mismatch")
                evidence_score[0] += 1

    if feature_payload is not None:
        if feature_payload.get("schema_version") != "objective-feature-list.v1":
            blocked.append("implementation:feature_list:schema_version")
        features = feature_payload.get("features")
        if not isinstance(features, list) or not features:
            missing.append("implementation:feature_list:features")
        else:
            requirement_ids = {
                str(item.get("requirement_id", "")).strip()
                for item in plan.get("objective_requirements", [])
                if isinstance(item, dict) and str(item.get("requirement_id", "")).strip()
            }
            seen_requirements: set[str] = set()
            for idx, item in enumerate(features, start=1):
                prefix = f"implementation:feature_list:{idx}"
                if not isinstance(item, dict):
                    missing.append(f"{prefix}:item_not_object")
                    continue
                requirement_id = str(item.get("requirement_id", "")).strip()
                if requirement_id not in requirement_ids:
                    blocked.append(f"{prefix}:requirement_id_unknown")
                    continue
                seen_requirements.add(requirement_id)
                status = str(item.get("status", "")).strip()
                if status not in FEATURE_STATUS_VALUES:
                    missing.append(f"{prefix}:status_invalid")
                mapped_packets = set(to_string_list(item.get("packet_ids")))
                if not mapped_packets:
                    missing.append(f"{prefix}:packet_ids_invalid")
                elif not mapped_packets.issubset(packet_ids):
                    blocked.append(f"{prefix}:packet_ids_unknown")
                if accepted_packet_ids and mapped_packets and mapped_packets.issubset(accepted_packet_ids) and status != "verified":
                    missing.append(f"{prefix}:status_should_be_verified")
                if accepted_packet_ids == packet_ids and status == "pending":
                    missing.append(f"{prefix}:pending_not_allowed_after_complete")
            for requirement_id in sorted(requirement_ids - seen_requirements):
                missing.append(f"implementation:feature_list:missing_requirement:{requirement_id}")
        evidence_score[0] += 1

    if context_payload is not None:
        if context_payload.get("schema_version") != "objective-context-index.v1":
            blocked.append("implementation:context_index:schema_version")
        categories = context_payload.get("categories")
        if not isinstance(categories, dict):
            missing.append("implementation:context_index:categories")
        else:
            for key in CONTEXT_INDEX_CATEGORIES:
                if not isinstance(categories.get(key), list):
                    missing.append(f"implementation:context_index:{key}")
        evidence_score[0] += 1

    if momentum_payload is not None:
        if momentum_payload.get("schema_version") != "objective-momentum.v1":
            blocked.append("implementation:momentum:schema_version")
        entries = momentum_payload.get("entries")
        if not isinstance(entries, list) or not entries:
            missing.append("implementation:momentum:entries")
        evidence_score[0] += 1

    if blockers_payload is not None:
        if blockers_payload.get("schema_version") != "objective-blockers.v1":
            blocked.append("implementation:blockers:schema_version")
        items = blockers_payload.get("items")
        if not isinstance(items, list):
            missing.append("implementation:blockers:items")
        evidence_score[0] += 1

    if session_harness.get("ui_evidence_required") is True and not frontend_roundtrip_evidence:
        missing.append("implementation:frontend_roundtrip_evidence:required_for_ui_harness")


def validate_impl_contract(
    impl: dict[str, Any],
    *,
    artifacts_root: Path,
    track_id: str | None = None,
    cwd: str | None = None,
    plan: dict[str, Any] | None = None,
) -> ContractResult:
    artifacts_root = artifacts_root.resolve()
    missing: list[str] = []
    blocked: list[str] = []
    evidence_score = [0]
    budget_within_plan = False
    budget_exception_used = False
    route_hint = _plan_route_hint(plan) if isinstance(plan, dict) else ""
    closure_required = _plan_requires_contract_closure(plan, route_hint=route_hint) if isinstance(plan, dict) else False

    if impl.get("schema_version") != IMPLEMENTATION_SCHEMA_VERSION:
        blocked.append(f"schema_version:{impl.get('schema_version')}")

    for key in IMPLEMENTATION_REQUIRED_FIELDS:
        if key not in impl:
            missing.append(f"implementation:{key}")

    changed_files = impl.get("changed_files")
    if not isinstance(changed_files, list) or not any(str(v).strip() for v in changed_files):
        missing.append("implementation:changed_files")
        normalized_changed_files: list[str] = []
    else:
        normalized_changed_files = [str(v).strip() for v in changed_files if str(v).strip()]
        evidence_score[0] += min(6, len(normalized_changed_files))

    budget_within_plan, budget_exception_used = _validate_budget_outcome(
        impl.get("budget_outcome"),
        plan=plan,
        changed_files=normalized_changed_files,
        artifacts_root=artifacts_root,
        missing=missing,
        blocked=blocked,
        expected_track_id=track_id,
        cwd=cwd,
    )
    _validate_closure_drift_report(
        impl.get("closure_drift_report"),
        required=closure_required,
        missing=missing,
        blocked=blocked,
    )

    tests_run = impl.get("tests_run")
    if not isinstance(tests_run, list) or not tests_run:
        missing.append("implementation:tests_run")
    else:
        for idx, item in enumerate(tests_run, start=1):
            if not isinstance(item, dict):
                missing.append(f"implementation:tests_run:{idx}:item_not_object")
                continue
            for key in TEST_RUN_REQUIRED_FIELDS:
                if key not in item:
                    missing.append(f"implementation:tests_run:{idx}:{key}")
            _verify_proof_reference(
                payload=item,
                field_prefix=f"implementation:tests_run:{idx}",
                artifacts_root=artifacts_root,
                missing=missing,
                blocked=blocked,
                expected_track_id=track_id,
                expected_name_prefixes=("worker-", "verifier-", "test-"),
                require_success_exit=str(item.get("status", "")).strip().lower() in PASS_STATUSES,
                cwd=cwd,
            )
            if str(item.get("status", "")).strip().lower() in PASS_STATUSES:
                evidence_score[0] += 1
            if len(str(item.get("result", "")).strip()) >= 12:
                evidence_score[0] += 1

    smoke_results = impl.get("smoke_results")
    smoke_100_pass = False
    if not isinstance(smoke_results, list):
        missing.append("implementation:smoke_results:not_list")
        smoke_results = []

    smoke_100_pass, smoke_quality = _validate_smoke_gates(
        smoke_results,
        SMOKE_RESULT_REQUIRED_FIELDS,
        missing,
        blocked,
        "implementation:smoke_results",
    )
    evidence_score[0] += smoke_quality

    for idx, gate in enumerate(smoke_results, start=1):
        if isinstance(gate, dict):
            stage = normalize_stage(gate.get("stage"))
            _verify_proof_reference(
                payload=gate,
                field_prefix=f"implementation:smoke_results:{idx}",
                artifacts_root=artifacts_root,
                missing=missing,
                blocked=blocked,
                expected_track_id=track_id,
                expected_stage=stage or None,
                expected_name_prefixes=("smoke-",),
                require_success_exit=str(gate.get("status", "")).strip().lower() in PASS_STATUSES,
                cwd=cwd,
            )

    logging_evidence = impl.get("logging_evidence")
    if not isinstance(logging_evidence, list) or not logging_evidence:
        missing.append("implementation:logging_evidence")
    else:
        for idx, item in enumerate(logging_evidence, start=1):
            if not isinstance(item, dict):
                missing.append(f"implementation:logging_evidence:{idx}:item_not_object")
                continue
            for key in LOG_EVIDENCE_REQUIRED_FIELDS:
                if key not in item:
                    missing.append(f"implementation:logging_evidence:{idx}:{key}")
            _verify_proof_reference(
                payload=item,
                field_prefix=f"implementation:logging_evidence:{idx}",
                artifacts_root=artifacts_root,
                missing=missing,
                blocked=blocked,
                expected_track_id=track_id,
                expected_name_prefixes=("log-",),
                cwd=cwd,
            )
        if any(isinstance(item, dict) for item in logging_evidence):
            evidence_score[0] += 2

    rollback = impl.get("rollback_validation")
    if not isinstance(rollback, dict):
        missing.append("implementation:rollback_validation:not_object")
    else:
        for key in ROLLBACK_REQUIRED_FIELDS:
            if key not in rollback:
                missing.append(f"implementation:rollback_validation:{key}")
        _verify_proof_reference(
            payload=rollback,
            field_prefix="implementation:rollback_validation",
            artifacts_root=artifacts_root,
            missing=missing,
            blocked=blocked,
            expected_track_id=track_id,
            expected_name_prefixes=("rollback-",),
            require_success_exit=str(rollback.get("result", "")).strip().lower() in PASS_STATUSES,
            cwd=cwd,
        )
        if rollback.get("executed") is True:
            evidence_score[0] += 2
        if str(rollback.get("result", "")).strip().lower() in PASS_STATUSES:
            evidence_score[0] += 2

    memory_retrieval_evidence = impl.get("memory_retrieval_evidence")
    _validate_memory_retrieval_evidence(
        memory_retrieval_evidence,
        missing=missing,
        evidence_score=evidence_score,
    )

    preferences_applied = impl.get("preferences_applied")
    _validate_preferences_applied(
        preferences_applied,
        missing=missing,
        evidence_score=evidence_score,
    )

    skill_trigger_eval_results = impl.get("skill_trigger_eval_results")
    _validate_skill_trigger_eval_results(
        skill_trigger_eval_results,
        missing=missing,
        blocked=blocked,
        evidence_score=evidence_score,
    )

    prompt_contract_used = impl.get("prompt_contract_used")
    _validate_prompt_contract_used(
        prompt_contract_used,
        missing=missing,
        evidence_score=evidence_score,
    )

    frontend_roundtrip_evidence = impl.get("frontend_roundtrip_evidence")
    requires_frontend_roundtrip = _is_frontend_scope(
        normalized_changed_files,
        prompt_contract_used if isinstance(prompt_contract_used, list) else [],
        frontend_roundtrip_evidence if isinstance(frontend_roundtrip_evidence, list) else [],
    )
    _validate_frontend_roundtrip(
        frontend_roundtrip_evidence,
        require_non_empty=requires_frontend_roundtrip,
        missing=missing,
        evidence_score=evidence_score,
    )

    validation_plan_payload = impl.get("validation_plan") if isinstance(impl.get("validation_plan"), dict) else {}
    generated_validation_packets = (
        [
            packet
            for packet in validation_plan_payload.get("generated_packets", [])
            if isinstance(packet, dict) and str(packet.get("packet_id", "")).strip()
        ]
        if isinstance(validation_plan_payload, dict)
        else []
    )
    plan_packets = (
        {
            str(packet.get("packet_id", "")).strip(): packet
            for packet in plan.get("packets", [])
            if isinstance(packet, dict) and str(packet.get("packet_id", "")).strip()
        }
        if isinstance(plan, dict)
        else {}
    )
    plan_packets.update(
        {
            str(packet.get("packet_id", "")).strip(): packet
            for packet in generated_validation_packets
            if str(packet.get("packet_id", "")).strip()
        }
    )
    required_packet_ids = (
        {
            str(packet_id).strip()
            for packet_id in plan.get("required_packets", [])
            if str(packet_id).strip()
        }
        if isinstance(plan, dict)
        else set()
    )
    required_packet_ids.update(
        {
            str(packet.get("packet_id", "")).strip()
            for packet in generated_validation_packets
            if str(packet.get("packet_id", "")).strip()
        }
    )
    objective_status = impl.get("objective_status")
    status_closure_state, status_accepted_type = _validate_objective_status(
        objective_status,
        required_packets=required_packet_ids or set(plan_packets),
        artifacts_root=artifacts_root,
        missing=missing,
        blocked=blocked,
        cwd=cwd,
    )
    support_confidence = impl.get("support_confidence") if isinstance(impl.get("support_confidence"), dict) else {}
    runtime_state_payload, closure_state, accepted_type, runtime_stop_allowed = _validate_objective_runtime_state(
        impl.get("objective_runtime_state"),
        artifacts_root=artifacts_root,
        objective_status=objective_status if isinstance(objective_status, dict) else None,
        support_confidence=support_confidence if isinstance(support_confidence, dict) else None,
        track_id=track_id,
        missing=missing,
        blocked=blocked,
        evidence_score=evidence_score,
        cwd=cwd,
    )
    if not closure_state:
        closure_state = status_closure_state
    if not accepted_type:
        accepted_type = status_accepted_type

    packet_verdicts = impl.get("packet_verdicts")
    packet_ids = required_packet_ids or set(plan_packets)
    if not packet_ids and isinstance(packet_verdicts, list):
        packet_ids = {str(item.get("packet_id", "")).strip() for item in packet_verdicts if isinstance(item, dict)}
    if not packet_ids and isinstance(objective_status, dict):
        packet_ids = {
            str(item).strip()
            for item in (
                objective_status.get("completed_packets", [])
                + objective_status.get("pending_packets", [])
                + objective_status.get("blocked_packets", [])
                + objective_status.get("deferred_packets", [])
                + objective_status.get("boundary_shrunk_remainder", [])
            )
            if str(item).strip()
        }
    _validate_packet_verdicts(
        packet_verdicts,
        packet_ids=packet_ids,
        artifacts_root=artifacts_root,
        missing=missing,
        blocked=blocked,
        evidence_score=evidence_score,
        cwd=cwd,
    )
    accepted_packet_ids = {
        str(item.get("packet_id", "")).strip()
        for item in (packet_verdicts or [])
        if isinstance(item, dict)
        and str(item.get("packet_id", "")).strip()
        and str(item.get("runtime_state", "")).strip() == "accepted"
        and str(item.get("verifier_output", "")).strip() == "accepted"
    }

    _validate_schedule_artifact(
        impl.get("schedule_artifact"),
        artifacts_root=artifacts_root,
        packets=plan_packets or {packet_id: {"packet_id": packet_id, "allowed_scope": []} for packet_id in packet_ids},
        objective_status=objective_status if isinstance(objective_status, dict) else None,
        missing=missing,
        blocked=blocked,
        evidence_score=evidence_score,
        cwd=cwd,
    )
    _validate_supporting_runtime_artifact(
        impl.get("objective_summary"),
        field_name="objective_summary",
        artifacts_root=artifacts_root,
        missing=missing,
        blocked=blocked,
        evidence_score=evidence_score,
        cwd=cwd,
    )
    _validate_supporting_runtime_artifact(
        impl.get("validation_plan"),
        field_name="validation_plan",
        artifacts_root=artifacts_root,
        missing=missing,
        blocked=blocked,
        evidence_score=evidence_score,
        cwd=cwd,
    )
    _validate_supporting_runtime_artifact(
        impl.get("execution_coverage"),
        field_name="execution_coverage",
        artifacts_root=artifacts_root,
        missing=missing,
        blocked=blocked,
        evidence_score=evidence_score,
        cwd=cwd,
    )
    _validate_support_confidence_artifact(
        support_confidence,
        artifacts_root=artifacts_root,
        objective_closure_state=closure_state,
        missing=missing,
        blocked=blocked,
        evidence_score=evidence_score,
        cwd=cwd,
    )
    _validate_supporting_artifact_path(
        impl.get("execution_ledger"),
        field_name="execution_ledger",
        artifacts_root=artifacts_root,
        missing=missing,
        blocked=blocked,
        evidence_score=evidence_score,
        cwd=cwd,
    )
    _validate_supporting_artifact_path(
        impl.get("packet_results_artifact"),
        field_name="packet_results_artifact",
        artifacts_root=artifacts_root,
        missing=missing,
        blocked=blocked,
        evidence_score=evidence_score,
        cwd=cwd,
    )
    _validate_checkpoint_metadata(
        impl,
        plan=plan,
        artifacts_root=artifacts_root,
        track_id=track_id,
        packet_ids=packet_ids,
        accepted_packet_ids=accepted_packet_ids,
        missing=missing,
        blocked=blocked,
        evidence_score=evidence_score,
        frontend_roundtrip_evidence=frontend_roundtrip_evidence,
        cwd=cwd,
    )

    if closure_state == "OBJECTIVE_COMPLETE":
        if impl.get("checkpoint_blocked") is True:
            blocked.append("implementation:objective_complete:checkpoint_blocked_not_allowed")
        rollback_payload = impl.get("rollback_validation") if isinstance(impl.get("rollback_validation"), dict) else {}
        if rollback_payload.get("executed") is not True:
            missing.append("implementation:objective_complete:rollback_validation_required")
        if str(rollback_payload.get("result", "")).strip().lower() not in PASS_STATUSES:
            blocked.append("implementation:objective_complete:rollback_validation_failed")

    migration_fallback_used = _validate_migration_fallback(
        impl.get("migration_fallback"),
        artifacts_root=artifacts_root,
        missing=missing,
        blocked=blocked,
        cwd=cwd,
    )

    if closure_state == "OBJECTIVE_BLOCKED_MIGRATION_DEFECT" and not migration_fallback_used:
        missing.append("implementation:migration_fallback:required_for_blocked_migration_defect")
    if closure_state == "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK":
        if not isinstance(objective_status, dict) or not objective_status.get("boundary_shrunk_remainder"):
            missing.append("implementation:objective_status:boundary_shrunk_remainder_required")

    closure_eval = evaluate_objective_closure(
        packets=[
            {
                "packet_id": str(item.get("packet_id", "")).strip(),
                "runtime_state": str(item.get("runtime_state", "")).strip(),
            }
            for item in (packet_verdicts or [])
            if isinstance(item, dict)
        ],
        boundary_shrunk_remainder=objective_status.get("boundary_shrunk_remainder", []) if isinstance(objective_status, dict) else [],
        migration_fallback_used=migration_fallback_used,
    )
    checkpoint_blocked_impl = impl.get("checkpoint_blocked") is True
    checkpoint_blocked_closure = (
        closure_state == "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED"
        and checkpoint_blocked_impl
        and closure_eval["closure_state"] == "OBJECTIVE_COMPLETE"
        and accepted_type == "ACCEPTED_BLOCKED"
        and closure_eval["accepted_type"] == "ACCEPTED_SUCCESS"
    )
    if closure_state and closure_eval["closure_state"] and closure_state != closure_eval["closure_state"] and not checkpoint_blocked_closure:
        blocked.append("implementation:objective_status:closure_state_mismatch")
    if accepted_type and closure_eval["accepted_type"] and accepted_type != closure_eval["accepted_type"] and not checkpoint_blocked_closure:
        blocked.append("implementation:objective_status:accepted_type_mismatch")
    if runtime_state_payload and runtime_stop_allowed and not _accepted_type_for_closure_state(closure_state):
        blocked.append("implementation:objective_runtime_state:stop_allowed_requires_accepted_closure")

    return ContractResult(
        missing=sorted(set(missing)),
        blocked=sorted(set(blocked)),
        smoke_100_pass=smoke_100_pass,
        evidence_quality_score=evidence_score[0],
        objective_closure_state=closure_state,
        accepted_type=accepted_type or closure_eval["accepted_type"],
        migration_fallback_used=migration_fallback_used,
        budget_within_plan=budget_within_plan,
        budget_exception_used=budget_exception_used,
    )


@contextmanager
def file_lock(lock_path: Path, timeout_seconds: float = 10.0, stale_after_seconds: int = 300):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    token = f"{os.getpid()}:{time.time_ns()}"
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, token.encode("utf-8"))
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > stale_after_seconds:
                    lock_path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.time() - start >= timeout_seconds:
                raise TimeoutError(f"lock_timeout:{lock_path}")
            time.sleep(0.05)

    try:
        yield
    finally:
        try:
            current = lock_path.read_text(encoding="utf-8")
            if current == token:
                lock_path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
