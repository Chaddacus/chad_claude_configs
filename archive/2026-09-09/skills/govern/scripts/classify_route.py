#!/usr/bin/env python3
"""Route classifier — reads route_manifest.json, classifies work into R1–R5.

Input (JSON on stdin):
  file_count_estimate, touches_auth, touches_security, touches_migrations,
  touches_production_behavior, estimated_complexity, has_ambiguity

Output (JSON on stdout):
  route_id, route_name, execution_shape, profile_overrides, lane_caps,
  frontier_dispatch_order, reviewer_barrier_points, swarm_cap,
  convergence_required
"""

import json
import os
import sys
from pathlib import Path

MANIFEST_PATH = Path(
    os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude"))
) / "state" / "route_manifest.json"

COMPLEXITY_RANKS = {
    "trivial": 0,
    "simple": 1,
    "moderate": 2,
    "complex": 3,
    "high": 3,
}


def load_manifest(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def classify(task: dict, manifest: dict) -> dict:
    """Apply classification rules in order — first match wins.

    Conservative by default: classify higher when borderline.
    """
    file_count = task.get("file_count_estimate", 0)
    touches_auth = task.get("touches_auth", False)
    touches_security = task.get("touches_security", False)
    touches_migrations = task.get("touches_migrations", False)
    touches_prod = task.get("touches_production_behavior", False)
    complexity = task.get("estimated_complexity", "moderate")
    has_ambiguity = task.get("has_ambiguity", False)
    complexity_rank = COMPLEXITY_RANKS.get(complexity, 2)

    high_risk = touches_auth or touches_security or touches_migrations

    # Build a lookup of route rules by id
    rules_by_id = {r["id"]: r for r in manifest.get("rules", [])}

    # --- Classification rules (first match wins) ---

    # R5: ambiguous and unresolvable from context
    if has_ambiguity:
        route_id = "R5"

    # R1: zero files, trivial/simple complexity
    elif file_count == 0 and complexity_rank <= 1:
        route_id = "R1"

    # R4: high-risk (auth/security/migrations)
    elif high_risk:
        route_id = "R4"

    # R2: small scope, no high-risk factors
    elif file_count <= 2 and not high_risk and complexity_rank <= 2:
        route_id = "R2"

    # R3: everything else
    else:
        route_id = "R3"

    # --- Conservative bump: borderline cases go higher ---
    # If touching production behavior and classified as R2, bump to R3
    if route_id == "R2" and touches_prod and complexity_rank >= 2:
        route_id = "R3"

    rule = rules_by_id.get(route_id, {})

    return {
        "route_id": route_id,
        "route_name": rule.get("name", "unknown"),
        "execution_shape": rule.get("execution_shape", "single_lane"),
        "profile_overrides": rule.get("profile_overrides", {}),
        "lane_caps": rule.get("lane_caps", {}),
        "frontier_dispatch_order": rule.get("frontier_dispatch_order", []),
        "reviewer_barrier_points": rule.get("reviewer_barrier_points", []),
        "swarm_cap": rule.get("route_swarm_cap", 1),
        "convergence_required": rule.get(
            "convergence_required_for_closure", False
        ),
        "risk_class": rule.get("risk_class", "unknown"),
        "packetization_required": rule.get("packetization_required", False),
        "default_parallelism_policy": rule.get(
            "default_parallelism_policy", "serial_only"
        ),
    }


def main():
    try:
        task = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON input: {e}"}), file=sys.stderr)
        sys.exit(1)

    if not MANIFEST_PATH.exists():
        print(
            json.dumps({"error": f"Route manifest not found: {MANIFEST_PATH}"}),
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        manifest = load_manifest(MANIFEST_PATH)
    except (json.JSONDecodeError, OSError) as e:
        print(
            json.dumps({"error": f"Failed to load manifest: {e}"}),
            file=sys.stderr,
        )
        sys.exit(1)

    result = classify(task, manifest)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
