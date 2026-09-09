#!/usr/bin/env python3
"""hermes_invoke.py — thin client for Chad's Hermes phase orchestrator.

Hermes runs on :3345 with REST + SSE. It is a phase orchestrator (Greenfield
8-phase: plan/design/backend/frontend/test/e2e/security/validate; Refactor
5-phase: index/plan/refactor/test/validate), NOT a council.

Source: cloudwarriors-ai/hermes (~/code/hermes)
Plan:   ~/.claude/plans/users-chadsimon-thoughts-md-take-a-giggly-moore.md (slice 4)

Usage:
    hermes_invoke.py --flow greenfield <prompt>
    hermes_invoke.py --flow refactor <repo-path> <goal>
    hermes_invoke.py --status <workflow-id>
    hermes_invoke.py --metrics <workflow-id>

Streams SSE events from :3345/api/events to stdout (one JSON line per event).
On completion, prints a phase summary to stderr and the workflow result to
stdout as a single JSON blob.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERMES_BASE = os.environ.get("HERMES_BASE", "http://localhost:3345")
HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/code/hermes")))
TIMEOUT_S = int(os.environ.get("HERMES_TIMEOUT", "1800"))  # 30 min default

GREENFIELD_PHASES = ["plan", "design", "backend", "frontend", "test", "e2e", "security", "validate"]
REFACTOR_PHASES = ["index", "plan", "refactor", "test", "validate"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_json(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{HERMES_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": "http", "status": e.code, "body": e.read().decode("utf-8", errors="replace")}
    except (urllib.error.URLError, OSError) as e:
        return {"error": "transport", "message": str(e)}


def health_check() -> bool:
    """Return True iff Hermes API is reachable."""
    try:
        with urllib.request.urlopen(f"{HERMES_BASE}/api/sessions", timeout=5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def ensure_hermes_up() -> bool:
    """If Hermes isn't reachable, attempt `docker compose up -d` from HERMES_HOME."""
    if health_check():
        return True
    if not HERMES_HOME.exists():
        print(f"hermes: repo missing at {HERMES_HOME} — clone via "
              f"`git clone https://github.com/cloudwarriors-ai/hermes {HERMES_HOME}`", file=sys.stderr)
        return False
    if not shutil.which("docker"):
        print("hermes: docker not found in PATH", file=sys.stderr)
        return False
    print("hermes: attempting `docker compose up -d` ...", file=sys.stderr)
    try:
        subprocess.run(["docker", "compose", "up", "-d"], cwd=HERMES_HOME, timeout=120,
                       check=True, capture_output=True)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"hermes: docker compose up failed: {exc}", file=sys.stderr)
        return False
    # Poll for readiness up to 30s
    for _ in range(15):
        if health_check():
            return True
        time.sleep(2)
    print("hermes: started but not yet ready after 30s", file=sys.stderr)
    return False


def start_workflow(prompt: str, flow_type: str, repo_path: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"prompt": prompt, "flowType": flow_type}
    if repo_path:
        body["repoPath"] = repo_path
    return _http_json("POST", "/api/prompt", body)


def stream_events(workflow_id: str | None = None, deadline_s: int = TIMEOUT_S) -> int:
    """Stream SSE events from /api/events to stdout (one JSON line per event).

    Returns 0 on clean completion (workflow_complete event seen), 1 on timeout,
    2 on transport error.
    """
    url = f"{HERMES_BASE}/api/events"
    if workflow_id:
        url += f"?workflowId={workflow_id}"
    start = time.time()
    try:
        req = urllib.request.Request(url)
        req.add_header("Accept", "text/event-stream")
        with urllib.request.urlopen(req, timeout=deadline_s) as resp:
            current_event = ""
            current_data = ""
            for raw in resp:
                if time.time() - start > deadline_s:
                    print(json.dumps({"event": "client_timeout", "after_s": deadline_s}), flush=True)
                    return 1
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    current_data = line[5:].strip()
                elif line == "":
                    if current_event or current_data:
                        try:
                            payload = json.loads(current_data) if current_data else {}
                        except json.JSONDecodeError:
                            payload = {"raw": current_data}
                        out = {"event": current_event or "message", "data": payload, "ts": _now()}
                        print(json.dumps(out), flush=True)
                        if current_event in ("workflow_complete", "workflow_failed"):
                            return 0
                        current_event = ""
                        current_data = ""
        return 0
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        print(json.dumps({"event": "transport_error", "message": str(exc)}), flush=True)
        return 2


def workflow_status(workflow_id: str) -> dict[str, Any]:
    return _http_json("GET", f"/api/sessions?workflowId={workflow_id}")


def workflow_metrics(workflow_id: str) -> dict[str, Any]:
    return _http_json("GET", f"/api/metrics?workflowId={workflow_id}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Thin client for Hermes phase orchestrator.")
    ap.add_argument("--flow", choices=("greenfield", "refactor"),
                    help="Start a new workflow with this flow type.")
    ap.add_argument("--prompt", help="Greenfield prompt or refactor goal.")
    ap.add_argument("--repo", type=Path, default=None,
                    help="Repo path (required for refactor flow).")
    ap.add_argument("--status", metavar="WORKFLOW_ID", help="Get status for an existing workflow.")
    ap.add_argument("--metrics", metavar="WORKFLOW_ID", help="Get metrics for an existing workflow.")
    ap.add_argument("--no-stream", action="store_true",
                    help="Don't tail SSE events after starting workflow.")
    ap.add_argument("--no-autostart", action="store_true",
                    help="Don't try to docker-compose-up Hermes if it's down.")
    args = ap.parse_args()

    # Health gate
    if not args.no_autostart and not health_check():
        if not ensure_hermes_up():
            print("hermes: API unreachable on " + HERMES_BASE, file=sys.stderr)
            return 4

    if args.status:
        print(json.dumps(workflow_status(args.status), indent=2))
        return 0
    if args.metrics:
        print(json.dumps(workflow_metrics(args.metrics), indent=2))
        return 0

    if not args.flow:
        ap.error("provide --flow {greenfield,refactor} or --status/--metrics")

    if args.flow == "refactor" and not args.repo:
        ap.error("--repo is required for refactor flow")
    if not args.prompt:
        ap.error("--prompt is required when starting a workflow")

    repo_path = str(args.repo.resolve()) if args.repo else None
    started = start_workflow(args.prompt, args.flow, repo_path)
    if started.get("error"):
        print(json.dumps(started, indent=2), file=sys.stderr)
        return 5

    workflow_id = started.get("workflowId") or started.get("id") or started.get("workflow_id")
    print(json.dumps({"event": "workflow_started", "data": started, "ts": _now()}), flush=True)

    expected_phases = GREENFIELD_PHASES if args.flow == "greenfield" else REFACTOR_PHASES
    print(json.dumps({"event": "expected_phases", "data": expected_phases, "ts": _now()}), flush=True)

    if args.no_stream or not workflow_id:
        return 0

    return stream_events(workflow_id)


if __name__ == "__main__":
    sys.exit(main())
