#!/usr/bin/env python3
"""Canonical execution-strategy registry and packet validation helpers."""

from __future__ import annotations

from typing import Any


COMMON_PACKET_FIELDS = (
    "packet_id",
    "primary_behavior",
    "execution_strategy",
    "strategy_inputs",
    "allowed_scope",
    "dependencies",
    "dependency_mode",
    "acceptance_checks",
    "failure_signals",
    "fallback_or_rollback",
    "verifier_mapping",
    "evidence_destination",
    "shared_surface_categories",
    "classification",
)


STRATEGY_REGISTRY: dict[str, dict[str, Any]] = {
    "command_capture": {
        "strategy_name": "command_capture",
        "runner_kind": "command",
        "required_packet_fields": COMMON_PACKET_FIELDS,
        "required_strategy_inputs": ("command",),
        "result_requirements": ("captured_commands", "evidence_refs", "result_artifact_path"),
        "supports_parallel": True,
        "allowed_packet_classes": ("implementation", "validation"),
        "fallback_policy": "no_fallback",
        "alternate_strategies": ("multi_command_pipeline",),
    },
    "test_command": {
        "strategy_name": "test_command",
        "runner_kind": "command",
        "required_packet_fields": COMMON_PACKET_FIELDS,
        "required_strategy_inputs": ("command", "test_lane"),
        "result_requirements": ("captured_commands", "evidence_refs", "result_artifact_path"),
        "supports_parallel": True,
        "allowed_packet_classes": ("validation",),
        "fallback_policy": "no_fallback",
        "alternate_strategies": ("multi_command_pipeline",),
    },
    "validation_command": {
        "strategy_name": "validation_command",
        "runner_kind": "command",
        "required_packet_fields": COMMON_PACKET_FIELDS,
        "required_strategy_inputs": ("command", "validation_lane"),
        "result_requirements": ("captured_commands", "evidence_refs", "result_artifact_path"),
        "supports_parallel": True,
        "allowed_packet_classes": ("validation",),
        "fallback_policy": "no_fallback",
        "alternate_strategies": ("multi_command_pipeline",),
    },
    "lint_command": {
        "strategy_name": "lint_command",
        "runner_kind": "command",
        "required_packet_fields": COMMON_PACKET_FIELDS,
        "required_strategy_inputs": ("command", "validation_lane"),
        "result_requirements": ("captured_commands", "evidence_refs", "result_artifact_path"),
        "supports_parallel": True,
        "allowed_packet_classes": ("validation",),
        "fallback_policy": "no_fallback",
        "alternate_strategies": ("validation_command", "multi_command_pipeline"),
    },
    "typecheck_command": {
        "strategy_name": "typecheck_command",
        "runner_kind": "command",
        "required_packet_fields": COMMON_PACKET_FIELDS,
        "required_strategy_inputs": ("command", "validation_lane"),
        "result_requirements": ("captured_commands", "evidence_refs", "result_artifact_path"),
        "supports_parallel": True,
        "allowed_packet_classes": ("validation",),
        "fallback_policy": "no_fallback",
        "alternate_strategies": ("validation_command", "multi_command_pipeline"),
    },
    "build_command": {
        "strategy_name": "build_command",
        "runner_kind": "command",
        "required_packet_fields": COMMON_PACKET_FIELDS,
        "required_strategy_inputs": ("command", "validation_lane"),
        "result_requirements": ("captured_commands", "evidence_refs", "result_artifact_path"),
        "supports_parallel": True,
        "allowed_packet_classes": ("validation", "implementation"),
        "fallback_policy": "no_fallback",
        "alternate_strategies": ("validation_command", "multi_command_pipeline"),
    },
    "smoke_command": {
        "strategy_name": "smoke_command",
        "runner_kind": "command",
        "required_packet_fields": COMMON_PACKET_FIELDS,
        "required_strategy_inputs": ("command", "validation_lane"),
        "result_requirements": ("captured_commands", "evidence_refs", "result_artifact_path"),
        "supports_parallel": False,
        "allowed_packet_classes": ("validation",),
        "fallback_policy": "no_fallback",
        "alternate_strategies": ("multi_command_pipeline",),
    },
    "schema_check_command": {
        "strategy_name": "schema_check_command",
        "runner_kind": "command",
        "required_packet_fields": COMMON_PACKET_FIELDS,
        "required_strategy_inputs": ("command", "validation_lane"),
        "result_requirements": ("captured_commands", "evidence_refs", "result_artifact_path"),
        "supports_parallel": False,
        "allowed_packet_classes": ("validation",),
        "fallback_policy": "no_fallback",
        "alternate_strategies": ("validation_command",),
    },
    "artifact_transform": {
        "strategy_name": "artifact_transform",
        "runner_kind": "artifact",
        "required_packet_fields": COMMON_PACKET_FIELDS,
        "required_strategy_inputs": ("input_artifacts", "output_artifacts"),
        "result_requirements": ("produced_artifacts", "result_artifact_path"),
        "supports_parallel": True,
        "allowed_packet_classes": ("implementation", "supporting"),
        "fallback_policy": "no_fallback",
        "alternate_strategies": (),
    },
    "review_evidence_packet": {
        "strategy_name": "review_evidence_packet",
        "runner_kind": "review",
        "required_packet_fields": COMMON_PACKET_FIELDS,
        "required_strategy_inputs": ("review_focus", "expected_artifacts"),
        "result_requirements": ("produced_artifacts", "evidence_refs", "result_artifact_path"),
        "supports_parallel": False,
        "allowed_packet_classes": ("review", "validation"),
        "fallback_policy": "no_fallback",
        "alternate_strategies": (),
    },
    "multi_command_pipeline": {
        "strategy_name": "multi_command_pipeline",
        "runner_kind": "pipeline",
        "required_packet_fields": COMMON_PACKET_FIELDS,
        "required_strategy_inputs": ("commands",),
        "result_requirements": ("captured_commands", "evidence_refs", "result_artifact_path", "step_results"),
        "supports_parallel": False,
        "allowed_packet_classes": ("validation", "implementation"),
        "fallback_policy": "no_fallback",
        "alternate_strategies": (),
    },
    "codex_prompt_worker": {
        "strategy_name": "codex_prompt_worker",
        "runner_kind": "codex",
        "required_packet_fields": COMMON_PACKET_FIELDS + ("fallback_reason",),
        "required_strategy_inputs": ("worker_goal", "prompt_contract_ref", "expected_artifacts"),
        "result_requirements": ("evidence_refs", "result_artifact_path", "fallback_used", "fallback_reason"),
        "supports_parallel": False,
        "allowed_packet_classes": ("implementation", "review", "supporting"),
        "fallback_policy": "explicit_only",
        "alternate_strategies": (),
    },
}


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _non_empty_string(value: Any, minimum: int = 1) -> bool:
    return isinstance(value, str) and len(value.strip()) >= minimum


