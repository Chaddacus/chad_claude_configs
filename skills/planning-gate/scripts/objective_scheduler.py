#!/usr/bin/env python3
"""Deterministic packet-DAG scheduler helpers for governed control plane."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from execution_strategies import execution_readiness_failures, strategy_spec, validate_strategy_packet

STRUCTURAL_STATES = {"ready", "blocked_dependency", "blocked_authority", "deferred"}
EXECUTION_MODES = {"parallel_safe", "sequence_required", "blocked", "deferred"}
RUNTIME_STATES = {
    "queued",
    "dispatched",
    "in_progress",
    "awaiting_verifier",
    "accepted",
    "rejected_rework",
    "escalated",
    "cancelled",
}
PARALLELISM_POLICIES = {"serial_only", "bounded_parallel", "aggressive_parallel"}
EXECUTION_SHAPES = {"single_lane", "bounded_swarm"}
PACKET_LANES = {"explorer", "worker", "validator", "reviewer"}
PARALLELISM_CLASSES = {"isolated", "bounded", "serial"}
LANE_CAP_KEYS = PACKET_LANES | {"planner"}
DEPENDENCY_MODES = {"accepted_upstream", "explicit_stub"}
FAILURE_CLASSES = {"incidental", "structural", "authority", "migration_defect"}
ADMISSION_CHECKS = (
    "packet validity",
    "autonomy readiness",
    "dependency readiness",
    "conflict check",
    "retry-budget check",
)
RECOMPUTE_TRIGGERS = (
    "verifier_verdict",
    "packet_cancellation",
    "repacketization",
    "escalation_decision",
    "boundary_shrink_decision",
)
TERMINAL_STOP_CONDITIONS = {
    "objective_closed",
    "escalation_required_no_runnable_packets",
    "migration_defect_fallback_invoked",
    "unrecoverable_graph_invalidity",
}
OBJECTIVE_CLOSURE_STATES = {
    "OBJECTIVE_COMPLETE",
    "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK",
    "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED",
    "OBJECTIVE_BLOCKED_MIGRATION_DEFECT",
    "OBJECTIVE_REJECTED_FALSE_COMPLETION",
}
CLOSURE_STATES = OBJECTIVE_CLOSURE_STATES
DEFAULT_RECOMPUTE_TRIGGERS = RECOMPUTE_TRIGGERS
DEFAULT_TERMINAL_STOP_CONDITIONS = TERMINAL_STOP_CONDITIONS
VERDICT_STATES = {
    "accepted",
    "rejected_rework",
    "blocked_boundary",
    "blocked_migration_defect",
    "escalate",
}
ACTIVE_RUNTIME_STATES = {"queued", "dispatched", "in_progress", "awaiting_verifier"}
VERIFIER_OUTPUTS = VERDICT_STATES
FORWARD_MOTION_STATES = {
    "closure_advancing",
    "uncertainty_reducing",
    "blocker_isolating",
    "invalid_noop",
}
FRONTIER_MOVEMENT_REASONS = {
    "dependency_unlocked",
    "packet_completed",
    "packet_rewritten",
    "blocker_isolated",
    "authority_blocked",
}


@dataclass
class SchedulerValidationResult:
    missing: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def extend(self, *, missing: list[str] | None = None, blocked: list[str] | None = None) -> None:
        if missing:
            self.missing.extend(missing)
        if blocked:
            self.blocked.extend(blocked)


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _non_empty_string(value: Any, minimum: int = 1) -> bool:
    return isinstance(value, str) and len(value.strip()) >= minimum


def _packet_lane(packet: dict[str, Any]) -> str:
    lane = str(packet.get("packet_lane", "")).strip()
    if lane in PACKET_LANES:
        return lane
    strategy_name = str(packet.get("execution_strategy", "")).strip()
    if strategy_name in {"test_command", "lint_command", "typecheck_command", "build_command", "smoke_command", "schema_check_command", "validation_command", "command_capture"}:
        return "validator"
    if strategy_name == "review_evidence_packet":
        return "reviewer"
    if strategy_name == "artifact_transform":
        return "explorer"
    return "worker"


def _parallelism_class(packet: dict[str, Any]) -> str:
    value = str(packet.get("parallelism_class", "")).strip()
    if value in PARALLELISM_CLASSES:
        return value
    if str(packet.get("execution_mode", "")).strip() == "sequence_required":
        return "serial"
    return "bounded"


def _normalize_lane_caps(lane_caps: Any) -> dict[str, int]:
    normalized: dict[str, int] = {}
    if not isinstance(lane_caps, dict):
        return normalized
    for lane, raw_value in lane_caps.items():
        lane_name = str(lane).strip()
        if lane_name not in PACKET_LANES:
            continue
        try:
            value = int(raw_value)
        except Exception:
            continue
        if value > 0:
            normalized[lane_name] = value
    return normalized


def _lane_count(packets: dict[str, dict[str, Any]], packet_ids: set[str]) -> dict[str, int]:
    counts = {lane: 0 for lane in PACKET_LANES}
    for packet_id in packet_ids:
        packet = packets.get(packet_id)
        if not packet:
            continue
        counts[_packet_lane(packet)] += 1
    return counts


def _compute_dispatch_analysis(
    *,
    packets: dict[str, dict[str, Any]],
    accepted_packets: set[str],
    active_packets: set[str],
    retry_counters: dict[str, Any],
    max_parallel_packets: int,
    parallelism_policy: str,
    execution_shape: str = "single_lane",
    lane_caps: dict[str, int] | None = None,
    route_swarm_cap: int | None = None,
    frontier_dispatch_order: list[str] | None = None,
    reviewer_barrier_points: list[str] | None = None,
) -> dict[str, Any]:
    if parallelism_policy == "serial_only":
        capacity = 1
    else:
        capacity = max_parallel_packets
    if route_swarm_cap is not None:
        try:
            capacity = min(capacity, max(int(route_swarm_cap), 0))
        except Exception:
            capacity = 0
    available_slots = max(capacity - len(active_packets), 0)
    lane_caps_map = _normalize_lane_caps(lane_caps)
    frontier_order = [lane for lane in (frontier_dispatch_order or []) if lane in PACKET_LANES]
    if not frontier_order:
        frontier_order = ["validator", "explorer", "worker", "reviewer"]

    if available_slots <= 0:
        return {
            "selected": [],
            "admission_failures": {},
            "runnable_candidates": [],
            "runnable_but_not_dispatched": [],
            "dispatch_block_reasons": {},
            "lane_queue_depths": {lane: 0 for lane in PACKET_LANES},
            "active_packets_by_lane": {
                lane: sorted(
                    packet_id for packet_id in active_packets if _packet_lane(packets.get(packet_id, {})) == lane
                )
                for lane in PACKET_LANES
            },
            "awaiting_reviewer_barrier": [],
            "convergence_status": "capacity_exhausted",
            "swarm_status": execution_shape,
        }

    occupied_scopes = _active_scopes(packets, active_packets)
    occupied_shared_surfaces = _active_shared_surfaces(packets, active_packets)
    active_lane_counts = _lane_count(packets, active_packets)
    candidate_packets: list[dict[str, Any]] = []
    admission_failures: dict[str, list[str]] = {}
    for packet_id in sorted(packets):
        if packet_id in accepted_packets or packet_id in active_packets:
            continue
        packet = packets[packet_id]
        reasons: list[str] = []
        execution_mode = str(packet.get("execution_mode", "")).strip()
        if execution_mode in {"blocked", "deferred"}:
            reasons.append("execution_mode_blocked")
        if not packet_autonomy_ready(packet):
            reasons.append("not_autonomy_ready")
        if not dependency_ready(packet, accepted_packets):
            reasons.append("dependencies_not_ready")
        if retry_budget_exhausted(packet_id, retry_counters):
            reasons.append("retry_budget_exhausted")
        reasons.extend(execution_readiness_failures(packet))
        strategy = strategy_spec(str(packet.get("execution_strategy", "")).strip())
        if strategy and strategy.get("supports_parallel") is False and (active_packets):
            reasons.append("parallel_dispatch_not_allowed")
        if conflicts_with_scopes(packet, occupied_scopes):
            reasons.append("allowed_scope_conflict")
        if conflicts_with_shared_surfaces(packet, occupied_shared_surfaces):
            reasons.append("shared_surface_conflict")
        lane = _packet_lane(packet)
        if lane_caps_map.get(lane) is not None and active_lane_counts.get(lane, 0) >= lane_caps_map[lane]:
            reasons.append("lane_cap_reached")
        if str(packet.get("swarm_eligible", True)).lower() not in {"true", "false"}:
            reasons.append("swarm_eligible_invalid")
        if execution_shape == "single_lane" and packet.get("swarm_eligible") is False and parallelism_policy != "serial_only":
            reasons.append("swarm_ineligible")
        parallelism_class = _parallelism_class(packet)
        if lane == "reviewer" and reviewer_barrier_points and active_packets:
            reasons.append("reviewer_barrier_pending")
        if reasons:
            admission_failures[packet_id] = sorted(set(reasons))
            continue
        candidate_packets.append(
            {
                "packet_id": packet_id,
                "packet": packet,
                "lane": lane,
                "parallelism_class": parallelism_class,
                "priority": frontier_order.index(lane) if lane in frontier_order else len(frontier_order),
            }
        )

    lane_queue_depths = {lane: 0 for lane in PACKET_LANES}
    for candidate in candidate_packets:
        lane_queue_depths[candidate["lane"]] += 1

    candidate_packets.sort(key=lambda item: (item["priority"], item["packet_id"]))
    selected: list[str] = []
    blocked_dispatch_reasons: dict[str, list[str]] = {}
    occupied_scopes_selected = set(occupied_scopes)
    occupied_shared_selected = set(occupied_shared_surfaces)
    current_lane_counts = dict(active_lane_counts)
    serial_candidate = next((item for item in candidate_packets if item["parallelism_class"] == "serial"), None)
    if serial_candidate:
        return {
            "selected": [serial_candidate["packet_id"]],
            "admission_failures": admission_failures,
            "runnable_candidates": [item["packet_id"] for item in candidate_packets],
            "runnable_but_not_dispatched": [item["packet_id"] for item in candidate_packets if item["packet_id"] != serial_candidate["packet_id"]],
            "dispatch_block_reasons": {
                item["packet_id"]: ["serial_barrier_active"]
                for item in candidate_packets
                if item["packet_id"] != serial_candidate["packet_id"]
            },
            "lane_queue_depths": lane_queue_depths,
            "active_packets_by_lane": {
                lane: sorted(
                    packet_id for packet_id in active_packets if _packet_lane(packets.get(packet_id, {})) == lane
                )
                for lane in PACKET_LANES
            },
            "awaiting_reviewer_barrier": [item["packet_id"] for item in candidate_packets if item["lane"] == "reviewer" and item["packet_id"] != serial_candidate["packet_id"]],
            "convergence_status": "reviewer_barrier" if serial_candidate["lane"] == "reviewer" else "dispatching",
            "swarm_status": execution_shape,
        }

    for candidate in candidate_packets:
        if len(selected) >= available_slots:
            blocked_dispatch_reasons[candidate["packet_id"]] = ["route_swarm_cap_reached" if execution_shape == "bounded_swarm" else "capacity_reached"]
            continue
        packet = candidate["packet"]
        lane = candidate["lane"]
        if lane_caps_map.get(lane) is not None and current_lane_counts.get(lane, 0) >= lane_caps_map[lane]:
            blocked_dispatch_reasons[candidate["packet_id"]] = ["lane_cap_reached"]
            continue
        if candidate["lane"] == "reviewer" and selected:
            blocked_dispatch_reasons[candidate["packet_id"]] = ["reviewer_barrier_pending"]
            continue
        if conflicts_with_scopes(packet, occupied_scopes_selected):
            blocked_dispatch_reasons[candidate["packet_id"]] = ["allowed_scope_conflict"]
            continue
        if conflicts_with_shared_surfaces(packet, occupied_shared_selected):
            blocked_dispatch_reasons[candidate["packet_id"]] = ["shared_surface_conflict"]
            continue
        selected.append(candidate["packet_id"])
        current_lane_counts[lane] = current_lane_counts.get(lane, 0) + 1
        occupied_scopes_selected.update(_as_string_list(packet.get("allowed_scope")))
        occupied_shared_selected.update(_as_string_list(packet.get("shared_surface_categories")))

    return {
        "selected": selected,
        "admission_failures": admission_failures,
        "runnable_candidates": [item["packet_id"] for item in candidate_packets],
        "runnable_but_not_dispatched": [item["packet_id"] for item in candidate_packets if item["packet_id"] not in selected],
        "dispatch_block_reasons": {
            packet_id: blocked_dispatch_reasons.get(packet_id, ["reviewer_barrier_pending"])
            for packet_id in [item["packet_id"] for item in candidate_packets if item["packet_id"] not in selected]
        },
        "lane_queue_depths": lane_queue_depths,
        "active_packets_by_lane": {
            lane: sorted(
                packet_id for packet_id in active_packets if _packet_lane(packets.get(packet_id, {})) == lane
            )
            for lane in PACKET_LANES
        },
        "awaiting_reviewer_barrier": [
            item["packet_id"] for item in candidate_packets if item["lane"] == "reviewer" and item["packet_id"] not in selected
        ],
        "convergence_status": "reviewer_barrier" if any(
            item["lane"] == "reviewer" and item["packet_id"] not in selected for item in candidate_packets
        ) else ("dispatching" if selected else "idle"),
        "swarm_status": execution_shape,
    }

def packet_valid(packet: dict[str, Any]) -> bool:
    return not validate_packet(packet).missing and not validate_packet(packet).blocked


def packet_autonomy_ready(packet: dict[str, Any]) -> bool:
    if not packet_valid(packet):
        return False
    return (
        packet.get("product_meaning_resolved") is True
        and packet.get("automatable_acceptance") is True
        and packet.get("prohibited_action_required") is False
        and packet.get("maintainable_completion_path") is True
        and bool(_as_string_list(packet.get("allowed_scope")))
    )


def dependency_ready(packet: dict[str, Any], accepted_packets: set[str]) -> bool:
    dependency_mode = str(packet.get("dependency_mode", "")).strip()
    dependencies = _as_string_list(packet.get("dependencies"))
    if dependency_mode == "accepted_upstream":
        return all(dep in accepted_packets for dep in dependencies)
    if dependency_mode == "explicit_stub":
        return bool(_as_string_list(packet.get("stub_dependencies")))
    return False


def validate_packet(packet: Any) -> SchedulerValidationResult:
    result = SchedulerValidationResult()
    if not isinstance(packet, dict):
        result.missing.append("packet:item_not_object")
        return result

    packet_id = str(packet.get("packet_id", "")).strip() or "unknown"
    prefix = f"packet:{packet_id}"
    required_string_fields = (
        ("packet_id", 1),
        ("primary_behavior", 8),
        ("dependency_mode", 3),
        ("fallback_or_rollback", 8),
        ("classification", 3),
        ("execution_mode", 3),
    )
    for field_name, minimum in required_string_fields:
        if not _non_empty_string(packet.get(field_name), minimum):
            result.missing.append(f"{prefix}:{field_name}")

    if not _non_empty_string(packet.get("execution_strategy"), 3):
        result.missing.append(f"{prefix}:execution_strategy")
    if not isinstance(packet.get("strategy_inputs"), dict):
        result.missing.append(f"{prefix}:strategy_inputs")
    if not _non_empty_string(packet.get("verifier_mapping"), 3):
        result.missing.append(f"{prefix}:verifier_mapping")
    if not _non_empty_string(packet.get("evidence_destination"), 3):
        result.missing.append(f"{prefix}:evidence_destination")

    if len(_as_string_list(packet.get("allowed_scope"))) == 0:
        result.missing.append(f"{prefix}:allowed_scope")
    if len(_as_string_list(packet.get("acceptance_checks"))) == 0:
        result.missing.append(f"{prefix}:acceptance_checks")
    if len(_as_string_list(packet.get("failure_signals"))) == 0:
        result.missing.append(f"{prefix}:failure_signals")
    if not isinstance(packet.get("constraints"), list):
        result.missing.append(f"{prefix}:constraints")
    if not isinstance(packet.get("shared_surface_categories"), list):
        result.missing.append(f"{prefix}:shared_surface_categories")

    dependency_mode = str(packet.get("dependency_mode", "")).strip()
    if dependency_mode not in DEPENDENCY_MODES:
        result.blocked.append(f"{prefix}:dependency_mode_invalid")

    classification = str(packet.get("classification", "")).strip()
    if classification not in STRUCTURAL_STATES:
        result.blocked.append(f"{prefix}:classification_invalid")
    execution_mode = str(packet.get("execution_mode", "")).strip()
    if execution_mode not in EXECUTION_MODES:
        result.blocked.append(f"{prefix}:execution_mode_invalid")
    if classification == "ready" and execution_mode in {"blocked", "deferred"}:
        result.blocked.append(f"{prefix}:execution_mode_contradicts_classification")

    if packet.get("product_meaning_resolved") is not True:
        result.missing.append(f"{prefix}:product_meaning_resolved")
    if packet.get("automatable_acceptance") is not True:
        result.missing.append(f"{prefix}:automatable_acceptance")
    if packet.get("prohibited_action_required") not in {True, False}:
        result.missing.append(f"{prefix}:prohibited_action_required")
    elif packet.get("prohibited_action_required") is True:
        result.blocked.append(f"{prefix}:prohibited_action_required")
    if packet.get("maintainable_completion_path") is not True:
        result.missing.append(f"{prefix}:maintainable_completion_path")

    dependencies = _as_string_list(packet.get("dependencies"))
    stub_dependencies = _as_string_list(packet.get("stub_dependencies"))
    if dependency_mode == "accepted_upstream" and stub_dependencies:
        result.blocked.append(f"{prefix}:stub_dependencies_not_allowed")
    if dependency_mode == "explicit_stub" and dependencies:
        result.blocked.append(f"{prefix}:accepted_dependencies_not_allowed")
    if dependency_mode == "explicit_stub" and not stub_dependencies:
        result.missing.append(f"{prefix}:stub_dependencies")

    retry_budget = packet.get("retry_budget")
    if retry_budget is not None and not isinstance(retry_budget, dict):
        result.missing.append(f"{prefix}:retry_budget")
    if packet.get("alternate_strategies") is not None and not isinstance(packet.get("alternate_strategies"), list):
        result.missing.append(f"{prefix}:alternate_strategies")
    if packet.get("adaptation_policy") is not None and not _non_empty_string(packet.get("adaptation_policy"), 3):
        result.missing.append(f"{prefix}:adaptation_policy")
    if packet.get("max_adaptations") is not None:
        try:
            if int(packet.get("max_adaptations", 0) or 0) < 0:
                result.blocked.append(f"{prefix}:max_adaptations_invalid")
        except Exception:
            result.missing.append(f"{prefix}:max_adaptations")

    packet_lane = packet.get("packet_lane")
    if packet_lane is not None:
        lane_value = str(packet_lane).strip()
        if lane_value not in PACKET_LANES:
            result.blocked.append(f"{prefix}:packet_lane_invalid")
    parallelism_class = packet.get("parallelism_class")
    if parallelism_class is not None:
        parallelism_value = str(parallelism_class).strip()
        if parallelism_value not in PARALLELISM_CLASSES:
            result.blocked.append(f"{prefix}:parallelism_class_invalid")
        elif str(packet.get("packet_lane", "")).strip() == "reviewer" and parallelism_value == "isolated":
            result.blocked.append(f"{prefix}:reviewer_parallelism_invalid")
    swarm_eligible = packet.get("swarm_eligible")
    if swarm_eligible is not None and not isinstance(swarm_eligible, bool):
        result.missing.append(f"{prefix}:swarm_eligible")
    if packet.get("lane_affinity") is not None and not _non_empty_string(packet.get("lane_affinity"), 2):
        result.missing.append(f"{prefix}:lane_affinity")
    if packet.get("preferred_agent_type") is not None and not _non_empty_string(packet.get("preferred_agent_type"), 2):
        result.missing.append(f"{prefix}:preferred_agent_type")

    strategy_missing, strategy_blocked = validate_strategy_packet(packet)
    result.extend(missing=strategy_missing, blocked=strategy_blocked)

    return result


def validate_packet_dag(packets: Any, required_packets: Any | None = None) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    result = SchedulerValidationResult()
    if not isinstance(packets, list) or not packets:
        result.missing.append("packets")
        return result.missing, result.blocked, []
    if required_packets is not None and (not isinstance(required_packets, list) or not required_packets):
        result.missing.append("required_packets")
        return result.missing, result.blocked, []

    packet_ids: list[str] = []
    packet_index: dict[str, dict[str, Any]] = {}
    for packet in packets:
        packet_result = validate_packet(packet)
        result.extend(missing=packet_result.missing, blocked=packet_result.blocked)
        if isinstance(packet, dict):
            packet_id = str(packet.get("packet_id", "")).strip()
            if packet_id:
                if packet_id in packet_index:
                    result.blocked.append(f"packet:{packet_id}:duplicate_id")
                else:
                    packet_index[packet_id] = packet
                    packet_ids.append(packet_id)

    required_packet_ids = _as_string_list(required_packets)
    for packet_id in required_packet_ids:
        if packet_id not in packet_index:
            result.blocked.append(f"required_packets:missing:{packet_id}")

    for packet_id, packet in packet_index.items():
        for dep in _as_string_list(packet.get("dependencies")):
            if dep not in packet_index:
                result.blocked.append(f"packet:{packet_id}:missing_dependency:{dep}")

    cycle = _detect_cycle(packet_index)
    if cycle:
        result.blocked.append("packets:dependency_cycle")
        result.blocked.append(f"packets:cycle:{'->'.join(cycle)}")

    normalized_packets = [packet_index[packet_id] for packet_id in sorted(packet_index)]
    return sorted(set(result.missing)), sorted(set(result.blocked)), normalized_packets

def _detect_cycle(packet_index: dict[str, dict[str, Any]]) -> list[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    trail: list[str] = []

    def visit(packet_id: str) -> list[str]:
        if packet_id in visiting:
            cycle_start = trail.index(packet_id)
            return trail[cycle_start:] + [packet_id]
        if packet_id in visited:
            return []
        visiting.add(packet_id)
        trail.append(packet_id)
        packet = packet_index[packet_id]
        for dep in _as_string_list(packet.get("dependencies")):
            cycle = visit(dep)
            if cycle:
                return cycle
        trail.pop()
        visiting.remove(packet_id)
        visited.add(packet_id)
        return []

    for packet_id in packet_index:
        cycle = visit(packet_id)
        if cycle:
            return cycle
    return []


def validate_scheduler_policy(policy: Any) -> SchedulerValidationResult:
    result = SchedulerValidationResult()
    if not isinstance(policy, dict):
        result.missing.append("scheduler_policy")
        return result

    try:
        max_parallel = int(policy.get("max_parallel_packets", 0))
    except Exception:
        max_parallel = 0
    if max_parallel <= 0:
        result.missing.append("scheduler_policy:max_parallel_packets")

    parallelism_policy = str(policy.get("parallelism_policy", "")).strip()
    if parallelism_policy not in PARALLELISM_POLICIES:
        result.blocked.append("scheduler_policy:parallelism_policy_invalid")

    execution_shape = policy.get("execution_shape")
    if execution_shape is not None and str(execution_shape).strip() not in EXECUTION_SHAPES:
        result.blocked.append("scheduler_policy:execution_shape_invalid")

    lane_caps = policy.get("lane_caps")
    if lane_caps is not None:
        if not isinstance(lane_caps, dict):
            result.missing.append("scheduler_policy:lane_caps")
        else:
            for lane, raw_value in lane_caps.items():
                if str(lane).strip() not in LANE_CAP_KEYS:
                    result.blocked.append(f"scheduler_policy:lane_caps:{lane}:invalid_lane")
                    continue
                try:
                    if int(raw_value) <= 0:
                        result.blocked.append(f"scheduler_policy:lane_caps:{lane}:invalid_cap")
                except Exception:
                    result.missing.append(f"scheduler_policy:lane_caps:{lane}")

    route_swarm_cap = policy.get("route_swarm_cap")
    if route_swarm_cap is not None:
        try:
            if int(route_swarm_cap) <= 0:
                result.blocked.append("scheduler_policy:route_swarm_cap_invalid")
        except Exception:
            result.missing.append("scheduler_policy:route_swarm_cap")

    frontier_dispatch_order = policy.get("frontier_dispatch_order")
    if frontier_dispatch_order is not None:
        if not isinstance(frontier_dispatch_order, list) or not frontier_dispatch_order:
            result.missing.append("scheduler_policy:frontier_dispatch_order")
        elif any(str(item).strip() not in PACKET_LANES for item in frontier_dispatch_order):
            result.blocked.append("scheduler_policy:frontier_dispatch_order_invalid")

    reviewer_barrier_points = policy.get("reviewer_barrier_points")
    if reviewer_barrier_points is not None and not isinstance(reviewer_barrier_points, list):
        result.missing.append("scheduler_policy:reviewer_barrier_points")

    convergence_required_for_closure = policy.get("convergence_required_for_closure")
    if convergence_required_for_closure is not None and convergence_required_for_closure not in {True, False}:
        result.missing.append("scheduler_policy:convergence_required_for_closure")

    admission_checks = {item.lower() for item in _as_string_list(policy.get("admission_rule"))}
    if admission_checks != set(ADMISSION_CHECKS):
        result.missing.append("scheduler_policy:admission_checks")

    recompute_triggers = set(_as_string_list(policy.get("recompute_triggers")))
    if recompute_triggers != set(RECOMPUTE_TRIGGERS):
        result.missing.append("scheduler_policy:recompute_triggers")

    terminal_stop_conditions = set(_as_string_list(policy.get("terminal_stop_conditions")))
    if terminal_stop_conditions != TERMINAL_STOP_CONDITIONS:
        result.missing.append("scheduler_policy:terminal_stop_conditions")

    return result


def validate_objective_status(value: Any, required_packets: set[str]) -> SchedulerValidationResult:
    result = SchedulerValidationResult()
    if not isinstance(value, dict):
        result.missing.append("objective_status_artifact:not_object")
        return result

    closure_state = str(value.get("closure_state") or value.get("final_closure_state") or "").strip()
    if closure_state not in OBJECTIVE_CLOSURE_STATES:
        result.blocked.append("objective_status_artifact:final_closure_state_invalid")

    accepted_packets = set(_as_string_list(value.get("completed_packets")))
    blocked_packets = set(_as_string_list(value.get("blocked_packets")))
    deferred_packets = set(_as_string_list(value.get("deferred_packets")))
    pending_packets = set(_as_string_list(value.get("pending_packets")))

    all_known = accepted_packets | blocked_packets | deferred_packets | pending_packets
    unknown = sorted(all_known - required_packets)
    for packet_id in unknown:
        result.blocked.append(f"objective_status_artifact:unknown_packet:{packet_id}")

    if closure_state == "OBJECTIVE_COMPLETE" and accepted_packets != required_packets:
        result.blocked.append("objective_status_artifact:complete_requires_all_required_packets")
    if closure_state == "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK" and not _as_string_list(
        value.get("boundary_shrunk_remainder")
    ):
        result.missing.append("objective_status_artifact:boundary_shrunk_remainder")
    if closure_state == "OBJECTIVE_REJECTED_FALSE_COMPLETION" and accepted_packets == required_packets:
        result.blocked.append("objective_status_artifact:false_completion_cannot_accept_all_packets")

    result.meta["closure_state"] = closure_state
    result.meta["accepted_packets"] = sorted(accepted_packets)
    return result


def validate_schedule_state(
    value: Any,
    packets: dict[str, dict[str, Any]],
    objective_status: dict[str, Any],
) -> SchedulerValidationResult:
    result = SchedulerValidationResult()
    if not isinstance(value, dict):
        result.missing.append("schedule_artifact:not_object")
        return result

    required_fields = (
        "objective_id",
        "execution_shape",
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
        "total_packet_count",
        "accepted_packet_count",
        "rejected_packet_count",
        "blocked_packet_count",
        "repacketization_count",
        "escalation_count",
        "migration_fallback_used",
        "total_runtime_attempts",
    )
    for key in required_fields:
        if key not in value:
            result.missing.append(f"schedule_artifact:{key}")

    parallelism_policy = str(value.get("parallelism_policy", "")).strip()
    if parallelism_policy not in PARALLELISM_POLICIES:
        result.blocked.append("schedule_artifact:parallelism_policy_invalid")
    execution_shape = str(value.get("execution_shape", "")).strip()
    if execution_shape not in EXECUTION_SHAPES:
        result.blocked.append("schedule_artifact:execution_shape_invalid")
    lane_caps = value.get("lane_caps") if isinstance(value.get("lane_caps"), dict) else {}
    route_swarm_cap = value.get("route_swarm_cap")
    frontier_dispatch_order = value.get("frontier_dispatch_order") if isinstance(value.get("frontier_dispatch_order"), list) else []
    reviewer_barrier_points = value.get("reviewer_barrier_points") if isinstance(value.get("reviewer_barrier_points"), list) else []
    convergence_required_for_closure = value.get("convergence_required_for_closure")
    if convergence_required_for_closure not in {True, False}:
        result.missing.append("schedule_artifact:convergence_required_for_closure")
    if frontier_dispatch_order and any(str(item).strip() not in PACKET_LANES for item in frontier_dispatch_order):
        result.blocked.append("schedule_artifact:frontier_dispatch_order_invalid")

    runtime_states = value.get("runtime_states")
    if not isinstance(runtime_states, dict):
        result.missing.append("schedule_artifact:runtime_states")
        runtime_states = {}

    accepted_packets = set(_as_string_list(objective_status.get("completed_packets")))
    active_packets = {
        packet_id
        for packet_id, runtime_state in runtime_states.items()
        if str(runtime_state).strip() in ACTIVE_RUNTIME_STATES
    }
    retry_counters = value.get("retry_counters") if isinstance(value.get("retry_counters"), dict) else {}
    runnable_expected = compute_runnable_set(
        packets=packets,
        accepted_packets=accepted_packets,
        active_packets=active_packets,
        retry_counters=retry_counters,
        max_parallel_packets=int(value.get("max_parallel_packets", 0) or 0),
        parallelism_policy=parallelism_policy or "bounded_parallel",
        execution_shape=execution_shape or "single_lane",
        lane_caps=_normalize_lane_caps(lane_caps),
        route_swarm_cap=int(route_swarm_cap or 0) if route_swarm_cap is not None else None,
        frontier_dispatch_order=[str(item).strip() for item in frontier_dispatch_order if str(item).strip()],
        reviewer_barrier_points=[str(item).strip() for item in reviewer_barrier_points if str(item).strip()],
    )
    runnable_actual = sorted(_as_string_list(value.get("runnable_set")))
    if runnable_actual != runnable_expected:
        result.blocked.append("schedule_artifact:runnable_set_mismatch")
    safe_momentum_available = value.get("safe_momentum_available")
    if not isinstance(safe_momentum_available, bool):
        result.missing.append("schedule_artifact:safe_momentum_available_invalid")
    elif safe_momentum_available is not bool(runnable_expected):
        result.blocked.append("schedule_artifact:safe_momentum_available_mismatch")

    frontier_movement = value.get("frontier_movement")
    if not isinstance(frontier_movement, bool):
        result.missing.append("schedule_artifact:frontier_movement_invalid")
    frontier_reason = str(value.get("frontier_movement_reason", "")).strip()
    if frontier_reason and frontier_reason not in FRONTIER_MOVEMENT_REASONS:
        result.blocked.append("schedule_artifact:frontier_movement_reason_invalid")
    if frontier_movement is True and not frontier_reason:
        result.missing.append("schedule_artifact:frontier_movement_reason_required")

    cycle_log = value.get("cycle_log")
    if not isinstance(cycle_log, list) or not cycle_log:
        result.missing.append("schedule_artifact:cycle_log")
    else:
        required_cycle_fields = (
            "cycle_id",
            "objective_id",
            "packet_ids",
            "purpose",
            "expected_evidence",
            "closure_impact",
            "strategy_label",
            "stop_condition",
            "pivot_condition",
            "escalation_condition",
            "classification",
            "frontier_movement",
            "frontier_movement_reason",
        )
        for idx, item in enumerate(cycle_log, start=1):
            prefix = f"schedule_artifact:cycle_log:{idx}"
            if not isinstance(item, dict):
                result.missing.append(f"{prefix}:item_not_object")
                continue
            for field_name in required_cycle_fields:
                if field_name not in item:
                    result.missing.append(f"{prefix}:{field_name}")
            classification = str(item.get("classification", "")).strip()
            if classification not in FORWARD_MOTION_STATES:
                result.blocked.append(f"{prefix}:classification_invalid")
            packet_ids = _as_string_list(item.get("packet_ids"))
            for packet_id in packet_ids:
                if packet_id not in packets:
                    result.blocked.append(f"{prefix}:unknown_packet:{packet_id}")
            cycle_frontier_movement = item.get("frontier_movement")
            if not isinstance(cycle_frontier_movement, bool):
                result.missing.append(f"{prefix}:frontier_movement_invalid")
            cycle_reason = str(item.get("frontier_movement_reason", "")).strip()
            if cycle_reason and cycle_reason not in FRONTIER_MOVEMENT_REASONS:
                result.blocked.append(f"{prefix}:frontier_movement_reason_invalid")
            if cycle_frontier_movement is True and not cycle_reason:
                result.missing.append(f"{prefix}:frontier_movement_reason_required")
            if not _non_empty_string(item.get("purpose"), 8):
                result.missing.append(f"{prefix}:purpose_invalid")
            if len(_as_string_list(item.get("expected_evidence"))) == 0:
                result.missing.append(f"{prefix}:expected_evidence_invalid")

    for field_name in (
        "max_same_strategy_retries",
        "max_noop_cycles",
        "max_verifier_return_cycles",
        "max_no_frontier_movement_cycles",
    ):
        try:
            if int(value.get(field_name, 0) or 0) <= 0:
                result.missing.append(f"schedule_artifact:{field_name}_invalid")
        except Exception:
            result.missing.append(f"schedule_artifact:{field_name}_invalid")

    closure_state = str(objective_status.get("closure_state", "")).strip()
    if closure_state in {
        "OBJECTIVE_COMPLETE",
        "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK",
        "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED",
        "OBJECTIVE_BLOCKED_MIGRATION_DEFECT",
    } and bool(runnable_expected):
        result.blocked.append("schedule_artifact:closure_with_safe_momentum")

    return result


def compute_runnable_set(
    *,
    packets: dict[str, dict[str, Any]],
    accepted_packets: set[str],
    active_packets: set[str],
    retry_counters: dict[str, Any],
    max_parallel_packets: int,
    parallelism_policy: str,
    execution_shape: str = "single_lane",
    lane_caps: dict[str, int] | None = None,
    route_swarm_cap: int | None = None,
    frontier_dispatch_order: list[str] | None = None,
    reviewer_barrier_points: list[str] | None = None,
) -> list[str]:
    return _compute_dispatch_analysis(
        packets=packets,
        accepted_packets=accepted_packets,
        active_packets=active_packets,
        retry_counters=retry_counters,
        max_parallel_packets=max_parallel_packets,
        parallelism_policy=parallelism_policy,
        execution_shape=execution_shape,
        lane_caps=lane_caps,
        route_swarm_cap=route_swarm_cap,
        frontier_dispatch_order=frontier_dispatch_order,
        reviewer_barrier_points=reviewer_barrier_points,
    )["selected"]


def retry_budget_exhausted(packet_id: str, retry_counters: dict[str, Any]) -> bool:
    counters = retry_counters.get(packet_id)
    if not isinstance(counters, dict):
        return False
    try:
        same_method = int(counters.get("same_method_attempts", 0))
        alternate_strategy = int(counters.get("alternate_strategy_attempts", 0))
    except Exception:
        return True
    return same_method >= 2 and alternate_strategy >= 2


def conflicts_with_scopes(packet: dict[str, Any], occupied_scopes: set[str]) -> bool:
    return bool(set(_as_string_list(packet.get("allowed_scope"))) & occupied_scopes)


def conflicts_with_shared_surfaces(packet: dict[str, Any], occupied_categories: set[str]) -> bool:
    return bool(set(_as_string_list(packet.get("shared_surface_categories"))) & occupied_categories)


def _active_scopes(packets: dict[str, dict[str, Any]], active_packets: set[str]) -> set[str]:
    occupied: set[str] = set()
    for packet_id in active_packets:
        packet = packets.get(packet_id)
        if packet:
            occupied.update(_as_string_list(packet.get("allowed_scope")))
    return occupied


def _active_shared_surfaces(packets: dict[str, dict[str, Any]], active_packets: set[str]) -> set[str]:
    occupied: set[str] = set()
    for packet_id in active_packets:
        packet = packets.get(packet_id)
        if packet:
            occupied.update(_as_string_list(packet.get("shared_surface_categories")))
    return occupied


def evaluate_objective_closure(
    *,
    packets: list[dict[str, Any]],
    boundary_shrunk_remainder: Any,
    migration_fallback_used: bool,
) -> dict[str, str]:
    runtime_states = {str(item.get("runtime_state", "")).strip() for item in packets if isinstance(item, dict)}
    if runtime_states and runtime_states <= {"accepted"}:
        return {"closure_state": "OBJECTIVE_COMPLETE", "accepted_type": "ACCEPTED_SUCCESS"}
    if migration_fallback_used:
        return {"closure_state": "OBJECTIVE_BLOCKED_MIGRATION_DEFECT", "accepted_type": "ACCEPTED_BLOCKED"}
    if _as_string_list(boundary_shrunk_remainder):
        return {"closure_state": "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK", "accepted_type": "ACCEPTED_BLOCKED"}
    if "rejected_rework" in runtime_states:
        return {"closure_state": "OBJECTIVE_REJECTED_FALSE_COMPLETION", "accepted_type": ""}
    return {"closure_state": "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED", "accepted_type": "ACCEPTED_BLOCKED"}


def safe_momentum_exists(
    *,
    packets: dict[str, dict[str, Any]],
    accepted_packets: set[str],
    active_packets: set[str],
    retry_counters: dict[str, Any],
    max_parallel_packets: int,
    parallelism_policy: str,
    execution_shape: str = "single_lane",
    lane_caps: dict[str, int] | None = None,
    route_swarm_cap: int | None = None,
    frontier_dispatch_order: list[str] | None = None,
    reviewer_barrier_points: list[str] | None = None,
) -> bool:
    return bool(
        compute_runnable_set(
            packets=packets,
            accepted_packets=accepted_packets,
            active_packets=active_packets,
            retry_counters=retry_counters,
            max_parallel_packets=max_parallel_packets,
            parallelism_policy=parallelism_policy,
            execution_shape=execution_shape,
            lane_caps=lane_caps,
            route_swarm_cap=route_swarm_cap,
            frontier_dispatch_order=frontier_dispatch_order,
            reviewer_barrier_points=reviewer_barrier_points,
        )
    )


def build_schedule(
    *,
    objective_id: str,
    packets: dict[str, dict[str, Any]],
    accepted_packets: set[str],
    active_packets: set[str],
    retry_counters: dict[str, Any],
    max_parallel_packets: int,
    parallelism_policy: str,
    execution_shape: str = "single_lane",
    lane_caps: dict[str, int] | None = None,
    route_swarm_cap: int | None = None,
    frontier_dispatch_order: list[str] | None = None,
    reviewer_barrier_points: list[str] | None = None,
    convergence_required_for_closure: bool = False,
    blocked_set: list[str] | None = None,
    dispatch_history: list[dict[str, Any]] | None = None,
    previous_frontier: list[str] | None = None,
    frontier_movement_reason: str | None = None,
    cycle_log: list[dict[str, Any]] | None = None,
    max_same_strategy_retries: int = 2,
    max_noop_cycles: int = 2,
    max_verifier_return_cycles: int = 2,
    max_no_frontier_movement_cycles: int = 2,
) -> dict[str, Any]:
    analysis = _compute_dispatch_analysis(
        packets=packets,
        accepted_packets=accepted_packets,
        active_packets=active_packets,
        retry_counters=retry_counters,
        max_parallel_packets=max_parallel_packets,
        parallelism_policy=parallelism_policy,
        execution_shape=execution_shape,
        lane_caps=lane_caps,
        route_swarm_cap=route_swarm_cap,
        frontier_dispatch_order=frontier_dispatch_order,
        reviewer_barrier_points=reviewer_barrier_points,
    )
    runnable_set = analysis["selected"]
    occupied_scopes = _active_scopes(packets, active_packets)
    occupied_shared_surfaces = _active_shared_surfaces(packets, active_packets)
    admission_failures: dict[str, list[str]] = {}
    for packet_id, packet in packets.items():
        reasons: list[str] = []
        if packet_id in accepted_packets:
            reasons.append("already_accepted")
        if packet_id in active_packets:
            reasons.append("already_active")
        if not packet_autonomy_ready(packet):
            reasons.append("not_autonomy_ready")
        if not dependency_ready(packet, accepted_packets):
            reasons.append("dependencies_not_ready")
        if retry_budget_exhausted(packet_id, retry_counters):
            reasons.append("retry_budget_exhausted")
        reasons.extend(execution_readiness_failures(packet))
        if conflicts_with_scopes(packet, occupied_scopes):
            reasons.append("allowed_scope_conflict")
        if conflicts_with_shared_surfaces(packet, occupied_shared_surfaces):
            reasons.append("shared_surface_conflict")
        strategy = strategy_spec(str(packet.get("execution_strategy", "")).strip())
        if strategy and strategy.get("supports_parallel") is False and active_packets:
            reasons.append("parallel_dispatch_not_allowed")
        if reasons and packet_id not in runnable_set:
            admission_failures[packet_id] = sorted(set(reasons))
    previous_frontier_list = sorted(_as_string_list(previous_frontier))
    frontier_movement = runnable_set != previous_frontier_list if previous_frontier is not None else bool(runnable_set)
    resolved_frontier_reason = (
        frontier_movement_reason
        or ("dependency_unlocked" if frontier_movement and runnable_set else "blocker_isolated" if blocked_set else "")
    )
    default_cycle_log = cycle_log or [
        {
            "cycle_id": f"{objective_id}-cycle-001",
            "objective_id": objective_id,
            "packet_ids": runnable_set[:1],
            "purpose": "Advance the current executable frontier.",
            "expected_evidence": ["frontier movement" if frontier_movement else "blocker isolation"],
            "closure_impact": "Move the objective toward verifier-accepted closure.",
            "strategy_label": "scheduler_frontier_evaluation",
            "stop_condition": "Frontier and admission state recomputed.",
            "pivot_condition": "No safe momentum or retry budget remains.",
            "escalation_condition": "No safe momentum remains for required work.",
            "classification": "closure_advancing" if frontier_movement else ("blocker_isolating" if blocked_set else "uncertainty_reducing"),
            "frontier_movement": frontier_movement,
            "frontier_movement_reason": resolved_frontier_reason,
        }
    ]
    return {
        "objective_id": objective_id,
        "execution_shape": execution_shape,
        "max_parallel_packets": max_parallel_packets,
        "parallelism_policy": parallelism_policy,
        "lane_caps": _normalize_lane_caps(lane_caps),
        "route_swarm_cap": route_swarm_cap,
        "frontier_dispatch_order": [lane for lane in (frontier_dispatch_order or []) if lane in PACKET_LANES],
        "reviewer_barrier_points": [str(item).strip() for item in (reviewer_barrier_points or []) if str(item).strip()],
        "convergence_required_for_closure": convergence_required_for_closure,
        "dispatch_history": dispatch_history or [],
        "runnable_set": runnable_set,
        "runnable_candidates": analysis["runnable_candidates"],
        "runnable_but_not_dispatched": analysis["runnable_but_not_dispatched"],
        "dispatch_block_reasons": analysis["dispatch_block_reasons"],
        "lane_queue_depths": analysis["lane_queue_depths"],
        "active_packets_by_lane": analysis["active_packets_by_lane"],
        "awaiting_reviewer_barrier": analysis["awaiting_reviewer_barrier"],
        "convergence_status": analysis["convergence_status"],
        "swarm_status": analysis["swarm_status"],
        "blocked_set": blocked_set or [],
        "retry_counters": retry_counters,
        "strategy_switches": {},
        "repacketization_events": [],
        "current_frontier": runnable_set,
        "closure_readiness": "pending",
        "cycle_log": default_cycle_log,
        "safe_momentum_available": safe_momentum_exists(
            packets=packets,
            accepted_packets=accepted_packets,
            active_packets=active_packets,
            retry_counters=retry_counters,
            max_parallel_packets=max_parallel_packets,
            parallelism_policy=parallelism_policy,
            execution_shape=execution_shape,
            lane_caps=lane_caps,
            route_swarm_cap=route_swarm_cap,
            frontier_dispatch_order=frontier_dispatch_order,
            reviewer_barrier_points=reviewer_barrier_points,
        ),
        "frontier_movement": frontier_movement,
        "frontier_movement_reason": resolved_frontier_reason,
        "max_same_strategy_retries": max_same_strategy_retries,
        "max_noop_cycles": max_noop_cycles,
        "max_verifier_return_cycles": max_verifier_return_cycles,
        "max_no_frontier_movement_cycles": max_no_frontier_movement_cycles,
        "admission_failures": admission_failures,
        "total_packet_count": len(packets),
        "accepted_packet_count": len(accepted_packets),
        "rejected_packet_count": 0,
        "blocked_packet_count": len(blocked_set or []),
        "repacketization_count": 0,
        "escalation_count": 0,
        "migration_fallback_used": False,
        "total_runtime_attempts": sum(
            int(counters.get("same_method_attempts", 0)) + int(counters.get("alternate_strategy_attempts", 0))
            for counters in retry_counters.values()
            if isinstance(counters, dict)
        ),
        "runtime_states": {packet_id: "accepted" if packet_id in accepted_packets else "queued" for packet_id in packets},
    }


def initialize_runtime_packets(packets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    initialized: dict[str, dict[str, Any]] = {}
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        packet_id = str(packet.get("packet_id", "")).strip()
        if not packet_id:
            continue
        copy_packet = dict(packet)
        copy_packet["runtime_state"] = "queued"
        initialized[packet_id] = copy_packet
    return initialized


def build_objective_status_payload(
    *,
    objective_id: str,
    packets: dict[str, dict[str, Any]],
    boundary_shrunk_remainder: list[str] | None = None,
    migration_fallback_used: bool = False,
) -> dict[str, Any]:
    completed = sorted(
        packet_id for packet_id, packet in packets.items() if str(packet.get("runtime_state", "")).strip() == "accepted"
    )
    pending = sorted(
        packet_id for packet_id, packet in packets.items() if str(packet.get("runtime_state", "")).strip() == "queued"
    )
    blocked = sorted(
        packet_id
        for packet_id, packet in packets.items()
        if str(packet.get("runtime_state", "")).strip() in {"escalated", "rejected_rework"}
    )
    deferred = sorted(
        packet_id for packet_id, packet in packets.items() if str(packet.get("runtime_state", "")).strip() == "cancelled"
    )
    closure = evaluate_objective_closure(
        packets=list(packets.values()),
        boundary_shrunk_remainder=boundary_shrunk_remainder or [],
        migration_fallback_used=migration_fallback_used,
    )
    return {
        "schema_version": "objective-status.v1",
        "objective_id": objective_id,
        "closure_state": closure["closure_state"],
        "completed_packets": completed,
        "pending_packets": pending,
        "blocked_packets": blocked,
        "deferred_packets": deferred,
        "boundary_shrunk_remainder": sorted(boundary_shrunk_remainder or []),
    }


def classify_cycle_outcome(
    *,
    verdicts: list[dict[str, Any]],
    frontier_movement: bool,
    escalation_required: bool = False,
) -> str:
    outputs = {str(item.get("verifier_output", "")).strip() for item in verdicts if isinstance(item, dict)}
    if frontier_movement and "accepted" in outputs:
        return "closure_advancing"
    if escalation_required or outputs & {"blocked_migration_defect", "blocked_boundary", "escalate"}:
        return "blocker_isolating"
    if outputs & {"rejected_rework"}:
        return "uncertainty_reducing"
    return "invalid_noop"


def infer_frontier_movement_reason(
    *,
    previous_frontier: list[str],
    current_frontier: list[str],
    verdicts: list[dict[str, Any]],
    repacketized: bool = False,
    escalation_required: bool = False,
) -> str:
    outputs = {str(item.get("verifier_output", "")).strip() for item in verdicts if isinstance(item, dict)}
    if repacketized:
        return "packet_rewritten"
    if escalation_required or outputs & {"blocked_boundary", "escalate"}:
        return "authority_blocked"
    if current_frontier != previous_frontier:
        if "accepted" in outputs:
            return "packet_completed"
        if current_frontier:
            return "dependency_unlocked"
    if outputs & {"blocked_migration_defect"}:
        return "blocker_isolated"
    return ""


def apply_cycle_review(
    *,
    packets: dict[str, dict[str, Any]],
    retry_counters: dict[str, Any],
    review: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    updated_packets = {packet_id: dict(packet) for packet_id, packet in packets.items()}
    updated_counters = {
        packet_id: dict(counter) if isinstance(counter, dict) else {}
        for packet_id, counter in retry_counters.items()
    }
    adaptation_events: list[dict[str, Any]] = []
    verdicts = review.get("packet_verdicts") if isinstance(review.get("packet_verdicts"), list) else []
    for verdict in verdicts:
        if not isinstance(verdict, dict):
            continue
        packet_id = str(verdict.get("packet_id", "")).strip()
        if packet_id not in updated_packets:
            continue
        output = str(verdict.get("verifier_output", "")).strip()
        packet = updated_packets[packet_id]
        packet["last_verdict_summary"] = str(verdict.get("summary") or "").strip()
        packet["last_changed_files"] = [
            str(path).strip()
            for path in verdict.get("changed_files", [])
            if str(path).strip()
        ]
        packet["last_evidence_refs"] = [
            str(ref).strip()
            for ref in verdict.get("evidence_refs", [])
            if str(ref).strip()
        ]
        packet["last_strategy_name"] = str(verdict.get("strategy_name") or "").strip()
        packet["last_runner_kind"] = str(verdict.get("runner_kind") or "").strip()
        if output == "accepted":
            packet["runtime_state"] = "accepted"
            updated_counters.setdefault(packet_id, {})
            updated_counters[packet_id]["same_method_attempts"] = 0
            updated_counters[packet_id]["alternate_strategy_attempts"] = 0
            continue
        if output == "rejected_rework":
            retry_mode = str(verdict.get("retry_mode", "same_method")).strip() or "same_method"
            counters = updated_counters.setdefault(
                packet_id,
                {"same_method_attempts": 0, "alternate_strategy_attempts": 0, "adaptation_count": 0},
            )
            key = "alternate_strategy_attempts" if retry_mode == "alternate_strategy" else "same_method_attempts"
            counters[key] = int(counters.get(key, 0) or 0) + 1
            packet["runtime_state"] = "queued"
            alternate_strategies = _as_string_list(packet.get("alternate_strategies"))
            max_adaptations = int(packet.get("max_adaptations", 1) or 1)
            current_strategy = str(packet.get("execution_strategy", "")).strip()
            if (
                retry_mode != "alternate_strategy"
                and int(counters.get("same_method_attempts", 0) or 0) >= 2
                and int(counters.get("adaptation_count", 0) or 0) < max_adaptations
                and alternate_strategies
            ):
                next_strategy = next(
                    (candidate for candidate in alternate_strategies if candidate and candidate != current_strategy),
                    "",
                )
                if next_strategy:
                    packet["execution_strategy"] = next_strategy
                    counters["adaptation_count"] = int(counters.get("adaptation_count", 0) or 0) + 1
                    packet["last_adaptation_reason"] = "same_runner_retry_budget_exhausted"
                    adaptation_events.append(
                        {
                            "packet_id": packet_id,
                            "old_strategy": current_strategy,
                            "new_strategy": next_strategy,
                            "trigger_evidence": str(verdict.get("summary") or "").strip(),
                            "policy_basis": "bounded_retry_then_alternate",
                            "frontier_effect": "packet_requeued_with_alternate_strategy",
                            "verifier_impact": "rejected_rework",
                        }
                    )
            continue
        if output in {"blocked_boundary", "blocked_migration_defect", "escalate"}:
            packet["runtime_state"] = "escalated"
            continue

    repacketization = review.get("repacketization_requests") if isinstance(review.get("repacketization_requests"), list) else []
    for request in repacketization:
        if not isinstance(request, dict):
            continue
        for packet_id in _as_string_list(request.get("superseded_packet_ids")):
            packet = updated_packets.get(packet_id)
            if packet and str(packet.get("runtime_state", "")).strip() != "accepted":
                packet["runtime_state"] = "cancelled"
        for packet in request.get("new_packets", []):
            if not isinstance(packet, dict):
                continue
            packet_id = str(packet.get("packet_id", "")).strip()
            if not packet_id:
                continue
            new_packet = dict(packet)
            new_packet["runtime_state"] = "queued"
            updated_packets[packet_id] = new_packet
    return updated_packets, updated_counters, adaptation_events
