#!/usr/bin/env python3
"""Run a command, redact output, and capture a hashed proof artifact."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from common import (
    CAPTURE_PRODUCER,
    CAPTURE_SCHEMA_VERSION,
    ensure_python_3_11,
    now_iso,
    redact_text,
    resolve_artifacts_root,
    sanitize_token,
    sha256_file,
)

DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s+-rf\s+/"),
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+checkout\s+--\b"),
    re.compile(r"\bcurl\b[^\n|]*\|\s*(sh|bash)\b"),
    re.compile(r"\bwget\b[^\n|]*\|\s*(sh|bash)\b"),
    re.compile(r"\bchmod\s+-R\s+777\s+/\b"),
]


def _check_command_safety(argv: list[str], allow_dangerous: bool, reason: str) -> tuple[bool, str]:
    command_str = " ".join(argv)
    for pattern in DANGEROUS_PATTERNS:
        if pattern.search(command_str):
            if allow_dangerous and reason.strip():
                return True, "allowed_with_override"
            return False, "dangerous_command_blocked"
    return True, "ok"


def _safe_env_view(extra_allow: list[str]) -> dict[str, str]:
    base = {"PATH", "HOME", "USER", "SHELL", "PWD", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP"}
    base.update(extra_allow)
    view = {}
    for key in sorted(base):
        if key in os.environ:
            view[key] = os.environ[key]
    return view


def main() -> int:
    ensure_python_3_11()

    parser = argparse.ArgumentParser(description="Run command and capture proof artifact.")
    parser.add_argument("--track-id", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--artifacts-root", default=None)
    parser.add_argument("--allow-dangerous", action="store_true")
    parser.add_argument("--reason", default="")
    parser.add_argument("--env-allow", action="append", default=[])
    parser.add_argument("--timeout-sec", type=int, default=300)
    parser.add_argument("--max-output-bytes", type=int, default=1_000_000)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]

    if not command:
        print(json.dumps({"type": "capture_result", "status": "blocked", "error": "missing_command"}))
        return 2

    safe, safety_reason = _check_command_safety(command, args.allow_dangerous, args.reason)
    if not safe:
        print(
            json.dumps(
                {
                    "type": "capture_result",
                    "status": "blocked",
                    "error": safety_reason,
                    "next_step": "Use a safer command or pass --allow-dangerous with --reason.",
                }
            )
        )
        return 2

    cwd_path = Path(args.cwd).expanduser().resolve()
    if not cwd_path.exists() or not cwd_path.is_dir():
        print(
            json.dumps(
                {
                    "type": "capture_result",
                    "status": "blocked",
                    "error": "invalid_cwd",
                    "cwd": str(cwd_path),
                },
                sort_keys=True,
            )
        )
        return 2

    artifacts_root = resolve_artifacts_root(args.artifacts_root, cwd=args.cwd)
    track_id = sanitize_token(args.track_id)
    stage = sanitize_token(args.stage)
    name = sanitize_token(args.name)

    ts = int(time.time())
    capture_dir = artifacts_root / track_id / "captures" / f"{ts}-{name}-{stage}"
    try:
        capture_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            json.dumps(
                {
                    "type": "capture_result",
                    "status": "blocked",
                    "error": "artifact_path_error",
                    "detail": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2

    started_at = now_iso()
    started_ns = time.time_ns()
    execution_env = _safe_env_view(args.env_allow)
    execution_env["PWD"] = str(cwd_path)
    timeout_hit = False
    try:
        with tempfile.TemporaryFile() as stdout_tmp, tempfile.TemporaryFile() as stderr_tmp:
            process = subprocess.Popen(
                command,
                cwd=str(cwd_path),
                stdout=stdout_tmp,
                stderr=stderr_tmp,
                env=execution_env,
            )
            try:
                process.wait(timeout=args.timeout_sec)
            except subprocess.TimeoutExpired:
                timeout_hit = True
                process.kill()
                process.wait()

            stdout_tmp.seek(0)
            stderr_tmp.seek(0)
            stdout_bytes = stdout_tmp.read(max(args.max_output_bytes, 0) + 1)
            stderr_bytes = stderr_tmp.read(max(args.max_output_bytes, 0) + 1)
            stdout_truncated = len(stdout_bytes) > max(args.max_output_bytes, 0)
            stderr_truncated = len(stderr_bytes) > max(args.max_output_bytes, 0)
            if stdout_truncated:
                stdout_bytes = stdout_bytes[: max(args.max_output_bytes, 0)]
            if stderr_truncated:
                stderr_bytes = stderr_bytes[: max(args.max_output_bytes, 0)]
            exit_code = 124 if timeout_hit else int(process.returncode)
    except FileNotFoundError as exc:
        print(
            json.dumps(
                {
                    "type": "capture_result",
                    "status": "blocked",
                    "error": "command_not_found",
                    "detail": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2
    except OSError as exc:
        print(
            json.dumps(
                {
                    "type": "capture_result",
                    "status": "blocked",
                    "error": "command_execution_error",
                    "detail": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "type": "capture_result",
                    "status": "blocked",
                    "error": "capture_runtime_error",
                    "detail": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2
    ended_ns = time.time_ns()
    ended_at = now_iso()

    stdout_text = redact_text(stdout_bytes.decode("utf-8", errors="replace"))
    stderr_text = redact_text(stderr_bytes.decode("utf-8", errors="replace"))
    if stdout_truncated:
        stdout_text += "\n[TRUNCATED]"
    if stderr_truncated:
        stderr_text += "\n[TRUNCATED]"

    stdout_path = capture_dir / "stdout.redacted.txt"
    stderr_path = capture_dir / "stderr.redacted.txt"
    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")

    manifest = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "producer": CAPTURE_PRODUCER,
        "captured_at": ended_at,
        "track_id": track_id,
        "stage": args.stage,
        "name": args.name,
        "cwd": str(cwd_path),
        "command_argv": command,
        "exit_code": exit_code,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": int((ended_ns - started_ns) / 1_000_000),
        "stdout_path": str(stdout_path.resolve()),
        "stderr_path": str(stderr_path.resolve()),
        "stdout_sha256": sha256_file(stdout_path),
        "stderr_sha256": sha256_file(stderr_path),
        "safety": {
            "decision": safety_reason,
            "override_used": bool(args.allow_dangerous),
            "override_reason": args.reason.strip(),
        },
        "timeout_sec": args.timeout_sec,
        "timeout_exceeded": timeout_hit,
        "output_truncated": stdout_truncated or stderr_truncated,
        "env_whitelist": execution_env,
    }

    manifest_path = capture_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    proof_hash = sha256_file(manifest_path)

    result = {
        "type": "capture_result",
        "status": "pass" if exit_code == 0 else "fail",
        "track_id": track_id,
        "stage": args.stage,
        "name": args.name,
        "exit_code": exit_code,
        "proof_artifact": str(manifest_path.resolve()),
        "proof_hash": proof_hash,
        "stdout_path": str(stdout_path.resolve()),
        "stderr_path": str(stderr_path.resolve()),
        "timeout_exceeded": timeout_hit,
        "next_step": "Use proof_artifact and proof_hash in implementation evidence.",
    }
    print(json.dumps(result, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
