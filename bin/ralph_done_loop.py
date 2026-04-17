#!/usr/bin/env python3
"""Postflight Completion Gate (Ralph Loop) v1.4."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
LOCK_STALE_SECONDS = 3600
REASON_MAX_LEN = 160
TRANSIENT_REASON_CODES = {"TIMEOUT", "TEMPORARY_IO", "SPAWN_EAGAIN"}
REQUIRED_PROVENANCE_FIELDS = {
    "generated_by",
    "generated_at",
    "artifact_sha256",
    "tool_version",
}
DEFAULT_PROGRAM_CLOSE_COMMAND = ["npm", "run", "release-close-gate"]
DEFAULT_PROGRAM_CLOSE_REPORT = "planning_artifacts/{track_id}/release/program-scoreboard.json"
REQUIRED_EXTERNAL_SEQUENCE = [
    "validate_plan",
    "run_cmd_capture_25",
    "run_cmd_capture_50",
    "run_cmd_capture_75",
    "run_cmd_capture_100",
    "validate_impl",
    "planning_gate_finalize",
    "planning_gate_finalize_repeat",
    "program_close_gate",
]
REQUIRED_DOD_CATEGORIES = ["correctness", "tests", "security", "observability", "rollback"]
REQUIRED_SCOREBOARD_SCHEMA = "program-scoreboard.v2"


@dataclass
class GateResult:
    gate_name: str
    status: str
    reason_code: str
    reason: str
    missing_fields: list[str]
    blocked_fields: list[str]
    elapsed_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate_name": self.gate_name,
            "status": self.status,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "missing_fields": self.missing_fields,
            "blocked_fields": self.blocked_fields,
            "elapsed_ms": self.elapsed_ms,
        }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitize_token(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or "unnamed"


def safe_reason(value: str) -> str:
    trimmed = (value or "").replace("\n", " ").replace("\r", " ").strip()
    return trimmed[:REASON_MAX_LEN]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    path.write_bytes(data + b"\n")


def load_json(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    if len(data) > MAX_ARTIFACT_BYTES:
        raise RuntimeError(f"artifact_too_large:{path}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"artifact_non_utf8:{path}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"artifact_invalid_json:{path}:{exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"artifact_not_object:{path}")
    return payload


def validate_artifact_path(path: Path, allowed_roots: list[Path]) -> Path:
    if not path.is_absolute():
        raise RuntimeError(f"path_not_absolute:{path}")
    resolved = path.resolve()
    if ".." in path.parts:
        raise RuntimeError(f"path_contains_dotdot:{path}")
    for root in allowed_roots:
        if resolved == root or root in resolved.parents:
            return resolved
    raise RuntimeError(f"path_outside_allowed_roots:{resolved}")


def resolve_planning_gate_scripts(codex_home: Path) -> Path:
    candidates: list[Path] = []
    override = os.environ.get("PLANNING_GATE_SKILL_DIR", "").strip()
    if override:
        override_path = Path(override).expanduser().resolve()
        candidates.append(override_path if override_path.name == "scripts" else override_path / "scripts")
    candidates.extend(
        [
            codex_home / "skills" / "planning-gate" / "scripts",
            codex_home / "skills" / "codex-planning-gate" / "scripts",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def ensure_provenance(path: Path, payload: dict[str, Any], run_id: str) -> tuple[dict[str, Any], list[str]]:
    missing: list[str] = []
    for field in REQUIRED_PROVENANCE_FIELDS:
        if not isinstance(payload.get(field), str) or not str(payload.get(field)).strip():
            missing.append(field)
    if not (isinstance(payload.get("source_run_id"), str) or isinstance(payload.get("source_command"), str)):
        missing.append("source_run_id_or_source_command")

    # Always hard-set source_run_id to bind this loop artifact lineage.
    payload["source_run_id"] = run_id
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    payload["artifact_sha256"] = sha256_bytes(data)
    return payload, missing


def emit_ralph_meta(
    *,
    telemetry_path: Path,
    run_id: str,
    route_task_id: str,
    track_id: str,
    route_class: str,
    loop_idx: int,
    gate: str,
    status: str,
    reason_code: str,
    reason: str,
    elapsed_ms: int,
    finalize_json_path: Path | None,
    exit_code: int | None,
) -> None:
    line = (
        f"[ralph-meta] v=1 run={run_id} task={route_task_id} track={track_id} route={route_class} "
        f"loop={loop_idx} gate={gate} status={status} reason_code={reason_code} "
        f'reason="{safe_reason(reason)}" elapsed_ms={elapsed_ms} '
        f"finalize_json_path={str(finalize_json_path) if finalize_json_path else ''} "
        f"exit_code={'' if exit_code is None else exit_code}"
    )
    with telemetry_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line)


def write_blocker_artifact(
    *,
    codex_home: Path,
    route_task_id: str,
    track_id: str,
    route_class: str,
    run_id: str,
    loop_count: int,
    reason_code: str,
    missing_fields: list[str],
    blocked_fields: list[str],
    run_dir: Path,
    stdout_log_path: Path,
    stderr_log_path: Path,
    status: str = "blocked",
    finalize_json_path: str = "",
) -> Path:
    blocker_artifact = codex_home / "state" / "postflight_done" / route_task_id / track_id / "finalize.blocked.json"
    payload = {
        "schema_version": "postflight_blocker.v1",
        "accepted_type": "ACCEPTED_BLOCKED",
        "run_id": run_id,
        "route_task_id": route_task_id,
        "track_id": track_id,
        "route_class": route_class,
        "loop_count": loop_count,
        "status": status,
        "reason_code": reason_code,
        "missing_fields": missing_fields,
        "blocked_fields": blocked_fields,
        "all_loop_artifact_paths": [str(p) for p in sorted(run_dir.glob("*.json"))],
        "stdout_log_path": str(stdout_log_path),
        "stderr_log_path": str(stderr_log_path),
        "finalize_json_path": finalize_json_path,
        "generated_at": now_iso(),
    }
    payload["artifact_sha256"] = sha256_bytes(json.dumps(payload, sort_keys=True).encode("utf-8"))
    write_json(blocker_artifact, payload)
    return blocker_artifact


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # If we cannot signal but process exists, treat as alive.
        return True


class TaskLock:
    def __init__(self, lock_path: Path, run_id: str) -> None:
        self.lock_path = lock_path
        self.run_id = run_id
        self.acquired = False

    def acquire(self) -> tuple[bool, str]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": now_iso(),
            "run_id": self.run_id,
        }
        payload = json.dumps(metadata).encode("utf-8")
        try:
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
            self.acquired = True
            return True, "LOCK_ACQUIRED"
        except FileExistsError:
            pass

        try:
            existing = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except Exception:
            return False, "LOCK_BUSY"
        started_at_raw = str(existing.get("started_at") or "")
        pid = int(existing.get("pid") or 0)
        host = str(existing.get("host") or "")
        age_seconds = 0
        try:
            started_epoch = datetime.fromisoformat(started_at_raw).timestamp()
            age_seconds = int(time.time() - started_epoch)
        except Exception:
            age_seconds = 0

        same_host = host == socket.gethostname()
        if age_seconds > LOCK_STALE_SECONDS and same_host and not pid_alive(pid):
            try:
                self.lock_path.unlink(missing_ok=True)
            except Exception:
                return False, "LOCK_BUSY"
            return self.acquire_recovered()
        return False, "LOCK_BUSY"

    def acquire_recovered(self) -> tuple[bool, str]:
        ok, code = self.acquire()
        if ok:
            return True, "STALE_LOCK_RECOVERED"
        return False, code

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.lock_path.unlink(missing_ok=True)
        except Exception:
            pass
        self.acquired = False


def check_route_class_immutable(state_file: Path, route_task_id: str, route_class: str) -> tuple[bool, str]:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, str] = {}
    if state_file.exists():
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = {str(k): str(v) for k, v in loaded.items()}
        except Exception:
            payload = {}
    existing = payload.get(route_task_id)
    if existing and existing != route_class:
        return False, "ROUTE_CLASS_IMMUTABLE_VIOLATION"
    if not existing:
        payload[route_task_id] = route_class
        write_json(state_file, payload)
    return True, "OK"


def track_id_from_inputs(route_task_id: str, workspace_root: str) -> str:
    digest = hashlib.sha256(f"{route_task_id}|{workspace_root}".encode("utf-8")).hexdigest()
    return digest[:16]


def run_cmd(argv: list[str], timeout_sec: int, cwd: str | None = None) -> tuple[int, str, str, int, str]:
    def _to_text(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        return str(value)

    started = time.time()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_sec, check=False, cwd=cwd)
        elapsed_ms = int((time.time() - started) * 1000)
        return proc.returncode, _to_text(proc.stdout), _to_text(proc.stderr), elapsed_ms, "OK"
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        return 124, _to_text(exc.stdout), _to_text(exc.stderr), elapsed_ms, "TIMEOUT"
    except OSError as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        if getattr(exc, "errno", None) == 11:
            return 30, "", str(exc), elapsed_ms, "SPAWN_EAGAIN"
        return 30, "", str(exc), elapsed_ms, "TEMPORARY_IO"


def run_gate_with_retry(argv: list[str], timeout_sec: int, cwd: str | None = None) -> tuple[int, str, str, int, str]:
    rc, out, err, elapsed_ms, reason_code = run_cmd(argv, timeout_sec, cwd=cwd)
    if reason_code in TRANSIENT_REASON_CODES:
        rc2, out2, err2, elapsed_ms2, reason_code2 = run_cmd(argv, timeout_sec, cwd=cwd)
        return rc2, out2, err2, elapsed_ms + elapsed_ms2, reason_code2
    return rc, out, err, elapsed_ms, reason_code


def parse_review_payload(path: Path) -> tuple[str, list[str], list[str], str]:
    try:
        payload = load_json(path)
    except Exception as exc:
        return "error", [], [str(exc)], "REVIEW_READ_ERROR"
    status = str(payload.get("status") or "").strip().lower()
    missing = [str(x) for x in payload.get("missing_fields") or []]
    blocked = [str(x) for x in payload.get("blocked_fields") or []]
    if status == "approve":
        return "pass", missing, blocked, "OK"
    if status == "blocked":
        return "blocked", missing, blocked, "BLOCKED"
    return "fail", missing, blocked, "REVISE"


def parse_finalize_payload(path: Path) -> tuple[bool, str, list[str], list[str]]:
    payload = load_json(path)
    ok = bool(payload.get("ok"))
    reason = str(payload.get("reason") or "")
    missing = [str(x) for x in payload.get("missing_fields") or []]
    blocked = [str(x) for x in payload.get("blocked_fields") or []]
    return ok, reason, missing, blocked


def build_next_action_prompt(reason_code: str, missing_fields: list[str], blocked_fields: list[str], gate_results: list[dict[str, Any]]) -> str:
    lines = [
        "Ralph postflight requested another iteration.",
        f"Primary reason: {reason_code}",
    ]
    if missing_fields:
        lines.append("Missing DoD evidence/fields:")
        for item in missing_fields[:20]:
            lines.append(f"- {item}")
    if blocked_fields:
        lines.append("Blocked fields/conditions:")
        for item in blocked_fields[:20]:
            lines.append(f"- {item}")
    latest = gate_results[-3:] if gate_results else []
    if latest:
        lines.append("Latest gate outcomes:")
        for gate in latest:
            gate_name = str(gate.get("gate_name") or "unknown_gate")
            gate_status = str(gate.get("status") or "unknown")
            gate_reason = safe_reason(str(gate.get("reason_code") or gate.get("reason") or ""))
            lines.append(f"- {gate_name}: {gate_status} ({gate_reason})")
    lines.append("Revise implementation artifacts and DoD evidence, then rerun finalization.")
    return "\n".join(lines)


def load_postflight_policy(codex_home: Path) -> dict[str, Any]:
    manifest_path = codex_home / "state" / "route_manifest.json"
    policy: dict[str, Any] = {
        "require_program_close": False,
        "program_close_command": list(DEFAULT_PROGRAM_CLOSE_COMMAND),
        "program_close_report": DEFAULT_PROGRAM_CLOSE_REPORT,
        "gate_chain": list(REQUIRED_EXTERNAL_SEQUENCE),
        "lock_dir": str((codex_home / "state" / "locks").resolve()),
    }
    if not manifest_path.exists():
        return policy
    try:
        payload = load_json(manifest_path)
    except Exception:
        return policy
    postflight = payload.get("postflight")
    if not isinstance(postflight, dict):
        return policy

    policy["require_program_close"] = bool(postflight.get("require_program_close", False))
    cmd = postflight.get("program_close_command")
    if isinstance(cmd, list) and cmd and all(isinstance(item, str) and item.strip() for item in cmd):
        policy["program_close_command"] = [item.strip() for item in cmd]
    report = postflight.get("program_close_report")
    if isinstance(report, str) and report.strip():
        policy["program_close_report"] = report.strip()
    gate_chain = postflight.get("gate_chain")
    if isinstance(gate_chain, list) and all(isinstance(item, str) for item in gate_chain):
        policy["gate_chain"] = [str(item).strip() for item in gate_chain if str(item).strip()]
    lock_dir = postflight.get("lock_dir")
    if isinstance(lock_dir, str) and lock_dir.strip():
        policy["lock_dir"] = lock_dir.strip()
    return policy


def validate_gate_chain(policy: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
    observed = [str(item).strip() for item in policy.get("gate_chain", []) if str(item).strip()]
    if observed == REQUIRED_EXTERNAL_SEQUENCE:
        return True, [], []
    missing = [stage for stage in REQUIRED_EXTERNAL_SEQUENCE if stage not in observed]
    extra = [stage for stage in observed if stage not in REQUIRED_EXTERNAL_SEQUENCE]
    return False, missing, extra


def normalize_stage(value: Any) -> str:
    raw = str(value or "").strip().lower().replace(" ", "")
    if raw.startswith("25"):
        return "25%"
    if raw.startswith("50"):
        return "50%"
    if raw.startswith("75"):
        return "75%"
    if raw.startswith("100"):
        return "100%"
    return ""


def evaluate_capture_stages(impl_payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    smoke_results = impl_payload.get("smoke_results")
    if not isinstance(smoke_results, list):
        return [], ["implementation:smoke_results:not_list"]
    by_stage: dict[str, dict[str, Any]] = {}
    for item in smoke_results:
        if not isinstance(item, dict):
            continue
        stage = normalize_stage(item.get("stage"))
        if stage:
            by_stage[stage] = item
    checks: list[dict[str, Any]] = []
    missing: list[str] = []
    for stage in ["25%", "50%", "75%", "100%"]:
        item = by_stage.get(stage)
        if item is None:
            missing.append(f"implementation:smoke_results:missing_stage:{stage}")
            checks.append({"stage": stage, "ok": False, "reason": "missing_stage"})
            continue
        status = str(item.get("status", "")).strip().lower()
        proof_artifact = str(item.get("proof_artifact", "")).strip()
        proof_hash = str(item.get("proof_hash", "")).strip()
        ok = status == "pass" and bool(proof_artifact) and bool(proof_hash)
        if not ok:
            if status != "pass":
                missing.append(f"implementation:smoke_results:{stage}:status_not_pass")
            if not proof_artifact:
                missing.append(f"implementation:smoke_results:{stage}:proof_artifact")
            if not proof_hash:
                missing.append(f"implementation:smoke_results:{stage}:proof_hash")
        checks.append(
            {
                "stage": stage,
                "ok": ok,
                "status": status,
                "proof_artifact": proof_artifact,
                "proof_hash": proof_hash,
            }
        )
    return checks, missing


def render_program_close_command(template: list[str], track_id: str) -> list[str]:
    rendered = [token.replace("{track_id}", track_id) for token in template]
    if "{track_id}" not in " ".join(template):
        rendered = rendered + ["--", "--track-id", track_id]
    return rendered


def collect_program_close_blockers(report_path: Path, expected_track_id: str) -> list[str]:
    if not report_path.exists():
        return [f"missing_program_close_report:{report_path}"]
    try:
        payload = load_json(report_path)
    except Exception as exc:
        return [safe_reason(str(exc))]

    blockers: list[str] = []
    status = str(payload.get("status") or "").strip().lower()
    schema_version = str(payload.get("schema_version") or "").strip()
    report_track_id = str(payload.get("track_id") or "").strip()
    reason = payload.get("reason")
    if schema_version != REQUIRED_SCOREBOARD_SCHEMA:
        blockers.append(f"program_scoreboard_schema_mismatch:{schema_version or 'missing'}")
    if report_track_id != expected_track_id:
        blockers.append(f"program_scoreboard_track_mismatch:{report_track_id or 'missing'}")
    if status != "pass":
        blockers.append(f"program_scoreboard_status:{status or 'missing'}")
    if status == "fail" and isinstance(reason, str) and reason.strip():
        blockers.append(f"program_scoreboard_reason:{safe_reason(reason)}")
    checks = payload.get("checks")
    if isinstance(checks, dict):
        for key, value in checks.items():
            if value is False:
                blockers.append(f"program_scoreboard_check_failed:{key}")
    else:
        blockers.append("program_scoreboard_checks_missing")
    categories = payload.get("categories")
    if isinstance(categories, dict):
        for category in REQUIRED_DOD_CATEGORIES:
            if categories.get(category) is not True:
                blockers.append(f"program_scoreboard_category_failed:{category}")
    else:
        blockers.append("program_scoreboard_categories_missing")
    failures = payload.get("failures")
    if isinstance(failures, list):
        blockers.extend([f"program_scoreboard:{str(item)}" for item in failures])
    if status == "fail" and not blockers:
        blockers.append("program_close_report_missing_failure_details")
    return blockers


def resolve_program_close_report_path(workspace_root: str, artifacts_root: Path, report_pattern: str, track_id: str) -> Path:
    pattern = report_pattern.strip()
    if "{track_id}" not in pattern:
        raise RuntimeError("program_close_report_missing_track_placeholder")
    rendered = pattern.replace("{track_id}", track_id)
    resolved = (Path(workspace_root) / rendered).resolve()
    workspace = Path(workspace_root).resolve()
    artifacts = artifacts_root.resolve()
    if resolved != workspace and workspace not in resolved.parents:
        raise RuntimeError(f"program_close_report_outside_workspace:{resolved}")
    if resolved != artifacts and artifacts not in resolved.parents:
        raise RuntimeError(f"program_close_report_outside_artifacts_root:{resolved}")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Postflight Completion Gate (Ralph Loop) v1.4.")
    parser.add_argument("--route-task-id", required=True)
    parser.add_argument("--route-class", required=True, choices=["R3", "R4"])
    parser.add_argument("--track-id", default="")
    parser.add_argument("--plan-json", required=True)
    parser.add_argument("--impl-json", required=True)
    parser.add_argument("--review-json", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument(
        "--codex-home",
        default=os.environ.get("CLAUDE_HOME") or os.environ.get("CODEX_HOME", "/Users/chadsimon/.claude"),
    )
    parser.add_argument("--max-loops", type=int, default=3)
    parser.add_argument("--mode", default="enforce", choices=["enforce", "audit"])
    parser.add_argument("--artifacts-root", required=True)
    parser.add_argument("--timeout-sec", type=int, default=240)
    parser.add_argument("--external-remediation-loop", action="store_true")
    parser.add_argument("--loop-index", type=int, default=0)
    args = parser.parse_args()
    external_mode = bool(args.external_remediation_loop)
    if external_mode:
        if args.loop_index <= 0:
            raise RuntimeError("loop_index_required_for_external_mode")
        if args.loop_index > args.max_loops:
            raise RuntimeError("loop_index_exceeds_max_loops")

    route_task_id = sanitize_token(args.route_task_id)
    workspace_root = str(Path(args.workspace_root).expanduser().resolve())
    track_id = sanitize_token(args.track_id) if args.track_id else track_id_from_inputs(route_task_id, workspace_root)
    codex_home = Path(args.codex_home).expanduser().resolve()
    run_id = f"{int(time.time())}-{sha256_bytes(os.urandom(16))[:10]}"
    run_dir = (codex_home / "state" / "postflight_runs" / route_task_id / track_id / run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    telemetry_path = run_dir / "telemetry.log"
    stdout_log_path = run_dir / "stdout.log"
    stderr_log_path = run_dir / "stderr.log"
    route_binding_state = codex_home / "state" / "postflight_route_bindings.json"
    postflight_policy = load_postflight_policy(codex_home)
    lock_dir = Path(str(postflight_policy.get("lock_dir", codex_home / "state" / "locks"))).expanduser().resolve()
    codex_state_dir = (codex_home / "state").resolve()
    if lock_dir != codex_state_dir and codex_state_dir not in lock_dir.parents:
        raise RuntimeError(f"lock_dir_outside_codex_state:{lock_dir}")
    lock = TaskLock(lock_dir / f"{route_task_id}.lock", run_id=run_id)

    allowed_roots = [codex_home.resolve(), Path(workspace_root).resolve()]
    input_paths = [
        Path(args.plan_json).expanduser(),
        Path(args.impl_json).expanduser(),
        Path(args.review_json).expanduser(),
    ]

    base_result: dict[str, Any] = {
        "schema_version": "postflight_result.v1",
        "run_id": run_id,
        "route_task_id": route_task_id,
        "route_class": args.route_class,
        "track_id": track_id,
        "loop_count": 0,
        "gate_results": [],
        "missing_fields": [],
        "blocked_fields": [],
        "reason_code": "UNKNOWN",
        "reason": "",
        "next_action_prompt": "",
        "finalize_json_path": "",
        "stdout_log_path": str(stdout_log_path),
        "stderr_log_path": str(stderr_log_path),
        "acceptance_artifact_path": "",
        "blocker_artifact_path": "",
        "gate_script_fingerprints": {},
    }

    def write_run_summary(
        status: str,
        exit_code: int,
        finalize_json_path: str,
        acceptance_artifact_path: str,
        blocker_artifact_path: str,
    ) -> None:
        summary = {
            "schema_version": "postflight_run_summary.v1",
            "route_task_id": route_task_id,
            "track_id": track_id,
            "route_class": args.route_class,
            "run_id": run_id,
            "status": status,
            "exit_code": exit_code,
            "loop_count": base_result["loop_count"],
            "gate_chain_version": "planning-gate-v1",
            "finalize_json_path": finalize_json_path,
            "acceptance_artifact_path": acceptance_artifact_path,
            "blocker_artifact_path": blocker_artifact_path,
            "gate_script_fingerprints": base_result.get("gate_script_fingerprints", {}),
            "telemetry_log_path": str(telemetry_path),
            "mode": args.mode,
            "timestamp": now_iso(),
        }
        write_json(run_dir / "run_summary.json", summary)

    ok, lock_reason = lock.acquire()
    if not ok:
        blocker_artifact = write_blocker_artifact(
            codex_home=codex_home,
            route_task_id=route_task_id,
            track_id=track_id,
            route_class=args.route_class,
            run_id=run_id,
            loop_count=0,
            reason_code=lock_reason,
            missing_fields=[],
            blocked_fields=[],
            run_dir=run_dir,
            stdout_log_path=stdout_log_path,
            stderr_log_path=stderr_log_path,
        )
        base_result.update(
            {
                "ok": False,
                "status": "blocked",
                "reason_code": lock_reason,
                "reason": lock_reason,
                "exit_code": 20,
                "blocker_artifact_path": str(blocker_artifact),
            }
        )
        emit_ralph_meta(
            telemetry_path=telemetry_path,
            run_id=run_id,
            route_task_id=route_task_id,
            track_id=track_id,
            route_class=args.route_class,
            loop_idx=0,
            gate="planning_gate_finalize",
            status="blocked",
            reason_code=lock_reason,
            reason=lock_reason,
            elapsed_ms=0,
            finalize_json_path=None,
            exit_code=20,
        )
        write_run_summary("blocked", 20, "", "", str(blocker_artifact))
        print(json.dumps(base_result, sort_keys=True))
        return 20

    try:
        immutable_ok, immutable_reason = check_route_class_immutable(route_binding_state, route_task_id, args.route_class)
        if not immutable_ok:
            blocker_artifact = write_blocker_artifact(
                codex_home=codex_home,
                route_task_id=route_task_id,
                track_id=track_id,
                route_class=args.route_class,
                run_id=run_id,
                loop_count=0,
                reason_code=immutable_reason,
                missing_fields=[],
                blocked_fields=[],
                run_dir=run_dir,
                stdout_log_path=stdout_log_path,
                stderr_log_path=stderr_log_path,
            )
            base_result.update(
                {
                    "ok": False,
                    "status": "blocked",
                    "reason_code": immutable_reason,
                    "reason": immutable_reason,
                    "exit_code": 20,
                    "blocker_artifact_path": str(blocker_artifact),
                }
            )
            emit_ralph_meta(
                telemetry_path=telemetry_path,
                run_id=run_id,
                route_task_id=route_task_id,
                track_id=track_id,
                route_class=args.route_class,
                loop_idx=0,
                gate="planning_gate_finalize",
                status="blocked",
                reason_code=immutable_reason,
                reason=immutable_reason,
                elapsed_ms=0,
                finalize_json_path=None,
                exit_code=20,
            )
            write_run_summary("blocked", 20, "", "", str(blocker_artifact))
            print(json.dumps(base_result, sort_keys=True))
            return 20

        validated_inputs: list[Path] = []
        for p in input_paths:
            validated = validate_artifact_path(p.resolve(), allowed_roots)
            if not validated.exists():
                raise RuntimeError(f"missing_artifact:{validated}")
            _ = load_json(validated)
            validated_inputs.append(validated)

        plan_input, impl_input, review_input = validated_inputs
        planning_scripts = resolve_planning_gate_scripts(codex_home)
        validate_plan_py = planning_scripts / "validate_plan.py"
        validate_impl_py = planning_scripts / "validate_impl.py"
        finalize_gate_py = planning_scripts / "finalize_gate.py"
        for gate_script in (validate_plan_py, validate_impl_py, finalize_gate_py):
            if not gate_script.exists():
                raise RuntimeError(f"missing_gate_script:{gate_script}")
        base_result["gate_script_fingerprints"] = {
            "validate_plan.py": sha256_file(validate_plan_py),
            "validate_impl.py": sha256_file(validate_impl_py),
            "finalize_gate.py": sha256_file(finalize_gate_py),
        }

        artifacts_root = Path(args.artifacts_root).expanduser().resolve()
        workspace_root_path = Path(workspace_root).resolve()
        if artifacts_root != workspace_root_path and workspace_root_path not in artifacts_root.parents:
            raise RuntimeError(f"artifacts_root_outside_workspace:{artifacts_root}")
        gate_chain_ok, gate_chain_missing, gate_chain_extra = validate_gate_chain(postflight_policy)
        if not gate_chain_ok:
            raise RuntimeError(
                "invalid_gate_chain:"
                + ",".join(gate_chain_missing or ["none"])
                + ":extra:"
                + ",".join(gate_chain_extra or ["none"])
            )

        loop_indices = [args.loop_index] if external_mode else list(range(1, args.max_loops + 1))
        for loop_idx in loop_indices:
            base_result["loop_count"] = loop_idx
            loop_plan = run_dir / f"plan.loop{loop_idx}.json"
            loop_impl = run_dir / f"implementation.loop{loop_idx}.json"
            loop_review = run_dir / f"review.impl.loop{loop_idx}.json"
            loop_finalize = run_dir / f"finalize.loop{loop_idx}.json"
            loop_finalize_repeat = run_dir / f"finalize.repeat.loop{loop_idx}.json"
            plan_review = run_dir / f"review.plan.loop{loop_idx}.json"

            plan_payload = load_json(plan_input)
            impl_payload = load_json(impl_input)
            review_payload = load_json(review_input)
            plan_payload, plan_missing = ensure_provenance(loop_plan, plan_payload, run_id)
            impl_payload, impl_missing = ensure_provenance(loop_impl, impl_payload, run_id)
            review_payload, review_missing = ensure_provenance(loop_review, review_payload, run_id)

            write_json(loop_plan, plan_payload)
            write_json(loop_impl, impl_payload)
            write_json(loop_review, review_payload)
            if plan_missing or impl_missing or review_missing:
                gate = GateResult(
                    gate_name="provenance",
                    status="pass",
                    reason_code="PROVENANCE_AUTOFILLED",
                    reason="Missing provenance fields were auto-filled for loop artifacts.",
                    missing_fields=sorted(set(plan_missing + impl_missing + review_missing)),
                    blocked_fields=[],
                    elapsed_ms=0,
                )
                base_result["gate_results"].append(gate.as_dict())
                emit_ralph_meta(
                    telemetry_path=telemetry_path,
                    run_id=run_id,
                    route_task_id=route_task_id,
                    track_id=track_id,
                    route_class=args.route_class,
                    loop_idx=loop_idx,
                    gate="provenance",
                    status="approve",
                    reason_code="PROVENANCE_AUTOFILLED",
                    reason="Missing provenance fields auto-filled.",
                    elapsed_ms=0,
                    finalize_json_path=None,
                    exit_code=None,
                )

            plan_cmd = [
                "python3.11",
                str(validate_plan_py),
                "--plan-json",
                str(loop_plan),
                "--review-json-out",
                str(plan_review),
                "--track-id",
                track_id,
                "--artifacts-root",
                str(artifacts_root),
            ]
            rc, out, err, elapsed_ms, reason_code = run_gate_with_retry(plan_cmd, args.timeout_sec)
            stdout_log_path.write_text(stdout_log_path.read_text(encoding="utf-8") + out, encoding="utf-8") if stdout_log_path.exists() else stdout_log_path.write_text(out, encoding="utf-8")
            stderr_log_path.write_text(stderr_log_path.read_text(encoding="utf-8") + err, encoding="utf-8") if stderr_log_path.exists() else stderr_log_path.write_text(err, encoding="utf-8")
            p_status, p_missing, p_blocked, p_reason = parse_review_payload(plan_review)
            plan_gate = GateResult(
                gate_name="validate_plan",
                status="pass" if p_status == "pass" else ("blocked" if p_status == "blocked" else "fail"),
                reason_code=reason_code if reason_code != "OK" else p_reason,
                reason=p_reason,
                missing_fields=p_missing,
                blocked_fields=p_blocked,
                elapsed_ms=elapsed_ms,
            )
            base_result["gate_results"].append(plan_gate.as_dict())
            emit_ralph_meta(
                telemetry_path=telemetry_path,
                run_id=run_id,
                route_task_id=route_task_id,
                track_id=track_id,
                route_class=args.route_class,
                loop_idx=loop_idx,
                gate="validate_plan",
                status="approve" if p_status == "pass" else ("blocked" if p_status == "blocked" else "revise"),
                reason_code=plan_gate.reason_code,
                reason=plan_gate.reason,
                elapsed_ms=elapsed_ms,
                finalize_json_path=None,
                exit_code=None,
            )
            if p_status == "blocked":
                blocker_artifact = write_blocker_artifact(
                    codex_home=codex_home,
                    route_task_id=route_task_id,
                    track_id=track_id,
                    route_class=args.route_class,
                    run_id=run_id,
                    loop_count=loop_idx,
                    reason_code="PLAN_BLOCKED",
                    missing_fields=p_missing,
                    blocked_fields=p_blocked,
                    run_dir=run_dir,
                    stdout_log_path=stdout_log_path,
                    stderr_log_path=stderr_log_path,
                )
                base_result.update(
                    {
                        "ok": False,
                        "status": "blocked",
                        "reason_code": "PLAN_BLOCKED",
                        "reason": "Plan gate blocked",
                        "exit_code": 20,
                        "blocker_artifact_path": str(blocker_artifact),
                        "missing_fields": p_missing,
                        "blocked_fields": p_blocked,
                    }
                )
                emit_ralph_meta(
                    telemetry_path=telemetry_path,
                    run_id=run_id,
                    route_task_id=route_task_id,
                    track_id=track_id,
                    route_class=args.route_class,
                    loop_idx=loop_idx,
                    gate="planning_gate_finalize",
                    status="blocked",
                    reason_code="PLAN_BLOCKED",
                    reason="Plan gate blocked",
                    elapsed_ms=0,
                    finalize_json_path=None,
                    exit_code=20,
                )
                write_run_summary("blocked", 20, "", "", str(blocker_artifact))
                print(json.dumps(base_result, sort_keys=True))
                return 20
            if p_status != "pass":
                if loop_idx == args.max_loops:
                    blocker_artifact = write_blocker_artifact(
                        codex_home=codex_home,
                        route_task_id=route_task_id,
                        track_id=track_id,
                        route_class=args.route_class,
                        run_id=run_id,
                        loop_count=loop_idx,
                        reason_code="BUDGET_EXHAUSTED",
                        missing_fields=p_missing,
                        blocked_fields=p_blocked,
                        run_dir=run_dir,
                        stdout_log_path=stdout_log_path,
                        stderr_log_path=stderr_log_path,
                    )
                    base_result.update(
                        {
                            "ok": False,
                            "status": "blocked",
                            "reason_code": "BUDGET_EXHAUSTED",
                            "reason": "Loop budget exhausted during plan revision",
                            "exit_code": 20,
                            "blocker_artifact_path": str(blocker_artifact),
                            "missing_fields": p_missing,
                            "blocked_fields": p_blocked,
                        }
                    )
                    emit_ralph_meta(
                        telemetry_path=telemetry_path,
                        run_id=run_id,
                        route_task_id=route_task_id,
                        track_id=track_id,
                        route_class=args.route_class,
                        loop_idx=loop_idx,
                        gate="planning_gate_finalize",
                        status="blocked",
                        reason_code="BUDGET_EXHAUSTED",
                        reason="Loop budget exhausted during plan revision",
                        elapsed_ms=0,
                        finalize_json_path=None,
                        exit_code=20,
                    )
                    write_run_summary("blocked", 20, "", "", str(blocker_artifact))
                    print(json.dumps(base_result, sort_keys=True))
                    return 20
                if external_mode:
                    next_prompt = build_next_action_prompt(
                        "PLAN_REVISE_REQUIRED",
                        p_missing,
                        p_blocked,
                        base_result.get("gate_results", []),
                    )
                    base_result.update(
                        {
                            "ok": False,
                            "status": "revise",
                            "reason_code": "PLAN_REVISE_REQUIRED",
                            "reason": "Plan validation requires revision",
                            "next_action_prompt": next_prompt,
                            "exit_code": 10,
                            "missing_fields": p_missing,
                            "blocked_fields": p_blocked,
                        }
                    )
                    write_run_summary("revise", 10, "", "", "")
                    print(json.dumps(base_result, sort_keys=True))
                    return 10
                continue

            capture_checks, capture_missing = evaluate_capture_stages(impl_payload)
            capture_failed = False
            for item in capture_checks:
                stage = str(item.get("stage", "")).replace("%", "")
                gate_name = f"run_cmd_capture_{stage}"
                ok_capture = bool(item.get("ok"))
                capture_gate = GateResult(
                    gate_name=gate_name,
                    status="pass" if ok_capture else "fail",
                    reason_code="OK" if ok_capture else "RUN_CMD_CAPTURE_STAGE_FAILED",
                    reason="capture stage passed" if ok_capture else str(item.get("reason", "capture_stage_failed")),
                    missing_fields=[] if ok_capture else [f"implementation:smoke_results:{item.get('stage')}"],
                    blocked_fields=[],
                    elapsed_ms=0,
                )
                base_result["gate_results"].append(capture_gate.as_dict())
                emit_ralph_meta(
                    telemetry_path=telemetry_path,
                    run_id=run_id,
                    route_task_id=route_task_id,
                    track_id=track_id,
                    route_class=args.route_class,
                    loop_idx=loop_idx,
                    gate=gate_name,
                    status="approve" if ok_capture else "revise",
                    reason_code=capture_gate.reason_code,
                    reason=capture_gate.reason,
                    elapsed_ms=0,
                    finalize_json_path=None,
                    exit_code=None,
                )
                if not ok_capture:
                    capture_failed = True

            if capture_missing:
                capture_failed = True

            if capture_failed:
                if loop_idx == args.max_loops:
                    blocker_artifact = write_blocker_artifact(
                        codex_home=codex_home,
                        route_task_id=route_task_id,
                        track_id=track_id,
                        route_class=args.route_class,
                        run_id=run_id,
                        loop_count=loop_idx,
                        reason_code="BUDGET_EXHAUSTED",
                        missing_fields=sorted(set(capture_missing)),
                        blocked_fields=[],
                        run_dir=run_dir,
                        stdout_log_path=stdout_log_path,
                        stderr_log_path=stderr_log_path,
                    )
                    base_result.update(
                        {
                            "ok": False,
                            "status": "blocked",
                            "reason_code": "BUDGET_EXHAUSTED",
                            "reason": "Loop budget exhausted during run_cmd_capture stage enforcement",
                            "exit_code": 20,
                            "blocker_artifact_path": str(blocker_artifact),
                            "missing_fields": sorted(set(capture_missing)),
                            "blocked_fields": [],
                        }
                    )
                    emit_ralph_meta(
                        telemetry_path=telemetry_path,
                        run_id=run_id,
                        route_task_id=route_task_id,
                        track_id=track_id,
                        route_class=args.route_class,
                        loop_idx=loop_idx,
                        gate="planning_gate_finalize",
                        status="blocked",
                        reason_code="BUDGET_EXHAUSTED",
                        reason="Loop budget exhausted during run_cmd_capture stage enforcement",
                        elapsed_ms=0,
                        finalize_json_path=None,
                        exit_code=20,
                    )
                    write_run_summary("blocked", 20, "", "", str(blocker_artifact))
                    print(json.dumps(base_result, sort_keys=True))
                    return 20
                if external_mode:
                    capture_missing_fields = sorted(set(capture_missing))
                    next_prompt = build_next_action_prompt(
                        "RUN_CMD_CAPTURE_REVISE_REQUIRED",
                        capture_missing_fields,
                        [],
                        base_result.get("gate_results", []),
                    )
                    base_result.update(
                        {
                            "ok": False,
                            "status": "revise",
                            "reason_code": "RUN_CMD_CAPTURE_REVISE_REQUIRED",
                            "reason": "run_cmd_capture stage evidence requires revision",
                            "next_action_prompt": next_prompt,
                            "exit_code": 10,
                            "missing_fields": capture_missing_fields,
                            "blocked_fields": [],
                        }
                    )
                    write_run_summary("revise", 10, "", "", "")
                    print(json.dumps(base_result, sort_keys=True))
                    return 10
                continue

            impl_cmd = [
                "python3.11",
                str(validate_impl_py),
                "--plan-json",
                str(loop_plan),
                "--impl-json",
                str(loop_impl),
                "--review-json-out",
                str(loop_review),
                "--track-id",
                track_id,
                "--artifacts-root",
                str(artifacts_root),
            ]
            rc2, out2, err2, elapsed_ms2, reason_code2 = run_gate_with_retry(impl_cmd, args.timeout_sec)
            stdout_log_path.write_text(stdout_log_path.read_text(encoding="utf-8") + out2, encoding="utf-8")
            stderr_log_path.write_text(stderr_log_path.read_text(encoding="utf-8") + err2, encoding="utf-8")
            i_status, i_missing, i_blocked, i_reason = parse_review_payload(loop_review)
            impl_gate = GateResult(
                gate_name="validate_impl",
                status="pass" if i_status == "pass" else ("blocked" if i_status == "blocked" else "fail"),
                reason_code=reason_code2 if reason_code2 != "OK" else i_reason,
                reason=i_reason,
                missing_fields=i_missing,
                blocked_fields=i_blocked,
                elapsed_ms=elapsed_ms2,
            )
            base_result["gate_results"].append(impl_gate.as_dict())
            emit_ralph_meta(
                telemetry_path=telemetry_path,
                run_id=run_id,
                route_task_id=route_task_id,
                track_id=track_id,
                route_class=args.route_class,
                loop_idx=loop_idx,
                gate="validate_impl",
                status="approve" if i_status == "pass" else ("blocked" if i_status == "blocked" else "revise"),
                reason_code=impl_gate.reason_code,
                reason=impl_gate.reason,
                elapsed_ms=elapsed_ms2,
                finalize_json_path=None,
                exit_code=None,
            )
            if i_status == "blocked":
                blocker_artifact = write_blocker_artifact(
                    codex_home=codex_home,
                    route_task_id=route_task_id,
                    track_id=track_id,
                    route_class=args.route_class,
                    run_id=run_id,
                    loop_count=loop_idx,
                    reason_code="IMPL_BLOCKED",
                    missing_fields=i_missing,
                    blocked_fields=i_blocked,
                    run_dir=run_dir,
                    stdout_log_path=stdout_log_path,
                    stderr_log_path=stderr_log_path,
                )
                base_result.update(
                    {
                        "ok": False,
                        "status": "blocked",
                        "reason_code": "IMPL_BLOCKED",
                        "reason": "Implementation gate blocked",
                        "exit_code": 20,
                        "blocker_artifact_path": str(blocker_artifact),
                        "missing_fields": i_missing,
                        "blocked_fields": i_blocked,
                    }
                )
                emit_ralph_meta(
                    telemetry_path=telemetry_path,
                    run_id=run_id,
                    route_task_id=route_task_id,
                    track_id=track_id,
                    route_class=args.route_class,
                    loop_idx=loop_idx,
                    gate="planning_gate_finalize",
                    status="blocked",
                    reason_code="IMPL_BLOCKED",
                    reason="Implementation gate blocked",
                    elapsed_ms=0,
                    finalize_json_path=None,
                    exit_code=20,
                )
                write_run_summary("blocked", 20, "", "", str(blocker_artifact))
                print(json.dumps(base_result, sort_keys=True))
                return 20
            if i_status != "pass":
                if loop_idx == args.max_loops:
                    blocker_artifact = write_blocker_artifact(
                        codex_home=codex_home,
                        route_task_id=route_task_id,
                        track_id=track_id,
                        route_class=args.route_class,
                        run_id=run_id,
                        loop_count=loop_idx,
                        reason_code="BUDGET_EXHAUSTED",
                        missing_fields=i_missing,
                        blocked_fields=i_blocked,
                        run_dir=run_dir,
                        stdout_log_path=stdout_log_path,
                        stderr_log_path=stderr_log_path,
                    )
                    base_result.update(
                        {
                            "ok": False,
                            "status": "blocked",
                            "reason_code": "BUDGET_EXHAUSTED",
                            "reason": "Loop budget exhausted during implementation revision",
                            "exit_code": 20,
                            "blocker_artifact_path": str(blocker_artifact),
                            "missing_fields": i_missing,
                            "blocked_fields": i_blocked,
                        }
                    )
                    emit_ralph_meta(
                        telemetry_path=telemetry_path,
                        run_id=run_id,
                        route_task_id=route_task_id,
                        track_id=track_id,
                        route_class=args.route_class,
                        loop_idx=loop_idx,
                        gate="planning_gate_finalize",
                        status="blocked",
                        reason_code="BUDGET_EXHAUSTED",
                        reason="Loop budget exhausted during implementation revision",
                        elapsed_ms=0,
                        finalize_json_path=None,
                        exit_code=20,
                    )
                    write_run_summary("blocked", 20, "", "", str(blocker_artifact))
                    print(json.dumps(base_result, sort_keys=True))
                    return 20
                if external_mode:
                    next_prompt = build_next_action_prompt(
                        "IMPL_REVISE_REQUIRED",
                        i_missing,
                        i_blocked,
                        base_result.get("gate_results", []),
                    )
                    base_result.update(
                        {
                            "ok": False,
                            "status": "revise",
                            "reason_code": "IMPL_REVISE_REQUIRED",
                            "reason": "Implementation validation requires revision",
                            "next_action_prompt": next_prompt,
                            "exit_code": 10,
                            "missing_fields": i_missing,
                            "blocked_fields": i_blocked,
                        }
                    )
                    write_run_summary("revise", 10, "", "", "")
                    print(json.dumps(base_result, sort_keys=True))
                    return 10
                continue

            finalize_cmd = [
                "python3.11",
                str(finalize_gate_py),
                "--plan-json",
                str(loop_plan),
                "--impl-json",
                str(loop_impl),
                "--review-json",
                str(loop_review),
                "--track-id",
                track_id,
                "--out",
                str(loop_finalize),
                "--artifacts-root",
                str(artifacts_root),
            ]
            rc3, out3, err3, elapsed_ms3, reason_code3 = run_gate_with_retry(finalize_cmd, args.timeout_sec)
            stdout_log_path.write_text(stdout_log_path.read_text(encoding="utf-8") + out3, encoding="utf-8")
            stderr_log_path.write_text(stderr_log_path.read_text(encoding="utf-8") + err3, encoding="utf-8")
            try:
                ok_finalize, finalize_reason, f_missing, f_blocked = parse_finalize_payload(loop_finalize)
            except Exception as exc:
                ok_finalize = False
                finalize_reason = "finalize_output_unreadable"
                f_missing = []
                f_blocked = [safe_reason(str(exc))]
                if reason_code3 == "OK":
                    reason_code3 = "FINALIZE_READ_ERROR"
            finalize_status = "pass" if ok_finalize else ("blocked" if f_blocked else "fail")
            finalize_gate = GateResult(
                gate_name="planning_gate_finalize",
                status=finalize_status,
                reason_code=reason_code3 if reason_code3 != "OK" else (finalize_reason or "FINALIZE_FAILED"),
                reason=finalize_reason or "finalize_failed",
                missing_fields=f_missing,
                blocked_fields=f_blocked,
                elapsed_ms=elapsed_ms3,
            )
            base_result["gate_results"].append(finalize_gate.as_dict())
            emit_ralph_meta(
                telemetry_path=telemetry_path,
                run_id=run_id,
                route_task_id=route_task_id,
                track_id=track_id,
                route_class=args.route_class,
                loop_idx=loop_idx,
                gate="planning_gate_finalize",
                status="approve" if ok_finalize else ("blocked" if f_blocked else "revise"),
                reason_code=finalize_gate.reason_code,
                reason=finalize_gate.reason,
                elapsed_ms=elapsed_ms3,
                finalize_json_path=loop_finalize,
                exit_code=None,
            )

            if ok_finalize:
                finalize_repeat_cmd = [
                    "python3.11",
                    str(finalize_gate_py),
                    "--plan-json",
                    str(loop_plan),
                    "--impl-json",
                    str(loop_impl),
                    "--review-json",
                    str(loop_review),
                    "--track-id",
                    track_id,
                    "--out",
                    str(loop_finalize_repeat),
                    "--artifacts-root",
                    str(artifacts_root),
                ]
                rc3b, out3b, err3b, elapsed_ms3b, reason_code3b = run_gate_with_retry(finalize_repeat_cmd, args.timeout_sec)
                stdout_log_path.write_text(stdout_log_path.read_text(encoding="utf-8") + out3b, encoding="utf-8")
                stderr_log_path.write_text(stderr_log_path.read_text(encoding="utf-8") + err3b, encoding="utf-8")
                try:
                    ok_repeat, repeat_reason, fr_missing, fr_blocked = parse_finalize_payload(loop_finalize_repeat)
                except Exception as exc:
                    ok_repeat = False
                    repeat_reason = "finalize_repeat_output_unreadable"
                    fr_missing = []
                    fr_blocked = [safe_reason(str(exc))]
                    if reason_code3b == "OK":
                        reason_code3b = "FINALIZE_REPEAT_READ_ERROR"

                primary_payload = load_json(loop_finalize) if loop_finalize.exists() else {}
                repeat_payload = load_json(loop_finalize_repeat) if loop_finalize_repeat.exists() else {}
                class_primary = str(primary_payload.get("status_class") or primary_payload.get("status") or "").strip().lower()
                class_repeat = str(repeat_payload.get("status_class") or repeat_payload.get("status") or "").strip().lower()
                finalize_repeat_equivalent = bool(ok_repeat and class_primary and class_primary == class_repeat)
                if not finalize_repeat_equivalent and not fr_blocked:
                    fr_blocked = ["RALPH_NON_DETERMINISTIC_FINALIZE"]
                    repeat_reason = "finalize_repeat_not_equivalent"

                finalize_repeat_gate = GateResult(
                    gate_name="planning_gate_finalize_repeat",
                    status="pass" if finalize_repeat_equivalent else "blocked",
                    reason_code=(
                        "OK"
                        if finalize_repeat_equivalent
                        else ("RALPH_NON_DETERMINISTIC_FINALIZE" if reason_code3b == "OK" else reason_code3b)
                    ),
                    reason="Finalize repeat passed" if finalize_repeat_equivalent else repeat_reason,
                    missing_fields=fr_missing,
                    blocked_fields=fr_blocked,
                    elapsed_ms=elapsed_ms3b,
                )
                base_result["gate_results"].append(finalize_repeat_gate.as_dict())
                emit_ralph_meta(
                    telemetry_path=telemetry_path,
                    run_id=run_id,
                    route_task_id=route_task_id,
                    track_id=track_id,
                    route_class=args.route_class,
                    loop_idx=loop_idx,
                    gate="planning_gate_finalize_repeat",
                    status="approve" if finalize_repeat_equivalent else "blocked",
                    reason_code=finalize_repeat_gate.reason_code,
                    reason=finalize_repeat_gate.reason,
                    elapsed_ms=elapsed_ms3b,
                    finalize_json_path=loop_finalize_repeat,
                    exit_code=None if finalize_repeat_equivalent else 20,
                )

                if not finalize_repeat_equivalent:
                    blocker_artifact = write_blocker_artifact(
                        codex_home=codex_home,
                        route_task_id=route_task_id,
                        track_id=track_id,
                        route_class=args.route_class,
                        run_id=run_id,
                        loop_count=loop_idx,
                        reason_code="RALPH_NON_DETERMINISTIC_FINALIZE",
                        missing_fields=fr_missing,
                        blocked_fields=fr_blocked,
                        run_dir=run_dir,
                        stdout_log_path=stdout_log_path,
                        stderr_log_path=stderr_log_path,
                        finalize_json_path=str(loop_finalize_repeat),
                    )
                    base_result.update(
                        {
                            "ok": False,
                            "status": "blocked",
                            "reason_code": "RALPH_NON_DETERMINISTIC_FINALIZE",
                            "reason": "Finalize and finalize.repeat outcomes diverged",
                            "exit_code": 20,
                            "finalize_json_path": str(loop_finalize_repeat),
                            "acceptance_artifact_path": "",
                            "blocker_artifact_path": str(blocker_artifact),
                            "missing_fields": fr_missing,
                            "blocked_fields": fr_blocked,
                        }
                    )
                    write_run_summary("blocked", 20, str(loop_finalize_repeat), "", str(blocker_artifact))
                    print(json.dumps(base_result, sort_keys=True))
                    return 20

                if postflight_policy["require_program_close"]:
                    close_cmd = render_program_close_command(
                        [str(item) for item in postflight_policy["program_close_command"]],
                        track_id,
                    )
                    rc4, out4, err4, elapsed_ms4, _reason_code4 = run_gate_with_retry(
                        close_cmd,
                        args.timeout_sec,
                        cwd=workspace_root,
                    )
                    stdout_log_path.write_text(stdout_log_path.read_text(encoding="utf-8") + out4, encoding="utf-8")
                    stderr_log_path.write_text(stderr_log_path.read_text(encoding="utf-8") + err4, encoding="utf-8")
                    try:
                        close_report_path = resolve_program_close_report_path(
                            workspace_root,
                            artifacts_root,
                            str(postflight_policy["program_close_report"]),
                            track_id,
                        )
                        close_blockers = collect_program_close_blockers(close_report_path, track_id)
                        close_ok = rc4 == 0 and len(close_blockers) == 0
                    except Exception as exc:
                        close_blockers = [safe_reason(str(exc))]
                        close_ok = False
                    close_gate = GateResult(
                        gate_name="program_close_gate",
                        status="pass" if close_ok else "blocked",
                        reason_code="OK" if close_ok else "PROGRAM_NOT_CLOSED",
                        reason="Program close gate passed" if close_ok else "Program close gate failed",
                        missing_fields=[],
                        blocked_fields=[] if close_ok else close_blockers,
                        elapsed_ms=elapsed_ms4,
                    )
                    base_result["gate_results"].append(close_gate.as_dict())
                    emit_ralph_meta(
                        telemetry_path=telemetry_path,
                        run_id=run_id,
                        route_task_id=route_task_id,
                        track_id=track_id,
                        route_class=args.route_class,
                        loop_idx=loop_idx,
                        gate="program_close_gate",
                        status="approve" if close_ok else "blocked",
                        reason_code="OK" if close_ok else "PROGRAM_NOT_CLOSED",
                        reason=close_gate.reason,
                        elapsed_ms=elapsed_ms4,
                        finalize_json_path=loop_finalize_repeat,
                        exit_code=None if close_ok else 20,
                    )
                    if not close_ok:
                        blocker_artifact = write_blocker_artifact(
                            codex_home=codex_home,
                            route_task_id=route_task_id,
                            track_id=track_id,
                            route_class=args.route_class,
                            run_id=run_id,
                            loop_count=loop_idx,
                            reason_code="PROGRAM_NOT_CLOSED",
                            missing_fields=[],
                            blocked_fields=close_blockers,
                            run_dir=run_dir,
                            stdout_log_path=stdout_log_path,
                            stderr_log_path=stderr_log_path,
                            finalize_json_path=str(loop_finalize_repeat),
                        )
                        base_result.update(
                            {
                                "ok": False,
                                "status": "blocked",
                                "reason_code": "PROGRAM_NOT_CLOSED",
                                "reason": "Program close gate failed",
                                "exit_code": 20,
                                "finalize_json_path": str(loop_finalize_repeat),
                                "acceptance_artifact_path": "",
                                "blocker_artifact_path": str(blocker_artifact),
                                "missing_fields": [],
                                "blocked_fields": close_blockers,
                            }
                        )
                        write_run_summary("blocked", 20, str(loop_finalize_repeat), "", str(blocker_artifact))
                        print(json.dumps(base_result, sort_keys=True))
                        return 20

                finalize_payload = load_json(loop_finalize) if loop_finalize.exists() else {}
                finalize_payload_repeat = load_json(loop_finalize_repeat) if loop_finalize_repeat.exists() else {}
                accepted_type = str(finalize_payload_repeat.get("accepted_type") or finalize_payload.get("accepted_type") or "ACCEPTED_SUCCESS")
                success_artifact = (
                    codex_home / "state" / "postflight_done" / route_task_id / track_id / (
                        "finalize.accepted.json" if accepted_type == "ACCEPTED_SUCCESS" else "finalize.blocked.json"
                    )
                )
                success_payload = {
                    "schema_version": "postflight_acceptance.v1",
                    "accepted_type": accepted_type,
                    "run_id": run_id,
                    "route_task_id": route_task_id,
                    "track_id": track_id,
                    "route_class": args.route_class,
                    "finalize_json_path": str(loop_finalize),
                    "finalize_repeat_json_path": str(loop_finalize_repeat),
                    "ok": True,
                    "objective_closure_state": str(finalize_payload_repeat.get("objective_closure_state") or finalize_payload.get("objective_closure_state") or ""),
                    "migration_fallback_used": bool(finalize_payload_repeat.get("migration_fallback_used") or finalize_payload.get("migration_fallback_used")),
                    "gate": "planning_gate_finalize",
                    "generated_at": now_iso(),
                }
                success_payload["artifact_sha256"] = sha256_bytes(json.dumps(success_payload, sort_keys=True).encode("utf-8"))
                write_json(success_artifact, success_payload)
                emit_ralph_meta(
                    telemetry_path=telemetry_path,
                    run_id=run_id,
                    route_task_id=route_task_id,
                    track_id=track_id,
                    route_class=args.route_class,
                    loop_idx=loop_idx,
                    gate="planning_gate_finalize",
                    status="approve",
                    reason_code="APPROVED",
                    reason="Gate approved",
                    elapsed_ms=elapsed_ms3 + elapsed_ms3b,
                    finalize_json_path=loop_finalize_repeat,
                    exit_code=0,
                )
                base_result.update(
                    {
                        "ok": True,
                        "status": "approve",
                        "reason_code": "APPROVED" if accepted_type == "ACCEPTED_SUCCESS" else "ACCEPTED_BLOCKED",
                        "reason": "Gate approved" if accepted_type == "ACCEPTED_SUCCESS" else "Gate approved with blocked remainder",
                        "exit_code": 0,
                        "finalize_json_path": str(loop_finalize_repeat),
                        "acceptance_artifact_path": str(success_artifact),
                        "blocker_artifact_path": "",
                        "missing_fields": [],
                        "blocked_fields": [],
                    }
                )
                write_run_summary("approve", 0, str(loop_finalize_repeat), str(success_artifact), "")
                print(json.dumps(base_result, sort_keys=True))
                return 0

            # Non-approve path
            status = "blocked" if f_blocked else "revise"
            reason_code = "BUDGET_EXHAUSTED" if (status == "revise" and loop_idx == args.max_loops) else (
                "FINALIZE_BLOCKED" if status == "blocked" else "MISSING_FIELDS"
            )
            reason = "Gate blocked" if status == "blocked" else "Revision required"
            emit_ralph_meta(
                telemetry_path=telemetry_path,
                run_id=run_id,
                route_task_id=route_task_id,
                track_id=track_id,
                route_class=args.route_class,
                loop_idx=loop_idx,
                gate="planning_gate_finalize",
                status="blocked" if (status == "blocked" or loop_idx == args.max_loops) else "revise",
                reason_code=reason_code,
                reason=reason,
                elapsed_ms=elapsed_ms3,
                finalize_json_path=loop_finalize,
                exit_code=20 if (status == "blocked" or loop_idx == args.max_loops) else 10,
            )
            if status == "blocked" or loop_idx == args.max_loops:
                blocker_artifact = write_blocker_artifact(
                    codex_home=codex_home,
                    route_task_id=route_task_id,
                    track_id=track_id,
                    route_class=args.route_class,
                    run_id=run_id,
                    loop_count=loop_idx,
                    reason_code=reason_code,
                    missing_fields=f_missing,
                    blocked_fields=f_blocked,
                    run_dir=run_dir,
                    stdout_log_path=stdout_log_path,
                    stderr_log_path=stderr_log_path,
                    finalize_json_path=str(loop_finalize),
                )
                base_result.update(
                    {
                        "ok": False,
                        "status": "blocked",
                        "reason_code": reason_code,
                        "reason": safe_reason(reason),
                        "exit_code": 20,
                        "finalize_json_path": str(loop_finalize),
                        "acceptance_artifact_path": "",
                        "blocker_artifact_path": str(blocker_artifact),
                        "missing_fields": f_missing,
                        "blocked_fields": f_blocked,
                    }
                )
                write_run_summary("blocked", 20, str(loop_finalize), "", str(blocker_artifact))
                print(json.dumps(base_result, sort_keys=True))
                return 20
            if external_mode:
                next_prompt = build_next_action_prompt(
                    "FINALIZE_REVISE_REQUIRED",
                    f_missing,
                    f_blocked,
                    base_result.get("gate_results", []),
                )
                base_result.update(
                    {
                        "ok": False,
                        "status": "revise",
                        "reason_code": "FINALIZE_REVISE_REQUIRED",
                        "reason": "Finalize gate requires revision",
                        "next_action_prompt": next_prompt,
                        "exit_code": 10,
                        "finalize_json_path": str(loop_finalize),
                        "acceptance_artifact_path": "",
                        "blocker_artifact_path": "",
                        "missing_fields": f_missing,
                        "blocked_fields": f_blocked,
                    }
                )
                write_run_summary("revise", 10, str(loop_finalize), "", "")
                print(json.dumps(base_result, sort_keys=True))
                return 10
            # revise and retry

        # Fallback if loop exits unexpectedly without return.
        base_result.update(
            {
                "ok": False,
                "status": "error",
                "reason_code": "UNEXPECTED_FALLTHROUGH",
                "reason": "Postflight loop ended unexpectedly",
                "exit_code": 30,
            }
        )
        blocker_artifact = write_blocker_artifact(
            codex_home=codex_home,
            route_task_id=route_task_id,
            track_id=track_id,
            route_class=args.route_class,
            run_id=run_id,
            loop_count=int(base_result.get("loop_count") or 0),
            reason_code="UNEXPECTED_FALLTHROUGH",
            missing_fields=[],
            blocked_fields=[],
            run_dir=run_dir,
            stdout_log_path=stdout_log_path,
            stderr_log_path=stderr_log_path,
            status="error",
        )
        base_result["blocker_artifact_path"] = str(blocker_artifact)
        emit_ralph_meta(
            telemetry_path=telemetry_path,
            run_id=run_id,
            route_task_id=route_task_id,
            track_id=track_id,
            route_class=args.route_class,
            loop_idx=int(base_result.get("loop_count") or 0),
            gate="planning_gate_finalize",
            status="error",
            reason_code="UNEXPECTED_FALLTHROUGH",
            reason="Postflight loop ended unexpectedly",
            elapsed_ms=0,
            finalize_json_path=None,
            exit_code=30,
        )
        write_run_summary("error", 30, base_result["finalize_json_path"], base_result["acceptance_artifact_path"], base_result["blocker_artifact_path"])
        print(json.dumps(base_result, sort_keys=True))
        return 30

    except RuntimeError as exc:
        blocker_artifact = write_blocker_artifact(
            codex_home=codex_home,
            route_task_id=route_task_id,
            track_id=track_id,
            route_class=args.route_class,
            run_id=run_id,
            loop_count=int(base_result.get("loop_count") or 0),
            reason_code="INVALID_INPUT",
            missing_fields=[],
            blocked_fields=[safe_reason(str(exc))],
            run_dir=run_dir,
            stdout_log_path=stdout_log_path,
            stderr_log_path=stderr_log_path,
            status="error",
        )
        base_result.update(
            {
                "ok": False,
                "status": "error",
                "reason_code": "INVALID_INPUT",
                "reason": safe_reason(str(exc)),
                "exit_code": 30,
                "blocker_artifact_path": str(blocker_artifact),
            }
        )
        emit_ralph_meta(
            telemetry_path=telemetry_path,
            run_id=run_id,
            route_task_id=route_task_id,
            track_id=track_id,
            route_class=args.route_class,
            loop_idx=int(base_result.get("loop_count") or 0),
            gate="planning_gate_finalize",
            status="error",
            reason_code="INVALID_INPUT",
            reason=safe_reason(str(exc)),
            elapsed_ms=0,
            finalize_json_path=None,
            exit_code=30,
        )
        write_run_summary("error", 30, "", "", str(blocker_artifact))
        print(json.dumps(base_result, sort_keys=True))
        return 30
    except Exception as exc:  # pragma: no cover
        blocker_artifact = write_blocker_artifact(
            codex_home=codex_home,
            route_task_id=route_task_id,
            track_id=track_id,
            route_class=args.route_class,
            run_id=run_id,
            loop_count=int(base_result.get("loop_count") or 0),
            reason_code="INTERNAL_ERROR",
            missing_fields=[],
            blocked_fields=[safe_reason(str(exc))],
            run_dir=run_dir,
            stdout_log_path=stdout_log_path,
            stderr_log_path=stderr_log_path,
            status="error",
        )
        base_result.update(
            {
                "ok": False,
                "status": "error",
                "reason_code": "INTERNAL_ERROR",
                "reason": safe_reason(str(exc)),
                "exit_code": 30,
                "blocker_artifact_path": str(blocker_artifact),
            }
        )
        emit_ralph_meta(
            telemetry_path=telemetry_path,
            run_id=run_id,
            route_task_id=route_task_id,
            track_id=track_id,
            route_class=args.route_class,
            loop_idx=int(base_result.get("loop_count") or 0),
            gate="planning_gate_finalize",
            status="error",
            reason_code="INTERNAL_ERROR",
            reason=safe_reason(str(exc)),
            elapsed_ms=0,
            finalize_json_path=None,
            exit_code=30,
        )
        write_run_summary("error", 30, "", "", str(blocker_artifact))
        print(json.dumps(base_result, sort_keys=True))
        return 30
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
