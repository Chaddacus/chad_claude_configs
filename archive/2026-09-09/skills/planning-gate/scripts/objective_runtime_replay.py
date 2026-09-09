#!/usr/bin/env python3
"""Replay and explain planning-gate kernel runtime artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import (
    KERNEL_RUNTIME_TERMINAL_STATES,
    ensure_python_3_11,
    resolve_artifacts_root,
    runtime_artifact_paths,
)


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"artifact_not_object:{path}")
    return payload


def _load_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue
        payload = json.loads(raw)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _short_path(path_text: str) -> str:
    path = Path(str(path_text or "").strip())
    if not str(path):
        return ""
    parts = path.parts
    return "/".join(parts[-4:]) if len(parts) >= 4 else str(path)


def _evidence_summaries(evidence_refs: list[dict[str, Any]]) -> list[str]:
    summaries: list[str] = []
    for item in evidence_refs:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        path = _short_path(str(item.get("path") or "").strip())
        producer = str(item.get("producer") or "").strip()
        parts = [part for part in [kind, path, producer] if part]
        if parts:
            summaries.append(":".join(parts))
    return summaries


def _terminal_explanation(*, halt_reason: str, kernel_state_name: str, trap: dict[str, Any]) -> str:
    if trap.get("detected") is True:
        return "Runtime trapped to unsafe after an invalid transition or invariant failure."
    mapping = {
        "accepted_success": "All required acceptance checks passed within the authorized scope.",
        "accepted_partial": "A bounded subset completed and the remaining work is outside the current scope or authority.",
        "accepted_blocked": "A real external blocker was evidenced and no authorized path forward remained.",
        "needs_human_decision": "Desired behavior remained ambiguous, so the runtime stopped for a semantic decision.",
        "unsafe_to_continue": "Further mutation would have crossed a policy or safety boundary.",
        "no_safe_momentum": "No remaining legal action could produce trustworthy progress under the current evidence or budget.",
        "invalid_transition": "The kernel detected an illegal state transition or invariant failure and halted fail-closed.",
        "none": "The runtime has not reached a terminal halt reason.",
    }
    if halt_reason in mapping:
        return mapping[halt_reason]
    if kernel_state_name in KERNEL_RUNTIME_TERMINAL_STATES:
        return f"Kernel reached terminal state {kernel_state_name}."
    return "Terminal state could not be explained from kernel artifacts."


def _build_step_records(
    *,
    transition_rows: list[dict[str, Any]],
    verification_rows: list[dict[str, Any]],
    trap: dict[str, Any],
) -> list[dict[str, Any]]:
    verification_by_step: dict[str, list[dict[str, Any]]] = {}
    for row in verification_rows:
        step_id = str(row.get("step_id") or "").strip()
        if not step_id:
            continue
        verification_by_step.setdefault(step_id, []).append(row)

    ordered_step_ids: list[str] = []
    seen: set[str] = set()
    for row in transition_rows:
        step_id = str(row.get("step_id") or "").strip()
        if step_id and step_id not in seen:
            seen.add(step_id)
            ordered_step_ids.append(step_id)
    for step_id in verification_by_step:
        if step_id not in seen:
            seen.add(step_id)
            ordered_step_ids.append(step_id)
    trap_step_id = str(trap.get("step_id") or "").strip()
    if trap_step_id and trap_step_id not in seen:
        ordered_step_ids.append(trap_step_id)

    steps: list[dict[str, Any]] = []
    for step_id in ordered_step_ids:
        step_transitions = [row for row in transition_rows if str(row.get("step_id") or "").strip() == step_id]
        step_verifications = verification_by_step.get(step_id, [])
        from_state = str(step_transitions[0].get("from") or "").strip() if step_transitions else ""
        to_state = str(step_transitions[-1].get("to") or "").strip() if step_transitions else ""
        transitions = []
        for row in step_transitions:
            evidence_refs = row.get("evidence_refs") if isinstance(row.get("evidence_refs"), list) else []
            transitions.append(
                {
                    "from": str(row.get("from") or "").strip(),
                    "to": str(row.get("to") or "").strip(),
                    "guard": str(row.get("guard") or "").strip(),
                    "guard_result": row.get("guard_result") is True,
                    "trigger": str(row.get("trigger") or "").strip(),
                    "timestamp": str(row.get("timestamp") or "").strip(),
                    "evidence_count": len(evidence_refs),
                    "evidence": _evidence_summaries(evidence_refs),
                }
            )
        verifications = []
        for row in step_verifications:
            evidence = row.get("evidence") if isinstance(row.get("evidence"), list) else []
            verifications.append(
                {
                    "verification_id": str(row.get("verification_id") or "").strip(),
                    "unit_id": str(row.get("unit_id") or "").strip(),
                    "status": str(row.get("status") or "").strip(),
                    "scope": str(row.get("scope") or "").strip(),
                    "blame": str(row.get("blame") or "").strip(),
                    "repairability": str(row.get("repairability") or "").strip(),
                    "suggested_transition": str(row.get("suggested_transition") or "").strip(),
                    "evidence_count": len(evidence),
                    "evidence": _evidence_summaries(evidence),
                }
            )
        trap_errors = trap.get("errors", []) if trap_step_id == step_id and trap.get("detected") else []
        steps.append(
            {
                "step_id": step_id,
                "from_state": from_state,
                "to_state": to_state,
                "transition_count": len(transitions),
                "verification_count": len(verifications),
                "terminal_transition": to_state in KERNEL_RUNTIME_TERMINAL_STATES,
                "transitions": transitions,
                "verification_results": verifications,
                "trap_errors": trap_errors,
            }
        )
    return steps


def load_runtime_replay_payload(*, track_id: str, artifacts_root: Path) -> dict[str, Any]:
    paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    kernel_state = _load_json_object(paths["kernel_runtime_state"])
    transition_rows = _load_jsonl_objects(paths["transition_history"])
    verification_rows = _load_jsonl_objects(paths["verification_results"])
    invalid_transition = _load_json_object(paths["invalid_transition"])
    transaction_state = _load_json_object(paths["transaction_state"])
    transaction_log = _load_jsonl_objects(paths["transaction_log"])

    trap_errors = invalid_transition.get("errors") if isinstance(invalid_transition.get("errors"), list) else []
    trap = {
        "detected": bool(trap_errors),
        "step_id": str(invalid_transition.get("step_id") or "").strip(),
        "errors": [str(item).strip() for item in trap_errors if str(item).strip()],
        "timestamp": str(invalid_transition.get("timestamp") or "").strip(),
    }
    steps = _build_step_records(
        transition_rows=transition_rows,
        verification_rows=verification_rows,
        trap=trap,
    )
    halt = kernel_state.get("halt") if isinstance(kernel_state.get("halt"), dict) else {}
    kernel_state_name = str(kernel_state.get("state") or "").strip()
    halt_reason = str(halt.get("reason") or "none").strip() or "none"
    terminal = {
        "state": kernel_state_name,
        "terminal": halt.get("terminal") is True or kernel_state_name in KERNEL_RUNTIME_TERMINAL_STATES,
        "halt_reason": halt_reason,
        "explanation": _terminal_explanation(
            halt_reason=halt_reason,
            kernel_state_name=kernel_state_name,
            trap=trap,
        ),
    }
    latest_transaction_log = transaction_log[-1] if transaction_log and isinstance(transaction_log[-1], dict) else {}
    transaction = {
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
        "committed_artifact_count": int(
            transaction_state.get("committed_artifact_count")
            or latest_transaction_log.get("committed_artifact_count")
            or 0
        ),
    }
    return {
        "schema_version": "objective-runtime-replay.v1",
        "track_id": track_id,
        "artifact_paths": {name: str(path) for name, path in paths.items()},
        "kernel_summary": {
            "state": kernel_state_name,
            "terminal": terminal["terminal"],
            "halt_reason": halt_reason,
            "active_unit_id": str(kernel_state.get("active_unit_id") or "").strip(),
            "last_verification_id": str(kernel_state.get("last_verification_id") or "").strip(),
            "transition_count": len(transition_rows),
            "verification_count": len(verification_rows),
            "step_count": len(steps),
        },
        "transaction": transaction,
        "terminal": terminal,
        "trap": trap,
        "steps": steps,
    }


def _render_summary(payload: dict[str, Any]) -> str:
    kernel = payload.get("kernel_summary", {}) if isinstance(payload.get("kernel_summary"), dict) else {}
    terminal = payload.get("terminal", {}) if isinstance(payload.get("terminal"), dict) else {}
    trap = payload.get("trap", {}) if isinstance(payload.get("trap"), dict) else {}
    transaction = payload.get("transaction", {}) if isinstance(payload.get("transaction"), dict) else {}
    lines = [
        f"track: {payload.get('track_id', '')}",
        f"kernel state: {kernel.get('state', '') or '(none)'}",
        f"steps: {kernel.get('step_count', 0)}",
        f"transitions: {kernel.get('transition_count', 0)}",
        f"verifications: {kernel.get('verification_count', 0)}",
        f"terminal: {terminal.get('terminal', False)}",
        f"halt reason: {terminal.get('halt_reason', '') or '(none)'}",
        f"terminal explanation: {terminal.get('explanation', '') or '(none)'}",
        (
            "transaction: "
            f"{transaction.get('transaction_id', '') or '(none)'} "
            f"state={transaction.get('state', '') or '(none)'} "
            f"recovered={transaction.get('recovered', False)}"
        ),
        (
            f"trap: step={trap.get('step_id', '') or '(none)'} "
            f"errors={len(trap.get('errors', []))}"
            if trap.get("detected")
            else "trap: none"
        ),
    ]
    return "\n".join(lines)


def _render_timeline(payload: dict[str, Any]) -> str:
    steps = payload.get("steps", []) if isinstance(payload.get("steps"), list) else []
    if not steps:
        return "timeline: (none)"
    lines = ["timeline:"]
    for step in steps:
        if not isinstance(step, dict):
            continue
        lines.append(
            f"- {step.get('step_id', '')}: {step.get('from_state', '') or '(none)'} -> {step.get('to_state', '') or '(none)'} "
            f"(transitions={step.get('transition_count', 0)} verifications={step.get('verification_count', 0)})"
        )
        for transition in step.get("transitions", []):
            if not isinstance(transition, dict):
                continue
            lines.append(
                "  "
                + f"{transition.get('from', '')} -> {transition.get('to', '')} "
                + f"[guard={transition.get('guard', '') or 'n/a'} result={transition.get('guard_result', False)} "
                + f"trigger={transition.get('trigger', '') or 'n/a'}]"
            )
        for verification in step.get("verification_results", []):
            if not isinstance(verification, dict):
                continue
            lines.append(
                "  "
                + f"verification {verification.get('unit_id', '') or '(none)'}: "
                + f"status={verification.get('status', '') or 'n/a'} "
                + f"scope={verification.get('scope', '') or 'n/a'} "
                + f"repairability={verification.get('repairability', '') or 'n/a'} "
                + f"suggested={verification.get('suggested_transition', '') or 'n/a'}"
            )
        trap_errors = step.get("trap_errors", []) if isinstance(step.get("trap_errors"), list) else []
        if trap_errors:
            lines.append("  " + f"trap: {'; '.join(str(item) for item in trap_errors)}")
    return "\n".join(lines)


def _render_trap(payload: dict[str, Any]) -> str:
    trap = payload.get("trap", {}) if isinstance(payload.get("trap"), dict) else {}
    transaction = payload.get("transaction", {}) if isinstance(payload.get("transaction"), dict) else {}
    if not trap.get("detected"):
        return (
            "trap: none\n"
            + f"transaction: {transaction.get('transaction_id', '') or '(none)'} "
            + f"state={transaction.get('state', '') or '(none)'}"
        )
    errors = trap.get("errors", []) if isinstance(trap.get("errors"), list) else []
    lines = [
        f"trap step: {trap.get('step_id', '') or '(none)'}",
        f"timestamp: {trap.get('timestamp', '') or '(none)'}",
        f"transaction: {transaction.get('transaction_id', '') or '(none)'} state={transaction.get('state', '') or '(none)'}",
        f"errors: {len(errors)}",
    ]
    for item in errors:
        lines.append(f"- {item}")
    return "\n".join(lines)


def _render_terminal(payload: dict[str, Any]) -> str:
    terminal = payload.get("terminal", {}) if isinstance(payload.get("terminal"), dict) else {}
    transaction = payload.get("transaction", {}) if isinstance(payload.get("transaction"), dict) else {}
    lines = [
        f"terminal: {terminal.get('terminal', False)}",
        f"state: {terminal.get('state', '') or '(none)'}",
        f"halt reason: {terminal.get('halt_reason', '') or '(none)'}",
        f"explanation: {terminal.get('explanation', '') or '(none)'}",
        f"transaction: {transaction.get('transaction_id', '') or '(none)'} state={transaction.get('state', '') or '(none)'}",
    ]
    return "\n".join(lines)


def render_runtime_replay_text(payload: dict[str, Any], *, selected_view: str) -> str:
    renderers = {
        "summary": _render_summary,
        "timeline": _render_timeline,
        "trap": _render_trap,
        "terminal": _render_terminal,
    }
    return renderers[selected_view](payload)


def main() -> int:
    ensure_python_3_11()
    parser = argparse.ArgumentParser(description="Replay and explain objective kernel runtime artifacts.")
    parser.add_argument("--track-id", required=True)
    parser.add_argument("--artifacts-root", default=None)
    parser.add_argument("--workspace-root", default=None)
    parser.add_argument("--view", default="summary", choices=("summary", "timeline", "trap", "terminal"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    artifacts_root = resolve_artifacts_root(args.artifacts_root, cwd=args.workspace_root)
    payload = load_runtime_replay_payload(track_id=args.track_id, artifacts_root=artifacts_root)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(render_runtime_replay_text(payload, selected_view=args.view))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
