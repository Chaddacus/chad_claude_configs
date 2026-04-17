#!/usr/bin/env bash
set -euo pipefail

APP_HOME="${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}"
SESSION_FILE=""
OUT_FILE="${APP_HOME}/state/route_audit.jsonl"
RALPH_OUT_FILE="${APP_HOME}/state/postflight_audit.jsonl"
MODE="append"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session-file)
      SESSION_FILE="$2"
      shift 2
      ;;
    --out)
      OUT_FILE="$2"
      shift 2
      ;;
    --mode)
      MODE="$2"
      shift 2
      ;;
    --ralph-out)
      RALPH_OUT_FILE="$2"
      shift 2
      ;;
    --help|-h)
      cat <<'USAGE'
Usage: route_audit_extract.sh [--session-file PATH] [--out PATH] [--ralph-out PATH] [--mode append|overwrite]

Extract route telemetry lines from Codex/Claude session JSONL.
Recognizes lines like:
- [route] rule=R2 task=... coordinator=... execution=... fallback=false reason=...
- [route-meta] rule=R2 task=... coordinator=... execution=... fallback=false reason=...
- [ralph-meta] v=1 run=... task=... track=... route=R3 loop=1 gate=... status=... reason_code=... reason="..."
USAGE
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$SESSION_FILE" ]]; then
  SESSION_FILE="$(python3 - "$APP_HOME" <<'PY'
import sys
from pathlib import Path

app_home = Path(sys.argv[1])
files = []

codex_sessions = app_home / "sessions"
if codex_sessions.exists():
    files.extend(codex_sessions.rglob("*.jsonl"))

claude_projects = app_home / "projects"
if claude_projects.exists():
    files.extend(claude_projects.rglob("*.jsonl"))

files = [p for p in files if p.is_file()]
if not files:
    print("")
    raise SystemExit(0)

latest = max(files, key=lambda p: p.stat().st_mtime)
print(str(latest))
PY
)"
fi

if [[ -z "$SESSION_FILE" || ! -f "$SESSION_FILE" ]]; then
  echo "No session file found" >&2
  exit 1
fi

if [[ "$MODE" != "append" && "$MODE" != "overwrite" ]]; then
  echo "Invalid mode: $MODE" >&2
  exit 2
fi

mkdir -p "$(dirname "$OUT_FILE")"
mkdir -p "$(dirname "$RALPH_OUT_FILE")"

python3 - "$SESSION_FILE" "$OUT_FILE" "$RALPH_OUT_FILE" "$MODE" <<'PY'
import json
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

session_file = Path(sys.argv[1]).resolve()
out_file = Path(sys.argv[2]).resolve()
ralph_out_file = Path(sys.argv[3]).resolve()
mode = sys.argv[4]

route_pattern = re.compile(r"^\[(route|route-meta)\]\s+(.+)$", re.MULTILINE)
ralph_pattern = re.compile(r"^\[ralph-meta\]\s+(.+)$", re.MULTILINE)

RULE_TO_RISK = {
    "R1": "low",
    "R2": "medium",
    "R3": "medium",
    "R4": "high",
    "R5": "unknown",
}


