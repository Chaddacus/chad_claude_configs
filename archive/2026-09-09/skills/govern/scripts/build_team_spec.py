#!/usr/bin/env python3
"""Team factory — given route classification, produce TeamCreate-compatible specs.

Input (JSON on stdin): output from classify_route.py
Output (JSON on stdout): team_name, execution_mode, members[]

For R1/R2: returns {"execution_mode": "inline"} (no team needed).
For R3/R4: builds full team with agents respecting lane_caps and profile_overrides.
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Tuple

CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")))
AGENTS_DIR = CLAUDE_HOME / "agents"


def parse_agent_frontmatter(path: Path) -> dict:
    """Extract YAML frontmatter from agent .md files."""
    text = path.read_text()
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}

    frontmatter = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            # Handle simple types
            if value.lower() in ("true", "false"):
                frontmatter[key] = value.lower() == "true"
            else:
                frontmatter[key] = value
    return frontmatter


def load_agent_definition(name: str) -> Tuple[dict, str]:
    """Load an agent's frontmatter and full content."""
    path = AGENTS_DIR / f"{name}.md"
    if not path.exists():
        return {}, ""
    frontmatter = parse_agent_frontmatter(path)
    content = path.read_text()
    return frontmatter, content


def resolve_model(agent_name: str, profile_overrides: dict, default_fm: dict) -> str:
    """Resolve the model for an agent: profile_overrides > frontmatter > default."""
    override = profile_overrides.get(agent_name, {})
    return override.get("model", default_fm.get("model", "claude-opus-4-6"))


def resolve_effort(agent_name: str, profile_overrides: dict, default_fm: dict) -> str:
    """Resolve the effort for an agent: profile_overrides > frontmatter > medium.

    Mirrors resolve_model so the route manifest's effort overrides (e.g. R4
    reviewer at xhigh) actually reach the team spec. Prior to this, effort
    was read only from agent frontmatter, silently ignoring profile_overrides.
    """
    override = profile_overrides.get(agent_name, {})
    return override.get("effort", default_fm.get("effort", "medium"))


def build_team(route: dict, track_id: str) -> dict:
    """Build a team spec for R3/R4 bounded swarm routes."""
    route_id = route["route_id"]
    lane_caps = route.get("lane_caps", {})
    profile_overrides = route.get("profile_overrides", {})
    swarm_cap = route.get("swarm_cap", 4 if route_id == "R3" else 2)

    team_name = f"govern-{track_id}"

    # Agent roles and their counts (based on lane_caps from manifest)
    agent_roles = ["planner", "worker", "explorer", "validator", "reviewer"]
    members = []

    for role in agent_roles:
        cap = lane_caps.get(role, 1)
        fm, content = load_agent_definition(role)
        model = resolve_model(role, profile_overrides, fm)
        effort = resolve_effort(role, profile_overrides, fm)

        for i in range(cap):
            suffix = f"-{i + 1}" if cap > 1 else ""
            name = f"{role}{suffix}"

            # Map sandbox to agent type
            sandbox = fm.get("sandbox", "read-only")
            if sandbox == "workspace-write":
                agent_type = "general-purpose"
            elif role == "explorer":
                agent_type = "Explore"
            elif role == "planner":
                agent_type = "Plan"
            else:
                agent_type = "general-purpose"

            member = {
                "name": name,
                "agentType": agent_type,
                "model": model,
                "role": role,
                "prompt": content,
                "sandbox": sandbox,
                "effort": effort,
            }
            members.append(member)

    return {
        "team_name": team_name,
        "execution_mode": "bounded_swarm",
        "route_id": route_id,
        "swarm_cap": swarm_cap,
        "lane_caps": lane_caps,
        "frontier_dispatch_order": route.get("frontier_dispatch_order", []),
        "reviewer_barrier_points": route.get("reviewer_barrier_points", []),
        "convergence_required": route.get("convergence_required", True),
        "members": members,
    }


def main():
    try:
        route = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON input: {e}"}), file=sys.stderr)
        sys.exit(1)

    route_id = route.get("route_id", "R1")

    # R1/R2: inline execution, no team needed
    if route_id in ("R1", "R2", "R5"):
        result = {
            "execution_mode": "inline",
            "route_id": route_id,
            "team_name": None,
            "members": [],
        }
        print(json.dumps(result, indent=2))
        return

    # Generate a short track id from timestamp
    import hashlib
    import time

    track_id = hashlib.sha256(str(time.time()).encode()).hexdigest()[:8]

    result = build_team(route, track_id)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
