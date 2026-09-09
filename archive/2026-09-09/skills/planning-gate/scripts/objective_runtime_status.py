#!/usr/bin/env python3
"""Render a read-only control-room view of objective runtime state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import ensure_python_3_11, resolve_artifacts_root, runtime_artifact_paths


def load_operator_view_payload(*, track_id: str, artifacts_root: Path) -> dict[str, Any]:
    paths = runtime_artifact_paths(artifacts_root=artifacts_root, track_id=track_id)
    runtime_state_path = paths["runtime_state"]
    operator_view_path = paths["operator_view"]
    transaction_state_path = paths["transaction_state"]
    transaction_log_path = paths["transaction_log"]
    runtime_state = {}
    if runtime_state_path.exists():
        runtime_state = json.loads(runtime_state_path.read_text(encoding="utf-8"))
        if not isinstance(runtime_state, dict):
            raise ValueError("runtime_state_not_object")
    payload: dict[str, Any] = {}
    if operator_view_path.exists():
        payload = json.loads(operator_view_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("operator_view_not_object")
    transaction_state: dict[str, Any] = {}
    if transaction_state_path.exists():
        transaction_state = json.loads(transaction_state_path.read_text(encoding="utf-8"))
        if not isinstance(transaction_state, dict):
            raise ValueError("transaction_state_not_object")
    latest_transaction_log: dict[str, Any] = {}
    if transaction_log_path.exists():
        for line in transaction_log_path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw:
                continue
            item = json.loads(raw)
            if isinstance(item, dict):
                latest_transaction_log = item
    if runtime_state:
        payload = {
            **payload,
            "track_id": str(runtime_state.get("track_id") or payload.get("track_id") or track_id),
            "route_hint": str(runtime_state.get("route_hint") or payload.get("route_hint") or ""),
            "closure_state": str(runtime_state.get("closure_state") or payload.get("closure_state") or ""),
            "lifecycle_status": str(runtime_state.get("lifecycle_status") or payload.get("lifecycle_status") or ""),
            "current_cycle_id": str(runtime_state.get("current_cycle_id") or payload.get("current_cycle_id") or ""),
            "current_frontier": runtime_state.get("current_frontier")
            if isinstance(runtime_state.get("current_frontier"), list)
            else payload.get("current_frontier", []),
            "current_packet": str(runtime_state.get("current_packet") or payload.get("current_packet") or ""),
            "next_recommended_packet": str(runtime_state.get("next_recommended_packet") or payload.get("next_recommended_packet") or ""),
            "required_work_remaining": runtime_state.get("required_work_remaining"),
            "material_optional_work_remaining": runtime_state.get("material_optional_work_remaining"),
            "stop_allowed": runtime_state.get("stop_allowed"),
            "stop_reason": str(runtime_state.get("stop_reason") or payload.get("stop_reason") or ""),
            "unsupported_closure_risk": str(runtime_state.get("unsupported_closure_risk") or payload.get("unsupported_closure_risk") or "none"),
            "last_verifier_result": runtime_state.get("last_verifier_result", {})
            if isinstance(runtime_state.get("last_verifier_result"), dict)
            else {},
            "authoritative_runtime_state_artifact": str(runtime_state_path),
        }
    payload["transaction"] = {
        **(payload.get("transaction", {}) if isinstance(payload.get("transaction"), dict) else {}),
        "transaction_id": str(
            transaction_state.get("transaction_id")
            or payload.get("transaction", {}).get("transaction_id")
            or latest_transaction_log.get("transaction_id")
            or ""
        ),
        "state": str(
            transaction_state.get("state")
            or payload.get("transaction", {}).get("state")
            or latest_transaction_log.get("state")
            or ""
        ),
        "step_id": str(
            transaction_state.get("step_id")
            or payload.get("transaction", {}).get("step_id")
            or latest_transaction_log.get("step_id")
            or ""
        ),
        "recovered": (
            transaction_state.get("state") == "recovered"
            or payload.get("transaction", {}).get("recovered") is True
            or latest_transaction_log.get("recovered") is True
            or str(transaction_state.get("recovery_outcome") or "").strip() == "finished_commit"
        ),
        "recovery_outcome": str(
            transaction_state.get("recovery_outcome")
            or payload.get("transaction", {}).get("recovery_outcome")
            or ""
        ),
        "updated_at": str(
            transaction_state.get("updated_at")
            or payload.get("transaction", {}).get("updated_at")
            or latest_transaction_log.get("timestamp")
            or ""
        ),
        "committed_artifact_count": int(
            transaction_state.get("committed_artifact_count")
            or payload.get("transaction", {}).get("committed_artifact_count")
            or latest_transaction_log.get("committed_artifact_count")
            or 0
        ),
        "artifact_paths": {
            "transaction_state": str(transaction_state_path),
            "transaction_log": str(transaction_log_path),
        },
    }
    artifacts = payload.get("artifacts", {}) if isinstance(payload.get("artifacts"), dict) else {}
    artifacts["transaction_state"] = str(transaction_state_path)
    artifacts["transaction_log"] = str(transaction_log_path)
    payload["artifacts"] = artifacts
    return payload


def _short_path(path_text: str) -> str:
    path = Path(path_text)
    parts = path.parts
    return "/".join(parts[-4:]) if len(parts) >= 4 else path_text


def _render_summary(view: dict[str, Any]) -> str:
    packet_counts = view.get("packet_counts", {}) if isinstance(view.get("packet_counts"), dict) else {}
    health = view.get("health_signals", {}) if isinstance(view.get("health_signals"), dict) else {}
    trust = view.get("trust_report", {}) if isinstance(view.get("trust_report"), dict) else {}
    verifier = view.get("last_verifier_result", {}) if isinstance(view.get("last_verifier_result"), dict) else {}
    transaction = view.get("transaction", {}) if isinstance(view.get("transaction"), dict) else {}
    lines = [
        f"track: {view.get('track_id', '')}",
        f"route: {view.get('route_hint', '')}",
        f"lifecycle: {view.get('lifecycle_status', '') or '(none)'}",
        f"closure: {view.get('closure_state', '')}",
        f"cycle: {view.get('current_cycle_id', '') or '(none)'}",
        f"frontier: {', '.join(view.get('current_frontier', [])) or '(none)'}",
        f"current packet: {view.get('current_packet', '') or '(none)'}",
        f"next packet: {view.get('next_recommended_packet', '') or '(none)'}",
        f"next action: {view.get('next_action', '') or '(none)'}",
        f"stop allowed: {view.get('stop_allowed', False)}",
        f"stop reason: {view.get('stop_reason', '') or '(none)'}",
        f"required work remaining: {view.get('required_work_remaining', False)}",
        f"material optional work remaining: {view.get('material_optional_work_remaining', False)}",
        f"verifier: {str(verifier.get('status', '')).strip() or '(none)'}",
        (
            "transaction: "
            f"{str(transaction.get('transaction_id') or '').strip() or '(none)'} "
            f"state={str(transaction.get('state') or '').strip() or '(none)'} "
            f"recovered={transaction.get('recovered', False)}"
        ),
        (
            "packets: "
            f"accepted={packet_counts.get('accepted', 0)} "
            f"queued={packet_counts.get('queued', 0)} "
            f"blocked={packet_counts.get('blocked', 0)} "
            f"escalated={packet_counts.get('escalated', 0)}"
        ),
        f"strategies: {', '.join(view.get('strategy_mix', [])) or '(none)'}",
        f"fallback packets: {', '.join(view.get('fallback_packets', [])) or '(none)'}",
        f"trust: {trust.get('closure_strength', '') or '(unknown)'}",
        f"support: {str(view.get('support_confidence', {}).get('objective_support_status', '')).strip() or '(unknown)'}",
        (
            "health: "
            f"safe_momentum={health.get('safe_momentum_available', False)} "
            f"closure_strong={health.get('closure_claim_is_strong', False)} "
            f"human_attention={health.get('human_attention_required', False)}"
        ),
        f"why next: {str(view.get('explanations', {}).get('why_this_is_next', '')).strip() or '(none)'}",
        f"why blocked: {str(view.get('explanations', {}).get('why_blocked', '')).strip() or '(none)'}",
    ]
    return "\n".join(lines)


def _render_why_blocked(view: dict[str, Any]) -> str:
    blockers = view.get("blockers", []) if isinstance(view.get("blockers"), list) else []
    if not blockers:
        return "blocked: (none)"
    lines = ["blocked:"]
    for item in blockers:
        if not isinstance(item, dict):
            continue
        packet = str(item.get("packet_id") or item.get("lane") or "").strip()
        prefix = f"- {packet}: " if packet else "- "
        lines.append(prefix + str(item.get("message") or "").strip())
        source = str(item.get("source_artifact") or "").strip()
        if source:
            lines.append(f"  source: {_short_path(source)}")
        proof = str(item.get("proof_artifact") or "").strip()
        if proof:
            lines.append(f"  proof: {_short_path(proof)}")
        cycle_id = str(item.get("cycle_id") or "").strip()
        if cycle_id:
            lines.append(f"  cycle: {cycle_id}")
    return "\n".join(lines)


def _render_frontier(view: dict[str, Any]) -> str:
    timeline = view.get("timeline", []) if isinstance(view.get("timeline"), list) else []
    latest = timeline[-1] if timeline and isinstance(timeline[-1], dict) else {}
    lines = [
        f"frontier: {', '.join(view.get('current_frontier', [])) or '(none)'}",
        f"safe momentum: {view.get('safe_momentum_available', False)}",
        f"frontier change: {str(view.get('explanations', {}).get('frontier_change_explanation', '')).strip() or '(none)'}",
    ]
    if latest:
        lines.append(f"last cycle: {latest.get('cycle_id', '')}")
        lines.append(f"last movement: {latest.get('frontier_movement', False)} ({latest.get('frontier_movement_reason', '') or 'n/a'})")
    return "\n".join(lines)


def _render_validation(view: dict[str, Any]) -> str:
    coverage = view.get("validation_coverage", []) if isinstance(view.get("validation_coverage"), list) else []
    if not coverage:
        return "validation: (none)"
    lines = ["validation:"]
    for lane in coverage:
        if not isinstance(lane, dict):
            continue
        lines.append(
            f"- {lane.get('lane', '')}: status={lane.get('status', '')} required={lane.get('required', False)} "
            f"generated={len(lane.get('generated_packet_ids', []))} accepted={len(lane.get('accepted_packet_ids', []))}"
        )
        if lane.get("manual_only_blocker"):
            lines.append(f"  blocker: {lane['manual_only_blocker']}")
        if lane.get("reasons"):
            lines.append(f"  reasons: {', '.join(lane['reasons'])}")
        artifact_ref = lane.get("artifact_ref", {}) if isinstance(lane.get("artifact_ref"), dict) else {}
        if artifact_ref.get("source"):
            lines.append(f"  source: {_short_path(str(artifact_ref['source']))}")
    return "\n".join(lines)


def _render_execution(view: dict[str, Any]) -> str:
    health = view.get("health_signals", {}) if isinstance(view.get("health_signals"), dict) else {}
    coverage = view.get("execution_coverage", {}) if isinstance(view.get("execution_coverage"), dict) else {}
    fallback_analysis = view.get("explanations", {}).get("fallback_analysis", {})
    lines = [
        f"strategies: {', '.join(view.get('strategy_mix', [])) or '(none)'}",
        f"fallback packets: {', '.join(view.get('fallback_packets', [])) or '(none)'}",
        f"fallback ratio: {health.get('fallback_ratio', 0.0):.2f}",
        f"fallback threshold: {health.get('fallback_warning_threshold', 0.0):.2f}",
        f"fallback burden high: {health.get('fallback_burden_high', False)}",
        f"deterministic coverage: {coverage.get('deterministic_ratio', 0.0):.2f}",
        f"non-review deterministic coverage: {coverage.get('non_review_deterministic_ratio', 0.0):.2f}",
        f"analysis: {str(fallback_analysis.get('message') or '').strip() or '(none)'}",
    ]
    return "\n".join(lines)


def _render_checkpoint(view: dict[str, Any]) -> str:
    checkpoint = view.get("checkpoint_health", {}) if isinstance(view.get("checkpoint_health"), dict) else {}
    lines = [
        f"checkpoint blocked: {checkpoint.get('checkpoint_blocked', False)}",
        f"checkpoint reason: {checkpoint.get('checkpoint_block_reason', '') or '(none)'}",
        f"checkpoint commit: {checkpoint.get('checkpoint_commit', '') or '(none)'}",
        f"rollback proof: {_short_path(str(checkpoint.get('rollback_validation_ref') or '')) if checkpoint.get('rollback_validation_ref') else '(none)'}",
        f"source: {_short_path(str(checkpoint.get('source_artifact') or '')) if checkpoint.get('source_artifact') else '(none)'}",
    ]
    return "\n".join(lines)


def _render_timeline(view: dict[str, Any]) -> str:
    timeline = view.get("timeline", []) if isinstance(view.get("timeline"), list) else []
    if not timeline:
        return "timeline: (none)"
    lines = ["timeline:"]
    for item in timeline:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"- {item.get('cycle_id', '')}: packets={', '.join(item.get('packet_ids', [])) or '(none)'} "
            f"movement={item.get('frontier_movement', False)} "
            f"reason={item.get('frontier_movement_reason', '') or 'n/a'} "
            f"closure={item.get('closure_state', '') or 'n/a'}"
        )
        checkpoint = item.get("checkpoint_outcome", {}) if isinstance(item.get("checkpoint_outcome"), dict) else {}
        if checkpoint:
            lines.append(
                f"  checkpoint: blocked={checkpoint.get('blocked', False)} reason={checkpoint.get('reason', '') or 'n/a'}"
            )
        artifacts = item.get("artifacts", {}) if isinstance(item.get("artifacts"), dict) else {}
        if artifacts.get("review"):
            lines.append(f"  review: {_short_path(str(artifacts['review']))}")
        if item.get("blockers_introduced"):
            lines.append(f"  blockers introduced: {', '.join(item['blockers_introduced'])}")
        if item.get("blockers_cleared"):
            lines.append(f"  blockers cleared: {', '.join(item['blockers_cleared'])}")
    return "\n".join(lines)


def _render_capabilities(view: dict[str, Any]) -> str:
    summary = view.get("repo_capabilities_summary", {}) if isinstance(view.get("repo_capabilities_summary"), dict) else {}
    enabled = summary.get("enabled_lanes", []) if isinstance(summary.get("enabled_lanes"), list) else []
    lines = [
        f"enabled lanes: {', '.join(enabled) or '(none)'}",
        f"detectors: {', '.join(summary.get('detectors_run', [])) or '(none)'}",
        f"low confidence lanes: {', '.join(summary.get('low_confidence_lanes', [])) or '(none)'}",
        f"source: {_short_path(str(summary.get('source_artifact') or '')) if summary.get('source_artifact') else '(none)'}",
    ]
    missing = summary.get("missing_capabilities", {}) if isinstance(summary.get("missing_capabilities"), dict) else {}
    if missing:
        lines.append("missing:")
        for lane, reason in sorted(missing.items()):
            lines.append(f"- {lane}: {reason}")
    support = view.get("support_confidence", {}) if isinstance(view.get("support_confidence"), dict) else {}
    if str(support.get("unsupported_closure_risk") or "none").strip() not in {"", "none"}:
        lines.append(f"unsupported closure risk: {support.get('unsupported_closure_risk')}")
    return "\n".join(lines)


def _render_packet_quality(view: dict[str, Any]) -> str:
    summary = view.get("packet_quality_summary", {}) if isinstance(view.get("packet_quality_summary"), dict) else {}
    budget = summary.get("budget", {}) if isinstance(summary.get("budget"), dict) else {}
    lines = [
        f"packet count: {summary.get('packet_count', 0)}",
        f"budget status: {budget.get('status', '') or '(none)'}",
        f"fallback ratio: {budget.get('fallback_ratio', 0.0):.2f}",
        f"hard fail packets: {', '.join(summary.get('hard_fail_packet_ids', [])) or '(none)'}",
        f"warning packets: {', '.join(summary.get('warning_packet_ids', [])) or '(none)'}",
        f"source: {_short_path(str(summary.get('source_artifact') or '')) if summary.get('source_artifact') else '(none)'}",
    ]
    return "\n".join(lines)


def _render_adaptation(view: dict[str, Any]) -> str:
    summary = view.get("adaptation_summary", {}) if isinstance(view.get("adaptation_summary"), dict) else {}
    events = summary.get("events", []) if isinstance(summary.get("events"), list) else []
    lines = [
        f"adaptation events: {summary.get('event_count', 0)}",
        f"source: {_short_path(str(summary.get('source_artifact') or '')) if summary.get('source_artifact') else '(none)'}",
    ]
    for item in events:
        if not isinstance(item, dict):
            continue
        lines.append(
            f"- {item.get('packet_id', '')}: {item.get('old_strategy', '') or '(new)'} -> {item.get('new_strategy', '') or '(none)'} "
            f"because {item.get('policy_basis', '') or 'n/a'}"
        )
    return "\n".join(lines)


def _render_trust(view: dict[str, Any]) -> str:
    trust = view.get("trust_report", {}) if isinstance(view.get("trust_report"), dict) else {}
    support = view.get("support_confidence", {}) if isinstance(view.get("support_confidence"), dict) else {}
    lines = [
        f"closure strength: {trust.get('closure_strength', '') or '(unknown)'}",
        f"validation coverage: {trust.get('validation_coverage_strength', '') or '(unknown)'}",
        f"checkpoint confidence: {trust.get('checkpoint_confidence', '') or '(unknown)'}",
        f"fallback dependence: {trust.get('fallback_dependence', 0.0):.2f}",
        f"deterministic coverage: {trust.get('deterministic_coverage', 0.0):.2f}",
        f"non-review deterministic coverage: {trust.get('non_review_deterministic_coverage', 0.0):.2f}",
        f"low confidence lanes: {', '.join(trust.get('low_confidence_lanes', [])) or '(none)'}",
        f"manual dependencies: {', '.join(trust.get('manual_dependencies', [])) or '(none)'}",
        f"weakening factors: {', '.join(trust.get('weakening_factors', [])) or '(none)'}",
        f"support status: {str(support.get('objective_support_status') or '').strip() or '(unknown)'}",
        f"unsupported closure risk: {str(support.get('unsupported_closure_risk') or '').strip() or '(none)'}",
        f"support remediation available: {support.get('support_remediation_available', False)}",
        f"support gaps: {', '.join(support.get('support_gap_reasons', [])) or '(none)'}",
    ]
    return "\n".join(lines)


def _render_swarm(view: dict[str, Any]) -> str:
    lines = [
        f"swarm status: {view.get('swarm_status', '') or '(none)'}",
        f"execution shape: {view.get('execution_shape', '') or '(none)'}",
        f"awaiting verifier: {', '.join(view.get('awaiting_verifier', [])) or '(none)'}",
        f"awaiting reviewer barrier: {', '.join(view.get('awaiting_reviewer_barrier', [])) or '(none)'}",
        f"convergence status: {view.get('convergence_status', '') or '(none)'}",
    ]
    return "\n".join(lines)


def _render_lanes(view: dict[str, Any]) -> str:
    lane_state = view.get("lane_state", {}) if isinstance(view.get("lane_state"), dict) else {}
    lane_queue_depths = view.get("lane_queue_depths", {}) if isinstance(view.get("lane_queue_depths"), dict) else {}
    lines = ["lanes:"]
    for lane in ("explorer", "worker", "validator", "reviewer"):
        active = lane_state.get(lane, []) if isinstance(lane_state.get(lane), list) else []
        lines.append(f"- {lane}: active={', '.join(active) or '(none)'} queue={lane_queue_depths.get(lane, 0)}")
    return "\n".join(lines)


def _render_frontier_why(view: dict[str, Any]) -> str:
    blocked = view.get("dispatch_block_reasons", {}) if isinstance(view.get("dispatch_block_reasons"), dict) else {}
    lines = [
        f"runnable but not dispatched: {', '.join(view.get('runnable_but_not_dispatched', [])) or '(none)'}",
    ]
    for packet_id, reasons in sorted(blocked.items()):
        reason_list = reasons if isinstance(reasons, list) else [str(reasons)]
        lines.append(f"- {packet_id}: {', '.join(str(item) for item in reason_list if str(item).strip()) or '(none)'}")
    return "\n".join(lines)


def _render_convergence(view: dict[str, Any]) -> str:
    lines = [
        f"convergence status: {view.get('convergence_status', '') or '(none)'}",
        f"awaiting verifier: {', '.join(view.get('awaiting_verifier', [])) or '(none)'}",
        f"awaiting reviewer barrier: {', '.join(view.get('awaiting_reviewer_barrier', [])) or '(none)'}",
    ]
    return "\n".join(lines)


def _render_benchmark(view: dict[str, Any]) -> str:
    summary = view.get("benchmark_summary", {}) if isinstance(view.get("benchmark_summary"), dict) else {}
    runs = summary.get("runs", []) if isinstance(summary.get("runs"), list) else []
    lines = [
        f"archetype: {summary.get('archetype', '') or '(none)'}",
        f"baseline: {summary.get('baseline_mode', '') or '(none)'}",
        f"recommended: {summary.get('recommended_mode', '') or '(none)'}",
        f"swarm helped: {summary.get('swarm_outperformed_serial', False)}",
        f"serial better: {summary.get('serial_better', False)}",
        f"reason: {summary.get('reason', '') or '(none)'}",
    ]
    for run in runs:
        if not isinstance(run, dict):
            continue
        lines.append(
            f"- {run.get('mode', '')}: closure={run.get('final_closure_state', '') or 'n/a'} "
            f"cycles={run.get('cycles_to_closure', 0)} "
            f"duration={float(run.get('wall_clock_seconds', 0.0)):.4f}s "
            f"fallback={float(run.get('fallback_ratio', 0.0)):.2f}"
        )
    return "\n".join(lines)


def _render_canary(view: dict[str, Any]) -> str:
    summary = view.get("canary_summary", {}) if isinstance(view.get("canary_summary"), dict) else {}
    metrics = summary.get("metrics", {}) if isinstance(summary.get("metrics"), dict) else {}
    lines = [
        f"route: {summary.get('route_hint', '') or '(none)'}",
        f"execution shape: {summary.get('execution_shape', '') or '(none)'}",
        f"safe to run: {summary.get('safe_to_run', False)}",
        f"refused: {summary.get('refused', False)}",
        f"refusal reason: {summary.get('refusal_reason', '') or '(none)'}",
        f"safety mode: {summary.get('safety_mode', '') or '(none)'}",
        f"isolation mode: {summary.get('isolation_mode', '') or '(none)'}",
        f"workspace: {summary.get('workspace_root', '') or '(none)'}",
        f"isolated workspace: {summary.get('isolated_workspace_root', '') or '(none)'}",
    ]
    if metrics:
        lines.append(
            f"metrics: closure={metrics.get('final_closure_state', '') or 'n/a'} "
            f"cycles={metrics.get('cycles_to_closure', 0)} "
            f"duration={float(metrics.get('wall_clock_seconds', 0.0)):.4f}s"
        )
    return "\n".join(lines)


def _render_evaluation(view: dict[str, Any]) -> str:
    summary = view.get("evaluation_summary", {}) if isinstance(view.get("evaluation_summary"), dict) else {}
    lines = [
        f"has benchmark: {summary.get('has_benchmark', False)}",
        f"has canary: {summary.get('has_canary', False)}",
        f"recommended mode: {summary.get('recommended_mode', '') or '(none)'}",
        f"swarm helped: {summary.get('swarm_outperformed_serial', False)}",
        f"serial better: {summary.get('serial_better', False)}",
        f"benchmark reason: {summary.get('benchmark_reason', '') or '(none)'}",
        f"canary refused: {summary.get('canary_refused', False)}",
        f"canary refusal reason: {summary.get('canary_refusal_reason', '') or '(none)'}",
        f"canary safety mode: {summary.get('canary_safety_mode', '') or '(none)'}",
        f"canary isolation mode: {summary.get('canary_isolation_mode', '') or '(none)'}",
        f"closure strength: {summary.get('closure_strength', '') or '(unknown)'}",
    ]
    return "\n".join(lines)


def render_operator_view_text(view: dict[str, Any], *, selected_view: str) -> str:
    renderers = {
        "summary": _render_summary,
        "why-blocked": _render_why_blocked,
        "frontier": _render_frontier,
        "frontier-why": _render_frontier_why,
        "validation": _render_validation,
        "execution": _render_execution,
        "checkpoint": _render_checkpoint,
        "timeline": _render_timeline,
        "capabilities": _render_capabilities,
        "packet-quality": _render_packet_quality,
        "adaptation": _render_adaptation,
        "trust": _render_trust,
        "swarm": _render_swarm,
        "lanes": _render_lanes,
        "convergence": _render_convergence,
        "benchmark": _render_benchmark,
        "canary": _render_canary,
        "evaluation": _render_evaluation,
    }
    return renderers[selected_view](view)


def main() -> int:
    ensure_python_3_11()
    parser = argparse.ArgumentParser(description="Render objective runtime operator view.")
    parser.add_argument("--track-id", required=True)
    parser.add_argument("--artifacts-root", default=None)
    parser.add_argument("--workspace-root", default=None)
    parser.add_argument(
        "--view",
        default="summary",
        choices=("summary", "why-blocked", "frontier", "frontier-why", "validation", "execution", "checkpoint", "timeline", "capabilities", "packet-quality", "adaptation", "trust", "swarm", "lanes", "convergence", "benchmark", "canary", "evaluation"),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    artifacts_root = resolve_artifacts_root(args.artifacts_root, cwd=args.workspace_root)
    operator_view = load_operator_view_payload(track_id=args.track_id, artifacts_root=artifacts_root)

    if args.json:
        print(json.dumps(operator_view, indent=2, sort_keys=True))
        return 0

    print(render_operator_view_text(operator_view, selected_view=args.view))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