def parse_kv(blob: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in shlex.split(blob):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields

if mode == "overwrite":
    out_file.write_text("", encoding="utf-8")
    ralph_out_file.write_text("", encoding="utf-8")

last_limit_name = None
route_records = []
ralph_records = []
assistant_index = 0
existing_route_keys = set()
existing_ralph_keys = set()
ralph_parse_errors = 0

if mode == "append" and out_file.exists():
    for line in out_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = (
            str(rec.get("session_file") or ""),
            str(rec.get("turn_index") or ""),
            str(rec.get("rule_id") or ""),
            str(rec.get("route_task_id") or ""),
            str(rec.get("declared_execution") or ""),
        )
        existing_route_keys.add(key)

if mode == "append" and ralph_out_file.exists():
    for line in ralph_out_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = (
            str(rec.get("session_file") or ""),
            str(rec.get("turn_index") or ""),
            str(rec.get("run_id") or ""),
            str(rec.get("route_task_id") or ""),
            str(rec.get("track_id") or ""),
            str(rec.get("gate") or ""),
            str(rec.get("status") or ""),
        )
        existing_ralph_keys.add(key)

with session_file.open("r", encoding="utf-8") as fh:
    for raw_line in fh:
        line = raw_line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue

        payload = evt.get("payload")
        evt_type = evt.get("type")

        if evt_type == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
            # Session payloads can place rate limits either at payload.rate_limits
            # or payload.info.rate_limits depending on app/runtime version.
            rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
            if not rate_limits:
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                rate_limits = info.get("rate_limits") if isinstance(info.get("rate_limits"), dict) else {}
            limit_name = rate_limits.get("limit_name")
            limit_id = rate_limits.get("limit_id")
            if isinstance(limit_name, str) and limit_name.strip():
                last_limit_name = limit_name.strip()
            elif isinstance(limit_id, str) and limit_id.strip():
                last_limit_name = limit_id.strip()
            continue

        text = ""
        if evt_type == "response_item" and isinstance(payload, dict):
            # Codex/OpenAI-style session event format.
            if payload.get("type") != "message" or payload.get("role") != "assistant":
                continue
            assistant_index += 1
            content = payload.get("content")
            if isinstance(content, list):
                text_fragments = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    t = part.get("type")
                    if t in {"output_text", "input_text"} and isinstance(part.get("text"), str):
                        text_fragments.append(part["text"])
                text = "\n".join(text_fragments).strip()
        elif evt_type == "assistant" and isinstance(evt.get("message"), dict):
            # Claude project JSONL format.
            msg = evt.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            assistant_index += 1
            model_name = msg.get("model")
            if isinstance(model_name, str) and model_name.strip():
                last_limit_name = model_name.strip()
            content = msg.get("content")
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                text_fragments = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text" and isinstance(part.get("text"), str):
                        text_fragments.append(part["text"])
                text = "\n".join(text_fragments).strip()
        else:
            continue

        if not text:
            continue

        route_matches = list(route_pattern.finditer(text))
        ralph_matches = list(ralph_pattern.finditer(text))

        for match in route_matches:
            fields = parse_kv(match.group(2))
            fallback_raw = (fields.get("fallback") or "false").strip().lower()
            fallback_used = fallback_raw in {"1", "true", "yes"}
            rule_id = fields.get("rule")
            declared_execution = fields.get("execution")
            # Ignore placeholder/template examples (e.g. <R#>, <agent:model@effort>).
            if not isinstance(rule_id, str) or not re.fullmatch(r"R[1-5]", rule_id):
                continue
            if isinstance(declared_execution, str) and "<" in declared_execution and ">" in declared_execution:
                continue
            risk_class = fields.get("risk") or RULE_TO_RISK.get(rule_id)

            record = {
                "timestamp": evt.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                "session_file": str(session_file),
                "turn_index": assistant_index,
                "route_task_id": fields.get("task"),
                "rule_id": rule_id,
                "risk_class": risk_class,
                "declared_execution": declared_execution,
                "observed_limit_name": last_limit_name,
                "fallback_used": fallback_used,
                "fallback_reason": fields.get("reason"),
            }
            key = (
                str(record["session_file"] or ""),
                str(record["turn_index"] or ""),
                str(record["rule_id"] or ""),
                str(record["route_task_id"] or ""),
                str(record["declared_execution"] or ""),
            )
            if key in existing_route_keys:
                continue
            existing_route_keys.add(key)
            route_records.append(record)

        for match in ralph_matches:
            fields = parse_kv(match.group(1))
            required = ("v", "run", "task", "track", "route", "loop", "gate", "status", "reason_code", "elapsed_ms")
            if any((fields.get(req) or "").strip() == "" for req in required):
                ralph_parse_errors += 1
                continue
            if fields.get("v") != "1":
                ralph_parse_errors += 1
                continue
            status = str(fields.get("status") or "").strip().lower()
            if status not in {"approve", "revise", "blocked", "error"}:
                ralph_parse_errors += 1
                continue
            try:
                loop_value = int(str(fields.get("loop") or "0"))
                elapsed_ms = int(str(fields.get("elapsed_ms") or "0"))
            except ValueError:
                ralph_parse_errors += 1
                continue

            record = {
                "timestamp": evt.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                "session_file": str(session_file),
                "turn_index": assistant_index,
                "record_type": "ralph_meta",
                "version": fields.get("v"),
                "run_id": fields.get("run"),
                "route_task_id": fields.get("task"),
                "track_id": fields.get("track"),
                "route_class": fields.get("route"),
                "loop": loop_value,
                "gate": fields.get("gate"),
                "status": status,
                "reason_code": fields.get("reason_code"),
                "reason": fields.get("reason"),
                "elapsed_ms": elapsed_ms,
                "finalize_json_path": fields.get("finalize_json_path"),
                "exit_code": fields.get("exit_code"),
                "observed_limit_name": last_limit_name,
            }
            key = (
                str(record["session_file"] or ""),
                str(record["turn_index"] or ""),
                str(record["run_id"] or ""),
                str(record["route_task_id"] or ""),
                str(record["track_id"] or ""),
                str(record["gate"] or ""),
                str(record["status"] or ""),
            )
            if key in existing_ralph_keys:
                continue
            existing_ralph_keys.add(key)
            ralph_records.append(record)

if route_records:
    with out_file.open("a", encoding="utf-8") as out:
        for record in route_records:
            out.write(json.dumps(record, sort_keys=True) + "\n")

if ralph_records:
    with ralph_out_file.open("a", encoding="utf-8") as out:
        for record in ralph_records:
            out.write(json.dumps(record, sort_keys=True) + "\n")

print(json.dumps({
    "status": "ok",
    "session_file": str(session_file),
    "out_file": str(out_file),
    "route_records_written": len(route_records),
    "ralph_out_file": str(ralph_out_file),
    "ralph_records_written": len(ralph_records),
    "ralph_parse_errors": ralph_parse_errors,
}, sort_keys=True))
PY
