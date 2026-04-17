#!/usr/bin/env python3
"""SessionStart hook — validate runtime prerequisites.

Checks:
1. Route manifest exists and is valid JSON
2. Required runtime files from manifest are present
3. Agent definitions exist
4. Lock directory is writable

Output: JSON status to stdout (consumed by Claude as hook output).
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")))


def check_file(path: Path) -> dict:
    """Check if a file exists and is readable."""
    exists = path.exists()
    readable = os.access(path, os.R_OK) if exists else False
    return {"path": str(path), "exists": exists, "readable": readable}


def validate_manifest(manifest_path: Path) -> Tuple[bool, Optional[dict], str]:
    """Validate that the route manifest is present and parseable."""
    if not manifest_path.exists():
        return False, None, f"manifest not found: {manifest_path}"
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
        if "rules" not in manifest:
            return False, manifest, "manifest missing 'rules' key"
        return True, manifest, "ok"
    except json.JSONDecodeError as e:
        return False, None, f"manifest parse error: {e}"


def check_agents() -> list[dict]:
    """Check that expected agent definitions exist."""
    agents_dir = CLAUDE_HOME / "agents"
    expected = ["planner", "worker", "reviewer", "explorer", "validator"]
    results = []
    for name in expected:
        path = agents_dir / f"{name}.md"
        results.append({
            "agent": name,
            "exists": path.exists(),
            "path": str(path),
        })
    return results


def check_lock_dir(manifest: Optional[dict]) -> dict:
    """Check that the postflight lock directory exists and is writable."""
    lock_dir = None
    if manifest:
        lock_dir = manifest.get("postflight", {}).get("lock_dir")
    if not lock_dir:
        lock_dir = str(CLAUDE_HOME / "state" / "locks")

    path = Path(lock_dir)
    exists = path.exists()
    writable = os.access(path, os.W_OK) if exists else False

    # Try to create it if missing
    if not exists:
        try:
            path.mkdir(parents=True, exist_ok=True)
            exists = True
            writable = True
        except OSError:
            pass

    return {"path": str(path), "exists": exists, "writable": writable}


def main():
    manifest_path = CLAUDE_HOME / "state" / "route_manifest.json"

    # Validate manifest
    manifest_ok, manifest, manifest_msg = validate_manifest(manifest_path)

    # Check agents
    agents = check_agents()
    agents_ok = all(a["exists"] for a in agents)

    # Check lock directory
    lock_status = check_lock_dir(manifest)

    # Check required runtime files from control_plane (may be inline or referenced)
    runtime_files = []
    if manifest:
        control_plane = manifest.get("control_plane", {})
        if not control_plane and manifest.get("control_plane_ref"):
            cp_path = CLAUDE_HOME / manifest["control_plane_ref"]
            try:
                control_plane = json.loads(cp_path.read_text())
            except Exception:
                control_plane = {}
        required = (
            control_plane
            .get("runtime_home_policy", {})
            .get("required_runtime_files", [])
        )
        for rel_path in required:
            full = CLAUDE_HOME / rel_path
            runtime_files.append(check_file(full))

    runtime_ok = all(f["exists"] for f in runtime_files) if runtime_files else True

    # Overall status
    all_ok = manifest_ok and agents_ok and lock_status.get("writable", False)

    status = {
        "governance_ready": all_ok,
        "manifest": {"valid": manifest_ok, "message": manifest_msg},
        "agents": {"all_present": agents_ok, "details": agents},
        "lock_dir": lock_status,
        "runtime_files": {
            "all_present": runtime_ok,
            "checked": len(runtime_files),
            "missing": [f["path"] for f in runtime_files if not f["exists"]],
        },
    }

    # Warnings (non-fatal)
    warnings = []
    if not runtime_ok:
        missing = [f["path"] for f in runtime_files if not f["exists"]]
        warnings.append(f"Missing runtime files: {', '.join(missing)}")
    if not agents_ok:
        missing_agents = [a["agent"] for a in agents if not a["exists"]]
        warnings.append(f"Missing agents: {', '.join(missing_agents)}")

    if warnings:
        status["warnings"] = warnings

    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()
