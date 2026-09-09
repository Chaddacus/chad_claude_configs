#!/usr/bin/env python3
"""Report packet strategy compatibility gaps before fail-closed execution enforcement."""

from __future__ import annotations

import argparse
import json

from common import ensure_python_3_11, load_json_file
from execution_strategies import validate_strategy_packet


def build_report(plan_payload: dict) -> dict:
    packets = [
        packet
        for packet in plan_payload.get("packets", [])
        if isinstance(packet, dict) and str(packet.get("packet_id", "")).strip()
    ]
    rows = []
    for packet in packets:
        packet_id = str(packet.get("packet_id", "")).strip()
        missing, blocked = validate_strategy_packet(packet)
        legacy_markers = []
        if packet.get("execution_command") and str(packet.get("execution_strategy") or "").strip() not in {
            "command_capture",
            "test_command",
            "validation_command",
            "lint_command",
            "typecheck_command",
            "build_command",
            "smoke_command",
            "schema_check_command",
            "multi_command_pipeline",
        }:
            legacy_markers.append("command_field_without_command_strategy")
        if not isinstance(packet.get("strategy_inputs"), dict):
            legacy_markers.append("missing_strategy_inputs")
        if not str(packet.get("execution_strategy") or "").strip():
            legacy_markers.append("implicit_generic_execution")
        if str(packet.get("execution_strategy") or "").strip() == "codex_prompt_worker" and not str(packet.get("fallback_reason") or "").strip():
            legacy_markers.append("fallback_reason_missing")
        rows.append(
            {
                "packet_id": packet_id,
                "missing": missing,
                "blocked": blocked,
                "legacy_markers": legacy_markers,
                "compatible": not missing and not blocked and not legacy_markers,
            }
        )
    return {
        "schema_version": "packet-strategy-compat-report.v1",
        "packet_count": len(rows),
        "compatible_count": sum(1 for row in rows if row["compatible"]),
        "rows": rows,
    }


def main() -> int:
    ensure_python_3_11()
    parser = argparse.ArgumentParser(description="Generate packet strategy compatibility report.")
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    plan_payload = load_json_file(args.plan_json)
    report = build_report(plan_payload)
    payload = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