def strategy_spec(strategy_name: str) -> dict[str, Any] | None:
    return STRATEGY_REGISTRY.get(str(strategy_name or "").strip())


def validate_strategy_packet(packet: dict[str, Any]) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    blocked: list[str] = []
    packet_id = str(packet.get("packet_id", "")).strip() or "unknown"
    prefix = f"packet:{packet_id}"
    strategy_name = str(packet.get("execution_strategy", "")).strip()
    if not _non_empty_string(strategy_name, 3):
        missing.append(f"{prefix}:execution_strategy")
        return missing, blocked
    spec = strategy_spec(strategy_name)
    if spec is None:
        blocked.append(f"{prefix}:strategy_unknown:{strategy_name}")
        return missing, blocked

    for field_name in spec["required_packet_fields"]:
        if field_name == "strategy_inputs":
            if not isinstance(packet.get("strategy_inputs"), dict):
                missing.append(f"{prefix}:strategy_inputs")
        elif field_name in {"allowed_scope", "dependencies", "acceptance_checks", "failure_signals", "shared_surface_categories"}:
            if not isinstance(packet.get(field_name), list):
                missing.append(f"{prefix}:{field_name}")
        elif not packet.get(field_name):
            missing.append(f"{prefix}:{field_name}")

    strategy_inputs = packet.get("strategy_inputs") if isinstance(packet.get("strategy_inputs"), dict) else {}
    for field_name in spec["required_strategy_inputs"]:
        value = strategy_inputs.get(field_name)
        if field_name in {"input_artifacts", "output_artifacts", "expected_artifacts", "commands"}:
            if not _as_string_list(value):
                missing.append(f"{prefix}:strategy_inputs:{field_name}")
        elif not _non_empty_string(value, 3):
            missing.append(f"{prefix}:strategy_inputs:{field_name}")

    has_execution_command = _non_empty_string(packet.get("execution_command"), 3)
    if has_execution_command and spec["runner_kind"] not in {"command", "pipeline"}:
        blocked.append(f"{prefix}:execution_command_not_allowed_for_strategy:{strategy_name}")
    if spec["runner_kind"] == "command":
        command = strategy_inputs.get("command") or packet.get("execution_command")
        if not _non_empty_string(command, 3):
            missing.append(f"{prefix}:strategy_inputs:command")
    if spec["runner_kind"] == "pipeline":
        commands = _as_string_list(strategy_inputs.get("commands"))
        if not commands:
            missing.append(f"{prefix}:strategy_inputs:commands")
    if spec["runner_kind"] == "artifact" and has_execution_command:
        blocked.append(f"{prefix}:command_not_allowed_for_artifact_transform")
    if spec["runner_kind"] == "review" and (has_execution_command or _non_empty_string(strategy_inputs.get("command"), 3)):
        blocked.append(f"{prefix}:command_not_allowed_for_review_evidence_packet")
    if strategy_name != "codex_prompt_worker" and strategy_inputs.get("worker_goal"):
        blocked.append(f"{prefix}:worker_goal_not_allowed_for_strategy:{strategy_name}")
    if strategy_name == "codex_prompt_worker" and has_execution_command:
        blocked.append(f"{prefix}:execution_command_not_allowed_for_codex_prompt_worker")
    if strategy_name == "codex_prompt_worker" and not _non_empty_string(packet.get("fallback_reason"), 3):
        missing.append(f"{prefix}:fallback_reason")
    if strategy_name != "codex_prompt_worker" and _non_empty_string(packet.get("fallback_reason"), 3):
        blocked.append(f"{prefix}:fallback_reason_not_allowed_for_strategy:{strategy_name}")
    packet_class = str(packet.get("packet_class") or packet.get("classification") or "").strip()
    allowed_classes = {str(item).strip() for item in spec.get("allowed_packet_classes", ()) if str(item).strip()}
    if allowed_classes and packet_class and packet_class not in allowed_classes and packet_class not in {"ready", "blocked_dependency", "blocked_authority", "deferred"}:
        blocked.append(f"{prefix}:packet_class_not_allowed_for_strategy:{strategy_name}:{packet_class}")
    return missing, blocked


def execution_readiness_failures(packet: dict[str, Any]) -> list[str]:
    missing, blocked = validate_strategy_packet(packet)
    failures: list[str] = []
    for item in missing:
        if item.endswith(":strategy_inputs:command"):
            failures.append("missing_command")
        elif ":strategy_inputs" in item:
            failures.append("strategy_inputs_invalid")
        else:
            failures.append(item.rsplit(":", 1)[-1])
    for item in blocked:
        if ":strategy_unknown:" in item:
            failures.append("strategy_unknown")
        elif "command_not_allowed" in item or "execution_command_not_allowed" in item:
            failures.append("strategy_inputs_invalid")
        else:
            failures.append(item.rsplit(":", 1)[-1])
    return sorted(set(failures))
