#!/usr/bin/env python3
"""Stateful proxy runtime for packetized autonomous completion."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from common import (
    append_jsonl,
    build_objective_summary,
    build_packet_quality_report,
    build_repo_validation_plan,
    build_runtime_bootstrap_artifacts,
    canonical_python_argv,
    cycle_artifact_paths,
    discover_repo_capabilities,
    ensure_python_3_11,
    EVIDENCE_REF_KINDS,
    EVIDENCE_REF_REQUIRED_FIELDS,
    EVIDENCE_REF_SCHEMA_VERSION,
    EXECUTION_PLAN_REQUIRED_FIELDS,
    EXECUTION_PLAN_UNIT_REQUIRED_FIELDS,
    EXECUTION_PLAN_SCHEMA_VERSION,
    FAILED_ATTEMPT_RECORD_SCHEMA_VERSION,
    FAILED_ATTEMPT_REQUIRED_FIELDS,
    file_lock,
    KERNEL_ACTION_KINDS,
    KERNEL_HALT_REASONS,
    KERNEL_RUNTIME_STATE_REQUIRED_FIELDS,
    KERNEL_RUNTIME_STATE_SCHEMA_VERSION,
    KERNEL_RUNTIME_STATES,
    KERNEL_RUNTIME_TERMINAL_STATES,
    load_json_file,
    now_iso,
    OBJECTIVE_RUNTIME_EXECUTION_COVERAGE_SCHEMA_VERSION,
    OBJECTIVE_RUNTIME_OPERATOR_VIEW_SCHEMA_VERSION,
    OBJECTIVE_RUNTIME_STATE_SCHEMA_VERSION,
    OBJECTIVE_RUNTIME_SUPPORT_CONFIDENCE_SCHEMA_VERSION,
    packet_definition_path,
    packet_verdict_path,
    resolve_artifacts_root,
    runtime_artifact_paths,
    sanitize_token,
    session_artifact_paths,
    sha256_file,
    stable_objective_id,
    transaction_artifact_paths,
    transaction_path_overrides,
    TRANSITION_HISTORY_RECORD_SCHEMA_VERSION,
    TRANSITION_HISTORY_REQUIRED_FIELDS,
    VERIFICATION_RESULT_BLAME,
    VERIFICATION_RESULT_REPAIRABILITY,
    VERIFICATION_RESULT_REQUIRED_FIELDS,
    VERIFICATION_RESULT_SCHEMA_VERSION,
    VERIFICATION_RESULT_SCOPES,
    VERIFICATION_RESULT_STATUSES,
    write_capture_manifest,
    write_json_file,
    write_text_atomic,
    resolve_transaction_managed_path,
)
from objective_scheduler import (
    apply_cycle_review,
    build_schedule,
    classify_cycle_outcome,
    compute_runnable_set,
    evaluate_objective_closure,
    infer_frontier_movement_reason,
)
from execution_strategies import strategy_spec
from run_cmd_capture import _check_command_safety, _safe_env_view
from verify_cycle import verify_cycle_payload

RUNTIME_EXIT_APPROVE = 0
RUNTIME_EXIT_REVISE = 10
RUNTIME_EXIT_BLOCKED = 20
RUNTIME_EXIT_ERROR = 30

CONTROLLER_MODE_AUDIT = "audit"
CONTROLLER_MODE_ENFORCE = "enforce"
TRANSACTION_STATE_SCHEMA_VERSION = "objective-transaction-state.v1"
TRANSACTION_LOG_SCHEMA_VERSION = "objective-transaction-record.v1"
TRANSACTION_TERMINAL_STATES = {"committed", "recovered", "aborted"}


def _resolve_plan_payload(
    *,
    plan_payload: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = plan_payload if isinstance(plan_payload, dict) else plan
    if not isinstance(payload, dict):
        raise RuntimeError("plan_payload_required")
    return payload


def _status_exit_code(status: str) -> int:
    if status == "approve":
        return RUNTIME_EXIT_APPROVE
    if status == "revise":
        return RUNTIME_EXIT_REVISE
    if status == "blocked":
        return RUNTIME_EXIT_BLOCKED
    return RUNTIME_EXIT_ERROR


def _normalize_controller_mode(controller_mode: str | None) -> str:
    normalized = str(controller_mode or os.environ.get("CODEX_OBJECTIVE_CONTROLLER_MODE") or CONTROLLER_MODE_ENFORCE).strip().lower()
    return normalized if normalized in {CONTROLLER_MODE_AUDIT, CONTROLLER_MODE_ENFORCE} else CONTROLLER_MODE_ENFORCE


TRANSACTIONAL_RUNTIME_ARTIFACT_KEYS = (
    "packet_dag",
    "status",
    "schedule",
    "summary",
    "execution_plan",
    "kernel_runtime_state",
    "transition_history",
    "verification_results",
    "runtime_state",
    "validation_plan",
    "repo_capabilities",
    "packet_quality",
    "execution_coverage",
    "support_confidence",
    "packet_results",
    "execution_ledger",
    "operator_view",
)
TRANSACTIONAL_SESSION_ARTIFACT_KEYS = ("checkpoint", "momentum", "blockers")
TRANSACTION_COMMIT_ORDER = (
    "execution_plan",
    "packet_dag",
    "status",
    "schedule",
    "transition_history",
    "verification_results",
    "packet_results",
    "execution_ledger",
    "validation_plan",
    "repo_capabilities",
    "packet_quality",
    "execution_coverage",
    "support_confidence",
    "summary",
    "runtime_state",
    "kernel_runtime_state",
    "operator_view",
    "momentum",
    "blockers",
    "checkpoint",
    "cycle_state",
)


def _next_transaction_id(*, track_id: str, step_id: str) -> str:
    return f"{sanitize_token(step_id or 'runtime-step')}-{sanitize_token(track_id)}-{int(time.time_ns())}"


def _transaction_state_path(*, artifacts_root: Path, track_id: str) -> Path:
    return runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["transaction_state"]


def _transaction_log_path(*, artifacts_root: Path, track_id: str) -> Path:
    return runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["transaction_log"]


def _load_transaction_state(*, artifacts_root: Path, track_id: str) -> dict[str, Any]:
    return _load_json_if_exists(_transaction_state_path(artifacts_root=artifacts_root, track_id=track_id))


def _transaction_summary_payload(
    *,
    artifacts_root: Path,
    track_id: str,
    transaction_state: dict[str, Any] | None = None,
    latest_transaction_log: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    tx_state = transaction_state if isinstance(transaction_state, dict) else _load_transaction_state(artifacts_root=artifacts_root, track_id=track_id)
    tx_log = latest_transaction_log if isinstance(latest_transaction_log, dict) else (
        (_load_jsonl(runtime_paths["transaction_log"])[-1]) if runtime_paths["transaction_log"].exists() and _load_jsonl(runtime_paths["transaction_log"]) else {}
    )
    return {
        "transaction_id": str(tx_state.get("transaction_id") or tx_log.get("transaction_id") or "").strip(),
        "state": str(tx_state.get("state") or tx_log.get("state") or "").strip(),
        "step_id": str(tx_state.get("step_id") or tx_log.get("step_id") or "").strip(),
        "recovered": (
            tx_state.get("state") == "recovered"
            or tx_log.get("recovered") is True
            or str(tx_state.get("recovery_outcome") or "").strip() == "finished_commit"
        ),
        "recovery_outcome": str(tx_state.get("recovery_outcome") or "").strip(),
        "updated_at": str(tx_state.get("updated_at") or tx_log.get("timestamp") or "").strip(),
        "committed_artifact_count": int(
            tx_state.get("committed_artifact_count")
            or tx_log.get("committed_artifact_count")
            or 0
        ),
        "artifact_paths": {
            "transaction_state": str(runtime_paths["transaction_state"]),
            "transaction_log": str(runtime_paths["transaction_log"]),
        },
    }


def _sync_operator_view_transaction_summary(
    *,
    artifacts_root: Path,
    track_id: str,
    transaction_state: dict[str, Any] | None = None,
    latest_transaction_log: dict[str, Any] | None = None,
) -> None:
    runtime_paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    operator_view = _load_json_if_exists(runtime_paths["operator_view"])
    if not operator_view:
        return
    artifacts = operator_view.get("artifacts") if isinstance(operator_view.get("artifacts"), dict) else {}
    artifacts["transaction_state"] = str(runtime_paths["transaction_state"])
    artifacts["transaction_log"] = str(runtime_paths["transaction_log"])
    operator_view["artifacts"] = artifacts
    operator_view["transaction"] = _transaction_summary_payload(
        artifacts_root=artifacts_root,
        track_id=track_id,
        transaction_state=transaction_state,
        latest_transaction_log=latest_transaction_log,
    )
    operator_view["updated_at"] = now_iso()
    write_json_file(runtime_paths["operator_view"], operator_view)


def _transaction_paths(
    *,
    artifacts_root: Path,
    track_id: str,
    transaction_id: str,
) -> dict[str, Path]:
    return transaction_artifact_paths(artifacts_root=artifacts_root, track_id=track_id, transaction_id=transaction_id)


def _artifact_sha256(path: Path) -> str:
    return sha256_file(path) if path.exists() else ""


def _replace_file_from_stage(*, source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f".{destination.name}.tx-{os.getpid()}-{time.time_ns()}")
    tmp.write_bytes(source.read_bytes())
    tmp.replace(destination)


def _transaction_fail_after() -> int:
    raw = str(os.environ.get("CODEX_OBJECTIVE_TX_FAIL_AFTER") or "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if value > 0 else 0


def _transaction_prepare_only() -> bool:
    return str(os.environ.get("CODEX_OBJECTIVE_TX_PREPARE_ONLY") or "").strip() == "1"


def _transaction_targets(
    *,
    artifacts_root: Path,
    track_id: str,
    step_id: str,
    cycle_state_path: Path | None = None,
) -> list[dict[str, Any]]:
    runtime_paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    session_paths = session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    track_root = artifacts_root / sanitize_token(track_id)
    targets: list[dict[str, Any]] = []
    for key in TRANSACTIONAL_RUNTIME_ARTIFACT_KEYS:
        live = runtime_paths[key]
        targets.append(
            {
                "artifact_key": key,
                "live_path": str(live),
                "relative_path": str(live.relative_to(track_root)),
                "session_artifact": False,
                "step_id": step_id,
            }
        )
    for key in TRANSACTIONAL_SESSION_ARTIFACT_KEYS:
        live = session_paths[key]
        targets.append(
            {
                "artifact_key": key,
                "live_path": str(live),
                "relative_path": str(live.relative_to(track_root)),
                "session_artifact": True,
                "step_id": step_id,
            }
        )
    if cycle_state_path is not None:
        targets.append(
            {
                "artifact_key": "cycle_state",
                "live_path": str(cycle_state_path),
                "relative_path": str(cycle_state_path.relative_to(track_root)),
                "session_artifact": False,
                "step_id": step_id,
            }
        )
    return targets


def _write_transaction_state(*, artifacts_root: Path, track_id: str, payload: dict[str, Any]) -> None:
    write_json_file(_transaction_state_path(artifacts_root=artifacts_root, track_id=track_id), payload)


def _append_transaction_log(*, artifacts_root: Path, track_id: str, payload: dict[str, Any]) -> None:
    append_jsonl(_transaction_log_path(artifacts_root=artifacts_root, track_id=track_id), payload)


def _prepare_runtime_transaction(
    *,
    artifacts_root: Path,
    track_id: str,
    step_id: str,
    cycle_state_path: Path | None = None,
) -> dict[str, Any]:
    existing = _load_transaction_state(artifacts_root=artifacts_root, track_id=track_id)
    if existing and str(existing.get("state") or "").strip() not in {"", *TRANSACTION_TERMINAL_STATES}:
        raise RuntimeError("transaction_already_open")
    transaction_id = _next_transaction_id(track_id=track_id, step_id=step_id)
    tx_paths = _transaction_paths(artifacts_root=artifacts_root, track_id=track_id, transaction_id=transaction_id)
    staged_root = tx_paths["staged"]
    staged_root.mkdir(parents=True, exist_ok=True)
    targets = _transaction_targets(
        artifacts_root=artifacts_root,
        track_id=track_id,
        step_id=step_id,
        cycle_state_path=cycle_state_path,
    )
    path_overrides: dict[Path, Path] = {}
    records: list[dict[str, Any]] = []
    for target in targets:
        live = Path(str(target["live_path"])).expanduser().resolve()
        staged = staged_root / str(target["relative_path"])
        staged.parent.mkdir(parents=True, exist_ok=True)
        if live.exists():
            staged.write_bytes(live.read_bytes())
        path_overrides[live] = staged
        records.append(
            {
                **target,
                "baseline_sha256": _artifact_sha256(live),
                "staged_path": str(staged),
                "staged_sha256": _artifact_sha256(staged),
            }
        )
    payload = {
        "schema_version": TRANSACTION_STATE_SCHEMA_VERSION,
        "track_id": track_id,
        "transaction_id": transaction_id,
        "step_id": step_id,
        "state": "prepared",
        "targets": records,
        "started_at": now_iso(),
        "updated_at": now_iso(),
        "recovery_outcome": "",
    }
    _write_transaction_state(artifacts_root=artifacts_root, track_id=track_id, payload=payload)
    _append_transaction_log(
        artifacts_root=artifacts_root,
        track_id=track_id,
        payload={
            "schema_version": TRANSACTION_LOG_SCHEMA_VERSION,
            "transaction_id": transaction_id,
            "track_id": track_id,
            "step_id": step_id,
            "state": "prepared",
            "timestamp": payload["updated_at"],
        },
    )
    return {"state": payload, "path_overrides": path_overrides, "paths": tx_paths}


def _refresh_transaction_hashes(tx_state: dict[str, Any]) -> dict[str, Any]:
    targets = tx_state.get("targets") if isinstance(tx_state.get("targets"), list) else []
    refreshed: list[dict[str, Any]] = []
    for target in targets:
        if not isinstance(target, dict):
            continue
        staged_path = Path(str(target.get("staged_path") or "")).expanduser()
        refreshed.append(
            {
                **target,
                "staged_sha256": _artifact_sha256(staged_path.resolve()) if str(staged_path).strip() else "",
            }
        )
    tx_state["targets"] = refreshed
    tx_state["updated_at"] = now_iso()
    return tx_state


def _validate_transaction_staged_files(tx_state: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for target in tx_state.get("targets", []) if isinstance(tx_state.get("targets"), list) else []:
        if not isinstance(target, dict):
            errors.append("transaction:target_not_object")
            continue
        live_path = str(target.get("live_path") or "").strip()
        staged_path = str(target.get("staged_path") or "").strip()
        artifact_key = str(target.get("artifact_key") or "").strip() or live_path
        if not live_path or not staged_path:
            errors.append(f"transaction:target_invalid:{artifact_key}")
            continue
        staged = Path(staged_path).expanduser().resolve()
        if not staged.exists():
            errors.append(f"transaction:staged_missing:{artifact_key}")
            continue
        expected_hash = str(target.get("staged_sha256") or "").strip()
        actual_hash = _artifact_sha256(staged)
        if expected_hash and expected_hash != actual_hash:
            errors.append(f"transaction:staged_hash_mismatch:{artifact_key}")
    return errors


def _transaction_commit_targets(tx_state: dict[str, Any]) -> list[dict[str, Any]]:
    targets_by_key = {
        str(target.get("artifact_key") or ""): target
        for target in tx_state.get("targets", [])
        if isinstance(target, dict) and str(target.get("artifact_key") or "").strip()
    }
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in TRANSACTION_COMMIT_ORDER:
        target = targets_by_key.get(key)
        if target is None:
            continue
        ordered.append(target)
        seen.add(key)
    for key, target in targets_by_key.items():
        if key not in seen:
            ordered.append(target)
    return ordered


def _commit_runtime_transaction(
    *,
    artifacts_root: Path,
    track_id: str,
    tx_state: dict[str, Any],
    recovered: bool = False,
) -> dict[str, Any]:
    tx_state = _refresh_transaction_hashes(dict(tx_state))
    errors = _validate_transaction_staged_files(tx_state)
    if errors:
        raise RuntimeError(",".join(errors))
    tx_state["state"] = "committing"
    tx_state["updated_at"] = now_iso()
    _write_transaction_state(artifacts_root=artifacts_root, track_id=track_id, payload=tx_state)
    fail_after = _transaction_fail_after()
    committed_count = 0
    for target in _transaction_commit_targets(tx_state):
        source = Path(str(target.get("staged_path") or "")).expanduser().resolve()
        destination = Path(str(target.get("live_path") or "")).expanduser().resolve()
        _replace_file_from_stage(source=source, destination=destination)
        committed_count += 1
        if fail_after and committed_count >= fail_after:
            raise RuntimeError("transaction_commit_interrupted")
    tx_state["state"] = "recovered" if recovered else "committed"
    tx_state["updated_at"] = now_iso()
    tx_state["recovery_outcome"] = "finished_commit" if recovered else ""
    tx_state["committed_artifact_count"] = committed_count
    _write_transaction_state(artifacts_root=artifacts_root, track_id=track_id, payload=tx_state)
    _append_transaction_log(
        artifacts_root=artifacts_root,
        track_id=track_id,
        payload={
            "schema_version": TRANSACTION_LOG_SCHEMA_VERSION,
            "transaction_id": str(tx_state.get("transaction_id") or ""),
            "track_id": track_id,
            "step_id": str(tx_state.get("step_id") or ""),
            "state": str(tx_state.get("state") or ""),
            "timestamp": str(tx_state.get("updated_at") or now_iso()),
            "recovered": recovered,
            "committed_artifact_count": committed_count,
        },
    )
    _sync_operator_view_transaction_summary(
        artifacts_root=artifacts_root,
        track_id=track_id,
        transaction_state=tx_state,
        latest_transaction_log={
            "transaction_id": str(tx_state.get("transaction_id") or ""),
            "state": str(tx_state.get("state") or ""),
            "step_id": str(tx_state.get("step_id") or ""),
            "timestamp": str(tx_state.get("updated_at") or now_iso()),
            "recovered": recovered,
            "committed_artifact_count": committed_count,
        },
    )
    return tx_state


def _run_in_runtime_transaction(
    *,
    artifacts_root: Path,
    track_id: str,
    step_id: str,
    cycle_state_path: Path | None,
    body,
) -> dict[str, Any]:
    tx = _prepare_runtime_transaction(
        artifacts_root=artifacts_root,
        track_id=track_id,
        step_id=step_id,
        cycle_state_path=cycle_state_path,
    )
    tx_state = tx["state"]
    tx_result: dict[str, Any] = {}
    try:
        with transaction_path_overrides(tx["path_overrides"]):
            tx_result = body()
            tx_state = _refresh_transaction_hashes(dict(_load_transaction_state(artifacts_root=artifacts_root, track_id=track_id) or tx_state))
            _write_transaction_state(artifacts_root=artifacts_root, track_id=track_id, payload=tx_state)
            if _transaction_prepare_only():
                raise RuntimeError("transaction_prepare_only")
        committed = _commit_runtime_transaction(
            artifacts_root=artifacts_root,
            track_id=track_id,
            tx_state=tx_state,
            recovered=False,
        )
        return {
            **tx_result,
            "transaction_id": str(committed.get("transaction_id") or ""),
            "transaction_state": str(committed.get("state") or ""),
            "recovered": False,
            "committed_artifact_count": int(committed.get("committed_artifact_count") or 0),
        }
    except Exception:
        current_state = _load_transaction_state(artifacts_root=artifacts_root, track_id=track_id) or tx_state
        if str(current_state.get("state") or "").strip() in {"prepared", "committing"}:
            _write_transaction_state(artifacts_root=artifacts_root, track_id=track_id, payload=current_state)
        raise


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    managed = resolve_transaction_managed_path(path)
    if not managed.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in managed.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    content = "\n".join(json.dumps(row, sort_keys=True) for row in rows)
    write_text_atomic(path, f"{content}\n" if content else "")


def _load_kernel_runtime_state(*, artifacts_root: Path, track_id: str) -> dict[str, Any]:
    return _load_json_if_exists(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["kernel_runtime_state"])


def _load_execution_plan(*, artifacts_root: Path, track_id: str) -> dict[str, Any]:
    return _load_json_if_exists(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["execution_plan"])


def _write_kernel_runtime_state(*, artifacts_root: Path, track_id: str, payload: dict[str, Any]) -> None:
    write_json_file(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["kernel_runtime_state"], payload)


def _terminal_reason_for_state(state: str) -> str:
    if state == "success":
        return "accepted_success"
    if state == "partial":
        return "accepted_partial"
    if state == "closed_blocked":
        return "accepted_blocked"
    if state == "unsafe":
        return "unsafe_to_continue"
    return "none"


def _evidence_ref(*, kind: str, path: str, producer: str, step_id: str) -> dict[str, Any]:
    return {
        "schema_version": EVIDENCE_REF_SCHEMA_VERSION,
        "evidence_id": f"ev-{sanitize_token(step_id)}-{sanitize_token(kind)}-{sanitize_token(Path(path).name or 'artifact')}",
        "kind": kind,
        "path": path,
        "producer": producer,
        "step_id": step_id,
    }


def _packet_group_id(packet_ids: list[str], cycle_id: str) -> str:
    normalized = [packet_id for packet_id in packet_ids if packet_id]
    if len(normalized) == 1:
        return normalized[0]
    return f"cycle:{cycle_id}"


def _primary_action_for_cycle_request(cycle_request: dict[str, Any]) -> str:
    packets = cycle_request.get("packets") if isinstance(cycle_request.get("packets"), list) else []
    if len(packets) > 1:
        return "delegate"
    if not packets:
        return "inspect"
    packet = packets[0] if isinstance(packets[0], dict) else {}
    strategy = str(packet.get("execution_strategy") or "").strip()
    if strategy in {"codex_prompt_worker", "artifact_transform"}:
        return "edit"
    if strategy in {"review_evidence_packet"}:
        return "verify"
    return "run_command"


def _kernel_verification_status(verifier_output: str) -> tuple[str, str]:
    if verifier_output == "accepted":
        return "pass", "narrow_scope"
    if verifier_output == "rejected_rework":
        return "soft_fail", "retryable"
    return "hard_fail", "blocked"


def _validate_record_fields(record: dict[str, Any], required_fields: tuple[str, ...], prefix: str) -> list[str]:
    missing: list[str] = []
    for field in required_fields:
        if field not in record:
            missing.append(f"{prefix}:{field}")
    return missing


def validate_state(
    *,
    state: dict[str, Any],
    execution_plan: dict[str, Any],
    validation_plan: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    errors.extend(_validate_record_fields(state, KERNEL_RUNTIME_STATE_REQUIRED_FIELDS, "kernel_state"))
    if state.get("schema_version") != KERNEL_RUNTIME_STATE_SCHEMA_VERSION:
        errors.append("kernel_state:schema_version_invalid")
    if str(state.get("state") or "") not in KERNEL_RUNTIME_STATES:
        errors.append("kernel_state:state_invalid")
    halt = state.get("halt") if isinstance(state.get("halt"), dict) else {}
    reason = str(halt.get("reason") or "")
    if reason not in KERNEL_HALT_REASONS:
        errors.append("kernel_state:halt_reason_invalid")
    active_unit_ids = state.get("active_unit_ids") if isinstance(state.get("active_unit_ids"), list) else []
    transition_history = state.get("transition_history") if isinstance(state.get("transition_history"), list) else []
    failed_attempts = state.get("failed_attempts") if isinstance(state.get("failed_attempts"), list) else []
    last_action = state.get("last_action")
    execution_units = execution_plan.get("units") if isinstance(execution_plan.get("units"), list) else []
    unit_ids = {str(unit.get("unit_id") or "").strip() for unit in execution_units if isinstance(unit, dict)}
    generated_packets = (
        validation_plan.get("generated_packets")
        if isinstance(validation_plan, dict) and isinstance(validation_plan.get("generated_packets"), list)
        else []
    )
    generated_unit_ids = {
        str(packet.get("packet_id") or "").strip()
        for packet in generated_packets
        if isinstance(packet, dict) and str(packet.get("packet_id") or "").strip()
    }
    if execution_plan.get("schema_version") != EXECUTION_PLAN_SCHEMA_VERSION:
        errors.append("execution_plan:schema_version_invalid")
    errors.extend(_validate_record_fields(execution_plan, EXECUTION_PLAN_REQUIRED_FIELDS, "execution_plan"))
    for unit in execution_units:
        if not isinstance(unit, dict):
            errors.append("execution_plan:unit_not_object")
            continue
        errors.extend(_validate_record_fields(unit, EXECUTION_PLAN_UNIT_REQUIRED_FIELDS, "execution_plan:unit"))
    for item in transition_history:
        if not isinstance(item, dict):
            errors.append("kernel_state:transition_history:item_not_object")
            continue
        errors.extend(_validate_record_fields(item, TRANSITION_HISTORY_REQUIRED_FIELDS, "kernel_state:transition_history"))
    for item in failed_attempts:
        if not isinstance(item, dict):
            errors.append("kernel_state:failed_attempts:item_not_object")
            continue
        errors.extend(_validate_record_fields(item, FAILED_ATTEMPT_REQUIRED_FIELDS, "kernel_state:failed_attempts"))
    current_state = str(state.get("state") or "")
    if current_state == "acting":
        if not active_unit_ids:
            errors.append("kernel_state:acting:active_unit_ids_missing")
        if not isinstance(last_action, dict) or str(last_action.get("kind") or "") not in KERNEL_ACTION_KINDS:
            errors.append("kernel_state:acting:last_action_invalid")
    if current_state == "verifying":
        if not active_unit_ids:
            errors.append("kernel_state:verifying:active_unit_ids_missing")
        if not state.get("evidence_refs"):
            errors.append("kernel_state:verifying:evidence_refs_missing")
    if current_state == "repair_pending" and not str(state.get("last_verification_id") or "").strip():
        errors.append("kernel_state:repair_pending:last_verification_id_missing")
    if current_state in KERNEL_RUNTIME_TERMINAL_STATES and halt.get("terminal") is not True:
        errors.append("kernel_state:terminal_requires_halt")
    if current_state not in KERNEL_RUNTIME_TERMINAL_STATES and halt.get("terminal") is True and reason != "none":
        errors.append("kernel_state:nonterminal_halt_conflict")
    for unit_id in active_unit_ids:
        text = str(unit_id).strip()
        if text and not text.startswith("cycle:") and text not in unit_ids and text not in generated_unit_ids:
            errors.append(f"kernel_state:active_unit_unknown:{text}")
    if isinstance(last_action, dict):
        if str(last_action.get("kind") or "") not in KERNEL_ACTION_KINDS:
            errors.append("kernel_state:last_action_kind_invalid")
    for ref in state.get("evidence_refs", []) if isinstance(state.get("evidence_refs"), list) else []:
        if not isinstance(ref, dict):
            errors.append("kernel_state:evidence_ref_not_object")
            continue
        errors.extend(_validate_record_fields(ref, EVIDENCE_REF_REQUIRED_FIELDS, "kernel_state:evidence_ref"))
        if str(ref.get("kind") or "") not in EVIDENCE_REF_KINDS:
            errors.append("kernel_state:evidence_ref_kind_invalid")
    return errors


def _append_transition_record(
    *,
    artifacts_root: Path,
    track_id: str,
    kernel_state: dict[str, Any],
    step_id: str,
    from_state: str,
    to_state: str,
    guard: str,
    guard_result: bool,
    trigger: str,
    evidence_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    record = {
        "schema_version": TRANSITION_HISTORY_RECORD_SCHEMA_VERSION,
        "step_id": step_id,
        "from": from_state,
        "to": to_state,
        "guard": guard,
        "guard_result": guard_result,
        "trigger": trigger,
        "evidence_refs": evidence_refs or [],
        "timestamp": now_iso(),
    }
    kernel_state.setdefault("transition_history", []).append(record)
    append_jsonl(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["transition_history"], record)
    return record


def _append_verification_result(
    *,
    artifacts_root: Path,
    track_id: str,
    payload: dict[str, Any],
) -> None:
    append_jsonl(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["verification_results"], payload)


def _record_failed_attempt(
    *,
    kernel_state: dict[str, Any],
    step_id: str,
    unit_id: str,
    failure_class: str,
    verification_id: str,
    count_against_retry_budget: bool,
) -> None:
    kernel_state.setdefault("failed_attempts", []).append(
        {
            "schema_version": FAILED_ATTEMPT_RECORD_SCHEMA_VERSION,
            "step_id": step_id,
            "unit_id": unit_id,
            "failure_class": failure_class,
            "verification_id": verification_id,
            "count_against_retry_budget": count_against_retry_budget,
            "timestamp": now_iso(),
        }
    )


def _force_kernel_unsafe(
    *,
    artifacts_root: Path,
    track_id: str,
    kernel_state: dict[str, Any],
    errors: list[str],
    step_id: str,
) -> dict[str, Any]:
    invalid_artifact = {
        "schema_version": "invalid-transition.v1",
        "track_id": track_id,
        "step_id": step_id,
        "errors": errors,
        "timestamp": now_iso(),
    }
    write_json_file(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["invalid_transition"], invalid_artifact)
    evidence = [
        _evidence_ref(
            kind="policy_violation",
            path=str(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["invalid_transition"]),
            producer="validate_state",
            step_id=step_id,
        )
    ]
    previous_state = str(kernel_state.get("state") or "unsafe")
    kernel_state["state"] = "unsafe"
    kernel_state["halt"] = {"terminal": True, "reason": "invalid_transition"}
    kernel_state["active_unit_id"] = None
    kernel_state["active_unit_ids"] = []
    kernel_state["evidence_refs"] = evidence
    _append_transition_record(
        artifacts_root=artifacts_root,
        track_id=track_id,
        kernel_state=kernel_state,
        step_id=step_id,
        from_state=previous_state,
        to_state="unsafe",
        guard="invalid_transition_detected",
        guard_result=False,
        trigger="validate_state",
        evidence_refs=evidence,
    )
    _write_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id, payload=kernel_state)
    return invalid_artifact


def _minimal_unsafe_kernel_state(*, track_id: str) -> dict[str, Any]:
    return {
        "schema_version": KERNEL_RUNTIME_STATE_SCHEMA_VERSION,
        "objective_id": stable_objective_id(track_id),
        "track_id": track_id,
        "state": "unsafe",
        "active_unit_id": None,
        "active_unit_ids": [],
        "completed_units": [],
        "failed_attempts": [],
        "last_action": None,
        "last_verification_id": None,
        "evidence_refs": [],
        "budget": {
            "remaining_steps": 0,
            "remaining_mutations": 0,
            "remaining_retries": 0,
        },
        "halt": {"terminal": True, "reason": "invalid_transition"},
        "transition_history": [],
    }


def _abort_transaction_integrity_failure(
    *,
    artifacts_root: Path,
    track_id: str,
    tx_state: dict[str, Any],
    errors: list[str],
) -> dict[str, Any]:
    invalid_artifact = {
        "schema_version": "invalid-transition.v1",
        "track_id": track_id,
        "step_id": str(tx_state.get("step_id") or ""),
        "errors": errors,
        "timestamp": now_iso(),
    }
    write_json_file(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["invalid_transition"], invalid_artifact)
    kernel_state = _load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id)
    if not kernel_state:
        staged_kernel = {}
        for target in tx_state.get("targets", []) if isinstance(tx_state.get("targets"), list) else []:
            if not isinstance(target, dict) or str(target.get("artifact_key") or "") != "kernel_runtime_state":
                continue
            staged_path = Path(str(target.get("staged_path") or "")).expanduser().resolve()
            if staged_path.exists():
                staged_kernel = _load_json_if_exists(staged_path)
                break
        kernel_state = staged_kernel if staged_kernel else _minimal_unsafe_kernel_state(track_id=track_id)
    _force_kernel_unsafe(
        artifacts_root=artifacts_root,
        track_id=track_id,
        kernel_state=kernel_state,
        errors=errors,
        step_id=str(tx_state.get("step_id") or "transaction-recovery"),
    )
    tx_state["state"] = "aborted"
    tx_state["updated_at"] = now_iso()
    tx_state["recovery_outcome"] = "integrity_failure"
    _write_transaction_state(artifacts_root=artifacts_root, track_id=track_id, payload=tx_state)
    _append_transaction_log(
        artifacts_root=artifacts_root,
        track_id=track_id,
        payload={
            "schema_version": TRANSACTION_LOG_SCHEMA_VERSION,
            "transaction_id": str(tx_state.get("transaction_id") or ""),
            "track_id": track_id,
            "step_id": str(tx_state.get("step_id") or ""),
            "state": "aborted",
            "timestamp": str(tx_state.get("updated_at") or now_iso()),
            "errors": errors,
        },
    )
    _sync_operator_view_transaction_summary(
        artifacts_root=artifacts_root,
        track_id=track_id,
        transaction_state=tx_state,
        latest_transaction_log={
            "transaction_id": str(tx_state.get("transaction_id") or ""),
            "state": "aborted",
            "step_id": str(tx_state.get("step_id") or ""),
            "timestamp": str(tx_state.get("updated_at") or now_iso()),
        },
    )
    return invalid_artifact


def _recover_runtime_transaction(
    *,
    artifacts_root: Path,
    track_id: str,
) -> dict[str, Any] | None:
    tx_state = _load_transaction_state(artifacts_root=artifacts_root, track_id=track_id)
    state_name = str(tx_state.get("state") or "").strip()
    if not tx_state or state_name in {"", *TRANSACTION_TERMINAL_STATES}:
        return None
    tx_state = _refresh_transaction_hashes(dict(tx_state))
    errors = _validate_transaction_staged_files(tx_state)
    if errors:
        _abort_transaction_integrity_failure(
            artifacts_root=artifacts_root,
            track_id=track_id,
            tx_state=tx_state,
            errors=errors,
        )
        return {
            "transaction_id": str(tx_state.get("transaction_id") or ""),
            "transaction_state": "aborted",
            "recovered": False,
            "errors": errors,
        }
    committed = _commit_runtime_transaction(
        artifacts_root=artifacts_root,
        track_id=track_id,
        tx_state=tx_state,
        recovered=True,
    )
    return {
        "transaction_id": str(committed.get("transaction_id") or ""),
        "transaction_state": str(committed.get("state") or ""),
        "recovered": True,
        "errors": [],
    }


def _runtime_capture(
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
) -> dict[str, Any]:
    manifest_path, manifest_hash = write_capture_manifest(
        artifacts_root=artifacts_root,
        track_id=track_id,
        name=name,
        stage=stage,
        cwd=cwd,
        command_argv=command_argv,
        exit_code=exit_code,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
    )
    return {"proof_artifact": str(manifest_path), "proof_hash": manifest_hash}


def _run_argv(argv: list[str], *, cwd: str) -> subprocess.CompletedProcess[str]:
    if not argv:
        raise RuntimeError("empty_command_argv")
    safe, safety_reason = _check_command_safety(argv, False, "")
    if not safe:
        raise RuntimeError(safety_reason)
    env = _safe_env_view([])
    env["PWD"] = cwd
    return subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def _capture_command(
    *,
    artifacts_root: Path,
    track_id: str,
    name: str,
    stage: str,
    command_text: str,
    cwd: str,
) -> dict[str, Any]:
    argv = shlex.split(command_text)
    completed = _run_argv(argv, cwd=cwd)
    proof = _runtime_capture(
        artifacts_root=artifacts_root,
        track_id=track_id,
        name=name,
        stage=stage,
        cwd=cwd,
        command_argv=argv,
        exit_code=int(completed.returncode),
        stdout_text=completed.stdout,
        stderr_text=completed.stderr,
    )
    return {
        "name": name,
        "stage": stage,
        "command": command_text,
        "exit_code": int(completed.returncode),
        **proof,
    }


def _capture_command_sequence(
    *,
    artifacts_root: Path,
    track_id: str,
    name: str,
    stage: str,
    commands: list[str],
    cwd: str,
) -> dict[str, Any]:
    manifest_path = _capture_manifest_path(artifacts_root=artifacts_root, track_id=track_id, name=name)
    if manifest_path.exists():
        return {
            "name": name,
            "stage": stage,
            "exit_code": load_json_file(manifest_path).get("exit_code", 1),
            "proof_artifact": str(manifest_path),
            "proof_hash": sha256_file(manifest_path),
        }
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    exit_code = 0
    for command in commands:
        argv = shlex.split(command)
        completed = _run_argv(argv, cwd=cwd)
        exit_code = max(exit_code, int(completed.returncode))
        stdout_chunks.append(f"$ {' '.join(argv)}\n{completed.stdout.strip()}".strip())
        stderr_chunks.append(f"$ {' '.join(argv)}\n{completed.stderr.strip()}".strip())
    proof = _runtime_capture(
        artifacts_root=artifacts_root,
        track_id=track_id,
        name=name,
        stage=stage,
        cwd=cwd,
        command_argv=["objective_runtime.py", "command-sequence", name],
        exit_code=exit_code,
        stdout_text="\n\n".join(item for item in stdout_chunks if item),
        stderr_text="\n\n".join(item for item in stderr_chunks if item),
    )
    return {"name": name, "stage": stage, "exit_code": exit_code, **proof}


def _capture_manifest_path(*, artifacts_root: Path, track_id: str, name: str) -> Path:
    return artifacts_root / sanitize_token(track_id) / "captures" / sanitize_token(name) / "manifest.json"


def _capture_exists(*, artifacts_root: Path, track_id: str, name: str) -> bool:
    return _capture_manifest_path(artifacts_root=artifacts_root, track_id=track_id, name=name).exists()


def _ensure_capture_command(
    *,
    artifacts_root: Path,
    track_id: str,
    name: str,
    stage: str,
    command_text: str,
    cwd: str,
) -> dict[str, Any]:
    manifest_path = _capture_manifest_path(artifacts_root=artifacts_root, track_id=track_id, name=name)
    if manifest_path.exists():
        return {
            "name": name,
            "stage": stage,
            "command": command_text,
            "exit_code": load_json_file(manifest_path).get("exit_code", 1),
            "proof_artifact": str(manifest_path),
            "proof_hash": sha256_file(manifest_path),
        }
    return _capture_command(
        artifacts_root=artifacts_root,
        track_id=track_id,
        name=name,
        stage=stage,
        command_text=command_text,
        cwd=cwd,
    )


def _sanitize_capture_suffix(value: str) -> str:
    token = "".join(ch if ch.isalnum() else "-" for ch in value.strip().lower())
    return "-".join(part for part in token.split("-") if part) or "capture"


def _simulation_file_writes(packet_result: dict[str, Any]) -> list[dict[str, str]]:
    writes = packet_result.get("file_writes")
    if not isinstance(writes, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in writes:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        normalized.append({"path": path, "content": str(item.get("content", ""))})
    return normalized


def _apply_file_writes(*, cwd: str | None, packet_results: list[dict[str, Any]]) -> None:
    if not cwd:
        return
    root = Path(cwd).resolve()
    for result in packet_results:
        for item in _simulation_file_writes(result):
            target = (root / item["path"]).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError(f"simulation_write_outside_workspace:{target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["content"], encoding="utf-8")


def _git_root(cwd: str | None) -> Path | None:
    if not cwd:
        return None
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    root = completed.stdout.strip()
    return Path(root).resolve() if root else None


def _status_paths(repo_root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=str(repo_root),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return []
    paths: list[str] = []
    for line in completed.stdout.splitlines():
        if len(line) < 4:
            continue
        path_text = line[3:].strip()
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1].strip()
        if path_text:
            paths.append(path_text)
    return paths


def _repo_relative_paths(*, repo_root: Path, cwd: str | None, changed_files: list[str]) -> set[str]:
    repo_paths: set[str] = set()
    workspace_root = Path(cwd).resolve() if cwd else repo_root
    for raw_path in changed_files:
        path_text = str(raw_path).strip()
        if not path_text:
            continue
        candidate = Path(path_text).expanduser()
        if not candidate.is_absolute():
            candidate = (workspace_root / candidate).resolve()
        else:
            candidate = candidate.resolve()
        if repo_root not in candidate.parents and candidate != repo_root:
            continue
        repo_paths.add(candidate.relative_to(repo_root).as_posix())
    return repo_paths


def _runtime_managed_repo_prefix(*, repo_root: Path, artifacts_root: Path, track_id: str) -> str | None:
    resolved_artifacts = (artifacts_root / sanitize_token(track_id)).resolve()
    if repo_root not in resolved_artifacts.parents and resolved_artifacts != repo_root:
        return None
    return resolved_artifacts.relative_to(repo_root).as_posix()


def _rollback_capture_name(checkpoint_id: str) -> str:
    return f"rollback-{_sanitize_capture_suffix(checkpoint_id)}"


def _attempt_git_checkpoint(
    *,
    artifacts_root: Path,
    track_id: str,
    cycle_id: str,
    cwd: str | None,
    changed_files: list[str],
) -> dict[str, Any]:
    attempted_at = now_iso()
    checkpoint_id = f"{track_id}-{_sanitize_capture_suffix(cycle_id)}-checkpoint"
    repo_root = _git_root(cwd)
    rollback_name = _rollback_capture_name(checkpoint_id)
    if repo_root is None:
        proof = _runtime_capture(
            artifacts_root=artifacts_root,
            track_id=track_id,
            name=rollback_name,
            stage="rollback",
            cwd=cwd or os.getcwd(),
            command_argv=["objective_runtime.py", "rollback", "blocked"],
            exit_code=1,
            stdout_text="",
            stderr_text="git repository required for rollback validation",
        )
        return {
            "checkpoint_id": checkpoint_id,
            "checkpoint_strategy": "git_checkpoint_required",
            "checkpoint_attempted_at": attempted_at,
            "checkpoint_commit": "",
            "checkpoint_blocked": True,
            "checkpoint_block_reason": "git_repo_required",
            "checkpoint_block_evidence": "No git repository was available for runtime checkpointing.",
            "rollback_validation_ref": proof["proof_artifact"],
            "rollback_validation": {
                "executed": False,
                "result": "blocked",
                "evidence": "git repository required for rollback validation",
                **proof,
            },
        }

    repo_paths = _repo_relative_paths(repo_root=repo_root, cwd=cwd, changed_files=changed_files)
    if not repo_paths:
        proof = _runtime_capture(
            artifacts_root=artifacts_root,
            track_id=track_id,
            name=rollback_name,
            stage="rollback",
            cwd=str(repo_root),
            command_argv=["objective_runtime.py", "rollback", "blocked"],
            exit_code=1,
            stdout_text="",
            stderr_text="no packet-reported changed files were available for checkpointing",
        )
        return {
            "checkpoint_id": checkpoint_id,
            "checkpoint_strategy": "git_checkpoint_required",
            "checkpoint_attempted_at": attempted_at,
            "checkpoint_commit": "",
            "checkpoint_blocked": True,
            "checkpoint_block_reason": "no_packet_reported_changes",
            "checkpoint_block_evidence": "Accepted packet state changed without packet-reported repo file changes.",
            "rollback_validation_ref": proof["proof_artifact"],
            "rollback_validation": {
                "executed": False,
                "result": "blocked",
                "evidence": "no packet-reported changed files were available for checkpointing",
                **proof,
            },
        }

    dirty_paths = set(_status_paths(repo_root))
    runtime_prefix = _runtime_managed_repo_prefix(repo_root=repo_root, artifacts_root=artifacts_root, track_id=track_id)
    unrelated_dirty = sorted(
        path
        for path in dirty_paths
        if path not in repo_paths
        and not (runtime_prefix and (path == runtime_prefix or path.startswith(f"{runtime_prefix}/")))
    )
    if unrelated_dirty:
        proof = _runtime_capture(
            artifacts_root=artifacts_root,
            track_id=track_id,
            name=rollback_name,
            stage="rollback",
            cwd=str(repo_root),
            command_argv=["objective_runtime.py", "rollback", "blocked"],
            exit_code=1,
            stdout_text="",
            stderr_text=f"unrelated dirty state: {', '.join(unrelated_dirty)}",
        )
        return {
            "checkpoint_id": checkpoint_id,
            "checkpoint_strategy": "git_checkpoint_required",
            "checkpoint_attempted_at": attempted_at,
            "checkpoint_commit": "",
            "checkpoint_blocked": True,
            "checkpoint_block_reason": "unrelated_dirty_state",
            "checkpoint_block_evidence": ", ".join(unrelated_dirty),
            "rollback_validation_ref": proof["proof_artifact"],
            "rollback_validation": {
                "executed": False,
                "result": "blocked",
                "evidence": f"unrelated dirty state: {', '.join(unrelated_dirty)}",
                **proof,
            },
        }

    add_result = _run_argv(["git", "add", "--", *sorted(repo_paths)], cwd=str(repo_root))
    if add_result.returncode != 0:
        proof = _runtime_capture(
            artifacts_root=artifacts_root,
            track_id=track_id,
            name=rollback_name,
            stage="rollback",
            cwd=str(repo_root),
            command_argv=["git", "add", "--", *sorted(repo_paths)],
            exit_code=add_result.returncode,
            stdout_text=add_result.stdout,
            stderr_text=add_result.stderr,
        )
        return {
            "checkpoint_id": checkpoint_id,
            "checkpoint_strategy": "git_checkpoint_required",
            "checkpoint_attempted_at": attempted_at,
            "checkpoint_commit": "",
            "checkpoint_blocked": True,
            "checkpoint_block_reason": "git_add_failed",
            "checkpoint_block_evidence": add_result.stderr.strip() or add_result.stdout.strip(),
            "rollback_validation_ref": proof["proof_artifact"],
            "rollback_validation": {
                "executed": False,
                "result": "blocked",
                "evidence": add_result.stderr.strip() or "git add failed",
                **proof,
            },
        }

    diff_result = _run_argv(["git", "diff", "--cached", "--name-only"], cwd=str(repo_root))
    staged_paths = [line.strip() for line in diff_result.stdout.splitlines() if line.strip()]
    if not staged_paths:
        proof = _runtime_capture(
            artifacts_root=artifacts_root,
            track_id=track_id,
            name=rollback_name,
            stage="rollback",
            cwd=str(repo_root),
            command_argv=["git", "diff", "--cached", "--name-only"],
            exit_code=1,
            stdout_text=diff_result.stdout,
            stderr_text="no staged changes available for runtime checkpointing",
        )
        return {
            "checkpoint_id": checkpoint_id,
            "checkpoint_strategy": "git_checkpoint_required",
            "checkpoint_attempted_at": attempted_at,
            "checkpoint_commit": "",
            "checkpoint_blocked": True,
            "checkpoint_block_reason": "no_staged_changes",
            "checkpoint_block_evidence": "git diff --cached reported no staged changes.",
            "rollback_validation_ref": proof["proof_artifact"],
            "rollback_validation": {
                "executed": False,
                "result": "blocked",
                "evidence": "git diff --cached reported no staged changes.",
                **proof,
            },
        }

    commit_env = os.environ.copy()
    commit_env.setdefault("GIT_AUTHOR_NAME", "Codex Runtime")
    commit_env.setdefault("GIT_AUTHOR_EMAIL", "codex-runtime@example.invalid")
    commit_env.setdefault("GIT_COMMITTER_NAME", commit_env["GIT_AUTHOR_NAME"])
    commit_env.setdefault("GIT_COMMITTER_EMAIL", commit_env["GIT_AUTHOR_EMAIL"])
    commit_message = f"chore(runtime-checkpoint): {track_id} {cycle_id}"
    commit_result = subprocess.run(
        ["git", "commit", "-m", commit_message],
        cwd=str(repo_root),
        text=True,
        capture_output=True,
        env=commit_env,
        check=False,
    )
    if commit_result.returncode != 0:
        proof = _runtime_capture(
            artifacts_root=artifacts_root,
            track_id=track_id,
            name=rollback_name,
            stage="rollback",
            cwd=str(repo_root),
            command_argv=["git", "commit", "-m", commit_message],
            exit_code=commit_result.returncode,
            stdout_text=commit_result.stdout,
            stderr_text=commit_result.stderr,
        )
        return {
            "checkpoint_id": checkpoint_id,
            "checkpoint_strategy": "git_checkpoint_required",
            "checkpoint_attempted_at": attempted_at,
            "checkpoint_commit": "",
            "checkpoint_blocked": True,
            "checkpoint_block_reason": "git_commit_failed",
            "checkpoint_block_evidence": commit_result.stderr.strip() or commit_result.stdout.strip(),
            "rollback_validation_ref": proof["proof_artifact"],
            "rollback_validation": {
                "executed": False,
                "result": "blocked",
                "evidence": commit_result.stderr.strip() or "git commit failed",
                **proof,
            },
        }

    commit_sha = _run_argv(["git", "rev-parse", "HEAD"], cwd=str(repo_root)).stdout.strip()
    patch_result = _run_argv(["git", "show", "--format=", "--binary", commit_sha], cwd=str(repo_root))
    with tempfile.NamedTemporaryFile(prefix="codex-runtime-index-", delete=False) as temp_index:
        temp_index_path = temp_index.name
    rollback_env = os.environ.copy()
    rollback_env["GIT_INDEX_FILE"] = temp_index_path
    read_tree = subprocess.run(
        ["git", "read-tree", commit_sha],
        cwd=str(repo_root),
        text=True,
        capture_output=True,
        env=rollback_env,
        check=False,
    )
    rollback_check = subprocess.run(
        ["git", "apply", "--check", "-R", "--cached"],
        cwd=str(repo_root),
        text=True,
        capture_output=True,
        input=patch_result.stdout,
        env=rollback_env,
        check=False,
    )
    try:
        Path(temp_index_path).unlink(missing_ok=True)
    except Exception:
        pass
    rollback_exit = 0 if read_tree.returncode == 0 and rollback_check.returncode == 0 else 1
    rollback_stdout = "\n".join(
        item for item in [patch_result.stdout.strip(), read_tree.stdout.strip(), rollback_check.stdout.strip()] if item
    )
    rollback_stderr = "\n".join(
        item for item in [patch_result.stderr.strip(), read_tree.stderr.strip(), rollback_check.stderr.strip()] if item
    )
    proof = _runtime_capture(
        artifacts_root=artifacts_root,
        track_id=track_id,
        name=rollback_name,
        stage="rollback",
        cwd=str(repo_root),
        command_argv=["git", "apply", "--check", "-R", "--cached"],
        exit_code=rollback_exit,
        stdout_text=rollback_stdout,
        stderr_text=rollback_stderr,
    )
    return {
        "checkpoint_id": checkpoint_id,
        "checkpoint_strategy": "git_checkpoint_required",
        "checkpoint_attempted_at": attempted_at,
        "checkpoint_commit": commit_sha,
        "checkpoint_blocked": rollback_exit != 0,
        "checkpoint_block_reason": "" if rollback_exit == 0 else "rollback_validation_failed",
        "checkpoint_block_evidence": "" if rollback_exit == 0 else rollback_stderr or "reverse patch validation failed",
        "rollback_validation_ref": proof["proof_artifact"],
        "rollback_validation": {
            "executed": True,
            "result": "pass" if rollback_exit == 0 else "fail",
            "evidence": "reverse patch applied cleanly in a temporary index"
            if rollback_exit == 0
            else (rollback_stderr or "reverse patch validation failed"),
            **proof,
        },
    }


def _packet_map(packet_dag_payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(packet.get("packet_id", "")).strip(): dict(packet)
        for packet in packet_dag_payload.get("packets", [])
        if isinstance(packet, dict) and str(packet.get("packet_id", "")).strip()
    }


def _load_runtime_state(*, artifacts_root: Path, track_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    return (
        load_json_file(paths["packet_dag"]),
        load_json_file(paths["status"]),
        load_json_file(paths["schedule"]),
    )


def _write_runtime_state(
    *,
    artifacts_root: Path,
    track_id: str,
    packet_dag: dict[str, Any],
    status: dict[str, Any],
    schedule: dict[str, Any],
) -> None:
    paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    write_json_file(paths["packet_dag"], packet_dag)
    write_json_file(paths["status"], status)
    write_json_file(paths["schedule"], schedule)


def _write_runtime_supporting_state(
    *,
    artifacts_root: Path,
    track_id: str,
    summary: dict[str, Any] | None = None,
    validation_plan: dict[str, Any] | None = None,
    repo_capabilities: dict[str, Any] | None = None,
    packet_quality: dict[str, Any] | None = None,
    execution_coverage: dict[str, Any] | None = None,
    support_confidence: dict[str, Any] | None = None,
) -> None:
    paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    if summary is not None:
        write_json_file(paths["summary"], summary)
    if validation_plan is not None:
        write_json_file(paths["validation_plan"], validation_plan)
    if repo_capabilities is not None:
        write_json_file(paths["repo_capabilities"], repo_capabilities)
    if packet_quality is not None:
        write_json_file(paths["packet_quality"], packet_quality)
    if execution_coverage is not None:
        write_json_file(paths["execution_coverage"], execution_coverage)
    if support_confidence is not None:
        write_json_file(paths["support_confidence"], support_confidence)


def _default_lifecycle_status(*, stop_allowed: bool, safe_momentum_available: bool, work_remaining: bool) -> str:
    if stop_allowed:
        return "approved"
    if work_remaining:
        return "revise" if safe_momentum_available else "blocked"
    return "running"


def _sync_runtime_state_record(
    *,
    artifacts_root: Path,
    track_id: str,
    lifecycle_status: str | None = None,
    last_verifier_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    summary = _load_json_if_exists(runtime_paths["summary"])
    status = _load_json_if_exists(runtime_paths["status"])
    schedule = _load_json_if_exists(runtime_paths["schedule"])
    support_confidence = _load_json_if_exists(runtime_paths["support_confidence"])
    checkpoint = _load_json_if_exists(session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["checkpoint"])
    existing = _load_json_if_exists(runtime_paths["runtime_state"])
    kernel_state = _load_json_if_exists(runtime_paths["kernel_runtime_state"])

    current_frontier = [str(item).strip() for item in summary.get("current_frontier", []) if str(item).strip()]
    next_packet = str(summary.get("next_recommended_packet") or checkpoint.get("next_recommended_packet") or "").strip()
    if not next_packet and current_frontier:
        next_packet = current_frontier[0]
    required_work_remaining = summary.get("required_work_remaining") is True
    material_optional_work_remaining = summary.get("material_optional_work_remaining") is True
    safe_momentum_available = summary.get("safe_momentum_available") is True
    stop_allowed = summary.get("stop_allowed") is True
    work_remaining = required_work_remaining or material_optional_work_remaining
    persisted_verifier = existing.get("last_verifier_result") if isinstance(existing.get("last_verifier_result"), dict) else {}

    payload = {
        "schema_version": OBJECTIVE_RUNTIME_STATE_SCHEMA_VERSION,
        "objective_id": str(summary.get("objective_id") or status.get("objective_id") or stable_objective_id(track_id)),
        "track_id": track_id,
        "route_hint": str(summary.get("route_hint") or ""),
        "controller_mode": str(summary.get("controller_mode") or CONTROLLER_MODE_ENFORCE),
        "lifecycle_status": lifecycle_status
        or str(existing.get("lifecycle_status") or "").strip()
        or _default_lifecycle_status(
            stop_allowed=stop_allowed,
            safe_momentum_available=safe_momentum_available,
            work_remaining=work_remaining,
        ),
        "closure_state": str(summary.get("closure_state") or status.get("closure_state") or ""),
        "current_cycle_id": str(summary.get("current_cycle_id") or ""),
        "current_frontier": current_frontier,
        "current_packet": str(kernel_state.get("active_unit_id") or next_packet),
        "safe_momentum_available": safe_momentum_available,
        "required_work_remaining": required_work_remaining,
        "required_work_reasons": summary.get("required_work_reasons", []) if isinstance(summary.get("required_work_reasons"), list) else [],
        "material_optional_work_remaining": material_optional_work_remaining,
        "material_optional_work_reasons": summary.get("material_optional_work_reasons", [])
        if isinstance(summary.get("material_optional_work_reasons"), list)
        else [],
        "stop_allowed": stop_allowed,
        "stop_reason": str(summary.get("stop_reason") or ""),
        "next_recommended_packet": next_packet,
        "unsupported_closure_risk": str(summary.get("unsupported_closure_risk") or support_confidence.get("unsupported_closure_risk") or "none"),
        "last_verifier_result": last_verifier_result if isinstance(last_verifier_result, dict) else persisted_verifier,
        "authoritative_artifacts": {
            "packet_dag": str(runtime_paths["packet_dag"]),
            "status": str(runtime_paths["status"]),
            "schedule": str(runtime_paths["schedule"]),
            "execution_plan": str(runtime_paths["execution_plan"]),
            "kernel_runtime_state": str(runtime_paths["kernel_runtime_state"]),
        },
        "compatibility_artifacts": {
            "summary": str(runtime_paths["summary"]),
            "support_confidence": str(runtime_paths["support_confidence"]),
            "operator_view": str(runtime_paths["operator_view"]),
            "transition_history": str(runtime_paths["transition_history"]),
            "verification_results": str(runtime_paths["verification_results"]),
        },
        "derived_support_status": str(support_confidence.get("objective_support_status") or ""),
        "updated_at": now_iso(),
    }
    write_json_file(runtime_paths["runtime_state"], payload)
    return payload


def _friendly_artifact_path(path_text: str, *, track_id: str) -> str:
    path = Path(path_text)
    parts = list(path.parts)
    track_token = sanitize_token(track_id)
    if track_token in parts:
        idx = parts.index(track_token)
        return "/".join(parts[idx:])
    return "/".join(parts[-4:]) if len(parts) >= 4 else path_text


def _load_json_if_exists(path: Path) -> dict[str, Any]:
    managed = resolve_transaction_managed_path(path)
    if not managed.exists():
        return {}
    payload = load_json_file(managed)
    return payload if isinstance(payload, dict) else {}


def _load_runtime_inputs(*, artifacts_root: Path, track_id: str) -> dict[str, Any]:
    runtime_paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    session_paths = session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    transaction_state = _load_json_if_exists(runtime_paths["transaction_state"])
    transaction_log = _load_jsonl(runtime_paths["transaction_log"])
    return {
        "runtime_paths": runtime_paths,
        "runtime_state": _load_json_if_exists(runtime_paths["runtime_state"]),
        "summary": _load_json_if_exists(runtime_paths["summary"]),
        "validation_plan": _load_json_if_exists(runtime_paths["validation_plan"]),
        "repo_capabilities": _load_json_if_exists(runtime_paths["repo_capabilities"]),
        "packet_quality": _load_json_if_exists(runtime_paths["packet_quality"]),
        "execution_coverage": _load_json_if_exists(runtime_paths["execution_coverage"]),
        "support_confidence": _load_json_if_exists(runtime_paths["support_confidence"]),
        "status": _load_json_if_exists(runtime_paths["status"]),
        "schedule": _load_json_if_exists(runtime_paths["schedule"]),
        "packet_dag": _load_json_if_exists(runtime_paths["packet_dag"]),
        "execution_ledger": _load_json_if_exists(runtime_paths["execution_ledger"]),
        "adaptation_log": _load_jsonl(runtime_paths["adaptation_log"]),
        "benchmark": _load_json_if_exists(runtime_paths["benchmark"]),
        "canary": _load_json_if_exists(runtime_paths["canary"]),
        "checkpoint": _load_json_if_exists(session_paths["checkpoint"]),
        "blockers": _load_json_if_exists(session_paths["blockers"]),
        "progress": _load_jsonl(session_paths["progress"]),
        "transaction_state": transaction_state,
        "transaction_log": transaction_log,
    }


def _support_confidence_mode() -> str:
    return str(os.environ.get("CODEX_SUPPORT_CONFIDENCE_MODE") or "enforce").strip().lower() or "enforce"


def _operator_packet_counts(*, packets: dict[str, dict[str, Any]], status: dict[str, Any]) -> dict[str, int]:
    runtime_states = [str(packet.get("runtime_state") or "").strip() for packet in packets.values()]
    counts = {
        "accepted": sum(1 for state in runtime_states if state == "accepted"),
        "queued": sum(1 for state in runtime_states if state == "queued"),
        "escalated": sum(1 for state in runtime_states if state == "escalated"),
        "cancelled": sum(1 for state in runtime_states if state == "cancelled"),
    }
    counts["blocked"] = len([item for item in status.get("blocked_packets", []) if str(item).strip()])
    counts["total"] = len(packets)
    return counts


def _build_execution_coverage_report(
    *,
    packets: dict[str, dict[str, Any]],
    execution_ledger: dict[str, Any],
    route_hint: str,
) -> dict[str, Any]:
    packet_rows = execution_ledger.get("packets") if isinstance(execution_ledger.get("packets"), list) else []
    packet_items = [
        packet
        for packet in packets.values()
        if isinstance(packet, dict) and str(packet.get("packet_id") or "").strip()
    ]
    packet_count = len(packet_items)
    review_packets = [
        packet
        for packet in packet_items
        if str(packet.get("packet_class") or packet.get("classification") or "").strip() == "review"
    ]
    deterministic_packet_count = sum(
        1 for packet in packet_items if str(packet.get("execution_strategy") or "").strip() != "codex_prompt_worker"
    )
    non_review_packets = [packet for packet in packet_items if packet not in review_packets]
    deterministic_non_review_count = sum(
        1 for packet in non_review_packets if str(packet.get("execution_strategy") or "").strip() != "codex_prompt_worker"
    )
    fallback_packet_ids = sorted(
        {
            str(item.get("packet_id") or "").strip()
            for item in packet_rows
            if isinstance(item, dict) and item.get("fallback_used") is True and str(item.get("packet_id") or "").strip()
        }
    )
    fallback_reasons = {
        packet_id: str(item.get("fallback_reason") or "").strip()
        for item in packet_rows
        if isinstance(item, dict)
        for packet_id in [str(item.get("packet_id") or "").strip()]
        if packet_id and item.get("fallback_used") is True
    }
    deterministic_ratio = (deterministic_packet_count / packet_count) if packet_count else 1.0
    non_review_ratio = (deterministic_non_review_count / len(non_review_packets)) if non_review_packets else 1.0
    thresholds = {
        "R3": {"deterministic_ratio_min": 0.9, "non_review_deterministic_ratio_min": 0.9},
        "R4": {"deterministic_ratio_min": 0.85, "non_review_deterministic_ratio_min": 0.95},
    }.get(route_hint, {"deterministic_ratio_min": 0.0, "non_review_deterministic_ratio_min": 0.0})
    status = "pass"
    if deterministic_ratio < thresholds["deterministic_ratio_min"] or non_review_ratio < thresholds["non_review_deterministic_ratio_min"]:
        status = "hard_fail"
    return {
        "schema_version": OBJECTIVE_RUNTIME_EXECUTION_COVERAGE_SCHEMA_VERSION,
        "route_hint": route_hint,
        "packet_count": packet_count,
        "deterministic_packet_count": deterministic_packet_count,
        "fallback_packet_count": len(fallback_packet_ids),
        "review_packet_count": len(review_packets),
        "deterministic_ratio": round(deterministic_ratio, 4),
        "non_review_deterministic_ratio": round(non_review_ratio, 4),
        "fallback_packet_ids": fallback_packet_ids,
        "fallback_reasons": fallback_reasons,
        "thresholds": thresholds,
        "status": status,
    }


def _operator_validation_coverage(
    *,
    validation_plan: dict[str, Any],
    packets: dict[str, dict[str, Any]],
    track_id: str,
    runtime_paths: dict[str, Path],
) -> tuple[list[dict[str, Any]], bool]:
    coverage: list[dict[str, Any]] = []
    validation_gap_present = False
    for lane in validation_plan.get("lanes", []):
        if not isinstance(lane, dict):
            continue
        lane_name = str(lane.get("lane") or "").strip()
        generated_packet_ids = [
            str(packet_id).strip()
            for packet_id in lane.get("generated_packet_ids", [])
            if str(packet_id).strip()
        ]
        accepted_packet_ids = [
            packet_id
            for packet_id in generated_packet_ids
            if str(packets.get(packet_id, {}).get("runtime_state") or "").strip() == "accepted"
        ]
        manual_only_blocker = str(lane.get("manual_only_blocker") or "").strip()
        missing_capability_reason = str(lane.get("missing_capability_reason") or "").strip()
        capability_confidence = str(lane.get("capability_confidence") or "none").strip() or "none"
        if lane.get("required") is True:
            if manual_only_blocker or (not generated_packet_ids and missing_capability_reason) or not generated_packet_ids:
                validation_gap_present = True
        if manual_only_blocker:
            status = "manual_blocked"
        elif lane.get("required") is not True:
            status = "optional"
        elif not generated_packet_ids and missing_capability_reason and capability_confidence == "low":
            status = "low_confidence_blocked"
        elif not generated_packet_ids and missing_capability_reason:
            status = "missing_capability"
        elif not generated_packet_ids:
            status = "generated_missing"
        elif len(accepted_packet_ids) == len(generated_packet_ids):
            status = "satisfied"
        elif accepted_packet_ids:
            status = "in_progress"
        else:
            status = "ready"
        coverage.append(
            {
                "lane": lane_name,
                "required": lane.get("required") is True,
                "status": status,
                "reasons": [str(item).strip() for item in lane.get("reasons", []) if str(item).strip()],
                "paths": [str(item).strip() for item in lane.get("paths", []) if str(item).strip()],
                "generated_packet_ids": generated_packet_ids,
                "accepted_packet_ids": accepted_packet_ids,
                "manual_only_blocker": manual_only_blocker,
                "missing_capability_reason": missing_capability_reason,
                "capability_confidence": capability_confidence,
                "artifact_ref": {
                    "source": str(runtime_paths["validation_plan"]),
                    "label": _friendly_artifact_path(str(runtime_paths["validation_plan"]), track_id=track_id),
                },
            }
        )
    return coverage, validation_gap_present


def _load_packet_verdict_payload(*, artifacts_root: Path, track_id: str, packet_id: str) -> dict[str, Any]:
    path = packet_verdict_path(artifacts_root=artifacts_root, track_id=track_id, packet_id=packet_id)
    return _load_json_if_exists(path)


def _build_support_confidence(
    *,
    artifacts_root: Path,
    track_id: str,
    packets: dict[str, dict[str, Any]],
    status: dict[str, Any],
    schedule: dict[str, Any],
    validation_plan: dict[str, Any],
    checkpoint: dict[str, Any],
    execution_coverage: dict[str, Any],
) -> dict[str, Any]:
    def _packet_blocked_by_boundary(packet_id: str, boundary_remainder: set[str], visiting: set[str] | None = None) -> bool:
        if packet_id in boundary_remainder:
            return True
        packet = packets.get(packet_id)
        if not isinstance(packet, dict):
            return False
        deps = [str(dep).strip() for dep in packet.get("dependencies", []) if str(dep).strip()]
        if not deps:
            return False
        seen = set(visiting or set())
        if packet_id in seen:
            return False
        seen.add(packet_id)
        return any(_packet_blocked_by_boundary(dep, boundary_remainder, seen) for dep in deps)

    validation_coverage, validation_gap_present = _operator_validation_coverage(
        validation_plan=validation_plan,
        packets=packets,
        track_id=track_id,
        runtime_paths=runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id),
    )
    packet_support: list[dict[str, Any]] = []
    support_gap_reasons: list[str] = []
    fallback_support_packet_ids: list[str] = []
    for packet_id in sorted(packets):
        packet = packets[packet_id]
        runtime_state = str(packet.get("runtime_state") or "").strip()
        verdict = _load_packet_verdict_payload(artifacts_root=artifacts_root, track_id=track_id, packet_id=packet_id)
        support_status = "pending"
        risk_reason = ""
        if runtime_state == "accepted":
            support_status = str(verdict.get("support_status") or "").strip() or "unsupported"
            risk_reason = str(verdict.get("unsupported_risk_reason") or "").strip()
            if support_status != "supported":
                risk_reason = risk_reason or "packet_claim_ahead_of_evidence"
                support_gap_reasons.append(risk_reason)
                if str(packet.get("execution_strategy") or "").strip() == "codex_prompt_worker":
                    fallback_support_packet_ids.append(packet_id)
        elif runtime_state == "escalated":
            support_status = "blocked"
        packet_support.append(
            {
                "packet_id": packet_id,
                "runtime_state": runtime_state,
                "strategy_name": str(packet.get("execution_strategy") or "").strip(),
                "support_status": support_status,
                "unsupported_risk_reason": risk_reason,
                "artifact_path": str(packet_verdict_path(artifacts_root=artifacts_root, track_id=track_id, packet_id=packet_id)),
            }
        )
    closure_state = str(status.get("closure_state") or "").strip()
    boundary_remainder = {
        str(packet_id).strip()
        for packet_id in status.get("boundary_shrunk_remainder", [])
        if str(packet_id).strip()
    }
    validation_plan_lanes = {
        str(lane.get("lane") or "").strip(): lane
        for lane in validation_plan.get("lanes", [])
        if isinstance(lane, dict) and str(lane.get("lane") or "").strip()
    }
    raw_required_lane_gaps = [
        lane for lane in validation_coverage if lane.get("required") and str(lane.get("status") or "").strip() != "satisfied"
    ]
    required_lane_gaps: list[dict[str, Any]] = []
    for lane in raw_required_lane_gaps:
        lane_name = str(lane.get("lane") or "").strip()
        lane_plan = validation_plan_lanes.get(lane_name, {})
        generated_packet_ids = [
            str(packet_id).strip()
            for packet_id in lane_plan.get("generated_packet_ids", [])
            if str(packet_id).strip()
        ]
        if (
            closure_state == "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK"
            and generated_packet_ids
            and boundary_remainder
            and all(_packet_blocked_by_boundary(packet_id, boundary_remainder) for packet_id in generated_packet_ids)
        ):
            continue
        required_lane_gaps.append(lane)
    if required_lane_gaps:
        support_gap_reasons.append("validation_claim_ahead_of_lane_coverage")
    if closure_state == "OBJECTIVE_COMPLETE" and (
        checkpoint.get("checkpoint_blocked") is True or not str(checkpoint.get("rollback_validation_ref") or "").strip()
    ):
        support_gap_reasons.append("checkpoint_claim_ahead_of_reproducibility")
    if fallback_support_packet_ids:
        support_gap_reasons.append("fallback_claim_ahead_of_support")
    unsupported_closure_risk = "none"
    if any(reason in support_gap_reasons for reason in ("validation_claim_ahead_of_lane_coverage", "checkpoint_claim_ahead_of_reproducibility", "fallback_claim_ahead_of_support")):
        unsupported_closure_risk = "objective_claim_ahead_of_external_support"
    elif support_gap_reasons:
        unsupported_closure_risk = support_gap_reasons[0]
    remediation_available = bool(schedule.get("safe_momentum_available")) and (
        bool(schedule.get("current_frontier"))
        or bool(required_lane_gaps)
        or any(str(packet.get("runtime_state") or "").strip() == "queued" for packet in packets.values())
    )
    support_backed_closure = closure_state in {"OBJECTIVE_COMPLETE", "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK"} and unsupported_closure_risk == "none"
    final_gate_recommendation = (
        "allow_closure"
        if unsupported_closure_risk == "none"
        else "continue_with_remediation"
        if remediation_available
        else "block_closure"
    )
    return {
        "schema_version": OBJECTIVE_RUNTIME_SUPPORT_CONFIDENCE_SCHEMA_VERSION,
        "objective_id": str(status.get("objective_id") or stable_objective_id(track_id)),
        "track_id": track_id,
        "mode": _support_confidence_mode(),
        "packet_support": packet_support,
        "objective_support_status": "supported" if unsupported_closure_risk == "none" else "unsupported",
        "unsupported_closure_risk": unsupported_closure_risk,
        "support_gap_reasons": sorted(set(support_gap_reasons)),
        "support_remediation_available": remediation_available,
        "support_backed_closure": support_backed_closure,
        "external_support_coverage": {
            "validation_gap_present": validation_gap_present,
            "required_lane_gap_count": len(required_lane_gaps),
            "checkpoint_ready": checkpoint.get("checkpoint_blocked") is False and bool(str(checkpoint.get("rollback_validation_ref") or "").strip()),
            "fallback_support_packet_ids": sorted(set(fallback_support_packet_ids)),
            "deterministic_coverage_status": str(execution_coverage.get("status") or "").strip(),
        },
        "final_gate_recommendation": final_gate_recommendation,
    }


def _operator_blocker_message(*, reason: str, packet_id: str = "", lane: str = "", checkpoint_reason: str = "") -> str:
    if checkpoint_reason:
        if checkpoint_reason == "unrelated_dirty_state":
            return "Checkpointing is blocked by unrelated dirty repo state."
        if checkpoint_reason == "rollback_validation_failed":
            return "Checkpoint rollback validation failed."
        if checkpoint_reason == "git_repo_required":
            return "Checkpointing requires a git repository."
        return f"Checkpoint is unhealthy: {checkpoint_reason}."
    if lane:
        return f"Validation lane {lane} is blocked: {reason}."
    if reason == "runtime_budget_exhausted":
        return "Runtime momentum budget is exhausted."
    if reason.startswith("external_evidence:"):
        packet_id = reason.split(":", 1)[1].strip()
        return f"Packet {packet_id} is blocked on external evidence."
    if reason.startswith("escalated:"):
        packet_id = reason.split(":", 1)[1].strip()
        return f"Packet {packet_id} is escalated and requires operator attention."
    if reason == "blocked_by_frontier_classification":
        return f"Packet {packet_id} is blocked by frontier classification."
    return reason.replace("_", " ").strip().capitalize() + "."


def _operator_blockers(
    *,
    summary: dict[str, Any],
    checkpoint: dict[str, Any],
    blockers_payload: dict[str, Any],
    validation_coverage: list[dict[str, Any]],
    support_confidence: dict[str, Any],
    track_id: str,
    runtime_paths: dict[str, Path],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for reason in summary.get("blocked_reasons", []):
        reason_text = str(reason).strip()
        if not reason_text:
            continue
        packet_id = reason_text.split(":", 1)[1].strip() if ":" in reason_text else ""
        items.append(
            {
                "kind": "runtime_blocker",
                "message": _operator_blocker_message(reason=reason_text, packet_id=packet_id),
                "packet_id": packet_id,
                "source_artifact": str(runtime_paths["summary"]),
                "source_label": _friendly_artifact_path(str(runtime_paths["summary"]), track_id=track_id),
                "cycle_id": str(summary.get("current_cycle_id") or "").strip(),
            }
        )
    for item in blockers_payload.get("items", []):
        if not isinstance(item, dict):
            continue
        packet_id = str(item.get("packet_id") or "").strip()
        reason = str(item.get("reason") or "").strip()
        items.append(
            {
                "kind": "packet_blocker",
                "message": _operator_blocker_message(reason=reason, packet_id=packet_id),
                "packet_id": packet_id,
                "authority_sensitive": item.get("authority_sensitive") is True,
                "source_artifact": str(session_artifact_paths(artifacts_root=runtime_paths["summary"].parent.parent, track_id=track_id)["blockers"]),
                "source_label": _friendly_artifact_path(
                    str(session_artifact_paths(artifacts_root=runtime_paths["summary"].parent.parent, track_id=track_id)["blockers"]),
                    track_id=track_id,
                ),
                "cycle_id": str(summary.get("current_cycle_id") or "").strip(),
            }
        )
    for lane in validation_coverage:
        if lane["status"] not in {"manual_blocked", "generated_missing", "missing_capability", "low_confidence_blocked"}:
            continue
        items.append(
            {
                "kind": "validation_gap",
                "message": _operator_blocker_message(
                    reason=lane["manual_only_blocker"] or lane.get("missing_capability_reason") or "required lane has no generated runnable packet",
                    lane=lane["lane"],
                ),
                "lane": lane["lane"],
                "packet_id": "",
                "confidence": lane.get("capability_confidence", "none"),
                "source_artifact": lane["artifact_ref"]["source"],
                "source_label": lane["artifact_ref"]["label"],
                "cycle_id": str(summary.get("current_cycle_id") or "").strip(),
            }
        )
    if checkpoint.get("checkpoint_blocked") is True:
        items.append(
            {
                "kind": "checkpoint",
                "message": _operator_blocker_message(
                    reason="",
                    checkpoint_reason=str(checkpoint.get("checkpoint_block_reason") or "").strip(),
                ),
                "packet_id": "",
                "source_artifact": str(session_artifact_paths(artifacts_root=runtime_paths["summary"].parent.parent, track_id=track_id)["checkpoint"]),
                "source_label": _friendly_artifact_path(
                    str(session_artifact_paths(artifacts_root=runtime_paths["summary"].parent.parent, track_id=track_id)["checkpoint"]),
                    track_id=track_id,
                ),
                "proof_artifact": str(checkpoint.get("rollback_validation_ref") or ""),
                "cycle_id": str(summary.get("current_cycle_id") or "").strip(),
            }
        )
    support_risk = str(support_confidence.get("unsupported_closure_risk") or "none").strip()
    if support_risk not in {"", "none"}:
        items.append(
            {
                "kind": "support_confidence",
                "message": f"Unsupported closure claim: {support_risk.replace('_', ' ')}.",
                "packet_id": "",
                "source_artifact": str(runtime_paths["support_confidence"]),
                "source_label": _friendly_artifact_path(str(runtime_paths["support_confidence"]), track_id=track_id),
                "cycle_id": str(summary.get("current_cycle_id") or "").strip(),
            }
        )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        key = (str(item.get("kind") or ""), str(item.get("message") or ""), str(item.get("packet_id") or item.get("lane") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _operator_health_signals(
    *,
    summary: dict[str, Any],
    schedule: dict[str, Any],
    validation_gap_present: bool,
    checkpoint: dict[str, Any],
    execution_ledger: dict[str, Any],
    execution_coverage: dict[str, Any],
    packet_quality: dict[str, Any],
    support_confidence: dict[str, Any],
) -> dict[str, Any]:
    ledger_packets = execution_ledger.get("packets") if isinstance(execution_ledger.get("packets"), list) else []
    total_packets = len(ledger_packets)
    ledger_fallback_count = sum(1 for item in ledger_packets if isinstance(item, dict) and item.get("fallback_used") is True)
    compiled_fallback_count = len([item for item in summary.get("fallback_packets", []) if str(item).strip()])
    fallback_count = max(ledger_fallback_count, compiled_fallback_count)
    fallback_ratio = (fallback_count / total_packets) if total_packets else 0.0
    packet_quality_budget = packet_quality.get("budget") if isinstance(packet_quality.get("budget"), dict) else {}
    fallback_warning_threshold = float(packet_quality_budget.get("warning_threshold", 0.25) or 0.25)
    fallback_burden_high = (
        fallback_ratio >= fallback_warning_threshold
        or str(packet_quality_budget.get("status") or "").strip() in {"warning", "hard_fail"}
        or str(execution_coverage.get("status") or "").strip() == "hard_fail"
    )
    stagnating = int(schedule.get("no_frontier_movement_cycle_count", 0) or 0) >= int(schedule.get("max_no_frontier_movement_cycles", 2) or 2)
    closure_state = str(summary.get("closure_state") or "").strip()
    closure_ready = closure_state in {"OBJECTIVE_COMPLETE", "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK"}
    checkpoint_unhealthy = checkpoint.get("checkpoint_blocked") is True or (
        closure_state == "OBJECTIVE_COMPLETE" and not str(checkpoint.get("rollback_validation_ref") or "").strip()
    )
    closure_claim_is_strong = bool(
        closure_ready
        and not validation_gap_present
        and not checkpoint_unhealthy
        and not fallback_burden_high
        and not stagnating
        and support_confidence.get("support_backed_closure") is True
    )
    closure_ready_but_weak = bool(closure_ready and not closure_claim_is_strong)
    human_attention_required = bool(
        validation_gap_present
        or checkpoint_unhealthy
        or fallback_burden_high
        or stagnating
        or (
            str(support_confidence.get("unsupported_closure_risk") or "none").strip() not in {"", "none"}
            and support_confidence.get("support_remediation_available") is not True
        )
        or (not summary.get("safe_momentum_available") and not closure_ready)
    )
    return {
        "safe_momentum_available": summary.get("safe_momentum_available") is True,
        "fallback_burden_high": fallback_burden_high,
        "validation_gap_present": validation_gap_present,
        "checkpoint_unhealthy": checkpoint_unhealthy,
        "stagnating": stagnating,
        "closure_ready": closure_ready,
        "closure_claim_is_strong": closure_claim_is_strong,
        "closure_ready_but_weak": closure_ready_but_weak,
        "human_attention_required": human_attention_required,
        "fallback_ratio": fallback_ratio,
        "fallback_count": fallback_count,
        "fallback_warning_threshold": fallback_warning_threshold,
        "support_backed_closure": support_confidence.get("support_backed_closure") is True,
    }


def _recommended_packet(
    *,
    summary: dict[str, Any],
    schedule: dict[str, Any],
    checkpoint: dict[str, Any],
) -> str:
    checkpoint_packet = str(checkpoint.get("next_recommended_packet") or "").strip()
    if checkpoint_packet:
        return checkpoint_packet
    frontier = summary.get("current_frontier")
    if not isinstance(frontier, list):
        frontier = schedule.get("current_frontier") if isinstance(schedule.get("current_frontier"), list) else []
    frontier_packet = next((str(item).strip() for item in frontier if str(item).strip()), "")
    if frontier_packet:
        return frontier_packet
    return str(summary.get("next_action") or "").strip()


def _build_stop_state(
    *,
    summary: dict[str, Any],
    schedule: dict[str, Any],
    checkpoint: dict[str, Any],
    health: dict[str, Any],
    blockers: list[dict[str, Any]],
    support_confidence: dict[str, Any],
) -> dict[str, Any]:
    closure_state = str(summary.get("closure_state") or "").strip()
    complete_closure = closure_state in {"OBJECTIVE_COMPLETE", "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK"}
    next_packet = _recommended_packet(summary=summary, schedule=schedule, checkpoint=checkpoint)
    support_risk = str(support_confidence.get("unsupported_closure_risk") or "none").strip()
    required_reasons: list[str] = []
    material_optional_reasons: list[str] = []

    if health.get("validation_gap_present"):
        required_reasons.append("required_validation_pending")
    if health.get("checkpoint_unhealthy"):
        required_reasons.append("checkpoint_unhealthy")
    if support_risk not in {"", "none"}:
        required_reasons.append(f"unsupported_closure_risk:{support_risk}")
    if closure_state == "OBJECTIVE_REJECTED_FALSE_COMPLETION":
        required_reasons.append("false_completion_rejected")
    if closure_state == "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED":
        required_reasons.append("blocked_without_safe_momentum")
    if closure_state == "OBJECTIVE_BLOCKED_MIGRATION_DEFECT":
        required_reasons.append("migration_defect_blocked")
    if not complete_closure and (health.get("safe_momentum_available") or next_packet):
        required_reasons.append("required_packets_pending")
    elif not complete_closure and not required_reasons:
        required_reasons.append("closure_not_reached")
    if blockers and not health.get("safe_momentum_available") and "blocked_without_safe_momentum" not in required_reasons:
        required_reasons.append("policy_blocker_open")
    if health.get("stagnating") and not complete_closure:
        required_reasons.append("stagnation_recovery_required")

    required_reasons = list(dict.fromkeys(reason for reason in required_reasons if reason))
    required_work_remaining = bool(required_reasons)

    if (
        not required_work_remaining
        and health.get("safe_momentum_available")
        and next_packet
        and not health.get("stagnating")
        and support_risk in {"", "none"}
    ):
        material_optional_reasons.append("policy_backed_finishing_packet_available")

    material_optional_reasons = list(dict.fromkeys(reason for reason in material_optional_reasons if reason))
    material_optional_work_remaining = bool(material_optional_reasons)
    stop_allowed = bool(
        complete_closure
        and not required_work_remaining
        and not material_optional_work_remaining
        and support_risk in {"", "none"}
        and health.get("support_backed_closure")
    )

    if required_work_remaining:
        stop_reason = required_reasons[0]
    elif material_optional_work_remaining:
        stop_reason = material_optional_reasons[0]
    elif stop_allowed:
        stop_reason = "all_policy_backed_work_satisfied"
    else:
        stop_reason = "closure_not_ready"

    return {
        "required_work_remaining": required_work_remaining,
        "required_work_reasons": required_reasons,
        "material_optional_work_remaining": material_optional_work_remaining,
        "material_optional_work_reasons": material_optional_reasons,
        "stop_allowed": stop_allowed,
        "stop_reason": stop_reason,
        "next_recommended_packet": next_packet,
        "unsupported_closure_risk": support_risk or "none",
    }


def _operator_explanations(
    *,
    summary: dict[str, Any],
    schedule: dict[str, Any],
    health: dict[str, Any],
    validation_coverage: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
    packets: dict[str, dict[str, Any]],
    execution_ledger: dict[str, Any],
    support_confidence: dict[str, Any],
) -> dict[str, Any]:
    frontier = [str(item).strip() for item in summary.get("current_frontier", []) if str(item).strip()]
    next_packet_id = frontier[0] if frontier else ""
    next_packet = packets.get(next_packet_id, {}) if next_packet_id else {}
    lane_for_packet = ""
    for lane in validation_coverage:
        if next_packet_id and next_packet_id in lane.get("generated_packet_ids", []):
            lane_for_packet = lane["lane"]
            break
    why_this_is_next = (
        f"Packet {next_packet_id} is next because it is in the runnable frontier, uses {str(next_packet.get('execution_strategy') or 'unknown')}, "
        f"and no unresolved dependency or conflict excludes it."
    ) if next_packet_id else "No packet is next because the runnable frontier is empty."
    if lane_for_packet:
        why_this_is_next += f" It advances required validation lane {lane_for_packet}."
    support_risk = str(support_confidence.get("unsupported_closure_risk") or "none").strip()
    if support_risk not in {"", "none"} and support_confidence.get("support_remediation_available") is not True:
        why_blocked = f"Closure is unsupported because {support_risk.replace('_', ' ')}."
    elif blockers and not health["safe_momentum_available"]:
        why_blocked = blockers[0]["message"]
    elif not health["safe_momentum_available"]:
        why_blocked = "Safe momentum is absent and no runnable frontier remains."
    else:
        why_blocked = "Runtime is not blocked."
    closure_confidence = {
        "level": "strong" if health["closure_claim_is_strong"] else "weak" if health["closure_ready_but_weak"] else "not_ready",
        "message": (
            "Closure is strong: validation coverage, checkpoint health, and fallback burden are all acceptable."
            if health["closure_claim_is_strong"]
            else "Closure is technically reached but weakened by support, checkpoint, fallback, or validation confidence signals."
            if health["closure_ready_but_weak"]
            else "Closure is not ready yet."
        ),
    }
    fallback_count = health["fallback_count"]
    fallback_analysis = {
        "message": (
            "No fallback packets are in the objective."
            if fallback_count == 0
            else f"{fallback_count} packet(s) rely on codex prompt fallback; ratio={health['fallback_ratio']:.2f}."
        ),
        "acceptable": not health["fallback_burden_high"],
    }
    cycle_log = schedule.get("cycle_log") if isinstance(schedule.get("cycle_log"), list) else []
    if cycle_log and isinstance(cycle_log[-1], dict):
        frontier_change_explanation = (
            f"Last cycle {str(cycle_log[-1].get('cycle_id') or '').strip()} "
            f"{'moved' if cycle_log[-1].get('frontier_movement') is True else 'did not move'} the frontier"
            f" because {str(cycle_log[-1].get('frontier_movement_reason') or 'no reason recorded').strip()}."
        )
    else:
        frontier_change_explanation = "No cycle history exists yet."
    return {
        "why_this_is_next": why_this_is_next,
        "why_blocked": why_blocked,
        "closure_confidence": closure_confidence,
        "fallback_analysis": fallback_analysis,
        "frontier_change_explanation": frontier_change_explanation,
    }


def _operator_timeline(
    *,
    schedule: dict[str, Any],
    progress: list[dict[str, Any]],
    artifacts_root: Path,
    track_id: str,
) -> list[dict[str, Any]]:
    applied_by_cycle = {
        str(item.get("cycle_id") or "").strip(): item
        for item in progress
        if isinstance(item, dict) and item.get("event_type") == "cycle_applied" and str(item.get("cycle_id") or "").strip()
    }
    timeline: list[dict[str, Any]] = []
    prior_blocked: set[str] = set()
    for item in schedule.get("cycle_log", []):
        if not isinstance(item, dict):
            continue
        cycle_id = str(item.get("cycle_id") or "").strip()
        cycle_paths = cycle_artifact_paths(artifacts_root=artifacts_root, track_id=track_id, cycle_id=cycle_id)
        review_payload = _load_json_if_exists(cycle_paths["review"])
        applied = applied_by_cycle.get(cycle_id, {})
        blocked_reasons = {
            str(reason).strip()
            for reason in applied.get("blocked_reasons", [])
            if str(reason).strip()
        }
        timeline.append(
            {
                "cycle_id": cycle_id,
                "packet_ids": [str(packet_id).strip() for packet_id in item.get("packet_ids", []) if str(packet_id).strip()],
                "verifier_outcome": {
                    "accepted": len(
                        [
                            verdict
                            for verdict in review_payload.get("packet_verdicts", [])
                            if isinstance(verdict, dict) and str(verdict.get("verifier_output") or "").strip() == "accepted"
                        ]
                    ),
                    "rejected_rework": len(
                        [
                            verdict
                            for verdict in review_payload.get("packet_verdicts", [])
                            if isinstance(verdict, dict) and str(verdict.get("verifier_output") or "").strip() == "rejected_rework"
                        ]
                    ),
                    "blocked_boundary": len(
                        [
                            verdict
                            for verdict in review_payload.get("packet_verdicts", [])
                            if isinstance(verdict, dict) and str(verdict.get("verifier_output") or "").strip() == "blocked_boundary"
                        ]
                    ),
                },
                "frontier_movement": item.get("frontier_movement") is True,
                "frontier_movement_reason": str(item.get("frontier_movement_reason") or "").strip(),
                "checkpoint_outcome": {
                    "blocked": applied.get("checkpoint_blocked") is True,
                    "reason": str(applied.get("checkpoint_block_reason") or "").strip(),
                },
                "closure_state": str(applied.get("closure_state") or "").strip(),
                "blockers_introduced": sorted(blocked_reasons - prior_blocked),
                "blockers_cleared": sorted(prior_blocked - blocked_reasons),
                "artifacts": {
                    "review": str(cycle_paths["review"]),
                    "result": str(cycle_paths["result"]),
                    "state": str(cycle_paths["state"]),
                },
            }
        )
        prior_blocked = blocked_reasons
    return timeline


def _operator_repo_capabilities_summary(repo_capabilities: dict[str, Any], *, runtime_paths: dict[str, Path]) -> dict[str, Any]:
    capabilities = repo_capabilities.get("capabilities") if isinstance(repo_capabilities.get("capabilities"), dict) else {}
    enabled = sorted(key for key, value in capabilities.items() if value is True)
    confidence_by_lane = repo_capabilities.get("confidence_by_lane", {}) if isinstance(repo_capabilities.get("confidence_by_lane"), dict) else {}
    return {
        "enabled_lanes": enabled,
        "detectors_run": repo_capabilities.get("detectors_run", []) if isinstance(repo_capabilities.get("detectors_run"), list) else [],
        "confidence_by_lane": confidence_by_lane,
        "low_confidence_lanes": sorted(
            key for key, value in confidence_by_lane.items() if str(value or "").strip() == "low"
        ),
        "missing_capabilities": repo_capabilities.get("missing_capabilities", {}) if isinstance(repo_capabilities.get("missing_capabilities"), dict) else {},
        "source_refs": repo_capabilities.get("source_refs", {}) if isinstance(repo_capabilities.get("source_refs"), dict) else {},
        "source_artifact": str(runtime_paths["repo_capabilities"]),
    }


def _operator_packet_quality_summary(packet_quality: dict[str, Any], *, runtime_paths: dict[str, Path]) -> dict[str, Any]:
    rows = packet_quality.get("rows") if isinstance(packet_quality.get("rows"), list) else []
    hard_fail_rows = [row for row in rows if isinstance(row, dict) and row.get("hard_fail_checks")]
    warning_rows = [row for row in rows if isinstance(row, dict) and row.get("warning_checks")]
    return {
        "packet_count": len(rows),
        "hard_fail_packet_ids": [str(row.get("packet_id") or "").strip() for row in hard_fail_rows if str(row.get("packet_id") or "").strip()],
        "warning_packet_ids": [str(row.get("packet_id") or "").strip() for row in warning_rows if str(row.get("packet_id") or "").strip()],
        "budget": packet_quality.get("budget", {}) if isinstance(packet_quality.get("budget"), dict) else {},
        "source_artifact": str(runtime_paths["packet_quality"]),
    }


def _operator_adaptation_summary(adaptation_log: list[dict[str, Any]], *, runtime_paths: dict[str, Path]) -> dict[str, Any]:
    events = [item for item in adaptation_log if isinstance(item, dict)]
    return {
        "event_count": len(events),
        "latest_event": events[-1] if events else {},
        "events": events[-5:],
        "source_artifact": str(runtime_paths["adaptation_log"]),
    }


def _operator_benchmark_summary(benchmark: dict[str, Any], *, runtime_paths: dict[str, Path]) -> dict[str, Any]:
    if not benchmark:
        return {}
    return {
        "archetype": str(benchmark.get("archetype") or "").strip(),
        "baseline_mode": str(benchmark.get("baseline_mode") or "").strip(),
        "recommended_mode": str(benchmark.get("recommended_mode") or "").strip(),
        "swarm_outperformed_serial": benchmark.get("swarm_outperformed_serial") is True,
        "serial_better": benchmark.get("serial_better") is True,
        "reason": str(benchmark.get("reason") or "").strip(),
        "runs": benchmark.get("runs", []) if isinstance(benchmark.get("runs"), list) else [],
        "source_artifact": str(runtime_paths["benchmark"]),
    }


def _operator_canary_summary(canary: dict[str, Any], *, runtime_paths: dict[str, Path]) -> dict[str, Any]:
    if not canary:
        return {}
    return {
        "route_hint": str(canary.get("route_hint") or "").strip(),
        "execution_shape": str(canary.get("execution_shape") or "").strip(),
        "isolation_mode": str(canary.get("isolation_mode") or "").strip(),
        "safety_mode": str(canary.get("safety_mode") or "").strip(),
        "safe_to_run": canary.get("safe_to_run") is True,
        "refused": canary.get("refused") is True,
        "refusal_reason": str(canary.get("refusal_reason") or "").strip(),
        "workspace_root": str(canary.get("workspace_root") or "").strip(),
        "isolated_workspace_root": str(canary.get("isolated_workspace_root") or "").strip(),
        "metrics": canary.get("metrics", {}) if isinstance(canary.get("metrics"), dict) else {},
        "source_artifact": str(runtime_paths["canary"]),
    }


def _operator_trust_report(
    *,
    health: dict[str, Any],
    packet_quality_summary: dict[str, Any],
    validation_coverage: list[dict[str, Any]],
    checkpoint_health: dict[str, Any],
    adaptation_summary: dict[str, Any],
    execution_coverage: dict[str, Any],
    support_confidence: dict[str, Any],
) -> dict[str, Any]:
    weakening_factors: list[str] = []
    if health.get("fallback_burden_high"):
        weakening_factors.append("fallback_burden_high")
    if health.get("validation_gap_present"):
        weakening_factors.append("validation_gap_present")
    if health.get("checkpoint_unhealthy"):
        weakening_factors.append("checkpoint_unhealthy")
    if packet_quality_summary.get("hard_fail_packet_ids"):
        weakening_factors.append("packet_quality_hard_fail")
    if str(execution_coverage.get("status") or "").strip() == "hard_fail":
        weakening_factors.append("deterministic_coverage_below_threshold")
    if adaptation_summary.get("event_count", 0):
        weakening_factors.append("runtime_adapted")
    if any(
        isinstance(item, dict)
        and str(item.get("capability_confidence") or "").strip() == "low"
        and str(item.get("status") or "").strip() in {"low_confidence_blocked", "generated_missing", "missing_capability"}
        for item in validation_coverage
    ):
        weakening_factors.append("capability_detection_low_confidence")
    if str(support_confidence.get("unsupported_closure_risk") or "none").strip() not in {"", "none"}:
        weakening_factors.append("unsupported_closure_risk")
    manual_dependencies = [
        item.get("lane")
        for item in validation_coverage
        if isinstance(item, dict) and str(item.get("manual_only_blocker") or "").strip()
    ]
    closure_strength = "strong" if not weakening_factors and health.get("closure_claim_is_strong") else "weak" if health.get("closure_ready_but_weak") else "pending"
    return {
        "closure_strength": closure_strength,
        "weakening_factors": weakening_factors,
        "manual_dependencies": manual_dependencies,
        "fallback_dependence": health.get("fallback_ratio", 0.0),
        "deterministic_coverage": execution_coverage.get("deterministic_ratio", 0.0),
        "non_review_deterministic_coverage": execution_coverage.get("non_review_deterministic_ratio", 0.0),
        "low_confidence_lanes": [
            str(item.get("lane") or "").strip()
            for item in validation_coverage
            if isinstance(item, dict) and str(item.get("capability_confidence") or "").strip() == "low"
        ],
        "validation_coverage_strength": "complete" if not health.get("validation_gap_present") else "incomplete",
        "checkpoint_confidence": "healthy" if checkpoint_health.get("checkpoint_blocked") is False else "blocked",
        "support_confidence": str(support_confidence.get("objective_support_status") or "").strip(),
        "support_remediation_available": support_confidence.get("support_remediation_available") is True,
        "support_gap_reasons": support_confidence.get("support_gap_reasons", []) if isinstance(support_confidence.get("support_gap_reasons"), list) else [],
    }


def _build_operator_view(*, artifacts_root: Path, track_id: str) -> dict[str, Any]:
    inputs = _load_runtime_inputs(artifacts_root=artifacts_root, track_id=track_id)
    runtime_paths = inputs["runtime_paths"]
    runtime_state = inputs["runtime_state"]
    summary = inputs["summary"]
    status = inputs["status"]
    schedule = inputs["schedule"]
    checkpoint = inputs["checkpoint"]
    validation_plan = inputs["validation_plan"]
    repo_capabilities = inputs["repo_capabilities"]
    packet_quality = inputs["packet_quality"]
    execution_coverage = inputs["execution_coverage"]
    support_confidence = inputs["support_confidence"]
    execution_ledger = inputs["execution_ledger"]
    adaptation_log = inputs["adaptation_log"]
    benchmark = inputs["benchmark"]
    canary = inputs["canary"]
    blockers_payload = inputs["blockers"]
    transaction_state = inputs["transaction_state"]
    transaction_log = inputs["transaction_log"]
    packet_dag = inputs["packet_dag"]
    packets = {
        str(packet.get("packet_id") or "").strip(): packet
        for packet in packet_dag.get("packets", [])
        if isinstance(packet, dict) and str(packet.get("packet_id") or "").strip()
    }
    packet_counts = _operator_packet_counts(packets=packets, status=status)
    validation_coverage, validation_gap_present = _operator_validation_coverage(
        validation_plan=validation_plan,
        packets=packets,
        track_id=track_id,
        runtime_paths=runtime_paths,
    )
    blockers = _operator_blockers(
        summary=summary,
        checkpoint=checkpoint,
        blockers_payload=blockers_payload,
        validation_coverage=validation_coverage,
        support_confidence=support_confidence,
        track_id=track_id,
        runtime_paths=runtime_paths,
    )
    health = _operator_health_signals(
        summary=summary,
        schedule=schedule,
        validation_gap_present=validation_gap_present,
        checkpoint=checkpoint,
        execution_ledger=execution_ledger,
        execution_coverage=execution_coverage,
        packet_quality=packet_quality,
        support_confidence=support_confidence,
    )
    stop_state = _build_stop_state(
        summary=summary,
        schedule=schedule,
        checkpoint=checkpoint,
        health=health,
        blockers=blockers,
        support_confidence=support_confidence,
    )
    explanations = _operator_explanations(
        summary=summary,
        schedule=schedule,
        health=health,
        validation_coverage=validation_coverage,
        blockers=blockers,
        packets=packets,
        execution_ledger=execution_ledger,
        support_confidence=support_confidence,
    )
    timeline = _operator_timeline(
        schedule=schedule,
        progress=inputs["progress"],
        artifacts_root=artifacts_root,
        track_id=track_id,
    )
    checkpoint_health = {
        "checkpoint_blocked": checkpoint.get("checkpoint_blocked") is True,
        "checkpoint_block_reason": str(checkpoint.get("checkpoint_block_reason") or "").strip(),
        "checkpoint_commit": str(checkpoint.get("checkpoint_commit") or "").strip(),
        "rollback_validation_ref": str(checkpoint.get("rollback_validation_ref") or "").strip(),
        "source_artifact": str(session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["checkpoint"]),
    }
    repo_capabilities_summary = _operator_repo_capabilities_summary(repo_capabilities, runtime_paths=runtime_paths)
    packet_quality_summary = _operator_packet_quality_summary(packet_quality, runtime_paths=runtime_paths)
    adaptation_summary = _operator_adaptation_summary(adaptation_log, runtime_paths=runtime_paths)
    trust_report = _operator_trust_report(
        health=health,
        packet_quality_summary=packet_quality_summary,
        validation_coverage=validation_coverage,
        checkpoint_health=checkpoint_health,
        adaptation_summary=adaptation_summary,
        execution_coverage=execution_coverage,
        support_confidence=support_confidence,
    )
    benchmark_summary = _operator_benchmark_summary(benchmark, runtime_paths=runtime_paths)
    canary_summary = _operator_canary_summary(canary, runtime_paths=runtime_paths)
    evaluation_summary = {
        "has_benchmark": bool(benchmark_summary),
        "has_canary": bool(canary_summary),
        "swarm_outperformed_serial": benchmark_summary.get("swarm_outperformed_serial") is True,
        "serial_better": benchmark_summary.get("serial_better") is True,
        "benchmark_reason": str(benchmark_summary.get("reason") or "").strip(),
        "recommended_mode": str(benchmark_summary.get("recommended_mode") or "").strip(),
        "canary_refused": canary_summary.get("refused") is True,
        "canary_refusal_reason": str(canary_summary.get("refusal_reason") or "").strip(),
        "canary_safety_mode": str(canary_summary.get("safety_mode") or "").strip(),
        "canary_isolation_mode": str(canary_summary.get("isolation_mode") or "").strip(),
        "closure_strength": str(trust_report.get("closure_strength") or "").strip(),
    }
    lane_state = schedule.get("active_packets_by_lane") if isinstance(schedule.get("active_packets_by_lane"), dict) else {}
    lane_queue_depths = schedule.get("lane_queue_depths") if isinstance(schedule.get("lane_queue_depths"), dict) else {}
    dispatch_block_reasons = schedule.get("dispatch_block_reasons") if isinstance(schedule.get("dispatch_block_reasons"), dict) else {}
    runnable_but_not_dispatched = [
        str(item).strip()
        for item in schedule.get("runnable_but_not_dispatched", [])
        if str(item).strip()
    ] if isinstance(schedule.get("runnable_but_not_dispatched"), list) else []
    latest_transaction_log = transaction_log[-1] if transaction_log and isinstance(transaction_log[-1], dict) else {}
    transaction_summary = {
        "transaction_id": str(
            transaction_state.get("transaction_id")
            or latest_transaction_log.get("transaction_id")
            or ""
        ).strip(),
        "state": str(transaction_state.get("state") or latest_transaction_log.get("state") or "").strip(),
        "step_id": str(transaction_state.get("step_id") or latest_transaction_log.get("step_id") or "").strip(),
        "recovered": (
            transaction_state.get("state") == "recovered"
            or latest_transaction_log.get("recovered") is True
            or str(transaction_state.get("recovery_outcome") or "").strip() == "finished_commit"
        ),
        "recovery_outcome": str(transaction_state.get("recovery_outcome") or "").strip(),
        "updated_at": str(transaction_state.get("updated_at") or latest_transaction_log.get("timestamp") or "").strip(),
        "committed_artifact_count": int(
            transaction_state.get("committed_artifact_count")
            or latest_transaction_log.get("committed_artifact_count")
            or 0
        ),
        "artifact_paths": {
            "transaction_state": str(runtime_paths["transaction_state"]),
            "transaction_log": str(runtime_paths["transaction_log"]),
        },
    }
    return {
        "schema_version": OBJECTIVE_RUNTIME_OPERATOR_VIEW_SCHEMA_VERSION,
        "objective_id": str(runtime_state.get("objective_id") or summary.get("objective_id") or status.get("objective_id") or stable_objective_id(track_id)),
        "track_id": track_id,
        "controller_mode": str(runtime_state.get("controller_mode") or summary.get("controller_mode") or CONTROLLER_MODE_ENFORCE),
        "route_hint": str(runtime_state.get("route_hint") or summary.get("route_hint") or validation_plan.get("route_hint") or ""),
        "closure_state": str(runtime_state.get("closure_state") or summary.get("closure_state") or status.get("closure_state") or ""),
        "lifecycle_status": str(runtime_state.get("lifecycle_status") or ""),
        "current_cycle_id": str(runtime_state.get("current_cycle_id") or summary.get("current_cycle_id") or ""),
        "current_frontier": [str(item).strip() for item in runtime_state.get("current_frontier", []) if str(item).strip()]
        if isinstance(runtime_state.get("current_frontier"), list)
        else [str(item).strip() for item in summary.get("current_frontier", []) if str(item).strip()],
        "safe_momentum_available": runtime_state.get("safe_momentum_available") is True
        if "safe_momentum_available" in runtime_state
        else summary.get("safe_momentum_available") is True,
        "packet_counts": packet_counts,
        "next_recommended_packet": str(runtime_state.get("next_recommended_packet") or stop_state["next_recommended_packet"]),
        "required_work_remaining": runtime_state.get("required_work_remaining")
        if "required_work_remaining" in runtime_state
        else stop_state["required_work_remaining"],
        "required_work_reasons": runtime_state.get("required_work_reasons")
        if isinstance(runtime_state.get("required_work_reasons"), list)
        else stop_state["required_work_reasons"],
        "material_optional_work_remaining": runtime_state.get("material_optional_work_remaining")
        if "material_optional_work_remaining" in runtime_state
        else stop_state["material_optional_work_remaining"],
        "material_optional_work_reasons": runtime_state.get("material_optional_work_reasons")
        if isinstance(runtime_state.get("material_optional_work_reasons"), list)
        else stop_state["material_optional_work_reasons"],
        "stop_allowed": runtime_state.get("stop_allowed") if "stop_allowed" in runtime_state else stop_state["stop_allowed"],
        "stop_reason": str(runtime_state.get("stop_reason") or stop_state["stop_reason"]),
        "next_action": str(summary.get("next_action") or ""),
        "validation_coverage": validation_coverage,
        "strategy_mix": [str(item).strip() for item in summary.get("strategy_mix", []) if str(item).strip()],
        "fallback_packets": [str(item).strip() for item in summary.get("fallback_packets", []) if str(item).strip()],
        "swarm_status": str(summary.get("swarm_status") or schedule.get("swarm_status") or "single_lane"),
        "execution_shape": str(summary.get("execution_shape") or schedule.get("execution_shape") or "single_lane"),
        "lane_state": lane_state,
        "lane_queue_depths": lane_queue_depths,
        "active_packets_by_lane": lane_state,
        "runnable_but_not_dispatched": runnable_but_not_dispatched,
        "dispatch_block_reasons": dispatch_block_reasons,
        "awaiting_verifier": sorted(
            packet_id
            for packet_id, packet in packets.items()
            if str(packet.get("runtime_state") or "").strip() == "awaiting_verifier"
        ),
        "awaiting_reviewer_barrier": [
            str(item).strip()
            for item in schedule.get("awaiting_reviewer_barrier", [])
            if str(item).strip()
        ] if isinstance(schedule.get("awaiting_reviewer_barrier"), list) else [],
        "convergence_status": str(schedule.get("convergence_status") or ""),
        "checkpoint_health": checkpoint_health,
        "health_signals": health,
        "blockers": blockers,
        "timeline": timeline,
        "explanations": explanations,
        "repo_capabilities_summary": repo_capabilities_summary,
        "packet_quality_summary": packet_quality_summary,
        "adaptation_summary": adaptation_summary,
        "benchmark_summary": benchmark_summary,
        "canary_summary": canary_summary,
        "evaluation_summary": evaluation_summary,
        "execution_coverage": execution_coverage,
        "support_confidence": support_confidence,
        "unsupported_closure_risk": str(runtime_state.get("unsupported_closure_risk") or stop_state["unsupported_closure_risk"]),
        "last_verifier_result": runtime_state.get("last_verifier_result", {}) if isinstance(runtime_state.get("last_verifier_result"), dict) else {},
        "support_gaps": support_confidence.get("support_gap_reasons", []) if isinstance(support_confidence.get("support_gap_reasons"), list) else [],
        "support_remediation_available": support_confidence.get("support_remediation_available") is True,
        "support_backed_closure": support_confidence.get("support_backed_closure") is True,
        "external_support_coverage": support_confidence.get("external_support_coverage", {}) if isinstance(support_confidence.get("external_support_coverage"), dict) else {},
        "trust_report": trust_report,
        "transaction": transaction_summary,
        "artifacts": {
            "runtime_state": str(runtime_paths["runtime_state"]),
            "summary": str(runtime_paths["summary"]),
            "validation_plan": str(runtime_paths["validation_plan"]),
            "repo_capabilities": str(runtime_paths["repo_capabilities"]),
            "packet_quality": str(runtime_paths["packet_quality"]),
            "execution_coverage": str(runtime_paths["execution_coverage"]),
            "support_confidence": str(runtime_paths["support_confidence"]),
            "status": str(runtime_paths["status"]),
            "schedule": str(runtime_paths["schedule"]),
            "execution_ledger": str(runtime_paths["execution_ledger"]),
            "adaptation_log": str(runtime_paths["adaptation_log"]),
            "benchmark": str(runtime_paths["benchmark"]),
            "canary": str(runtime_paths["canary"]),
            "packet_results": str(runtime_paths["packet_results"]),
            "operator_view": str(runtime_paths["operator_view"]),
            "transaction_state": str(runtime_paths["transaction_state"]),
            "transaction_log": str(runtime_paths["transaction_log"]),
        },
        "updated_at": now_iso(),
    }


def _sync_operator_view(*, artifacts_root: Path, track_id: str) -> None:
    write_json_file(
        runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["operator_view"],
        _build_operator_view(artifacts_root=artifacts_root, track_id=track_id),
    )


def _append_packet_results(
    *,
    artifacts_root: Path,
    track_id: str,
    packet_results: list[dict[str, Any]],
) -> None:
    if not packet_results:
        return
    path = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["packet_results"]
    existing = _load_jsonl(path)
    existing.extend(packet_results)
    _write_jsonl(path, existing)


def _append_adaptation_events(
    *,
    artifacts_root: Path,
    track_id: str,
    events: list[dict[str, Any]],
) -> None:
    if not events:
        return
    path = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["adaptation_log"]
    existing = _load_jsonl(path)
    existing.extend(events)
    _write_jsonl(path, existing)


def _update_execution_ledger(
    *,
    artifacts_root: Path,
    track_id: str,
    packets: dict[str, dict[str, Any]],
    latest_results: list[dict[str, Any]] | None = None,
    packet_verdicts: list[dict[str, Any]] | None = None,
) -> None:
    route_hint = str(_load_json_if_exists(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["summary"]).get("route_hint") or "").strip()
    latest_by_packet = {
        str(item.get("packet_id", "")).strip(): item
        for item in (latest_results or [])
        if isinstance(item, dict) and str(item.get("packet_id", "")).strip()
    }
    verdict_by_packet = {
        str(item.get("packet_id", "")).strip(): item
        for item in (packet_verdicts or [])
        if isinstance(item, dict) and str(item.get("packet_id", "")).strip()
    }
    ledger = {
        "schema_version": "objective-execution-ledger.v1",
        "objective_id": stable_objective_id(track_id),
        "track_id": track_id,
        "packets": [],
    }
    for packet_id in sorted(packets):
        packet = packets[packet_id]
        latest = latest_by_packet.get(packet_id, {})
        verdict = verdict_by_packet.get(packet_id, {})
        ledger["packets"].append(
            {
                "packet_id": packet_id,
                "strategy_name": str(packet.get("execution_strategy") or "").strip(),
                "packet_lane": str(packet.get("packet_lane") or "").strip(),
                "fallback_used": latest.get("fallback_used") is True,
                "fallback_reason": str(latest.get("fallback_reason") or packet.get("fallback_reason") or "").strip(),
                "evidence_destination": str(packet.get("evidence_destination") or "").strip(),
                "runtime_state": str(packet.get("runtime_state") or "").strip(),
                "verifier_output": str(verdict.get("verifier_output") or "").strip(),
                "retry_counters": {},
                "latest_result_artifact": str(latest.get("result_artifact_path") or "").strip(),
                "runner_metadata": latest.get("runner_metadata") if isinstance(latest.get("runner_metadata"), dict) else {},
                "support_status": str(verdict.get("support_status") or "").strip(),
                "unsupported_risk_reason": str(verdict.get("unsupported_risk_reason") or "").strip(),
            }
        )
    ledger["coverage"] = _build_execution_coverage_report(
        packets=packets,
        execution_ledger=ledger,
        route_hint=route_hint,
    )
    write_json_file(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["execution_ledger"], ledger)


def _current_blocked_reasons(
    *,
    packets: dict[str, dict[str, Any]],
    blocked_by_budget: bool,
    review: dict[str, Any] | None = None,
) -> list[str]:
    reasons = []
    if blocked_by_budget:
        reasons.append("runtime_budget_exhausted")
    if review:
        reasons.extend(str(reason).strip() for reason in review.get("blocked_by_authority", []) if str(reason).strip())
        reasons.extend(
            f"external_evidence:{packet_id}"
            for packet_id in review.get("blocked_by_external_evidence", [])
            if str(packet_id).strip()
        )
    reasons.extend(
        f"escalated:{packet_id}"
        for packet_id, packet in packets.items()
        if str(packet.get("runtime_state", "")).strip() == "escalated"
    )
    return sorted(set(reasons))


def _sync_runtime_summary(
    *,
    plan_payload: dict[str, Any],
    artifacts_root: Path,
    track_id: str,
    status: dict[str, Any],
    schedule: dict[str, Any],
    packets: dict[str, dict[str, Any]],
    cwd: str | None,
    review: dict[str, Any] | None = None,
    blocked_by_budget: bool = False,
    controller_mode: str | None = None,
) -> None:
    session_harness = plan_payload.get("session_harness") if isinstance(plan_payload.get("session_harness"), dict) else {}
    route_hint = str(session_harness.get("route_hint") or "").strip()
    frontier = list(schedule.get("current_frontier") or [])
    fallback_packets = sorted(
        packet_id
        for packet_id, packet in packets.items()
        if str(packet.get("execution_strategy") or "").strip() == "codex_prompt_worker"
    )
    strategy_mix = sorted(
        {
            str(packet.get("execution_strategy") or "").strip()
            for packet in packets.values()
            if str(packet.get("execution_strategy") or "").strip()
        }
    )
    summary = build_objective_summary(
        objective_id=stable_objective_id(track_id),
        track_id=track_id,
        route_hint=route_hint,
        closure_state=str(status.get("closure_state") or "").strip(),
        frontier=frontier,
        blocked_reasons=_current_blocked_reasons(
            packets=packets,
            blocked_by_budget=blocked_by_budget,
            review=review,
        ),
        accepted_packet_count=len(
            [
                packet_id
                for packet_id, packet in packets.items()
                if str(packet.get("runtime_state", "")).strip() == "accepted"
            ]
        ),
        next_action=frontier[0] if frontier else ("finalize" if str(status.get("closure_state") or "").strip().startswith("OBJECTIVE_COMPLETE") else "escalate"),
        execution_shape=str(schedule.get("execution_shape") or plan_payload.get("execution_shape") or "single_lane"),
        swarm_status=str(schedule.get("swarm_status") or ""),
        lane_queue_depths=schedule.get("lane_queue_depths") if isinstance(schedule.get("lane_queue_depths"), dict) else {},
        active_packets_by_lane=schedule.get("active_packets_by_lane") if isinstance(schedule.get("active_packets_by_lane"), dict) else {},
        convergence_status=str(schedule.get("convergence_status") or ""),
    )
    summary["current_cycle_id"] = ""
    cycle_log = schedule.get("cycle_log") if isinstance(schedule.get("cycle_log"), list) else []
    if cycle_log and isinstance(cycle_log[-1], dict):
        summary["current_cycle_id"] = str(cycle_log[-1].get("cycle_id") or "").strip()
    summary["fallback_packets"] = fallback_packets
    summary["strategy_mix"] = strategy_mix
    summary["safe_momentum_available"] = schedule.get("safe_momentum_available") is True
    summary["controller_mode"] = _normalize_controller_mode(controller_mode)
    summary["authoritative_state_artifact"] = str(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["runtime_state"])
    summary["authoritative_state_kind"] = "objective.runtime-state.json"
    summary["derived_view"] = True
    repo_capabilities = discover_repo_capabilities(cwd=cwd)
    validation_plan = build_repo_validation_plan(plan_payload, track_id=track_id, cwd=cwd, repo_capabilities=repo_capabilities)
    packet_quality = build_packet_quality_report(
        plan=plan_payload,
        route_hint=route_hint,
        packets=[packet for packet in packets.values() if isinstance(packet, dict)],
    )
    execution_coverage = _build_execution_coverage_report(
        packets=packets,
        execution_ledger=_load_json_if_exists(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["execution_ledger"]),
        route_hint=route_hint,
    )
    support_confidence = _build_support_confidence(
        artifacts_root=artifacts_root,
        track_id=track_id,
        packets=packets,
        status=status,
        schedule=schedule,
        validation_plan=validation_plan,
        checkpoint=_load_json_if_exists(session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["checkpoint"]),
        execution_coverage=execution_coverage,
    )
    checkpoint = _load_json_if_exists(session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["checkpoint"])
    validation_coverage, validation_gap_present = _operator_validation_coverage(
        validation_plan=validation_plan,
        packets=packets,
        track_id=track_id,
        runtime_paths=runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id),
    )
    blockers_payload = _load_json_if_exists(session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["blockers"])
    blockers = _operator_blockers(
        summary=summary,
        checkpoint=checkpoint,
        blockers_payload=blockers_payload,
        validation_coverage=validation_coverage,
        support_confidence=support_confidence,
        track_id=track_id,
        runtime_paths=runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id),
    )
    health = _operator_health_signals(
        summary=summary,
        schedule=schedule,
        validation_gap_present=validation_gap_present,
        checkpoint=checkpoint,
        execution_ledger=_load_json_if_exists(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["execution_ledger"]),
        execution_coverage=execution_coverage,
        packet_quality=packet_quality,
        support_confidence=support_confidence,
    )
    stop_state = _build_stop_state(
        summary=summary,
        schedule=schedule,
        checkpoint=checkpoint,
        health=health,
        blockers=blockers,
        support_confidence=support_confidence,
    )
    summary.update(stop_state)
    _write_runtime_supporting_state(
        artifacts_root=artifacts_root,
        track_id=track_id,
        summary=summary,
        validation_plan=validation_plan,
        repo_capabilities=repo_capabilities,
        packet_quality=packet_quality,
        execution_coverage=execution_coverage,
        support_confidence=support_confidence,
    )
    _sync_runtime_state_record(artifacts_root=artifacts_root, track_id=track_id)


def _runtime_stop_state_fields(*, artifacts_root: Path, track_id: str) -> dict[str, Any]:
    runtime_paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    runtime_state = _load_json_if_exists(runtime_paths["runtime_state"])
    summary = runtime_state or _load_json_if_exists(runtime_paths["summary"])
    return {
        "controller_mode": summary.get("controller_mode", CONTROLLER_MODE_ENFORCE),
        "required_work_remaining": summary.get("required_work_remaining"),
        "required_work_reasons": summary.get("required_work_reasons", []),
        "material_optional_work_remaining": summary.get("material_optional_work_remaining"),
        "material_optional_work_reasons": summary.get("material_optional_work_reasons", []),
        "stop_allowed": summary.get("stop_allowed"),
        "stop_reason": summary.get("stop_reason"),
        "next_recommended_packet": summary.get("next_recommended_packet", ""),
        "unsupported_closure_risk": summary.get("unsupported_closure_risk", "none"),
    }


def _controller_verdict(
    *,
    artifacts_root: Path,
    track_id: str,
    controller_mode: str | None = None,
) -> dict[str, Any]:
    normalized_mode = _normalize_controller_mode(controller_mode)
    runtime_paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    runtime_state = _load_json_if_exists(runtime_paths["runtime_state"])
    summary = runtime_state or _load_json_if_exists(runtime_paths["summary"])
    stop_state = _runtime_stop_state_fields(artifacts_root=artifacts_root, track_id=track_id)
    closure_state = str(summary.get("closure_state") or "").strip()
    safe_momentum_available = summary.get("safe_momentum_available") is True
    payload = {
        **stop_state,
        "controller_mode": normalized_mode,
        "closure_state": closure_state,
        "safe_momentum_available": safe_momentum_available,
        "advisory_only": False,
    }
    if stop_state.get("stop_allowed") is True:
        payload.update({"status": "approve", "reason_code": "STOP_ALLOWED"})
        return payload
    if stop_state.get("required_work_remaining") is True:
        if safe_momentum_available:
            payload.update({"status": "revise", "reason_code": "REQUIRED_WORK_REMAINING"})
        else:
            payload.update({"status": "blocked", "reason_code": "NO_SAFE_MOMENTUM"})
        return payload
    if stop_state.get("material_optional_work_remaining") is True:
        if normalized_mode == CONTROLLER_MODE_AUDIT:
            payload.update(
                {
                    "status": "approve",
                    "reason_code": "MATERIAL_FINISHING_WORK_REMAINING",
                    "advisory_only": True,
                }
            )
        else:
            payload.update({"status": "revise", "reason_code": "MATERIAL_FINISHING_WORK_REMAINING"})
        return payload
    if safe_momentum_available:
        payload.update({"status": "revise", "reason_code": "REQUIRED_WORK_REMAINING"})
    else:
        payload.update({"status": "blocked", "reason_code": "NO_SAFE_MOMENTUM"})
    return payload


def _run_policy_preflight(*, workspace_root: str | None) -> None:
    if not workspace_root:
        return
    root = Path(workspace_root).resolve()
    verifier = root / "scripts" / "check_codex_policy_consistency.py"
    policy_index = root / "docs" / "policy-index.json"
    if not verifier.exists() or not policy_index.exists():
        return
    completed = subprocess.run(
        canonical_python_argv(str(verifier), str(policy_index)),
        cwd=str(root),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stdout or completed.stderr or "policy verifier failed").strip()
        raise RuntimeError(f"policy_preflight_failed:{detail}")


def _append_progress_events(*, artifacts_root: Path, track_id: str, events: list[dict[str, Any]]) -> None:
    if not events:
        return
    progress_path = session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["progress"]
    existing = _load_jsonl(progress_path)
    existing.extend(events)
    _write_jsonl(progress_path, existing)


def _update_checkpoint(
    *,
    artifacts_root: Path,
    track_id: str,
    packets: dict[str, dict[str, Any]],
    schedule: dict[str, Any],
    last_forward_movement: str,
    checkpoint_meta: dict[str, Any],
) -> dict[str, Any]:
    paths = session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    checkpoint = load_json_file(paths["checkpoint"])
    accepted_packets = sorted(
        packet_id for packet_id, packet in packets.items() if str(packet.get("runtime_state", "")).strip() == "accepted"
    )
    frontier = list(schedule.get("current_frontier") or [])
    checkpoint.update(
        {
            "checkpoint_id": str(checkpoint_meta.get("checkpoint_id") or checkpoint.get("checkpoint_id") or ""),
            "last_verified_packet_ids": accepted_packets,
            "current_frontier": frontier,
            "next_recommended_packet": frontier[0] if frontier else "",
            "last_forward_movement": last_forward_movement,
            "stagnation_risk": "low" if frontier else "elevated",
            "escalation_candidates": sorted(
                packet_id for packet_id, packet in packets.items() if str(packet.get("runtime_state", "")).strip() == "escalated"
            ),
            "checkpoint_strategy": str(checkpoint_meta.get("checkpoint_strategy") or "git_checkpoint_required"),
            "checkpoint_attempted_at": str(checkpoint_meta.get("checkpoint_attempted_at") or ""),
            "rollback_validation_ref": str(checkpoint_meta.get("rollback_validation_ref") or ""),
            "checkpoint_blocked": checkpoint_meta.get("checkpoint_blocked") is True,
            "checkpoint_commit": str(checkpoint_meta.get("checkpoint_commit") or ""),
            "checkpoint_block_reason": str(checkpoint_meta.get("checkpoint_block_reason") or ""),
            "checkpoint_block_evidence": str(checkpoint_meta.get("checkpoint_block_evidence") or ""),
            "updated_at": schedule.get("updated_at") or checkpoint.get("updated_at") or now_iso(),
        }
    )
    write_json_file(paths["checkpoint"], checkpoint)
    return checkpoint


def _sync_kernel_state(
    *,
    artifacts_root: Path,
    track_id: str,
    step_id: str,
    to_state: str,
    guard: str,
    guard_result: bool,
    trigger: str,
    packet_ids: list[str] | None = None,
    action_kind: str | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    last_verification_id: str | None = None,
    record_failure: tuple[str, str, bool] | None = None,
) -> dict[str, Any]:
    kernel_state = _load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id)
    if not kernel_state:
        return {}
    previous_state = str(kernel_state.get("state") or "bootstrapped")
    normalized_packet_ids = [str(packet_id).strip() for packet_id in (packet_ids or []) if str(packet_id).strip()]
    if action_kind:
        kernel_state["last_action"] = {
            "kind": action_kind,
            "unit_id": _packet_group_id(normalized_packet_ids, step_id),
            "step_id": step_id,
        }
        budget = kernel_state.get("budget") if isinstance(kernel_state.get("budget"), dict) else {}
        if isinstance(budget.get("remaining_steps"), int) and budget["remaining_steps"] > 0:
            budget["remaining_steps"] -= 1
        kernel_state["budget"] = budget
    if evidence_refs is not None:
        kernel_state["evidence_refs"] = evidence_refs
    kernel_state["state"] = to_state
    kernel_state["active_unit_ids"] = normalized_packet_ids
    kernel_state["active_unit_id"] = _packet_group_id(normalized_packet_ids, step_id) if normalized_packet_ids else None
    if last_verification_id is not None:
        kernel_state["last_verification_id"] = last_verification_id
    if to_state in KERNEL_RUNTIME_TERMINAL_STATES:
        kernel_state["halt"] = {"terminal": True, "reason": _terminal_reason_for_state(to_state)}
        kernel_state["active_unit_ids"] = []
        kernel_state["active_unit_id"] = None
    else:
        kernel_state["halt"] = {"terminal": False, "reason": "none"}
    if record_failure is not None:
        failure_class, verification_id, count_against_retry_budget = record_failure
        _record_failed_attempt(
            kernel_state=kernel_state,
            step_id=step_id,
            unit_id=_packet_group_id(normalized_packet_ids, step_id),
            failure_class=failure_class,
            verification_id=verification_id,
            count_against_retry_budget=count_against_retry_budget,
        )
        budget = kernel_state.get("budget") if isinstance(kernel_state.get("budget"), dict) else {}
        if count_against_retry_budget and isinstance(budget.get("remaining_retries"), int) and budget["remaining_retries"] > 0:
            budget["remaining_retries"] -= 1
        kernel_state["budget"] = budget
    record = _append_transition_record(
        artifacts_root=artifacts_root,
        track_id=track_id,
        kernel_state=kernel_state,
        step_id=step_id,
        from_state=previous_state,
        to_state=to_state,
        guard=guard,
        guard_result=guard_result,
        trigger=trigger,
        evidence_refs=evidence_refs,
    )
    errors = validate_state(
        state=kernel_state,
        execution_plan=_load_execution_plan(artifacts_root=artifacts_root, track_id=track_id),
        validation_plan=_load_json_if_exists(
            runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["validation_plan"]
        ),
    )
    if errors:
        _force_kernel_unsafe(
            artifacts_root=artifacts_root,
            track_id=track_id,
            kernel_state=kernel_state,
            errors=errors,
            step_id=step_id,
        )
        return {}
    _write_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id, payload=kernel_state)
    return record


def _build_verification_results(
    *,
    cycle_request: dict[str, Any],
    cycle_result: dict[str, Any],
    review: dict[str, Any],
) -> list[dict[str, Any]]:
    packet_results = {
        str(item.get("packet_id") or "").strip(): item
        for item in cycle_result.get("packet_results", [])
        if isinstance(item, dict) and str(item.get("packet_id") or "").strip()
    }
    results: list[dict[str, Any]] = []
    for verdict in review.get("packet_verdicts", []):
        if not isinstance(verdict, dict):
            continue
        packet_id = str(verdict.get("packet_id") or "").strip()
        if not packet_id:
            continue
        packet_result = packet_results.get(packet_id, {})
        status, repairability = _kernel_verification_status(str(verdict.get("verifier_output") or "").strip())
        scope = "environment" if str(verdict.get("blocker_class") or "").strip() in {"external_evidence", "environment"} else "targeted"
        if str(verdict.get("allowed_scope_status") or "").strip() != "within_scope":
            scope = "broad"
        evidence_refs = [
            _evidence_ref(
                kind="json_artifact",
                path=str(verdict.get("artifact_path") or ""),
                producer="verify_cycle",
                step_id=str(cycle_request.get("cycle_id") or ""),
            )
        ]
        for ref in verdict.get("evidence_refs", []) if isinstance(verdict.get("evidence_refs"), list) else []:
            evidence_refs.append(
                _evidence_ref(
                    kind="runtime_artifact",
                    path=str(ref),
                    producer=str(packet_result.get("runner_kind") or "cycle_worker"),
                    step_id=str(cycle_request.get("cycle_id") or ""),
                )
            )
        results.append(
            {
                "schema_version": VERIFICATION_RESULT_SCHEMA_VERSION,
                "verification_id": f"{sanitize_token(str(cycle_request.get('cycle_id') or 'cycle'))}-{sanitize_token(packet_id)}-verify",
                "step_id": str(cycle_request.get("cycle_id") or ""),
                "unit_id": packet_id,
                "status": status,
                "scope": scope if scope in VERIFICATION_RESULT_SCOPES else "targeted",
                "blame": "introduced" if status != "pass" else "unknown",
                "repairability": repairability if repairability in VERIFICATION_RESULT_REPAIRABILITY else "blocked",
                "evidence": evidence_refs,
                "suggested_transition": "finalize_pending" if status == "pass" else ("repair_pending" if status == "soft_fail" else "blocked"),
            }
        )
    return results


def _update_feature_list(*, artifacts_root: Path, track_id: str, accepted_packets: set[str]) -> dict[str, Any]:
    path = session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["feature_list"]
    payload = load_json_file(path)
    for item in payload.get("features", []):
        if not isinstance(item, dict):
            continue
        packet_ids = {
            str(packet_id).strip()
            for packet_id in item.get("packet_ids", [])
            if str(packet_id).strip()
        }
        if packet_ids and packet_ids.issubset(accepted_packets):
            item["status"] = "verified"
    write_json_file(path, payload)
    return payload


def _update_momentum_and_blockers(
    *,
    artifacts_root: Path,
    track_id: str,
    packets: dict[str, dict[str, Any]],
    frontier: list[str],
    blocker_reason: str = "runtime_escalated",
) -> None:
    paths = session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    momentum = load_json_file(paths["momentum"])
    momentum["safe_momentum_ready"] = bool(frontier)
    momentum["current_frontier"] = frontier
    write_json_file(paths["momentum"], momentum)

    blockers = {
        "schema_version": "objective-blockers.v1",
        "objective_id": stable_objective_id(track_id),
        "track_id": track_id,
        "items": [
            {
                "packet_id": packet_id,
                "reason": blocker_reason,
                "authority_sensitive": True,
            }
            for packet_id, packet in packets.items()
            if str(packet.get("runtime_state", "")).strip() == "escalated"
        ],
    }
    write_json_file(paths["blockers"], blockers)


def _current_cycle_paths(*, artifacts_root: Path, track_id: str, cycle_id: str) -> dict[str, Path]:
    return cycle_artifact_paths(artifacts_root=artifacts_root, track_id=track_id, cycle_id=cycle_id)


def _open_cycle(*, artifacts_root: Path, track_id: str) -> tuple[str, dict[str, Any], dict[str, Path]] | None:
    cycles_root = artifacts_root / track_id / "cycles"
    if not cycles_root.exists():
        return None
    for state_path in sorted(cycles_root.rglob("cycle.state.json")):
        payload = load_json_file(state_path)
        if str(payload.get("phase", "")).strip() != "applied":
            cycle_id = str(payload.get("cycle_id", "")).strip()
            if cycle_id:
                return cycle_id, payload, _current_cycle_paths(artifacts_root=artifacts_root, track_id=track_id, cycle_id=cycle_id)
    return None


def _next_cycle_id(schedule: dict[str, Any], objective_id: str) -> str:
    dispatch_history = schedule.get("dispatch_history") if isinstance(schedule.get("dispatch_history"), list) else []
    return f"{objective_id}-cycle-{len(dispatch_history) + 1:03d}"


def _render_worker_prompt(*, cycle_request: dict[str, Any], result_path: Path) -> str:
    packet_ids = ", ".join(cycle_request.get("packet_ids", []))
    allowed_scope = sorted({item for packet in cycle_request.get("packets", []) for item in packet.get("allowed_scope", [])})
    allowed_scope_text = ", ".join(allowed_scope) if allowed_scope else "(no file edits allowed)"
    return (
        f"Execute governed packet cycle {cycle_request['cycle_id']} for packets: {packet_ids}.\n"
        f"Allowed scope: {allowed_scope_text}.\n"
        f"Write JSON to {result_path} with schema_version cycle-result.v1, cycle_id, packet_results[], "
        "changed_files, evidence_refs, summary, and allowed_scope_status."
    )


def _resolve_real_bin(codex_home: str | None) -> str | None:
    candidates = [
        os.environ.get("CODEX_RUNTIME_REAL_BIN"),
        os.environ.get("CODEX_REAL_BIN"),
        shutil.which("codex.real"),
    ]
    if codex_home:
        candidates.append(str(Path(codex_home).expanduser() / "bin" / "codex.real"))
    candidates.append(str(Path.home() / ".local" / "bin" / "codex.real"))
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().exists():
            return str(Path(candidate).expanduser())
    return None


def _attempt_index(packet_id: str, schedule: dict[str, Any]) -> int:
    counters = schedule.get("retry_counters") if isinstance(schedule.get("retry_counters"), dict) else {}
    value = counters.get(packet_id)
    if not isinstance(value, dict):
        return 0
    try:
        return int(value.get("same_method_attempts", 0) or 0) + int(value.get("alternate_strategy_attempts", 0) or 0)
    except Exception:
        return 0


def _base_packet_result(
    *,
    packet: dict[str, Any],
    verifier_output: str,
    summary: str,
    exit_code: int = 0,
    allowed_scope_status: str = "within_scope",
    changed_files: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    result_artifact_path: str = "",
    retry_mode: str = "same_method",
    blocked_reason: str = "",
    captured_commands: list[dict[str, Any]] | None = None,
    produced_artifacts: list[str] | None = None,
    fallback_used: bool = False,
    fallback_reason: str = "",
    step_results: list[dict[str, Any]] | None = None,
    runner_metadata: dict[str, Any] | None = None,
    alternate_candidates_considered: list[str] | None = None,
    stdout: str = "",
    stderr: str = "",
) -> dict[str, Any]:
    return {
        "packet_id": str(packet.get("packet_id", "")).strip(),
        "strategy_name": str(packet.get("execution_strategy") or "").strip(),
        "runner_kind": str(strategy_spec(str(packet.get("execution_strategy") or "").strip()).get("runner_kind") if strategy_spec(str(packet.get("execution_strategy") or "").strip()) else "").strip(),
        "exit_code": int(exit_code),
        "verifier_output": verifier_output,
        "allowed_scope_status": allowed_scope_status,
        "summary": summary,
        "stdout": stdout,
        "stderr": stderr,
        "changed_files": changed_files or [],
        "evidence_refs": evidence_refs or [],
        "result_artifact_path": result_artifact_path,
        "captured_commands": captured_commands or [],
        "produced_artifacts": produced_artifacts or [],
        "blocked_reason": blocked_reason,
        "retry_mode": retry_mode,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "step_results": step_results or [],
        "runner_metadata": runner_metadata or {},
        "alternate_candidates_considered": alternate_candidates_considered or [],
    }


def _packet_result_from_simulation(packet: dict[str, Any], schedule: dict[str, Any]) -> dict[str, Any]:
    packet_id = str(packet.get("packet_id", "")).strip()
    simulation = packet.get("simulation") if isinstance(packet.get("simulation"), dict) else {}
    attempts = simulation.get("attempts") if isinstance(simulation.get("attempts"), list) else []
    attempt = attempts[min(_attempt_index(packet_id, schedule), len(attempts) - 1)] if attempts else {}
    stdout = str(attempt.get("stdout", "")).strip()
    stderr = str(attempt.get("stderr", "")).strip()
    summary = str(attempt.get("summary") or stdout or stderr or f"Simulated execution for {packet_id}.").strip()
    file_writes = attempt.get("file_writes") if isinstance(attempt.get("file_writes"), list) else []
    changed_files = attempt.get("changed_files") if isinstance(attempt.get("changed_files"), list) else []
    if not changed_files and file_writes:
        changed_files = [
            str(item.get("path", "")).strip()
            for item in file_writes
            if isinstance(item, dict) and str(item.get("path", "")).strip()
        ]
    result = _base_packet_result(
        packet=packet,
        exit_code=int(attempt.get("worker_exit_code", 0) or 0),
        verifier_output=str(attempt.get("review_output") or ("accepted" if int(attempt.get("worker_exit_code", 0) or 0) == 0 else "rejected_rework")).strip(),
        allowed_scope_status=str(attempt.get("allowed_scope_status") or "within_scope").strip(),
        blocked_reason=str(attempt.get("blocked_reason") or "").strip(),
        summary=summary,
        stdout=stdout,
        stderr=stderr,
        changed_files=changed_files,
        evidence_refs=attempt.get("evidence_refs") if isinstance(attempt.get("evidence_refs"), list) else [],
        result_artifact_path=str(attempt.get("result_artifact_path") or "").strip(),
        retry_mode=str(attempt.get("retry_mode") or "same_method").strip(),
        captured_commands=attempt.get("captured_commands") if isinstance(attempt.get("captured_commands"), list) else [],
        produced_artifacts=attempt.get("produced_artifacts") if isinstance(attempt.get("produced_artifacts"), list) else [],
        fallback_used=attempt.get("fallback_used") is True,
        fallback_reason=str(attempt.get("fallback_reason") or "").strip(),
        step_results=attempt.get("step_results") if isinstance(attempt.get("step_results"), list) else [],
        runner_metadata=attempt.get("runner_metadata") if isinstance(attempt.get("runner_metadata"), dict) else {},
        alternate_candidates_considered=attempt.get("alternate_candidates_considered")
        if isinstance(attempt.get("alternate_candidates_considered"), list)
        else [],
    )
    result["repacketization_request"] = attempt.get("repacketization_request") if isinstance(attempt.get("repacketization_request"), dict) else None
    result["file_writes"] = file_writes
    return result


def _strategy_inputs(packet: dict[str, Any]) -> dict[str, Any]:
    return packet.get("strategy_inputs") if isinstance(packet.get("strategy_inputs"), dict) else {}


def _run_command_strategy(
    *,
    packet: dict[str, Any],
    cwd: str | None,
    stage: str,
) -> dict[str, Any]:
    inputs = _strategy_inputs(packet)
    command_text = str(inputs.get("command") or packet.get("execution_command") or "").strip()
    command_list = [str(item).strip() for item in inputs.get("commands", []) if str(item).strip()] if isinstance(inputs.get("commands"), list) else []
    if not command_text:
        return _base_packet_result(
            packet=packet,
            verifier_output="rejected_rework",
            summary=f"Missing command for {packet['packet_id']}.",
            exit_code=1,
            blocked_reason="missing_command",
        )
    commands_to_run = command_list or [command_text]
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    captured_commands: list[dict[str, Any]] = []
    exit_code = 0
    for command in commands_to_run:
        argv = shlex.split(command)
        completed = _run_argv(argv, cwd=str(inputs.get("cwd") or cwd or os.getcwd()))
        exit_code = max(exit_code, int(completed.returncode))
        stdout_chunks.append(completed.stdout.strip())
        stderr_chunks.append(completed.stderr.strip())
        captured_commands.append({"command": command, "exit_code": int(completed.returncode), "stage": stage})
    verifier_output = "accepted" if exit_code == 0 else "rejected_rework"
    stdout_text = "\n".join(chunk for chunk in stdout_chunks if chunk)
    stderr_text = "\n".join(chunk for chunk in stderr_chunks if chunk)
    return _base_packet_result(
        packet=packet,
        verifier_output=verifier_output,
        summary=(stdout_text or stderr_text or f"Executed {packet['packet_id']}").strip(),
        exit_code=exit_code,
        changed_files=[],
        evidence_refs=[f"command://{packet['packet_id']}:{stage}:{idx}" for idx, _ in enumerate(commands_to_run, start=1)],
        captured_commands=captured_commands,
        runner_metadata={"stage": stage, "command_count": len(commands_to_run)},
        stdout=stdout_text,
        stderr=stderr_text,
        result_artifact_path=f"{packet['packet_id']}.{stage}.result.json",
    )


def _run_artifact_transform_strategy(*, packet: dict[str, Any], cwd: str | None) -> dict[str, Any]:
    inputs = _strategy_inputs(packet)
    input_artifacts = [str(item).strip() for item in inputs.get("input_artifacts", []) if str(item).strip()]
    output_artifacts = [str(item).strip() for item in inputs.get("output_artifacts", []) if str(item).strip()]
    workspace = Path(cwd or os.getcwd()).resolve()
    missing_inputs = [path for path in input_artifacts if not (workspace / path).exists() and not Path(path).exists()]
    if missing_inputs:
        return _base_packet_result(
            packet=packet,
            verifier_output="rejected_rework",
            summary=f"Missing input artifacts for {packet['packet_id']}.",
            exit_code=1,
            blocked_reason="missing_artifact_inputs",
            produced_artifacts=[],
            result_artifact_path=f"{packet['packet_id']}.transform.result.json",
        )
    produced = []
    for raw_path in output_artifacts:
        target = Path(raw_path)
        if not target.is_absolute():
            target = workspace / raw_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"packet_id": packet["packet_id"], "source_inputs": input_artifacts}, indent=2) + "\n", encoding="utf-8")
        produced.append(str(target))
    return _base_packet_result(
        packet=packet,
        verifier_output="accepted" if produced else "rejected_rework",
        summary=f"Produced {len(produced)} transform artifacts for {packet['packet_id']}.",
        exit_code=0 if produced else 1,
        evidence_refs=produced,
        produced_artifacts=produced,
        runner_metadata={"transform_kind": str(inputs.get("transform_kind") or "").strip()},
        result_artifact_path=f"{packet['packet_id']}.transform.result.json",
    )


def _run_review_evidence_strategy(*, packet: dict[str, Any], cwd: str | None) -> dict[str, Any]:
    inputs = _strategy_inputs(packet)
    expected_artifacts = [str(item).strip() for item in inputs.get("expected_artifacts", []) if str(item).strip()]
    if not expected_artifacts:
        return _base_packet_result(
            packet=packet,
            verifier_output="rejected_rework",
            summary=f"Missing expected_artifacts for {packet['packet_id']}.",
            exit_code=1,
            blocked_reason="strategy_inputs_invalid",
        )
    workspace = Path(cwd or os.getcwd()).resolve()
    produced: list[str] = []
    for raw_path in expected_artifacts:
        target = Path(raw_path)
        if not target.is_absolute():
            target = workspace / raw_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "packet_id": packet["packet_id"],
                    "review_focus": str(inputs.get("review_focus") or "").strip(),
                    "allowed_scope": packet.get("allowed_scope", []),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        produced.append(str(target))
    return _base_packet_result(
        packet=packet,
        verifier_output="accepted",
        summary=f"Produced reviewer evidence for {packet['packet_id']}.",
        exit_code=0,
        evidence_refs=produced,
        produced_artifacts=produced,
        runner_metadata={"review_focus": str(inputs.get("review_focus") or "").strip()},
        result_artifact_path=f"{packet['packet_id']}.review.result.json",
    )


def _run_multi_command_pipeline_strategy(*, packet: dict[str, Any], cwd: str | None) -> dict[str, Any]:
    inputs = _strategy_inputs(packet)
    commands = [str(item).strip() for item in inputs.get("commands", []) if str(item).strip()]
    if not commands:
        return _base_packet_result(
            packet=packet,
            verifier_output="rejected_rework",
            summary=f"Missing commands for {packet['packet_id']}.",
            exit_code=1,
            blocked_reason="missing_command",
        )
    cwd_value = str(inputs.get("cwd") or cwd or os.getcwd())
    step_results: list[dict[str, Any]] = []
    captured_commands: list[dict[str, Any]] = []
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    exit_code = 0
    for index, command in enumerate(commands, start=1):
        completed = _run_argv(shlex.split(command), cwd=cwd_value)
        step_exit = int(completed.returncode)
        exit_code = max(exit_code, step_exit)
        step_results.append(
            {
                "step": index,
                "command": command,
                "exit_code": step_exit,
                "stdout": completed.stdout.strip(),
                "stderr": completed.stderr.strip(),
            }
        )
        captured_commands.append({"command": command, "exit_code": step_exit, "stage": f"pipeline:{index}"})
        if completed.stdout.strip():
            stdout_chunks.append(completed.stdout.strip())
        if completed.stderr.strip():
            stderr_chunks.append(completed.stderr.strip())
        if step_exit != 0:
            break
    return _base_packet_result(
        packet=packet,
        verifier_output="accepted" if exit_code == 0 else "rejected_rework",
        summary=(stdout_chunks[-1] if stdout_chunks else stderr_chunks[-1] if stderr_chunks else f"Executed pipeline for {packet['packet_id']}"),
        exit_code=exit_code,
        evidence_refs=[f"command://{packet['packet_id']}:pipeline:{idx}" for idx, _ in enumerate(step_results, start=1)],
        captured_commands=captured_commands,
        step_results=step_results,
        runner_metadata={"stage": "multi_command_pipeline", "command_count": len(commands)},
        stdout="\n".join(stdout_chunks),
        stderr="\n".join(stderr_chunks),
        result_artifact_path=f"{packet['packet_id']}.pipeline.result.json",
    )


def _simulation_cycle_result(
    cycle_request: dict[str, Any],
    schedule: dict[str, Any],
    track_id: str,
    cwd: str | None,
) -> dict[str, Any]:
    packet_results = [_packet_result_from_simulation(packet, schedule) for packet in cycle_request.get("packets", [])]
    _apply_file_writes(cwd=cwd, packet_results=packet_results)
    changed_files = sorted({path for item in packet_results for path in item.get("changed_files", []) if isinstance(path, str) and path.strip()})
    evidence_refs = [ref for item in packet_results for ref in item.get("evidence_refs", []) if isinstance(ref, str) and ref.strip()]
    return {
        "schema_version": "cycle-result.v1",
        "track_id": track_id,
        "objective_id": cycle_request.get("objective_id"),
        "cycle_id": cycle_request.get("cycle_id"),
        "packet_results": packet_results,
        "changed_files": changed_files,
        "evidence_refs": evidence_refs,
        "summary": f"Simulated execution for {', '.join(cycle_request.get('packet_ids', []))}.",
        "allowed_scope_status": "within_scope" if all(item.get("allowed_scope_status") == "within_scope" for item in packet_results) else "out_of_scope",
    }


def _default_cycle_result(
    *,
    cycle_request: dict[str, Any],
    track_id: str,
    summary: str,
    exit_code: int,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "cycle-result.v1",
        "track_id": track_id,
        "objective_id": cycle_request.get("objective_id"),
        "cycle_id": cycle_request.get("cycle_id"),
        "packet_results": [
            _base_packet_result(
                packet={"packet_id": packet_id, "execution_strategy": "codex_prompt_worker"},
                exit_code=exit_code,
                verifier_output="rejected_rework",
                allowed_scope_status="within_scope",
                blocked_reason="",
                summary=summary,
                changed_files=[],
                evidence_refs=evidence_refs or [],
                result_artifact_path="",
                retry_mode="same_method",
            )
            for packet_id in cycle_request.get("packet_ids", [])
        ],
        "changed_files": [],
        "evidence_refs": evidence_refs or [],
        "summary": summary,
        "allowed_scope_status": "within_scope",
    }


def _run_real_worker(
    *,
    cycle_request: dict[str, Any],
    track_id: str,
    cwd: str | None,
    codex_home: str | None,
) -> dict[str, Any]:
    real_bin = _resolve_real_bin(codex_home)
    if not real_bin:
        return _default_cycle_result(
            cycle_request=cycle_request,
            track_id=track_id,
            summary="No real Codex binary available for runtime packet execution.",
            exit_code=127,
        )
    result_path = Path(str(cycle_request.get("result_path", "")).strip())
    prompt = str(cycle_request.get("worker_prompt", "")).strip()
    completed = subprocess.run(
        [real_bin, "exec", prompt],
        cwd=cwd or os.getcwd(),
        text=True,
        capture_output=True,
        env={**os.environ, "CODEX_REAL_BIN": real_bin, "CODEX_RUNTIME_REAL_BIN": real_bin},
        check=False,
    )
    if result_path.exists():
        payload = load_json_file(result_path)
        if isinstance(payload, dict):
            return payload
    summary = str(completed.stderr or completed.stdout or "Runtime worker did not emit a result artifact.").strip()
    return _default_cycle_result(
        cycle_request=cycle_request,
        track_id=track_id,
        summary=summary,
        exit_code=int(completed.returncode),
        evidence_refs=[item for item in [completed.stdout.strip(), completed.stderr.strip()] if item],
    )


def _run_codex_prompt_strategy(
    *,
    packet: dict[str, Any],
    cycle_request: dict[str, Any],
    track_id: str,
    cwd: str | None,
    codex_home: str | None,
) -> dict[str, Any]:
    expected_artifacts = _strategy_inputs(packet).get("expected_artifacts")
    if not isinstance(expected_artifacts, list) or not [item for item in expected_artifacts if str(item).strip()]:
        return _base_packet_result(
            packet=packet,
            verifier_output="rejected_rework",
            summary=f"Missing expected_artifacts for {packet['packet_id']}.",
            exit_code=1,
            blocked_reason="strategy_inputs_invalid",
        )
    single_request = dict(cycle_request)
    single_request["packet_ids"] = [packet["packet_id"]]
    single_request["packets"] = [packet]
    prompt = (
        f"Execute packet {packet['packet_id']} using codex_prompt_worker.\n"
        f"Goal: {str(_strategy_inputs(packet).get('worker_goal') or '').strip()}\n"
        f"Allowed scope: {', '.join(packet.get('allowed_scope', [])) or '(none)'}\n"
        f"Expected artifacts: {', '.join(str(item) for item in expected_artifacts)}\n"
        f"Write normalized cycle-result JSON to {cycle_request.get('result_path')}."
    )
    single_request["worker_prompt"] = prompt
    result = _run_real_worker(cycle_request=single_request, track_id=track_id, cwd=cwd, codex_home=codex_home)
    normalized = result.get("packet_results") if isinstance(result.get("packet_results"), list) else []
    if normalized:
        item = normalized[0]
        item["strategy_name"] = "codex_prompt_worker"
        item["runner_kind"] = "codex"
        item["fallback_used"] = True
        item["fallback_reason"] = str(packet.get("fallback_reason") or "").strip()
        item["result_artifact_path"] = str(item.get("result_artifact_path") or f"{packet['packet_id']}.codex.result.json")
        item["evidence_refs"] = item.get("evidence_refs") if isinstance(item.get("evidence_refs"), list) else []
    return result


def dispatch_packet_by_strategy(
    *,
    packet: dict[str, Any],
    cycle_request: dict[str, Any],
    schedule: dict[str, Any],
    track_id: str,
    cwd: str | None,
    codex_home: str | None,
) -> dict[str, Any]:
    strategy_name = str(packet.get("execution_strategy") or "").strip()
    if packet.get("simulation"):
        return _packet_result_from_simulation(packet, schedule)
    if strategy_name == "command_capture":
        return _run_command_strategy(packet=packet, cwd=cwd, stage="command")
    if strategy_name == "test_command":
        return _run_command_strategy(packet=packet, cwd=cwd, stage=f"test:{_strategy_inputs(packet).get('test_lane', '')}")
    if strategy_name in {"validation_command", "lint_command", "typecheck_command", "build_command", "smoke_command", "schema_check_command"}:
        return _run_command_strategy(packet=packet, cwd=cwd, stage=f"validation:{_strategy_inputs(packet).get('validation_lane', '')}")
    if strategy_name == "artifact_transform":
        return _run_artifact_transform_strategy(packet=packet, cwd=cwd)
    if strategy_name == "review_evidence_packet":
        return _run_review_evidence_strategy(packet=packet, cwd=cwd)
    if strategy_name == "multi_command_pipeline":
        return _run_multi_command_pipeline_strategy(packet=packet, cwd=cwd)
    if strategy_name == "codex_prompt_worker":
        single_result = _run_codex_prompt_strategy(
            packet=packet,
            cycle_request=cycle_request,
            track_id=track_id,
            cwd=cwd,
            codex_home=codex_home,
        )
        packet_results = single_result.get("packet_results") if isinstance(single_result.get("packet_results"), list) else []
        return packet_results[0] if packet_results else _base_packet_result(
            packet=packet,
            verifier_output="rejected_rework",
            summary=f"Codex prompt execution failed for {packet['packet_id']}.",
            exit_code=1,
            blocked_reason="codex_prompt_failure",
            fallback_used=True,
            fallback_reason=str(packet.get("fallback_reason") or "").strip(),
        )
    return _base_packet_result(
        packet=packet,
        verifier_output="rejected_rework",
        summary=f"Unknown execution strategy {strategy_name}.",
        exit_code=1,
        blocked_reason="strategy_unknown",
    )


def _execute_worker(
    *,
    cycle_request: dict[str, Any],
    schedule: dict[str, Any],
    track_id: str,
    cwd: str | None,
    codex_home: str | None,
) -> dict[str, Any]:
    packet_results = [
        dispatch_packet_by_strategy(
            packet=packet,
            cycle_request=cycle_request,
            schedule=schedule,
            track_id=track_id,
            cwd=cwd,
            codex_home=codex_home,
        )
        for packet in cycle_request.get("packets", [])
        if isinstance(packet, dict)
    ]
    _apply_file_writes(cwd=cwd, packet_results=packet_results)
    changed_files = sorted({path for item in packet_results for path in item.get("changed_files", []) if isinstance(path, str) and path.strip()})
    evidence_refs = [ref for item in packet_results for ref in item.get("evidence_refs", []) if isinstance(ref, str) and ref.strip()]
    return {
        "schema_version": "cycle-result.v1",
        "track_id": track_id,
        "objective_id": cycle_request.get("objective_id"),
        "cycle_id": cycle_request.get("cycle_id"),
        "packet_results": packet_results,
        "changed_files": changed_files,
        "evidence_refs": evidence_refs,
        "summary": f"Executed {len(packet_results)} strategy-dispatched packets.",
        "allowed_scope_status": "within_scope" if all(item.get("allowed_scope_status") == "within_scope" for item in packet_results) else "out_of_scope",
    }


def _persist_runtime_packets(*, artifacts_root: Path, track_id: str, packets: dict[str, dict[str, Any]]) -> None:
    for packet_id, packet in packets.items():
        write_json_file(
            packet_definition_path(artifacts_root=artifacts_root, track_id=track_id, packet_id=packet_id),
            packet,
        )




def bootstrap_runtime(
    *,
    plan_payload: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    artifacts_root: Path,
    track_id: str,
    cwd: str | None = None,
    controller_mode: str | None = None,
) -> dict[str, Any]:
    resolved_plan = _resolve_plan_payload(plan_payload=plan_payload, plan=plan)
    runtime_paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    if runtime_paths["packet_dag"].exists() or runtime_paths["status"].exists() or runtime_paths["schedule"].exists():
        raise RuntimeError("runtime_state_already_exists")

    payloads = build_runtime_bootstrap_artifacts(resolved_plan, track_id=track_id, cwd=cwd)
    packet_quality = payloads.get("packet_quality") if isinstance(payloads.get("packet_quality"), dict) else {}
    quality_budget = packet_quality.get("budget") if isinstance(packet_quality.get("budget"), dict) else {}
    if packet_quality.get("hard_fail_packet_ids") or str(quality_budget.get("status") or "") == "hard_fail":
        raise RuntimeError("packet_quality_gate_failed")
    session_paths = session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    write_json_file(session_paths["feature_list"], payloads["feature_list"])
    _write_jsonl(runtime_paths["adaptation_log"], [])
    write_json_file(
        runtime_paths["invalid_transition"],
        {
            "schema_version": "invalid-transition.v1",
            "track_id": track_id,
            "step_id": "",
            "errors": [],
            "timestamp": "",
        },
    )
    _append_progress_events(artifacts_root=artifacts_root, track_id=track_id, events=payloads["progress_events"])
    _persist_runtime_packets(
        artifacts_root=artifacts_root,
        track_id=track_id,
        packets=_packet_map(payloads["packet_dag"]),
    )

    def _bootstrap_body() -> dict[str, Any]:
        write_json_file(session_paths["momentum"], payloads["momentum"])
        write_json_file(session_paths["blockers"], payloads["blockers"])
        _write_runtime_state(
            artifacts_root=artifacts_root,
            track_id=track_id,
            packet_dag=payloads["packet_dag"],
            status=payloads["status"],
            schedule=payloads["schedule"],
        )
        _write_runtime_supporting_state(
            artifacts_root=artifacts_root,
            track_id=track_id,
            summary=payloads["summary"],
            validation_plan=payloads["validation_plan"],
            repo_capabilities=payloads["repo_capabilities"],
            packet_quality=payloads["packet_quality"],
        )
        write_json_file(runtime_paths["execution_plan"], payloads["execution_plan"])
        _write_kernel_runtime_state(
            artifacts_root=artifacts_root,
            track_id=track_id,
            payload=payloads["kernel_runtime_state"],
        )
        _write_jsonl(runtime_paths["packet_results"], [])
        _write_jsonl(runtime_paths["transition_history"], payloads.get("transition_history", []))
        _write_jsonl(runtime_paths["verification_results"], payloads.get("verification_results", []))
        _update_execution_ledger(
            artifacts_root=artifacts_root,
            track_id=track_id,
            packets=_packet_map(payloads["packet_dag"]),
        )
        _sync_runtime_summary(
            plan_payload=resolved_plan,
            artifacts_root=artifacts_root,
            track_id=track_id,
            status=payloads["status"],
            schedule=payloads["schedule"],
            packets=_packet_map(payloads["packet_dag"]),
            cwd=cwd,
            controller_mode=controller_mode,
        )
        checkpoint = load_json_file(session_paths["checkpoint"])
        checkpoint.update(payloads["checkpoint_updates"])
        checkpoint["checkpoint_strategy"] = "git_checkpoint_required"
        checkpoint["checkpoint_attempted_at"] = ""
        checkpoint["rollback_validation_ref"] = ""
        checkpoint["checkpoint_blocked"] = True
        checkpoint["checkpoint_commit"] = ""
        checkpoint["checkpoint_block_reason"] = "checkpoint_not_attempted"
        checkpoint["checkpoint_block_evidence"] = "No verifier-accepted packet has been checkpointed yet."
        write_json_file(session_paths["checkpoint"], checkpoint)
        _sync_operator_view(artifacts_root=artifacts_root, track_id=track_id)
        verdict = _controller_verdict(
            artifacts_root=artifacts_root,
            track_id=track_id,
            controller_mode=controller_mode,
        )
        kernel_errors = validate_state(
            state=_load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id),
            execution_plan=_load_execution_plan(artifacts_root=artifacts_root, track_id=track_id),
            validation_plan=_load_json_if_exists(
                runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["validation_plan"]
            ),
        )
        if kernel_errors:
            _force_kernel_unsafe(
                artifacts_root=artifacts_root,
                track_id=track_id,
                kernel_state=_load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id),
                errors=kernel_errors,
                step_id="bootstrap-validate",
            )
            verdict = {
                "status": "blocked",
                "reason_code": "KERNEL_BOOTSTRAP_INVALID",
                "blocked_fields": kernel_errors,
            }
        _sync_runtime_state_record(
            artifacts_root=artifacts_root,
            track_id=track_id,
            lifecycle_status=str(verdict.get("status") or "running"),
        )
        verdict["message"] = "Runtime state bootstrapped."
        return verdict

    return _run_in_runtime_transaction(
        artifacts_root=artifacts_root,
        track_id=track_id,
        step_id="bootstrap",
        cycle_state_path=None,
        body=_bootstrap_body,
    )


def _terminal_status(
    *,
    packets: dict[str, dict[str, Any]],
    frontier: list[str],
    boundary_shrunk_remainder: list[str] | None = None,
    migration_fallback_used: bool = False,
    blocked: bool = False,
    checkpoint_ready: bool = False,
) -> str:
    closure = evaluate_objective_closure(
        packets=list(packets.values()),
        boundary_shrunk_remainder=boundary_shrunk_remainder or [],
        migration_fallback_used=migration_fallback_used,
    )["closure_state"]
    if closure == "OBJECTIVE_COMPLETE" and checkpoint_ready:
        return closure
    if closure == "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK":
        return closure
    if closure == "OBJECTIVE_BLOCKED_MIGRATION_DEFECT":
        return closure
    if blocked or not frontier:
        return "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED"
    if closure == "OBJECTIVE_REJECTED_FALSE_COMPLETION":
        return closure
    return ""


def _write_packet_verdict_artifacts(
    *,
    artifacts_root: Path,
    track_id: str,
    verdicts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    written: list[dict[str, Any]] = []
    for verdict in verdicts:
        packet_id = str(verdict.get("packet_id", "")).strip()
        if not packet_id:
            continue
        path = packet_verdict_path(artifacts_root=artifacts_root, track_id=track_id, packet_id=packet_id)
        payload = {
            "packet_id": packet_id,
            "strategy_name": verdict.get("strategy_name"),
            "runner_kind": verdict.get("runner_kind"),
            "runtime_state": verdict.get("runtime_state"),
            "verifier_output": verdict.get("verifier_output"),
            "allowed_scope_status": verdict.get("allowed_scope_status"),
            "summary": verdict.get("summary"),
            "changed_files": verdict.get("changed_files", []),
            "evidence_refs": verdict.get("evidence_refs", []),
            "captured_commands": verdict.get("captured_commands", []),
            "produced_artifacts": verdict.get("produced_artifacts", []),
            "fallback_used": verdict.get("fallback_used") is True,
            "fallback_reason": str(verdict.get("fallback_reason") or "").strip(),
            "step_results": verdict.get("step_results", []) if isinstance(verdict.get("step_results"), list) else [],
            "support_status": str(verdict.get("support_status") or "").strip(),
            "unsupported_risk_reason": str(verdict.get("unsupported_risk_reason") or "").strip(),
            "artifact_path": str(path),
        }
        write_json_file(path, payload)
        written.append(payload)
    return written


def _capture_worker_result(
    *,
    artifacts_root: Path,
    track_id: str,
    cycle_request: dict[str, Any],
    cycle_result: dict[str, Any],
    cwd: str,
) -> None:
    cycle_id = str(cycle_request.get("cycle_id", "")).strip() or "cycle"
    name = f"worker-{_sanitize_capture_suffix(cycle_id)}"
    if _capture_exists(artifacts_root=artifacts_root, track_id=track_id, name=name):
        return
    stdout_text = "\n".join(
        str(item.get("stdout", "")).strip()
        for item in cycle_result.get("packet_results", [])
        if isinstance(item, dict) and str(item.get("stdout", "")).strip()
    )
    stderr_text = "\n".join(
        str(item.get("stderr", "")).strip()
        for item in cycle_result.get("packet_results", [])
        if isinstance(item, dict) and str(item.get("stderr", "")).strip()
    )
    if not stdout_text:
        stdout_text = str(cycle_result.get("summary", "")).strip()
    _runtime_capture(
        artifacts_root=artifacts_root,
        track_id=track_id,
        name=name,
        stage="tests",
        cwd=cwd,
        command_argv=["objective_runtime.py", "worker-cycle", cycle_id],
        exit_code=max(
            int(item.get("exit_code", 0) or 0)
            for item in cycle_result.get("packet_results", [])
            if isinstance(item, dict)
        )
        if cycle_result.get("packet_results")
        else 1,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
    )


def _capture_verifier_result(
    *,
    artifacts_root: Path,
    track_id: str,
    cycle_request: dict[str, Any],
    review: dict[str, Any],
    cwd: str,
) -> None:
    cycle_id = str(cycle_request.get("cycle_id", "")).strip() or "cycle"
    name = f"verifier-{_sanitize_capture_suffix(cycle_id)}"
    if _capture_exists(artifacts_root=artifacts_root, track_id=track_id, name=name):
        return
    _runtime_capture(
        artifacts_root=artifacts_root,
        track_id=track_id,
        name=name,
        stage="tests",
        cwd=cwd,
        command_argv=["verify_cycle.py", "--cycle-id", cycle_id],
        exit_code=0 if not review.get("blocked_fields") else 1,
        stdout_text=json.dumps(review, sort_keys=True),
        stderr_text="\n".join(str(item) for item in review.get("blocked_fields", [])),
    )


def _capture_cycle_log(
    *,
    artifacts_root: Path,
    track_id: str,
    cycle_id: str,
    cwd: str,
    verdicts: list[dict[str, Any]],
    frontier_reason: str,
    closure_state: str,
) -> None:
    name = f"log-{_sanitize_capture_suffix(cycle_id)}"
    if _capture_exists(artifacts_root=artifacts_root, track_id=track_id, name=name):
        return
    lines = [f"cycle_id={cycle_id}", f"frontier_movement_reason={frontier_reason}", f"closure_state={closure_state}"]
    for verdict in verdicts:
        lines.append(
            f"{verdict.get('packet_id','')}:"
            f"{verdict.get('runtime_state','')}:{verdict.get('verifier_output','')}"
        )
    _runtime_capture(
        artifacts_root=artifacts_root,
        track_id=track_id,
        name=name,
        stage="logs",
        cwd=cwd,
        command_argv=["objective_runtime.py", "cycle-log", cycle_id],
        exit_code=0,
        stdout_text="\n".join(lines),
        stderr_text="",
    )


def _terminal_test_commands(plan_payload: dict[str, Any]) -> list[tuple[str, str]]:
    tests = plan_payload.get("tests") if isinstance(plan_payload.get("tests"), dict) else {}
    commands: list[tuple[str, str]] = []
    for lane in ("unit", "integration", "regression"):
        for idx, command in enumerate(tests.get(lane) or [], start=1):
            if not isinstance(command, str) or not command.strip():
                continue
            suffix = lane if idx == 1 else f"{lane}-{idx}"
            commands.append((f"test-{_sanitize_capture_suffix(suffix)}", command.strip()))
    if commands:
        return commands
    session_harness = plan_payload.get("session_harness") if isinstance(plan_payload.get("session_harness"), dict) else {}
    for idx, command in enumerate(session_harness.get("validation_commands") or [], start=1):
        if not isinstance(command, str) or not command.strip():
            continue
        suffix = "validation" if idx == 1 else f"validation-{idx}"
        commands.append((f"test-{_sanitize_capture_suffix(suffix)}", command.strip()))
    return commands


def _emit_terminal_captures(
    *,
    plan_payload: dict[str, Any],
    artifacts_root: Path,
    track_id: str,
    cwd: str,
    checkpoint_meta: dict[str, Any],
) -> None:
    tests = plan_payload.get("tests") if isinstance(plan_payload.get("tests"), dict) else {}
    smoke_gates = tests.get("smoke_gates") if isinstance(tests.get("smoke_gates"), list) else []
    for gate in smoke_gates:
        if not isinstance(gate, dict):
            continue
        stage = str(gate.get("stage", "")).strip()
        commands = [str(item).strip() for item in gate.get("commands", []) if str(item).strip()]
        if not stage or not commands:
            continue
        _capture_command_sequence(
            artifacts_root=artifacts_root,
            track_id=track_id,
            name=f"smoke-{stage.replace('%', '')}",
            stage=stage,
            commands=commands,
            cwd=cwd,
        )
    for name, command in _terminal_test_commands(plan_payload):
        _ensure_capture_command(
            artifacts_root=artifacts_root,
            track_id=track_id,
            name=name,
            stage="tests",
            command_text=command,
            cwd=cwd,
        )
    if not checkpoint_meta.get("rollback_validation_ref"):
        proof = _runtime_capture(
            artifacts_root=artifacts_root,
            track_id=track_id,
            name=_rollback_capture_name(str(checkpoint_meta.get("checkpoint_id") or track_id)),
            stage="rollback",
            cwd=cwd,
            command_argv=["objective_runtime.py", "rollback", "blocked"],
            exit_code=1,
            stdout_text="",
            stderr_text="rollback validation was not available",
        )
        checkpoint_meta["rollback_validation_ref"] = proof["proof_artifact"]
        checkpoint_meta["rollback_validation"] = {
            "executed": False,
            "result": "blocked",
            "evidence": "rollback validation was not available",
            **proof,
        }


def _rebuild_schedule(
    *,
    track_id: str,
    packets: dict[str, dict[str, Any]],
    previous_schedule: dict[str, Any],
    previous_frontier: list[str],
    verdicts: list[dict[str, Any]],
    review: dict[str, Any],
) -> tuple[dict[str, Any], list[str], str, str]:
    accepted_packets = {
        packet_id for packet_id, packet in packets.items() if str(packet.get("runtime_state", "")).strip() == "accepted"
    }
    blocked_set = [
        packet_id for packet_id, packet in packets.items() if str(packet.get("runtime_state", "")).strip() == "escalated"
    ]
    schedule = build_schedule(
        objective_id=stable_objective_id(track_id),
        packets=packets,
        accepted_packets=accepted_packets,
        active_packets=set(),
        retry_counters=previous_schedule.get("retry_counters") if isinstance(previous_schedule.get("retry_counters"), dict) else {},
        max_parallel_packets=int(previous_schedule.get("max_parallel_packets", 1) or 1),
        parallelism_policy=str(previous_schedule.get("parallelism_policy") or "bounded_parallel"),
        execution_shape=str(previous_schedule.get("execution_shape") or "single_lane"),
        lane_caps=previous_schedule.get("lane_caps") if isinstance(previous_schedule.get("lane_caps"), dict) else {},
        route_swarm_cap=int(previous_schedule.get("route_swarm_cap", 0) or 0) if previous_schedule.get("route_swarm_cap") is not None else None,
        frontier_dispatch_order=previous_schedule.get("frontier_dispatch_order") if isinstance(previous_schedule.get("frontier_dispatch_order"), list) else [],
        reviewer_barrier_points=previous_schedule.get("reviewer_barrier_points") if isinstance(previous_schedule.get("reviewer_barrier_points"), list) else [],
        convergence_required_for_closure=previous_schedule.get("convergence_required_for_closure") is True,
        blocked_set=blocked_set,
        dispatch_history=list(previous_schedule.get("dispatch_history") or []),
        previous_frontier=previous_frontier,
        cycle_log=list(previous_schedule.get("cycle_log") or []),
        max_same_strategy_retries=int(previous_schedule.get("max_same_strategy_retries", 2) or 2),
        max_noop_cycles=int(previous_schedule.get("max_noop_cycles", 2) or 2),
        max_verifier_return_cycles=int(previous_schedule.get("max_verifier_return_cycles", 2) or 2),
        max_no_frontier_movement_cycles=int(previous_schedule.get("max_no_frontier_movement_cycles", 2) or 2),
    )
    schedule["runtime_states"] = {packet_id: packet.get("runtime_state", "queued") for packet_id, packet in packets.items()}
    frontier = list(schedule.get("current_frontier") or [])
    reason = infer_frontier_movement_reason(
        previous_frontier=previous_frontier,
        current_frontier=frontier,
        verdicts=verdicts,
        repacketized=bool(review.get("repacketization_requests")),
        escalation_required=bool(review.get("escalation_required")),
    )
    classification = classify_cycle_outcome(
        verdicts=verdicts,
        frontier_movement=bool(schedule.get("frontier_movement")),
        escalation_required=bool(review.get("escalation_required")),
    )
    schedule["frontier_movement_reason"] = reason
    schedule["repacketization_events"] = list(previous_schedule.get("repacketization_events") or []) + list(review.get("repacketization_requests") or [])
    schedule["repacketization_count"] = len(schedule["repacketization_events"])
    schedule["rejected_packet_count"] = sum(
        1 for verdict in verdicts if str(verdict.get("verifier_output", "")).strip() == "rejected_rework"
    )
    schedule["blocked_packet_count"] = len(blocked_set)
    schedule["accepted_packet_count"] = len(accepted_packets)
    schedule["total_runtime_attempts"] = sum(
        int(counter.get("same_method_attempts", 0) or 0) + int(counter.get("alternate_strategy_attempts", 0) or 0)
        for counter in (schedule.get("retry_counters") or {}).values()
        if isinstance(counter, dict)
    )
    schedule["escalation_count"] = int(previous_schedule.get("escalation_count", 0) or 0) + (
        1 if review.get("escalation_required") else 0
    )
    schedule["noop_cycle_count"] = int(previous_schedule.get("noop_cycle_count", 0) or 0) + (
        1 if classification == "invalid_noop" else 0
    )
    schedule["verifier_return_cycle_count"] = int(previous_schedule.get("verifier_return_cycle_count", 0) or 0) + (
        1 if any(str(item.get("verifier_output", "")).strip() == "rejected_rework" for item in verdicts) else 0
    )
    schedule["no_frontier_movement_cycle_count"] = int(
        previous_schedule.get("no_frontier_movement_cycle_count", 0) or 0
    ) + (1 if not schedule.get("frontier_movement") and classification != "uncertainty_reducing" else 0)
    schedule["updated_at"] = now_iso()
    return schedule, frontier, reason, classification


def _create_cycle_request(
    *,
    artifacts_root: Path,
    track_id: str,
    packet_dag: dict[str, Any],
    schedule: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Path]]:
    packets = _packet_map(packet_dag)
    objective_id = stable_objective_id(track_id)
    cycle_id = _next_cycle_id(schedule, objective_id)
    paths = _current_cycle_paths(artifacts_root=artifacts_root, track_id=track_id, cycle_id=cycle_id)
    packet_ids = list(schedule.get("current_frontier") or [])
    payload = {
        "schema_version": "cycle-request.v1",
        "track_id": track_id,
        "objective_id": objective_id,
        "cycle_id": cycle_id,
        "packet_ids": packet_ids,
        "purpose": "Advance the current executable frontier.",
        "expected_evidence": ["packet verdicts", "frontier movement", "artifact updates"],
        "closure_impact": "Move the objective toward verifier-accepted closure.",
        "strategy_label": "runtime_dispatch",
        "stop_condition": "Worker result artifact written.",
        "pivot_condition": "Verifier returns structural failure or retry budget is exhausted.",
        "escalation_condition": "No safe momentum remains or an authority boundary blocks required work.",
        "packets": [packets[packet_id] for packet_id in packet_ids if packet_id in packets],
        "result_path": str(paths["result"]),
        "review_path": str(paths["review"]),
    }
    payload["worker_prompt"] = _render_worker_prompt(cycle_request=payload, result_path=paths["result"])
    state = {
        "schema_version": "cycle-state.v1",
        "track_id": track_id,
        "cycle_id": cycle_id,
        "phase": "requested",
        "requested_at": now_iso(),
        "executed_at": "",
        "reviewed_at": "",
        "applied_at": "",
        "applied_revision": 0,
    }
    write_json_file(paths["request"], payload)
    write_json_file(paths["state"], state)
    return payload, state, paths


def _inject_risk_validation_packets(
    *,
    packets: dict[str, dict[str, Any]],
    verdicts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    changed_files = sorted(
        {
            str(path).strip()
            for verdict in verdicts
            if isinstance(verdict, dict)
            for path in verdict.get("changed_files", [])
            if str(path).strip()
        }
    )
    if not changed_files:
        return []
    lower_paths = [path.lower() for path in changed_files]
    injected: list[dict[str, Any]] = []
    has_security_packet = any(
        "security_sensitive_review" in str(packet.get("packet_id", "")).strip()
        or str(packet.get("execution_strategy", "")).strip() == "review_evidence_packet"
        for packet in packets.values()
    )
    if not has_security_packet and any(token in path for path in lower_paths for token in ("auth", "billing", "secret", "token", "payment")):
        packet_id = "packet-validation-security-sensitive-runtime"
        if packet_id not in packets:
            injected.append(
                {
                    "packet_id": packet_id,
                    "primary_behavior": "Produce explicit security-sensitive review evidence after risky surfaces changed during execution.",
                    "execution_strategy": "review_evidence_packet",
                    "strategy_inputs": {
                        "review_focus": "Runtime changed security-sensitive surfaces; emit reviewer evidence before closure.",
                        "expected_artifacts": [f"{packet_id}.review.json"],
                    },
                    "execution_mode": "sequence_required",
                    "allowed_scope": changed_files,
                    "dependencies": [],
                    "dependency_mode": "accepted_upstream",
                    "acceptance_checks": ["security-sensitive reviewer evidence emitted"],
                    "failure_signals": ["security-sensitive reviewer evidence missing"],
                    "constraints": ["Read-only evidence packet."],
                    "fallback_or_rollback": "Escalate with explicit blocker evidence.",
                    "verifier_mapping": f"cycle-verifier:{packet_id}",
                    "evidence_destination": f"planning_artifacts/<track-id>/packets/{packet_id}.verdict.json",
                    "shared_surface_categories": ["security-sensitive-review"],
                    "classification": "ready",
                    "packet_class": "review",
                    "product_meaning_resolved": True,
                    "automatable_acceptance": True,
                    "prohibited_action_required": False,
                    "maintainable_completion_path": True,
                    "definition_of_done": {
                        "behavior_outcome": "Security-sensitive runtime evidence exists.",
                        "acceptance_checks": ["security-sensitive reviewer evidence emitted"],
                        "evidence_requirements": [f"{packet_id}.review.json"],
                        "allowed_scope": changed_files,
                        "rollback_or_fallback": "Escalate with explicit blocker evidence.",
                        "verifier_acceptance_condition": "Review artifact exists and is non-empty.",
                        "objective_linkage": "runtime-adaptation",
                    },
                }
            )
    return injected


def _load_cycle_request(paths: dict[str, Path]) -> dict[str, Any]:
    return load_json_file(paths["request"])


def _apply_review_once(
    *,
    plan_payload: dict[str, Any],
    artifacts_root: Path,
    track_id: str,
    packet_dag: dict[str, Any],
    status: dict[str, Any],
    schedule: dict[str, Any],
    cycle_id: str,
    cycle_state: dict[str, Any],
    cycle_paths: dict[str, Path],
    cwd: str | None,
    controller_mode: str | None,
) -> dict[str, Any]:
    review = load_json_file(cycle_paths["review"])
    packets = _packet_map(packet_dag)
    previous_frontier = list(schedule.get("current_frontier") or [])
    updated_packets, updated_counters, adaptation_events = apply_cycle_review(
        packets=packets,
        retry_counters=schedule.get("retry_counters") if isinstance(schedule.get("retry_counters"), dict) else {},
        review=review,
    )
    review_verdicts = review.get("packet_verdicts") if isinstance(review.get("packet_verdicts"), list) else []
    verdicts = _write_packet_verdict_artifacts(
        artifacts_root=artifacts_root,
        track_id=track_id,
        verdicts=review_verdicts,
    )
    verdict_ids = {str(item.get("packet_id", "")).strip() for item in verdicts if str(item.get("packet_id", "")).strip()}
    expected_verdict_ids = {
        str(item.get("packet_id", "")).strip()
        for item in review_verdicts
        if isinstance(item, dict) and str(item.get("packet_id", "")).strip()
    }
    if verdict_ids != expected_verdict_ids:
        return {
            "status": "blocked",
            "reason_code": "PACKET_VERDICT_ARTIFACTS_MISSING",
            "blocked_fields": [f"packet_verdict_artifacts:mismatch:{sorted(expected_verdict_ids - verdict_ids)}"],
        }
    injected_packets = _inject_risk_validation_packets(packets=updated_packets, verdicts=verdicts)
    if injected_packets:
        for packet in injected_packets:
            updated_packets[str(packet.get("packet_id") or "").strip()] = packet
        adaptation_events.extend(
            {
                "packet_id": str(packet.get("packet_id") or "").strip(),
                "old_strategy": "",
                "new_strategy": str(packet.get("execution_strategy") or "").strip(),
                "trigger_evidence": "runtime changed risky surfaces",
                "policy_basis": "risk_surface_injection",
                "frontier_effect": "new_validation_packet_added",
                "verifier_impact": "pending",
            }
            for packet in injected_packets
        )
    schedule["retry_counters"] = updated_counters
    rebuilt_schedule, frontier, frontier_reason, classification = _rebuild_schedule(
        track_id=track_id,
        packets=updated_packets,
        previous_schedule=schedule,
        previous_frontier=previous_frontier,
        verdicts=verdicts,
        review=review,
    )
    rebuilt_schedule["dispatch_history"] = list(schedule.get("dispatch_history") or []) + [
        {"cycle_id": cycle_id, "packet_ids": [item["packet_id"] for item in verdicts]}
    ]
    rebuilt_schedule["cycle_log"] = list(schedule.get("cycle_log") or []) + [
        {
            "cycle_id": cycle_id,
            "objective_id": stable_objective_id(track_id),
            "packet_ids": [item["packet_id"] for item in verdicts],
            "purpose": "Apply verifier-approved packet outcomes.",
            "expected_evidence": ["packet verdicts", "frontier movement", "checkpoint update"],
            "closure_impact": "Advance objective closure or isolate the blocker.",
            "strategy_label": "runtime_apply_review",
            "stop_condition": "Review applied and frontier recomputed.",
            "pivot_condition": "Retry budget exhausted or structural failure persists.",
            "escalation_condition": "No safe momentum remains.",
            "classification": classification,
            "frontier_movement": rebuilt_schedule.get("frontier_movement"),
            "frontier_movement_reason": frontier_reason,
        }
    ]
    blocked_by_budget = (
        int(rebuilt_schedule.get("noop_cycle_count", 0) or 0) >= int(rebuilt_schedule.get("max_noop_cycles", 2) or 2)
        or int(rebuilt_schedule.get("no_frontier_movement_cycle_count", 0) or 0)
        >= int(rebuilt_schedule.get("max_no_frontier_movement_cycles", 2) or 2)
    )
    changed_files = sorted(
        {
            str(path).strip()
            for packet in updated_packets.values()
            if str(packet.get("runtime_state", "")).strip() == "accepted"
            for path in packet.get("last_changed_files", [])
            if str(path).strip()
        }
    )
    current_cycle_changed_files = sorted(
        {
            str(path).strip()
            for verdict in verdicts
            if str(verdict.get("runtime_state", "")).strip() == "accepted"
            for path in verdict.get("changed_files", [])
            if str(path).strip()
        }
    )
    accepted_packets = {
        packet_id
        for packet_id, packet in updated_packets.items()
        if str(packet.get("runtime_state", "")).strip() == "accepted"
    }
    current_cycle_accepted = any(str(verdict.get("runtime_state", "")).strip() == "accepted" for verdict in verdicts)
    prior_checkpoint = load_json_file(session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["checkpoint"])
    if current_cycle_accepted:
        if (
            not current_cycle_changed_files
            and prior_checkpoint.get("checkpoint_blocked") is False
            and str(prior_checkpoint.get("checkpoint_commit") or "").strip()
        ):
            repo_root = _git_root(cwd)
            repo_paths = _repo_relative_paths(repo_root=repo_root, cwd=cwd, changed_files=changed_files) if repo_root else set()
            dirty_paths = set(_status_paths(repo_root)) if repo_root else set()
            runtime_prefix = (
                _runtime_managed_repo_prefix(repo_root=repo_root, artifacts_root=artifacts_root, track_id=track_id)
                if repo_root
                else None
            )
            unrelated_dirty = sorted(
                path
                for path in dirty_paths
                if path not in repo_paths
                and not (runtime_prefix and (path == runtime_prefix or path.startswith(f"{runtime_prefix}/")))
            )
            if unrelated_dirty:
                checkpoint_meta = {
                    "checkpoint_strategy": "git_checkpoint_required",
                    "checkpoint_attempted_at": now_iso(),
                    "checkpoint_commit": "",
                    "checkpoint_blocked": True,
                    "checkpoint_block_reason": "unrelated_dirty_state",
                    "checkpoint_block_evidence": ", ".join(unrelated_dirty),
                    "rollback_validation_ref": str(prior_checkpoint.get("rollback_validation_ref") or ""),
                    "rollback_validation": {
                        "executed": bool(prior_checkpoint.get("rollback_validation_ref")),
                        "result": "blocked",
                        "evidence": f"unrelated dirty state: {', '.join(unrelated_dirty)}",
                        "proof_artifact": str(prior_checkpoint.get("rollback_validation_ref") or ""),
                    },
                }
            else:
                checkpoint_meta = {
                    "checkpoint_strategy": str(prior_checkpoint.get("checkpoint_strategy") or "git_checkpoint_required"),
                    "checkpoint_attempted_at": str(prior_checkpoint.get("checkpoint_attempted_at") or ""),
                    "checkpoint_commit": str(prior_checkpoint.get("checkpoint_commit") or ""),
                    "checkpoint_blocked": False,
                    "checkpoint_block_reason": "",
                    "checkpoint_block_evidence": "Reused prior successful checkpoint; current cycle added no new staged repo delta.",
                    "rollback_validation_ref": str(prior_checkpoint.get("rollback_validation_ref") or ""),
                    "rollback_validation": {
                        "executed": bool(prior_checkpoint.get("rollback_validation_ref")),
                        "result": "pass",
                        "evidence": "Reused prior successful checkpoint after evidence-only acceptance.",
                        "proof_artifact": str(prior_checkpoint.get("rollback_validation_ref") or ""),
                    },
                }
        else:
            checkpoint_meta = _attempt_git_checkpoint(
                artifacts_root=artifacts_root,
                track_id=track_id,
                cycle_id=cycle_id,
                cwd=cwd,
                changed_files=changed_files,
            )
            if (
                checkpoint_meta.get("checkpoint_block_reason") == "no_staged_changes"
                and prior_checkpoint.get("checkpoint_blocked") is False
                and str(prior_checkpoint.get("checkpoint_commit") or "").strip()
            ):
                checkpoint_meta = {
                    "checkpoint_strategy": str(prior_checkpoint.get("checkpoint_strategy") or "git_checkpoint_required"),
                    "checkpoint_attempted_at": str(prior_checkpoint.get("checkpoint_attempted_at") or ""),
                    "checkpoint_commit": str(prior_checkpoint.get("checkpoint_commit") or ""),
                    "checkpoint_blocked": False,
                    "checkpoint_block_reason": "",
                    "checkpoint_block_evidence": "Reused prior successful checkpoint; current cycle added no new staged repo delta.",
                    "rollback_validation_ref": str(prior_checkpoint.get("rollback_validation_ref") or ""),
                    "rollback_validation": {
                        "executed": bool(prior_checkpoint.get("rollback_validation_ref")),
                        "result": "pass",
                        "evidence": "Reused prior successful checkpoint after evidence-only acceptance.",
                        "proof_artifact": str(prior_checkpoint.get("rollback_validation_ref") or ""),
                    },
                }
    elif accepted_packets:
        checkpoint_meta = {
            "checkpoint_strategy": str(prior_checkpoint.get("checkpoint_strategy") or "git_checkpoint_required"),
            "checkpoint_attempted_at": str(prior_checkpoint.get("checkpoint_attempted_at") or ""),
            "checkpoint_commit": str(prior_checkpoint.get("checkpoint_commit") or ""),
            "checkpoint_blocked": prior_checkpoint.get("checkpoint_blocked") is True,
            "checkpoint_block_reason": str(prior_checkpoint.get("checkpoint_block_reason") or ""),
            "checkpoint_block_evidence": str(prior_checkpoint.get("checkpoint_block_evidence") or ""),
            "rollback_validation_ref": str(prior_checkpoint.get("rollback_validation_ref") or ""),
            "rollback_validation": {
                "executed": bool(prior_checkpoint.get("rollback_validation_ref")),
                "result": "pass" if prior_checkpoint.get("checkpoint_blocked") is False else "blocked",
                "evidence": "Reused prior checkpoint metadata for a non-accepting cycle.",
                "proof_artifact": str(prior_checkpoint.get("rollback_validation_ref") or ""),
                "proof_hash": sha256_file(prior_checkpoint.get("rollback_validation_ref"))
                if str(prior_checkpoint.get("rollback_validation_ref") or "").strip()
                and Path(str(prior_checkpoint.get("rollback_validation_ref"))).exists()
                else "",
            },
        }
    else:
        checkpoint_meta = {
        "checkpoint_strategy": "git_checkpoint_required",
        "checkpoint_attempted_at": "",
        "checkpoint_commit": "",
        "checkpoint_blocked": True,
        "checkpoint_block_reason": "checkpoint_not_required_yet",
        "checkpoint_block_evidence": "No verifier-accepted packets exist yet.",
        "rollback_validation_ref": "",
        "rollback_validation": {
            "executed": False,
            "result": "not_run",
            "evidence": "No verifier-accepted packets exist yet.",
            "proof_artifact": "",
            "proof_hash": "",
        },
    }
    packet_dag["packets"] = [updated_packets[packet_id] for packet_id in sorted(updated_packets)]
    status.update(
        {
            "completed_packets": sorted(accepted_packets),
            "pending_packets": sorted(
                packet_id
                for packet_id, packet in updated_packets.items()
                if str(packet.get("runtime_state", "")).strip() == "queued"
            ),
            "blocked_packets": sorted(
                packet_id
                for packet_id, packet in updated_packets.items()
                if str(packet.get("runtime_state", "")).strip() == "escalated"
            ),
            "deferred_packets": sorted(
                packet_id
                for packet_id, packet in updated_packets.items()
                if str(packet.get("runtime_state", "")).strip() == "cancelled"
            ),
            "boundary_shrunk_remainder": sorted(
                str(packet_id).strip()
                for packet_id in review.get("boundary_shrunk_remainder", [])
                if str(packet_id).strip()
            ),
            "closure_state": _terminal_status(
                packets=updated_packets,
                frontier=frontier,
                boundary_shrunk_remainder=[
                    str(packet_id).strip()
                    for packet_id in review.get("boundary_shrunk_remainder", [])
                    if str(packet_id).strip()
                ],
                migration_fallback_used=review.get("migration_fallback_used") is True,
                blocked=blocked_by_budget,
                checkpoint_ready=checkpoint_meta.get("checkpoint_blocked") is False,
            ),
        }
    )
    _write_runtime_state(
        artifacts_root=artifacts_root,
        track_id=track_id,
        packet_dag=packet_dag,
        status=status,
        schedule=rebuilt_schedule,
    )
    _sync_runtime_summary(
        plan_payload=plan_payload,
        artifacts_root=artifacts_root,
        track_id=track_id,
        status=status,
        schedule=rebuilt_schedule,
        packets=updated_packets,
        cwd=cwd,
        review=review,
        blocked_by_budget=blocked_by_budget,
        controller_mode=controller_mode,
    )
    _persist_runtime_packets(artifacts_root=artifacts_root, track_id=track_id, packets=updated_packets)
    cycle_result_payload = load_json_file(cycle_paths["result"]) if cycle_paths["result"].exists() else {}
    _update_execution_ledger(
        artifacts_root=artifacts_root,
        track_id=track_id,
        packets=updated_packets,
        latest_results=cycle_result_payload.get("packet_results") if isinstance(cycle_result_payload.get("packet_results"), list) else [],
        packet_verdicts=verdicts,
    )
    _append_adaptation_events(artifacts_root=artifacts_root, track_id=track_id, events=adaptation_events)
    _update_feature_list(artifacts_root=artifacts_root, track_id=track_id, accepted_packets=accepted_packets)
    _update_momentum_and_blockers(
        artifacts_root=artifacts_root,
        track_id=track_id,
        packets=updated_packets,
        frontier=frontier,
        blocker_reason="runtime_budget_exhausted" if blocked_by_budget else "runtime_escalated",
    )
    checkpoint = _update_checkpoint(
        artifacts_root=artifacts_root,
        track_id=track_id,
        packets=updated_packets,
        schedule=rebuilt_schedule,
        last_forward_movement=frontier_reason or "review_applied",
        checkpoint_meta=checkpoint_meta,
    )
    support_confidence = _build_support_confidence(
        artifacts_root=artifacts_root,
        track_id=track_id,
        packets=updated_packets,
        status=status,
        schedule=rebuilt_schedule,
        validation_plan=_load_json_if_exists(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["validation_plan"]),
        checkpoint=checkpoint,
        execution_coverage=_load_json_if_exists(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["execution_coverage"]),
    )
    _write_runtime_supporting_state(
        artifacts_root=artifacts_root,
        track_id=track_id,
        support_confidence=support_confidence,
    )
    if _support_confidence_mode() == "enforce" and status["closure_state"] in {"OBJECTIVE_COMPLETE", "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK"}:
        recommendation = str(support_confidence.get("final_gate_recommendation") or "").strip()
        if recommendation == "continue_with_remediation":
            status["closure_state"] = "OBJECTIVE_REJECTED_FALSE_COMPLETION"
        elif recommendation == "block_closure":
            status["closure_state"] = "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED"
        _write_runtime_state(
            artifacts_root=artifacts_root,
            track_id=track_id,
            packet_dag=packet_dag,
            status=status,
            schedule=rebuilt_schedule,
        )
        _sync_runtime_summary(
            plan_payload=plan_payload,
            artifacts_root=artifacts_root,
            track_id=track_id,
            status=status,
            schedule=rebuilt_schedule,
            packets=updated_packets,
            cwd=cwd,
            review=review,
            blocked_by_budget=blocked_by_budget,
            controller_mode=controller_mode,
        )
    _append_progress_events(
        artifacts_root=artifacts_root,
        track_id=track_id,
        events=[
            {
                "schema_version": "objective-progress-event.v1",
                "event_type": "cycle_applied",
                "timestamp": checkpoint["updated_at"],
                "objective_id": stable_objective_id(track_id),
                "track_id": track_id,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "cycle_id": cycle_id,
                "packet_ids": [item["packet_id"] for item in verdicts],
                "frontier_movement_reason": frontier_reason,
                "closure_state": status["closure_state"],
                "checkpoint_blocked": checkpoint["checkpoint_blocked"],
                "checkpoint_block_reason": checkpoint["checkpoint_block_reason"],
                "blocked_reasons": _current_blocked_reasons(
                    packets=updated_packets,
                    blocked_by_budget=blocked_by_budget,
                    review=review,
                ),
            },
            {
                "schema_version": "objective-progress-event.v1",
                "event_type": "checkpoint",
                "timestamp": checkpoint["updated_at"],
                "objective_id": stable_objective_id(track_id),
                "track_id": track_id,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "last_verified_packet_ids": checkpoint["last_verified_packet_ids"],
                "current_frontier": checkpoint["current_frontier"],
                "next_recommended_packet": checkpoint["next_recommended_packet"],
            },
        ],
    )
    kernel_state = _load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id)
    if kernel_state:
        kernel_state["completed_units"] = sorted(accepted_packets)
        _write_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id, payload=kernel_state)
    latest_verification_id = ""
    verification_rows = _load_jsonl(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["verification_results"])
    if verification_rows:
        latest_verification_id = str(verification_rows[-1].get("verification_id") or "").strip()
    step_packet_ids = [str(item.get("packet_id") or "").strip() for item in verdicts if str(item.get("packet_id") or "").strip()]
    if status["closure_state"] in {"OBJECTIVE_COMPLETE", "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK"}:
        _sync_kernel_state(
            artifacts_root=artifacts_root,
            track_id=track_id,
            step_id=cycle_id,
            to_state="finalize_pending",
            guard="verification.status == pass && all_required_acceptance_checks_satisfied",
            guard_result=True,
            trigger="review_applied",
            packet_ids=step_packet_ids,
            action_kind="finalize",
            last_verification_id=latest_verification_id or None,
        )
        _sync_kernel_state(
            artifacts_root=artifacts_root,
            track_id=track_id,
            step_id=cycle_id,
            to_state="success" if status["closure_state"] == "OBJECTIVE_COMPLETE" else "partial",
            guard="terminal_finalize_path_resolved",
            guard_result=True,
            trigger="closure_adjudicated",
            packet_ids=[],
            last_verification_id=latest_verification_id or None,
        )
    elif status["closure_state"] in {"OBJECTIVE_BLOCKED_ESCALATION_REQUIRED", "OBJECTIVE_BLOCKED_MIGRATION_DEFECT"}:
        _sync_kernel_state(
            artifacts_root=artifacts_root,
            track_id=track_id,
            step_id=cycle_id,
            to_state="blocked",
            guard="verification.repairability == blocked || verification.scope == environment",
            guard_result=True,
            trigger="review_applied",
            packet_ids=step_packet_ids,
            action_kind="escalate_blocked",
            last_verification_id=latest_verification_id or None,
            record_failure=("blocked_runtime", latest_verification_id, False),
        )
        _sync_kernel_state(
            artifacts_root=artifacts_root,
            track_id=track_id,
            step_id=cycle_id,
            to_state="closed_blocked",
            guard="external_blocker_evidenced && no_authorized_path_forward",
            guard_result=True,
            trigger="closure_adjudicated",
            packet_ids=[],
            last_verification_id=latest_verification_id or None,
        )
    elif any(str(item.get("verifier_output") or "").strip() == "rejected_rework" for item in verdicts):
        _sync_kernel_state(
            artifacts_root=artifacts_root,
            track_id=track_id,
            step_id=cycle_id,
            to_state="repair_pending",
            guard="verification.status == soft_fail && verification.repairability in ['local_patch','retryable']",
            guard_result=True,
            trigger="review_applied",
            packet_ids=step_packet_ids,
            action_kind="repair",
            last_verification_id=latest_verification_id or None,
            record_failure=("verification_soft_fail", latest_verification_id, True),
        )
    else:
        _sync_kernel_state(
            artifacts_root=artifacts_root,
            track_id=track_id,
            step_id=cycle_id,
            to_state="ready",
            guard="verification.status == pass && work_remaining",
            guard_result=True,
            trigger="review_applied",
            packet_ids=[],
            last_verification_id=latest_verification_id or None,
        )
    _sync_operator_view(artifacts_root=artifacts_root, track_id=track_id)
    cycle_state["phase"] = "applied"
    cycle_state["applied_at"] = now_iso()
    cycle_state["applied_revision"] = int(cycle_state.get("applied_revision", 0) or 0) + 1
    write_json_file(cycle_paths["state"], cycle_state)
    _capture_cycle_log(
        artifacts_root=artifacts_root,
        track_id=track_id,
        cycle_id=cycle_id,
        cwd=cwd or os.getcwd(),
        verdicts=verdicts,
        frontier_reason=frontier_reason,
        closure_state=status["closure_state"],
    )
    if status["closure_state"] in {
        "OBJECTIVE_COMPLETE",
        "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK",
        "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED",
        "OBJECTIVE_BLOCKED_MIGRATION_DEFECT",
    }:
        _emit_terminal_captures(
            plan_payload=plan_payload,
            artifacts_root=artifacts_root,
            track_id=track_id,
            cwd=cwd or os.getcwd(),
            checkpoint_meta=checkpoint_meta,
        )
        if checkpoint.get("rollback_validation_ref") != checkpoint_meta.get("rollback_validation_ref"):
            checkpoint["rollback_validation_ref"] = str(checkpoint_meta.get("rollback_validation_ref") or "")
            write_json_file(session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["checkpoint"], checkpoint)
    if status["closure_state"] in {"OBJECTIVE_COMPLETE", "OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK"}:
        return {"status": "approve", "reason_code": "OBJECTIVE_COMPLETE", "closure_state": status["closure_state"]}
    if status["closure_state"] in {"OBJECTIVE_BLOCKED_ESCALATION_REQUIRED", "OBJECTIVE_BLOCKED_MIGRATION_DEFECT"}:
        return {
            "status": "blocked",
            "reason_code": "NO_SAFE_MOMENTUM",
            "closure_state": status["closure_state"],
        }
    return {"status": "continue", "reason_code": "CYCLE_APPLIED", "closure_state": status.get("closure_state", "")}


def _execute_cycle_once(
    *,
    plan_payload: dict[str, Any],
    artifacts_root: Path,
    track_id: str,
    cwd: str | None,
    codex_home: str | None,
    controller_mode: str | None,
) -> dict[str, Any]:
    packet_dag, status, schedule = _load_runtime_state(artifacts_root=artifacts_root, track_id=track_id)
    kernel_errors = validate_state(
        state=_load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id),
        execution_plan=_load_execution_plan(artifacts_root=artifacts_root, track_id=track_id),
        validation_plan=_load_json_if_exists(
            runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["validation_plan"]
        ),
    )
    if kernel_errors:
        _force_kernel_unsafe(
            artifacts_root=artifacts_root,
            track_id=track_id,
            kernel_state=_load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id),
            errors=kernel_errors,
            step_id="pre-step-validate",
        )
        return {
            "status": "blocked",
            "reason_code": "KERNEL_STATE_INVALID",
            "blocked_fields": kernel_errors,
        }
    open_cycle = _open_cycle(artifacts_root=artifacts_root, track_id=track_id)
    if open_cycle is None:
        packets = _packet_map(packet_dag)
        accepted_packets = {
            packet_id for packet_id, packet in packets.items() if str(packet.get("runtime_state", "")).strip() == "accepted"
        }
        frontier = compute_runnable_set(
            packets=packets,
            accepted_packets=accepted_packets,
            active_packets=set(),
            retry_counters=schedule.get("retry_counters") if isinstance(schedule.get("retry_counters"), dict) else {},
            max_parallel_packets=int(schedule.get("max_parallel_packets", 1) or 1),
            parallelism_policy=str(schedule.get("parallelism_policy") or "bounded_parallel"),
            execution_shape=str(schedule.get("execution_shape") or "single_lane"),
            lane_caps=schedule.get("lane_caps") if isinstance(schedule.get("lane_caps"), dict) else {},
            route_swarm_cap=int(schedule.get("route_swarm_cap", 0) or 0) if schedule.get("route_swarm_cap") is not None else None,
            frontier_dispatch_order=schedule.get("frontier_dispatch_order") if isinstance(schedule.get("frontier_dispatch_order"), list) else [],
            reviewer_barrier_points=schedule.get("reviewer_barrier_points") if isinstance(schedule.get("reviewer_barrier_points"), list) else [],
        )
        schedule["current_frontier"] = frontier
        schedule["safe_momentum_available"] = bool(frontier)
        if status.get("closure_state") == "OBJECTIVE_COMPLETE":
            checkpoint = load_json_file(session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["checkpoint"])
            checkpoint_meta = {
                "rollback_validation_ref": str(checkpoint.get("rollback_validation_ref") or ""),
            }
            _emit_terminal_captures(
                plan_payload=plan_payload,
                artifacts_root=artifacts_root,
                track_id=track_id,
                cwd=cwd or os.getcwd(),
                checkpoint_meta=checkpoint_meta,
            )
            if checkpoint_meta.get("rollback_validation_ref") and checkpoint.get("rollback_validation_ref") != checkpoint_meta.get("rollback_validation_ref"):
                checkpoint["rollback_validation_ref"] = checkpoint_meta["rollback_validation_ref"]
                write_json_file(session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["checkpoint"], checkpoint)
            _write_runtime_state(
                artifacts_root=artifacts_root,
                track_id=track_id,
                packet_dag=packet_dag,
                status=status,
                schedule=schedule,
            )
            _sync_runtime_summary(
                plan_payload=plan_payload,
                artifacts_root=artifacts_root,
                track_id=track_id,
                status=status,
                schedule=schedule,
                packets=packets,
                cwd=cwd,
                controller_mode=controller_mode,
            )
            _sync_operator_view(artifacts_root=artifacts_root, track_id=track_id)
            verdict = _controller_verdict(
                artifacts_root=artifacts_root,
                track_id=track_id,
                controller_mode=controller_mode,
            )
            if verdict["status"] in {"approve", "blocked"}:
                return verdict
        if not frontier:
            status["closure_state"] = "OBJECTIVE_BLOCKED_ESCALATION_REQUIRED"
            checkpoint = load_json_file(session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["checkpoint"])
            checkpoint_meta = {
                "rollback_validation_ref": str(checkpoint.get("rollback_validation_ref") or ""),
            }
            _emit_terminal_captures(
                plan_payload=plan_payload,
                artifacts_root=artifacts_root,
                track_id=track_id,
                cwd=cwd or os.getcwd(),
                checkpoint_meta=checkpoint_meta,
            )
            if checkpoint_meta.get("rollback_validation_ref") and checkpoint.get("rollback_validation_ref") != checkpoint_meta.get("rollback_validation_ref"):
                checkpoint["rollback_validation_ref"] = checkpoint_meta["rollback_validation_ref"]
                write_json_file(session_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["checkpoint"], checkpoint)
            _write_runtime_state(
                artifacts_root=artifacts_root,
                track_id=track_id,
                packet_dag=packet_dag,
                status=status,
                schedule=schedule,
            )
            _sync_runtime_summary(
                plan_payload=plan_payload,
                artifacts_root=artifacts_root,
                track_id=track_id,
                status=status,
                schedule=schedule,
                packets=packets,
                cwd=cwd,
                blocked_by_budget=True,
                controller_mode=controller_mode,
            )
            _sync_operator_view(artifacts_root=artifacts_root, track_id=track_id)
            return _controller_verdict(
                artifacts_root=artifacts_root,
                track_id=track_id,
                controller_mode=controller_mode,
            )
        _, cycle_state, cycle_paths = _create_cycle_request(
            artifacts_root=artifacts_root,
            track_id=track_id,
            packet_dag=packet_dag,
            schedule=schedule,
        )
        cycle_id = str(cycle_state.get("cycle_id", "")).strip()
    else:
        cycle_id, cycle_state, cycle_paths = open_cycle

    if str(cycle_state.get("phase", "")).strip() == "requested":
        cycle_request = _load_cycle_request(cycle_paths)
        kernel_state = _load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id)
        if str(kernel_state.get("state") or "") not in {"acting", "verifying"}:
            _sync_kernel_state(
                artifacts_root=artifacts_root,
                track_id=track_id,
                step_id=str(cycle_request.get("cycle_id") or cycle_id),
                to_state="acting",
                guard="exactly_one_action_selected && active_unit_ids_nonempty",
                guard_result=True,
                trigger="cycle_dispatch",
                packet_ids=[str(packet_id).strip() for packet_id in cycle_request.get("packet_ids", []) if str(packet_id).strip()],
                action_kind=_primary_action_for_cycle_request(cycle_request),
            )
        if not cycle_paths["result"].exists():
            result = _execute_worker(
                cycle_request=cycle_request,
                schedule=schedule,
                track_id=track_id,
                cwd=cwd,
                codex_home=codex_home,
            )
            write_json_file(cycle_paths["result"], result)
        result = load_json_file(cycle_paths["result"])
        _capture_worker_result(
            artifacts_root=artifacts_root,
            track_id=track_id,
            cycle_request=cycle_request,
            cycle_result=result,
            cwd=cwd or os.getcwd(),
        )
        verification_evidence = [
            _evidence_ref(
                kind="json_artifact",
                path=str(cycle_paths["result"]),
                producer="cycle_worker",
                step_id=str(cycle_request.get("cycle_id") or cycle_id),
            )
        ]
        for ref in result.get("evidence_refs", []) if isinstance(result.get("evidence_refs"), list) else []:
            verification_evidence.append(
                _evidence_ref(
                    kind="runtime_artifact",
                    path=str(ref),
                    producer="cycle_worker",
                    step_id=str(cycle_request.get("cycle_id") or cycle_id),
                )
            )
        if str(_load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id).get("state") or "") != "verifying":
            _sync_kernel_state(
                artifacts_root=artifacts_root,
                track_id=track_id,
                step_id=str(cycle_request.get("cycle_id") or cycle_id),
                to_state="verifying",
                guard="new_evidence_count >= 1",
                guard_result=bool(verification_evidence),
                trigger="worker_result_captured",
                packet_ids=[str(packet_id).strip() for packet_id in cycle_request.get("packet_ids", []) if str(packet_id).strip()],
                evidence_refs=verification_evidence,
            )
        _append_packet_results(
            artifacts_root=artifacts_root,
            track_id=track_id,
            packet_results=result.get("packet_results") if isinstance(result.get("packet_results"), list) else [],
        )
        cycle_state["phase"] = "executed"
        cycle_state["executed_at"] = now_iso()
        write_json_file(cycle_paths["state"], cycle_state)

    if str(cycle_state.get("phase", "")).strip() == "executed":
        cycle_request = _load_cycle_request(cycle_paths)
        if cycle_paths["review"].exists():
            review = load_json_file(cycle_paths["review"])
        else:
            cycle_result = load_json_file(cycle_paths["result"])
            current_packet_dag, _, _ = _load_runtime_state(artifacts_root=artifacts_root, track_id=track_id)
            review = verify_cycle_payload(
                plan_payload={**plan_payload, "packets": current_packet_dag.get("packets", plan_payload.get("packets", []))},
                cycle_request=cycle_request,
                cycle_result={**cycle_result, "artifact_path": str(cycle_paths["result"])},
                track_id=track_id,
            )
            write_json_file(cycle_paths["review"], review)
        cycle_result = load_json_file(cycle_paths["result"])
        verification_results = _build_verification_results(
            cycle_request=cycle_request,
            cycle_result=cycle_result,
            review=review,
        )
        existing_verification_ids = {
            str(item.get("verification_id") or "").strip()
            for item in _load_jsonl(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["verification_results"])
            if isinstance(item, dict)
        }
        for item in verification_results:
            verification_id = str(item.get("verification_id") or "").strip()
            if verification_id and verification_id not in existing_verification_ids:
                _append_verification_result(artifacts_root=artifacts_root, track_id=track_id, payload=item)
        if review.get("blocked_fields"):
            _force_kernel_unsafe(
                artifacts_root=artifacts_root,
                track_id=track_id,
                kernel_state=_load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id),
                errors=[str(item) for item in review.get("blocked_fields", [])],
                step_id=str(cycle_request.get("cycle_id") or cycle_id),
            )
            return {
                "status": "blocked",
                "reason_code": "CYCLE_REVIEW_BLOCKED",
                "blocked_fields": review["blocked_fields"],
            }
        _capture_verifier_result(
            artifacts_root=artifacts_root,
            track_id=track_id,
            cycle_request=cycle_request,
            review=review,
            cwd=cwd or os.getcwd(),
        )
        cycle_state["phase"] = "reviewed"
        cycle_state["reviewed_at"] = now_iso()
        write_json_file(cycle_paths["state"], cycle_state)

    if str(cycle_state.get("phase", "")).strip() == "reviewed":
        packet_dag, status, schedule = _load_runtime_state(artifacts_root=artifacts_root, track_id=track_id)
        return _apply_review_once(
            plan_payload=plan_payload,
            artifacts_root=artifacts_root,
            track_id=track_id,
            packet_dag=packet_dag,
            status=status,
            schedule=schedule,
            cycle_id=cycle_id,
            cycle_state=cycle_state,
            cycle_paths=cycle_paths,
            cwd=cwd,
            controller_mode=controller_mode,
        )
    if str(cycle_state.get("phase", "")).strip() == "applied":
        return {"status": "continue", "reason_code": "ALREADY_APPLIED"}
    return {"status": "error", "reason_code": "CYCLE_STATE_INVALID"}


def _ensure_step_cycle_state_path(
    *,
    artifacts_root: Path,
    track_id: str,
) -> Path | None:
    open_cycle = _open_cycle(artifacts_root=artifacts_root, track_id=track_id)
    if open_cycle is not None:
        return open_cycle[2]["state"]
    packet_dag, _, schedule = _load_runtime_state(artifacts_root=artifacts_root, track_id=track_id)
    packets = _packet_map(packet_dag)
    accepted_packets = {
        packet_id for packet_id, packet in packets.items() if str(packet.get("runtime_state", "")).strip() == "accepted"
    }
    frontier = compute_runnable_set(
        packets=packets,
        accepted_packets=accepted_packets,
        active_packets=set(),
        retry_counters=schedule.get("retry_counters") if isinstance(schedule.get("retry_counters"), dict) else {},
        max_parallel_packets=int(schedule.get("max_parallel_packets", 1) or 1),
        parallelism_policy=str(schedule.get("parallelism_policy") or "bounded_parallel"),
        execution_shape=str(schedule.get("execution_shape") or "single_lane"),
        lane_caps=schedule.get("lane_caps") if isinstance(schedule.get("lane_caps"), dict) else {},
        route_swarm_cap=int(schedule.get("route_swarm_cap", 0) or 0) if schedule.get("route_swarm_cap") is not None else None,
        frontier_dispatch_order=schedule.get("frontier_dispatch_order") if isinstance(schedule.get("frontier_dispatch_order"), list) else [],
        reviewer_barrier_points=schedule.get("reviewer_barrier_points") if isinstance(schedule.get("reviewer_barrier_points"), list) else [],
    )
    if not frontier:
        return None
    schedule["current_frontier"] = frontier
    schedule["safe_momentum_available"] = bool(frontier)
    _, _, cycle_paths = _create_cycle_request(
        artifacts_root=artifacts_root,
        track_id=track_id,
        packet_dag=packet_dag,
        schedule=schedule,
    )
    return cycle_paths["state"]


def step(
    *,
    plan_payload: dict[str, Any],
    artifacts_root: Path,
    track_id: str,
    cwd: str | None,
    codex_home: str | None,
    controller_mode: str | None,
) -> dict[str, Any]:
    recovery = _recover_runtime_transaction(artifacts_root=artifacts_root, track_id=track_id)
    if recovery and recovery["transaction_state"] == "aborted":
        runtime_paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
        return {
            "runtime_payload": {
                "status": "blocked",
                "reason_code": "TRANSACTION_INTEGRITY_FAILURE",
                "blocked_fields": recovery.get("errors", []),
                "transaction_id": recovery.get("transaction_id", ""),
                "transaction_state": recovery.get("transaction_state", ""),
                "recovered": False,
            },
            "new_state": _load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id),
            "action_taken": None,
            "evidence_written": [str(runtime_paths["invalid_transition"])],
            "verification_result": None,
            "transition": {},
            "valid": False,
            "transaction_id": recovery.get("transaction_id", ""),
            "transaction_state": recovery.get("transaction_state", ""),
            "recovered": False,
            "committed_artifact_count": 0,
        }
    pre_state = _load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id)
    execution_plan = _load_execution_plan(artifacts_root=artifacts_root, track_id=track_id)
    validation_plan = _load_json_if_exists(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["validation_plan"])
    pre_errors = validate_state(state=pre_state, execution_plan=execution_plan, validation_plan=validation_plan)
    if pre_errors:
        invalid_payload = _force_kernel_unsafe(
            artifacts_root=artifacts_root,
            track_id=track_id,
            kernel_state=pre_state,
            errors=pre_errors,
            step_id="step-preflight",
        )
        return {
            "runtime_payload": {
                "status": "blocked",
                "reason_code": "KERNEL_STATE_INVALID",
                "blocked_fields": pre_errors,
            },
            "new_state": _load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id),
            "action_taken": None,
            "evidence_written": [str(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["invalid_transition"])],
            "verification_result": None,
            "transition": invalid_payload,
            "valid": False,
            "transaction_id": recovery.get("transaction_id", "") if recovery else "",
            "transaction_state": recovery.get("transaction_state", "") if recovery else "",
            "recovered": recovery.get("recovered", False) if recovery else False,
            "committed_artifact_count": 0,
        }
    cycle_state_path = _ensure_step_cycle_state_path(artifacts_root=artifacts_root, track_id=track_id)

    def _step_body() -> dict[str, Any]:
        payload = _execute_cycle_once(
            plan_payload=plan_payload,
            artifacts_root=artifacts_root,
            track_id=track_id,
            cwd=cwd,
            codex_home=codex_home,
            controller_mode=controller_mode,
        )
        staged_post_state = _load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id)
        staged_errors = validate_state(state=staged_post_state, execution_plan=execution_plan, validation_plan=validation_plan)
        if staged_errors:
            invalid_payload = _force_kernel_unsafe(
                artifacts_root=artifacts_root,
                track_id=track_id,
                kernel_state=staged_post_state,
                errors=staged_errors,
                step_id="step-postflight",
            )
            return {
                "status": "blocked",
                "reason_code": "KERNEL_STATE_INVALID",
                "blocked_fields": staged_errors,
                "_invalid_transition": invalid_payload,
                "_valid": False,
            }
        return payload

    payload = _run_in_runtime_transaction(
        artifacts_root=artifacts_root,
        track_id=track_id,
        step_id=str(pre_state.get("active_unit_id") or "runtime-step"),
        cycle_state_path=cycle_state_path,
        body=_step_body,
    )
    post_state = _load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id)
    post_errors = validate_state(state=post_state, execution_plan=execution_plan, validation_plan=validation_plan)
    if post_errors:
        invalid_payload = _force_kernel_unsafe(
            artifacts_root=artifacts_root,
            track_id=track_id,
            kernel_state=post_state,
            errors=post_errors,
            step_id="step-postflight",
        )
        payload = {
            "status": "blocked",
            "reason_code": "KERNEL_STATE_INVALID",
            "blocked_fields": post_errors,
        }
        post_state = _load_kernel_runtime_state(artifacts_root=artifacts_root, track_id=track_id)
        valid = False
        transition = invalid_payload
    elif payload.get("_valid") is False:
        valid = False
        transition = payload.get("_invalid_transition", {})
        payload = {
            key: value
            for key, value in payload.items()
            if key not in {"_valid", "_invalid_transition"}
        }
    else:
        valid = True
        transition_history = post_state.get("transition_history") if isinstance(post_state.get("transition_history"), list) else []
        transition = transition_history[-1] if transition_history else {}
    verification_rows = _load_jsonl(runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)["verification_results"])
    latest_verification = verification_rows[-1] if verification_rows else None
    return {
        "runtime_payload": payload,
        "new_state": post_state,
        "action_taken": post_state.get("last_action") if isinstance(post_state.get("last_action"), dict) else None,
        "evidence_written": [
            str(ref.get("path") or "")
            for ref in post_state.get("evidence_refs", [])
            if isinstance(ref, dict) and str(ref.get("path") or "").strip()
        ],
        "verification_result": latest_verification,
        "transition": transition,
        "valid": valid,
        "transaction_id": str(payload.get("transaction_id") or recovery.get("transaction_id") or "") if recovery else str(payload.get("transaction_id") or ""),
        "transaction_state": str(payload.get("transaction_state") or recovery.get("transaction_state") or "") if recovery else str(payload.get("transaction_state") or ""),
        "recovered": bool(payload.get("recovered") or (recovery and recovery.get("recovered"))),
        "committed_artifact_count": int(payload.get("committed_artifact_count") or 0),
    }


def run_runtime(
    *,
    plan_payload: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    artifacts_root: Path,
    track_id: str,
    cwd: str | None = None,
    workspace_root: str | None = None,
    codex_home: str | None = None,
    command: str = "run",
    controller_mode: str | None = None,
) -> tuple[int, dict[str, Any]]:
    resolved_plan = _resolve_plan_payload(plan_payload=plan_payload, plan=plan)
    resolved_cwd = cwd or workspace_root
    normalized_controller_mode = _normalize_controller_mode(controller_mode)
    _run_policy_preflight(workspace_root=workspace_root)
    runtime_paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    recovery = _recover_runtime_transaction(artifacts_root=artifacts_root, track_id=track_id)
    if recovery and recovery["transaction_state"] == "aborted":
        payload = {
            "status": "blocked",
            "reason_code": "TRANSACTION_INTEGRITY_FAILURE",
            "track_id": track_id,
            "blocked_fields": recovery.get("errors", []),
            "transaction_id": recovery.get("transaction_id", ""),
            "transaction_state": recovery.get("transaction_state", ""),
            "recovered": False,
        }
        payload.update(_runtime_stop_state_fields(artifacts_root=artifacts_root, track_id=track_id))
        _sync_runtime_state_record(artifacts_root=artifacts_root, track_id=track_id, lifecycle_status="blocked")
        return _status_exit_code(payload["status"]), payload
    if not runtime_paths["packet_dag"].exists():
        bootstrap_runtime(
            plan_payload=resolved_plan,
            artifacts_root=artifacts_root,
            track_id=track_id,
            cwd=resolved_cwd,
            controller_mode=normalized_controller_mode,
        )
    executed_cycles = 0
    while True:
        step_result = step(
            plan_payload=resolved_plan,
            artifacts_root=artifacts_root,
            track_id=track_id,
            cwd=resolved_cwd,
            codex_home=codex_home,
            controller_mode=normalized_controller_mode,
        )
        payload = step_result["runtime_payload"]
        if payload["status"] == "error":
            payload.setdefault("track_id", track_id)
            payload.update(_runtime_stop_state_fields(artifacts_root=artifacts_root, track_id=track_id))
            _sync_runtime_state_record(artifacts_root=artifacts_root, track_id=track_id, lifecycle_status="error")
            return _status_exit_code(payload["status"]), payload
        if payload["status"] == "blocked" and payload.get("reason_code") not in {"NO_SAFE_MOMENTUM"}:
            payload.setdefault("track_id", track_id)
            payload.update(_runtime_stop_state_fields(artifacts_root=artifacts_root, track_id=track_id))
            _sync_runtime_state_record(artifacts_root=artifacts_root, track_id=track_id, lifecycle_status="blocked")
            return _status_exit_code(payload["status"]), payload
        verdict = _controller_verdict(
            artifacts_root=artifacts_root,
            track_id=track_id,
            controller_mode=normalized_controller_mode,
        )
        if verdict["status"] in {"approve", "blocked"}:
            verdict.setdefault("track_id", track_id)
            _sync_runtime_state_record(
                artifacts_root=artifacts_root,
                track_id=track_id,
                lifecycle_status="approved" if verdict["status"] == "approve" else "blocked",
            )
            return _status_exit_code(verdict["status"]), verdict
        executed_cycles += 1
        if command == "step" and executed_cycles >= 1:
            verdict.update({"track_id": track_id})
            _sync_runtime_state_record(artifacts_root=artifacts_root, track_id=track_id, lifecycle_status="revise")
            return _status_exit_code(verdict["status"]), verdict
        if verdict["status"] == "revise" and command in {"run", "resume"}:
            continue
        if command == "resume" and payload["status"] == "continue" and _open_cycle(artifacts_root=artifacts_root, track_id=track_id) is None:
            continue
        if command not in {"run", "resume", "step"}:
            payload = {"status": "error", "reason_code": "RUNTIME_MODE_INVALID", "track_id": track_id}
            _sync_runtime_state_record(artifacts_root=artifacts_root, track_id=track_id, lifecycle_status="error")
            return RUNTIME_EXIT_ERROR, payload


def _extract_last_json_payload(text: str) -> dict[str, Any]:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _build_verifier_remediation_prompt(base_prompt: str, verifier_payload: dict[str, Any], loop_idx: int) -> str:
    next_action = str(verifier_payload.get("next_action_prompt") or "").strip()
    if not next_action:
        missing = verifier_payload.get("missing_fields") if isinstance(verifier_payload.get("missing_fields"), list) else []
        blocked = verifier_payload.get("blocked_fields") if isinstance(verifier_payload.get("blocked_fields"), list) else []
        next_action = "Address remaining DoD gaps and re-run final verification."
        if missing:
            next_action += "\nMissing: " + ", ".join(str(item) for item in missing[:20])
        if blocked:
            next_action += "\nBlocked: " + ", ".join(str(item) for item in blocked[:20])
    if len(next_action) > 4000:
        next_action = next_action[:4000]
    return f"{base_prompt}\n\n[Ralph Remediation Loop {loop_idx}]\n{next_action}"


def _run_ralph_verifier(
    *,
    artifacts_root: Path,
    track_id: str,
    route_task_id: str,
    route_class: str,
    plan_json: str,
    impl_json: str,
    review_json: str,
    workspace_root: str,
    codex_home: str,
    mode: str,
    max_loops: int,
    loop_idx: int,
    timeout_sec: int | None,
) -> tuple[int, dict[str, Any]]:
    script = Path(codex_home).expanduser() / "bin" / "ralph_done_loop.py"
    cmd = canonical_python_argv(
        str(script),
        "--route-task-id",
        route_task_id,
        "--route-class",
        route_class,
        "--plan-json",
        plan_json,
        "--impl-json",
        impl_json,
        "--review-json",
        review_json,
        "--workspace-root",
        workspace_root,
        "--codex-home",
        codex_home,
        "--mode",
        mode,
        "--artifacts-root",
        str(artifacts_root),
        "--max-loops",
        str(max_loops),
        "--external-remediation-loop",
        "--loop-index",
        str(loop_idx),
    )
    if track_id:
        cmd.extend(["--track-id", track_id])
    if timeout_sec is not None:
        cmd.extend(["--timeout-sec", str(timeout_sec)])
    completed = subprocess.run(
        cmd,
        cwd=workspace_root,
        text=True,
        capture_output=True,
        env=_safe_env_view([]),
        check=False,
    )
    payload = _extract_last_json_payload(completed.stdout) or _extract_last_json_payload(completed.stderr)
    if not payload:
        payload = {
            "status": "error",
            "reason_code": "FINAL_VERIFIER_OUTPUT_MISSING",
            "reason": "Ralph did not emit a structured result payload.",
        }
    payload.setdefault("stdout", completed.stdout)
    payload.setdefault("stderr", completed.stderr)
    return int(completed.returncode), payload


def governed_runtime(
    *,
    plan_payload: dict[str, Any],
    plan_json_path: str = "",
    artifacts_root: Path,
    track_id: str,
    workspace_root: str | None,
    codex_home: str | None,
    controller_mode: str | None = None,
    finalize_attempt: bool = False,
    route_class: str = "",
    route_task_id: str = "",
    impl_json: str = "",
    review_json: str = "",
    verifier_mode: str = "enforce",
    verifier_max_loops: int = 3,
    verifier_timeout_sec: int | None = None,
    verifier_base_prompt: str = "",
) -> tuple[int, dict[str, Any]]:
    normalized_controller_mode = _normalize_controller_mode(controller_mode)
    require_final_verifier = finalize_attempt and route_class in {"R3", "R4"}
    verifier_loop_idx = 0

    while True:
        exit_code, payload = run_runtime(
            plan_payload=plan_payload,
            artifacts_root=artifacts_root,
            track_id=track_id,
            workspace_root=workspace_root,
            codex_home=codex_home,
            command="resume",
            controller_mode=normalized_controller_mode,
        )
        lifecycle_status = str(payload.get("status") or "running")
        if payload.get("status") == "approve" and require_final_verifier:
            _sync_runtime_state_record(
                artifacts_root=artifacts_root,
                track_id=track_id,
                lifecycle_status="approved_pending_verify",
            )
            verifier_loop_idx += 1
            verifier_exit, verifier_payload = _run_ralph_verifier(
                artifacts_root=artifacts_root,
                track_id=track_id,
                route_task_id=route_task_id,
                route_class=route_class,
                plan_json=plan_json_path or str(plan_payload.get("_plan_json_path") or ""),
                impl_json=impl_json,
                review_json=review_json,
                workspace_root=str(workspace_root or os.getcwd()),
                codex_home=str(codex_home or Path.home() / ".codex"),
                mode=verifier_mode,
                max_loops=verifier_max_loops,
                loop_idx=verifier_loop_idx,
                timeout_sec=verifier_timeout_sec,
            )
            verifier_result = {
                "status": str(verifier_payload.get("status") or ""),
                "reason_code": str(verifier_payload.get("reason_code") or ""),
                "reason": str(verifier_payload.get("reason") or ""),
                "loop_index": verifier_loop_idx,
            }
            if verifier_payload.get("status") == "approve" and verifier_exit == 0:
                _sync_runtime_state_record(
                    artifacts_root=artifacts_root,
                    track_id=track_id,
                    lifecycle_status="approved",
                    last_verifier_result=verifier_result,
                )
                payload["final_verifier"] = "ralph"
                payload["verifier_status"] = "approve"
                payload["verifier_reason_code"] = verifier_result["reason_code"]
                return exit_code, payload
            if verifier_payload.get("status") == "revise" and verifier_exit == 10:
                _sync_runtime_state_record(
                    artifacts_root=artifacts_root,
                    track_id=track_id,
                    lifecycle_status="revise",
                    last_verifier_result=verifier_result,
                )
                if verifier_loop_idx >= verifier_max_loops:
                    blocked_payload = {
                        "status": "blocked",
                        "reason_code": "BUDGET_EXHAUSTED",
                        "reason": "Final verifier loop budget exhausted without approval.",
                        "track_id": track_id,
                        **_runtime_stop_state_fields(artifacts_root=artifacts_root, track_id=track_id),
                    }
                    return RUNTIME_EXIT_BLOCKED, blocked_payload
                if not verifier_base_prompt:
                    payload = {
                        "status": "revise",
                        "reason_code": "FINAL_VERIFIER_REVISE_REQUIRED",
                        "next_action_prompt": str(verifier_payload.get("next_action_prompt") or "").strip(),
                        "track_id": track_id,
                        **_runtime_stop_state_fields(artifacts_root=artifacts_root, track_id=track_id),
                    }
                    return RUNTIME_EXIT_REVISE, payload
                real_bin = _resolve_real_bin(codex_home)
                if not real_bin:
                    error_payload = {
                        "status": "error",
                        "reason_code": "REAL_BIN_NOT_FOUND",
                        "reason": "Unable to resolve real codex binary for Ralph remediation.",
                        "track_id": track_id,
                        **_runtime_stop_state_fields(artifacts_root=artifacts_root, track_id=track_id),
                    }
                    return RUNTIME_EXIT_ERROR, error_payload
                remediation_prompt = _build_verifier_remediation_prompt(verifier_base_prompt, verifier_payload, verifier_loop_idx)
                completed = subprocess.run(
                    [real_bin, "exec", remediation_prompt],
                    cwd=workspace_root or os.getcwd(),
                    text=True,
                    capture_output=True,
                    env={**os.environ, "CODEX_REAL_BIN": real_bin, "CODEX_RUNTIME_REAL_BIN": real_bin},
                    check=False,
                )
                if completed.returncode != 0:
                    error_payload = {
                        "status": "error",
                        "reason_code": "EXECUTOR_REMEDIATION_FAILED",
                        "reason": (completed.stderr or completed.stdout or "Ralph remediation execution failed.").strip(),
                        "track_id": track_id,
                        "process_exit_code": int(completed.returncode),
                        **_runtime_stop_state_fields(artifacts_root=artifacts_root, track_id=track_id),
                    }
                    return RUNTIME_EXIT_ERROR, error_payload
                continue
            lifecycle_status = "blocked" if verifier_payload.get("status") == "blocked" else "error"
            _sync_runtime_state_record(
                artifacts_root=artifacts_root,
                track_id=track_id,
                lifecycle_status=lifecycle_status,
                last_verifier_result=verifier_result,
            )
            payload = {
                "status": lifecycle_status,
                "reason_code": str(verifier_payload.get("reason_code") or "FINAL_VERIFIER_FAILED"),
                "reason": str(verifier_payload.get("reason") or ""),
                "track_id": track_id,
                **_runtime_stop_state_fields(artifacts_root=artifacts_root, track_id=track_id),
            }
            return (RUNTIME_EXIT_BLOCKED if lifecycle_status == "blocked" else RUNTIME_EXIT_ERROR), payload

        _sync_runtime_state_record(
            artifacts_root=artifacts_root,
            track_id=track_id,
            lifecycle_status=lifecycle_status,
        )
        return exit_code, payload


def main() -> int:
    ensure_python_3_11()
    parser = argparse.ArgumentParser(description="Run the governed objective runtime.")
    parser.add_argument("mode", choices=["bootstrap", "step", "run", "resume", "governed"])
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--track-id", required=True)
    parser.add_argument("--artifacts-root", default=None)
    parser.add_argument("--workspace-root", default=None)
    parser.add_argument("--codex-home", default=None)
    parser.add_argument("--controller-mode", choices=[CONTROLLER_MODE_AUDIT, CONTROLLER_MODE_ENFORCE], default=CONTROLLER_MODE_ENFORCE)
    parser.add_argument("--finalize-attempt", action="store_true")
    parser.add_argument("--route-class", default="")
    parser.add_argument("--route-task-id", default="")
    parser.add_argument("--impl-json", default="")
    parser.add_argument("--review-json", default="")
    parser.add_argument("--verifier-mode", choices=[CONTROLLER_MODE_AUDIT, CONTROLLER_MODE_ENFORCE], default=CONTROLLER_MODE_ENFORCE)
    parser.add_argument("--verifier-max-loops", type=int, default=3)
    parser.add_argument("--verifier-timeout-sec", type=int, default=None)
    parser.add_argument("--verifier-base-prompt-file", default="")
    args = parser.parse_args()

    artifacts_root = resolve_artifacts_root(args.artifacts_root, cwd=args.workspace_root)
    runtime_paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=args.track_id)
    try:
        plan_payload = load_json_file(args.plan_json)
        plan_payload["_plan_json_path"] = str(Path(args.plan_json).expanduser().resolve())
        with file_lock(runtime_paths["lock"]):
            if args.mode == "bootstrap":
                payload = bootstrap_runtime(
                    plan_payload=plan_payload,
                    artifacts_root=artifacts_root,
                    track_id=args.track_id,
                    cwd=args.workspace_root,
                    controller_mode=args.controller_mode,
                )
                exit_code = _status_exit_code(payload["status"])
            elif args.mode == "governed":
                verifier_base_prompt = ""
                if args.verifier_base_prompt_file:
                    verifier_base_prompt = Path(args.verifier_base_prompt_file).read_text(encoding="utf-8")
                exit_code, payload = governed_runtime(
                    plan_payload=plan_payload,
                    plan_json_path=str(Path(args.plan_json).expanduser().resolve()),
                    artifacts_root=artifacts_root,
                    track_id=args.track_id,
                    workspace_root=args.workspace_root,
                    codex_home=args.codex_home,
                    controller_mode=args.controller_mode,
                    finalize_attempt=args.finalize_attempt,
                    route_class=args.route_class,
                    route_task_id=args.route_task_id,
                    impl_json=args.impl_json,
                    review_json=args.review_json,
                    verifier_mode=args.verifier_mode,
                    verifier_max_loops=args.verifier_max_loops,
                    verifier_timeout_sec=args.verifier_timeout_sec,
                    verifier_base_prompt=verifier_base_prompt,
                )
            else:
                exit_code, payload = run_runtime(
                    plan_payload=plan_payload,
                    artifacts_root=artifacts_root,
                    track_id=args.track_id,
                    workspace_root=args.workspace_root,
                    codex_home=args.codex_home,
                    command=args.mode,
                    controller_mode=args.controller_mode,
                )
    except Exception as exc:
        payload = {"status": "error", "reason_code": "INTERNAL_ERROR", "blocked_fields": [str(exc)]}
        exit_code = RUNTIME_EXIT_ERROR

    payload.update({"schema_version": "objective-runtime-result.v1", "track_id": args.track_id})
    print(json.dumps(payload, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
