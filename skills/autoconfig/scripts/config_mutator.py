"""Config mutation engine for autoconfig.

Generates config mutations for Phases 1-3 (deterministic sweeps) and
Phase 4 (Claude-directed).  Applies mutations atomically to the three
config surfaces:

  1. ``~/.claude/state/route_manifest.json``  -- routes, profiles, caps, thresholds
  2. ``~/.claude/settings.json``              -- model defaults, effort level
  3. ``~/.claude/agents/*.md``                -- agent definitions (YAML frontmatter)

Safety invariant: immutable fields are never written.  ``validate_mutation``
is always called before any write.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from itertools import permutations
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLAUDE_HOME: Path = Path.home() / ".claude"
MANIFEST_PATH: Path = CLAUDE_HOME / "state" / "route_manifest.json"
SETTINGS_PATH: Path = CLAUDE_HOME / "settings.json"
AGENTS_DIR: Path = CLAUDE_HOME / "agents"

VALID_MODELS: list[str] = [
    "claude-haiku-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
]
VALID_EFFORTS: list[str] = ["low", "medium", "high"]

LANE_CAP_RANGE: tuple[int, int] = (1, 4)
SWARM_CAP_RANGE: tuple[int, int] = (1, 8)
MAX_PARALLEL_RANGE: tuple[int, int] = (1, 6)

AGENT_NAMES: list[str] = ["worker", "planner", "reviewer", "explorer", "validator"]
ROUTE_IDS: list[str] = ["R1", "R2", "R3", "R4"]  # R5 is ambiguous, not optimized
PROFILE_NAMES: list[str] = [
    "coordinator",
    "worker",
    "explorer",
    "planner",
    "reviewer",
    "validator",
]

IMMUTABLE_PATHS: list[str] = [
    "permissions.deny",
    "permissions.allow",
    "env.CLAUDE_HOME",
    "mcpServers",
    "hooks",
    "control_plane_ref",
    "postflight.enabled",
    "postflight.mode",
    "thresholds.high_risk_false_negatives",
]

# YAML frontmatter fields in agent .md files that belong to the
# immutable/structural set and must not be mutated.
_IMMUTABLE_AGENT_FIELDS: frozenset[str] = frozenset({"sandbox"})


# ---------------------------------------------------------------------------
# JSON path helpers
# ---------------------------------------------------------------------------

_PATH_SEGMENT_RE = re.compile(r"^([^\[]+)(?:\[(\d+)\])?$")


def _parse_segments(path: str) -> list[tuple[str, Optional[int]]]:
    """Parse a dot-separated JSON path into (key, optional_index) segments.

    Examples::

        "rules[1].profile_overrides.worker.model"
        -> [("rules", 1), ("profile_overrides", None), ("worker", None), ("model", None)]
    """
    segments: list[tuple[str, Optional[int]]] = []
    for part in path.split("."):
        m = _PATH_SEGMENT_RE.match(part)
        if not m:
            raise ValueError(f"Invalid path segment: {part!r}")
        key = m.group(1)
        idx = int(m.group(2)) if m.group(2) is not None else None
        segments.append((key, idx))
    return segments


def get_by_path(obj: Any, path: str) -> Any:
    """Retrieve a value from a nested dict/list by dot-notation path.

    Raises ``KeyError`` or ``IndexError`` if the path does not exist.
    """
    current = obj
    for key, idx in _parse_segments(path):
        current = current[key]
        if idx is not None:
            current = current[idx]
    return current


def set_by_path(obj: Any, path: str, value: Any) -> None:
    """Set a value in a nested dict/list by dot-notation path.

    Intermediate dicts are created if they don't exist.  List indices must
    already exist (i.e., the list must be long enough).
    """
    segments = _parse_segments(path)
    current = obj
    for key, idx in segments[:-1]:
        if key not in current:
            current[key] = {}
        current = current[key]
        if idx is not None:
            current = current[idx]

    last_key, last_idx = segments[-1]
    if last_idx is not None:
        if last_key not in current:
            current[last_key] = []
        current[last_key][last_idx] = value
    else:
        current[last_key] = value


def _path_matches_immutable(path: str) -> bool:
    """Return True if *path* starts with any immutable prefix."""
    for immutable in IMMUTABLE_PATHS:
        if path == immutable or path.startswith(immutable + ".") or path.startswith(immutable + "["):
            return True
    return False


# ---------------------------------------------------------------------------
# YAML frontmatter parser (no external dependency)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?\n)---\s*\n",
    re.DOTALL,
)


def _parse_yaml_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown file.

    Returns (frontmatter_dict, body) where body is everything after the
    closing ``---``.  Uses a minimal parser that handles the simple
    key: value pairs found in agent definitions, including nested maps
    like ``model_overrides``.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text

    yaml_block = m.group(1)
    body = text[m.end():]
    result: dict[str, Any] = {}
    current_key: Optional[str] = None
    current_map: Optional[dict[str, str]] = None

    for line in yaml_block.splitlines():
        stripped = line.rstrip()
        if not stripped:
            continue

        # Detect indented sub-key (nested map value).
        if line.startswith("  ") and current_key is not None:
            sub_match = re.match(r"^\s+(\S+):\s*(.+)$", line)
            if sub_match:
                if current_map is None:
                    current_map = {}
                    result[current_key] = current_map
                current_map[sub_match.group(1)] = _coerce_yaml_value(sub_match.group(2).strip())
                continue

        # Top-level key.
        top_match = re.match(r"^(\S+):\s*(.*)?$", stripped)
        if top_match:
            # Flush previous nested map.
            current_key = top_match.group(1)
            current_map = None
            val = (top_match.group(2) or "").strip()
            if val:
                result[current_key] = _coerce_yaml_value(val)
            # If val is empty, the next lines might be a nested map;
            # current_key is set so the indented-line branch can catch them.

    return result, body


def _coerce_yaml_value(val: str) -> Any:
    """Coerce a YAML scalar string to a Python value."""
    if val in ("true", "True"):
        return True
    if val in ("false", "False"):
        return False
    if val in ("null", "~", ""):
        return None
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    # Strip optional quotes.
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        return val[1:-1]
    return val


def _serialize_yaml_frontmatter(fm: dict[str, Any]) -> str:
    """Serialize a frontmatter dict back to YAML-ish text (matching agent convention)."""
    lines: list[str] = ["---"]
    for key, val in fm.items():
        if isinstance(val, dict):
            lines.append(f"{key}:")
            for sub_key, sub_val in val.items():
                lines.append(f"  {sub_key}: {_yaml_scalar(sub_val)}")
        else:
            lines.append(f"{key}: {_yaml_scalar(val)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _yaml_scalar(val: Any) -> str:
    """Format a Python value as a YAML scalar."""
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to *path* via temp file + rename for crash safety."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.stem}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str)
            fh.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text to *path* via temp file + rename for crash safety."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.stem}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_manifest() -> dict:
    """Read route_manifest.json and return the parsed dict."""
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def save_manifest(data: dict) -> None:
    """Atomically write route_manifest.json."""
    _atomic_write_json(MANIFEST_PATH, data)


def load_settings() -> dict:
    """Read settings.json and return the parsed dict."""
    return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))


def save_settings(data: dict) -> None:
    """Atomically write settings.json."""
    _atomic_write_json(SETTINGS_PATH, data)


def load_agent(name: str) -> tuple[dict, str]:
    """Read an agent ``.md`` file and parse its YAML frontmatter.

    Parameters
    ----------
    name : str
        Agent name (e.g. ``"worker"``), without the ``.md`` extension.

    Returns
    -------
    tuple[dict, str]
        ``(frontmatter_dict, body_text)`` where body_text is the markdown
        content after the closing ``---``.
    """
    path = AGENTS_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    return _parse_yaml_frontmatter(text)


def save_agent(name: str, frontmatter: dict, body: str) -> None:
    """Write an agent ``.md`` file with YAML frontmatter."""
    path = AGENTS_DIR / f"{name}.md"
    text = _serialize_yaml_frontmatter(frontmatter) + body
    _atomic_write_text(path, text)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_mutation(mutation: dict) -> tuple[bool, str]:
    """Validate a mutation dict before application.

    Checks:
      - No immutable fields are touched.
      - Numeric values are within defined bounds.
      - Model and effort values are from valid sets.
      - The mutation structure is well-formed.

    Returns
    -------
    tuple[bool, str]
        ``(valid, error_message)``.  When valid, error_message is ``""``.
    """
    # Structural checks.
    if not isinstance(mutation, dict):
        return False, "Mutation must be a dict"
    for required in ("phase", "summary", "target_file", "changes"):
        if required not in mutation:
            return False, f"Missing required field: {required}"
    if not isinstance(mutation["changes"], list) or len(mutation["changes"]) == 0:
        return False, "Mutation must have at least one change"

    target = mutation["target_file"]
    for change in mutation["changes"]:
        path = change.get("path", "")
        new_val = change.get("new_value")

        # --- Immutable field check ---
        if target == "manifest":
            if _path_matches_immutable(path):
                return False, f"Immutable field: {path}"
            # Also check rules[*].risk_class pattern.
            if re.match(r"rules\[\d+\]\.risk_class", path):
                return False, f"Immutable field: {path}"
        elif target == "settings":
            if _path_matches_immutable(path):
                return False, f"Immutable field: {path}"

        # --- Bound checks ---
        # Model values.
        if path.endswith(".model") or path == "model":
            if new_val not in VALID_MODELS:
                return False, f"Invalid model {new_val!r} at {path}"

        # Effort values.
        if path.endswith(".effort") or path == "effort":
            if new_val not in VALID_EFFORTS:
                return False, f"Invalid effort {new_val!r} at {path}"

        # Lane caps.
        if "lane_caps." in path or path.endswith("lane_caps"):
            if isinstance(new_val, (int, float)):
                if not (LANE_CAP_RANGE[0] <= int(new_val) <= LANE_CAP_RANGE[1]):
                    return False, f"Lane cap {new_val} out of range {LANE_CAP_RANGE} at {path}"

        # Swarm cap.
        if "route_swarm_cap" in path:
            if isinstance(new_val, (int, float)):
                if not (SWARM_CAP_RANGE[0] <= int(new_val) <= SWARM_CAP_RANGE[1]):
                    return False, f"Swarm cap {new_val} out of range {SWARM_CAP_RANGE} at {path}"

        # Max parallel packets.
        if "max_parallel_packets" in path:
            if isinstance(new_val, (int, float)):
                if not (MAX_PARALLEL_RANGE[0] <= int(new_val) <= MAX_PARALLEL_RANGE[1]):
                    return False, f"Max parallel {new_val} out of range {MAX_PARALLEL_RANGE} at {path}"

        # Settings-level effort.
        if target == "settings" and path == "effortLevel":
            if new_val not in VALID_EFFORTS:
                return False, f"Invalid effortLevel {new_val!r}"

    return True, ""


# ---------------------------------------------------------------------------
# Mutation application
# ---------------------------------------------------------------------------

def apply_mutation(mutation: dict) -> bool:
    """Apply a validated mutation to live config files.

    Validates first, then reads the current file, applies changes, and
    writes back atomically.

    Returns
    -------
    bool
        ``True`` if the mutation was applied successfully.

    Raises
    ------
    ValueError
        If validation fails.
    """
    valid, err = validate_mutation(mutation)
    if not valid:
        raise ValueError(f"Mutation validation failed: {err}")

    target = mutation["target_file"]

    if target == "manifest":
        data = load_manifest()
        for change in mutation["changes"]:
            set_by_path(data, change["path"], change["new_value"])
        save_manifest(data)
        return True

    if target == "settings":
        data = load_settings()
        for change in mutation["changes"]:
            set_by_path(data, change["path"], change["new_value"])
        save_settings(data)
        return True

    if target.startswith("agents/"):
        agent_name = target.split("/", 1)[1].removesuffix(".md")
        if agent_name not in AGENT_NAMES:
            raise ValueError(f"Unknown agent: {agent_name}")
        frontmatter, body = load_agent(agent_name)
        for change in mutation["changes"]:
            field = change["path"]
            # Handle nested frontmatter fields like model_overrides.R2.
            parts = field.split(".")
            if len(parts) == 1:
                frontmatter[parts[0]] = change["new_value"]
            else:
                # Walk/create nested dicts.
                current = frontmatter
                for part in parts[:-1]:
                    if part not in current or not isinstance(current[part], dict):
                        current[part] = {}
                    current = current[part]
                current[parts[-1]] = change["new_value"]
        save_agent(agent_name, frontmatter, body)
        return True

    raise ValueError(f"Unknown target_file: {target}")


# ---------------------------------------------------------------------------
# Manifest structure helpers
# ---------------------------------------------------------------------------

def _find_rule_index(manifest: dict, route_id: str) -> Optional[int]:
    """Return the index of *route_id* in manifest['rules'], or None."""
    for i, rule in enumerate(manifest.get("rules", [])):
        if rule.get("id") == route_id:
            return i
    return None


def _get_current_model(manifest: dict, route_id: str, profile: str) -> Optional[str]:
    """Get the effective model for *profile* in *route_id*.

    Resolution order:
      1. rules[idx].profile_overrides.<profile>.model
      2. profiles.<profile>.model  (global default)
      3. coordinator.model  (if profile is "coordinator")
    """
    idx = _find_rule_index(manifest, route_id)
    if idx is not None:
        rule = manifest["rules"][idx]
        overrides = rule.get("profile_overrides", {})
        if profile in overrides and "model" in overrides[profile]:
            return overrides[profile]["model"]

    if profile == "coordinator":
        return manifest.get("coordinator", {}).get("model")

    return manifest.get("profiles", {}).get(profile, {}).get("model")


def _get_current_effort(manifest: dict, route_id: str, profile: str) -> Optional[str]:
    """Get the effective effort for *profile* in *route_id*."""
    idx = _find_rule_index(manifest, route_id)
    if idx is not None:
        rule = manifest["rules"][idx]
        overrides = rule.get("profile_overrides", {})
        if profile in overrides and "effort" in overrides[profile]:
            return overrides[profile]["effort"]

    if profile == "coordinator":
        return manifest.get("coordinator", {}).get("effort")

    return manifest.get("profiles", {}).get(profile, {}).get("effort")


def _profiles_in_route(manifest: dict, route_id: str) -> list[str]:
    """Return the list of profile names that are active in *route_id*.

    Uses both the global profiles and any profile_overrides in the route rule.
    """
    idx = _find_rule_index(manifest, route_id)
    if idx is None:
        return []

    rule = manifest["rules"][idx]
    profiles: set[str] = set()

    # Profiles from overrides.
    for name in rule.get("profile_overrides", {}):
        profiles.add(name)

    # Profiles implied by route string.
    route_str = rule.get("route", "")
    for token in re.split(r"[->+,\s]+", route_str):
        token = token.strip()
        if token in PROFILE_NAMES:
            profiles.add(token)

    return sorted(profiles)


# ---------------------------------------------------------------------------
# Phase 1: Model Assignment Sweep
# ---------------------------------------------------------------------------

def generate_phase1_mutations(tried: set[str]) -> list[dict]:
    """Generate model assignment mutations for all profiles in all routes.

    Also generates mutations for:
      - Global profile defaults (profiles.<name>.model)
      - Coordinator model (coordinator.model)
      - Agent .md frontmatter (model and model_overrides)
    """
    mutations: list[dict] = []
    manifest = load_manifest()

    # --- Per-route profile_overrides ---
    for route_id in ROUTE_IDS:
        idx = _find_rule_index(manifest, route_id)
        if idx is None:
            continue

        for profile in _profiles_in_route(manifest, route_id):
            current = _get_current_model(manifest, route_id, profile)
            if current is None:
                continue

            for model in VALID_MODELS:
                if model == current:
                    continue

                summary = f"{profile} model {route_id}: {current} -> {model}"
                if summary in tried:
                    continue

                path = f"rules[{idx}].profile_overrides.{profile}.model"
                mutations.append({
                    "phase": 1,
                    "summary": summary,
                    "target_file": "manifest",
                    "changes": [
                        {"path": path, "old_value": current, "new_value": model},
                    ],
                })

    # --- Global profile defaults ---
    for profile in list(manifest.get("profiles", {}).keys()):
        current = manifest["profiles"][profile].get("model")
        if current is None:
            continue
        for model in VALID_MODELS:
            if model == current:
                continue

            summary = f"{profile} model global: {current} -> {model}"
            if summary in tried:
                continue

            path = f"profiles.{profile}.model"
            mutations.append({
                "phase": 1,
                "summary": summary,
                "target_file": "manifest",
                "changes": [
                    {"path": path, "old_value": current, "new_value": model},
                ],
            })

    # --- Coordinator model ---
    coord_model = manifest.get("coordinator", {}).get("model")
    if coord_model:
        for model in VALID_MODELS:
            if model == coord_model:
                continue

            summary = f"coordinator model global: {coord_model} -> {model}"
            if summary in tried:
                continue

            mutations.append({
                "phase": 1,
                "summary": summary,
                "target_file": "manifest",
                "changes": [
                    {"path": "coordinator.model", "old_value": coord_model, "new_value": model},
                ],
            })

    # --- Agent .md frontmatter: model ---
    for agent_name in AGENT_NAMES:
        try:
            fm, _body = load_agent(agent_name)
        except FileNotFoundError:
            continue

        current = fm.get("model")
        if current:
            for model in VALID_MODELS:
                if model == current:
                    continue

                summary = f"{agent_name} agent model: {current} -> {model}"
                if summary in tried:
                    continue

                mutations.append({
                    "phase": 1,
                    "summary": summary,
                    "target_file": f"agents/{agent_name}.md",
                    "changes": [
                        {"path": "model", "old_value": current, "new_value": model},
                    ],
                })

        # model_overrides (e.g., worker has model_overrides.R2)
        overrides = fm.get("model_overrides")
        if isinstance(overrides, dict):
            for route_key, cur_model in overrides.items():
                if cur_model is None:
                    continue
                for model in VALID_MODELS:
                    if model == cur_model:
                        continue

                    summary = f"{agent_name} agent model_overrides.{route_key}: {cur_model} -> {model}"
                    if summary in tried:
                        continue

                    mutations.append({
                        "phase": 1,
                        "summary": summary,
                        "target_file": f"agents/{agent_name}.md",
                        "changes": [
                            {
                                "path": f"model_overrides.{route_key}",
                                "old_value": cur_model,
                                "new_value": model,
                            },
                        ],
                    })

    return mutations


# ---------------------------------------------------------------------------
# Phase 2: Effort Level Sweep
# ---------------------------------------------------------------------------

def generate_phase2_mutations(tried: set[str]) -> list[dict]:
    """Generate effort level mutations for all profiles in all routes.

    Also generates mutations for:
      - Global profile defaults (profiles.<name>.effort)
      - Coordinator effort (coordinator.effort)
      - Agent .md frontmatter (effort)
      - Settings effortLevel
    """
    mutations: list[dict] = []
    manifest = load_manifest()

    # --- Per-route profile_overrides ---
    for route_id in ROUTE_IDS:
        idx = _find_rule_index(manifest, route_id)
        if idx is None:
            continue

        for profile in _profiles_in_route(manifest, route_id):
            current = _get_current_effort(manifest, route_id, profile)
            if current is None:
                continue

            for effort in VALID_EFFORTS:
                if effort == current:
                    continue

                summary = f"{profile} effort {route_id}: {current} -> {effort}"
                if summary in tried:
                    continue

                path = f"rules[{idx}].profile_overrides.{profile}.effort"
                mutations.append({
                    "phase": 2,
                    "summary": summary,
                    "target_file": "manifest",
                    "changes": [
                        {"path": path, "old_value": current, "new_value": effort},
                    ],
                })

    # --- Global profile defaults ---
    for profile in list(manifest.get("profiles", {}).keys()):
        current = manifest["profiles"][profile].get("effort")
        if current is None:
            continue
        for effort in VALID_EFFORTS:
            if effort == current:
                continue

            summary = f"{profile} effort global: {current} -> {effort}"
            if summary in tried:
                continue

            path = f"profiles.{profile}.effort"
            mutations.append({
                "phase": 2,
                "summary": summary,
                "target_file": "manifest",
                "changes": [
                    {"path": path, "old_value": current, "new_value": effort},
                ],
            })

    # --- Coordinator effort ---
    coord_effort = manifest.get("coordinator", {}).get("effort")
    if coord_effort:
        for effort in VALID_EFFORTS:
            if effort == coord_effort:
                continue

            summary = f"coordinator effort global: {coord_effort} -> {effort}"
            if summary in tried:
                continue

            mutations.append({
                "phase": 2,
                "summary": summary,
                "target_file": "manifest",
                "changes": [
                    {"path": "coordinator.effort", "old_value": coord_effort, "new_value": effort},
                ],
            })

    # --- Agent .md frontmatter: effort ---
    for agent_name in AGENT_NAMES:
        try:
            fm, _body = load_agent(agent_name)
        except FileNotFoundError:
            continue

        current = fm.get("effort")
        if current:
            for effort in VALID_EFFORTS:
                if effort == current:
                    continue

                summary = f"{agent_name} agent effort: {current} -> {effort}"
                if summary in tried:
                    continue

                mutations.append({
                    "phase": 2,
                    "summary": summary,
                    "target_file": f"agents/{agent_name}.md",
                    "changes": [
                        {"path": "effort", "old_value": current, "new_value": effort},
                    ],
                })

    # --- Settings effortLevel ---
    try:
        settings = load_settings()
    except (FileNotFoundError, json.JSONDecodeError):
        settings = {}

    current_effort = settings.get("effortLevel")
    if current_effort:
        for effort in VALID_EFFORTS:
            if effort == current_effort:
                continue

            summary = f"settings effortLevel: {current_effort} -> {effort}"
            if summary in tried:
                continue

            mutations.append({
                "phase": 2,
                "summary": summary,
                "target_file": "settings",
                "changes": [
                    {"path": "effortLevel", "old_value": current_effort, "new_value": effort},
                ],
            })

    return mutations


# ---------------------------------------------------------------------------
# Phase 3: Swarm Topology Sweep
# ---------------------------------------------------------------------------

# Dispatch order candidates: meaningful permutations of the 4 agent roles
# that participate in frontier dispatch.
_DISPATCH_AGENTS: list[str] = ["validator", "explorer", "worker", "reviewer"]


def _clamp(value: int, bounds: tuple[int, int]) -> int:
    return max(bounds[0], min(bounds[1], value))


def generate_phase3_mutations(
    tried: set[str],
    allowed_families: Optional[set[str]] = None,
) -> list[dict]:
    """Generate swarm topology mutations for R3 and R4.

    Sweeps:
      - lane_caps: current +/- 1 for each agent lane
      - route_swarm_cap: current +/- 1
      - max_parallel_packets: current +/- 1
      - frontier_dispatch_order: alternative permutations
    """
    mutations: list[dict] = []
    manifest = load_manifest()

    for route_id in ("R3", "R4"):
        idx = _find_rule_index(manifest, route_id)
        if idx is None:
            continue

        rule = manifest["rules"][idx]

        # --- Lane caps ---
        lane_caps = rule.get("lane_caps", {})
        for agent, current_cap in lane_caps.items():
            if not isinstance(current_cap, int):
                continue
            for delta in (-1, 1):
                new_cap = _clamp(current_cap + delta, LANE_CAP_RANGE)
                if new_cap == current_cap:
                    continue

                summary = f"{agent} lane_cap {route_id}: {current_cap} -> {new_cap}"
                if summary in tried:
                    continue

                path = f"rules[{idx}].lane_caps.{agent}"
                mutations.append({
                    "phase": 3,
                    "family": "lane_cap",
                    "summary": summary,
                    "target_file": "manifest",
                    "changes": [
                        {"path": path, "old_value": current_cap, "new_value": new_cap},
                    ],
                })

        # --- Swarm cap ---
        swarm_cap = rule.get("route_swarm_cap")
        if isinstance(swarm_cap, int):
            for delta in (-1, 1):
                new_cap = _clamp(swarm_cap + delta, SWARM_CAP_RANGE)
                if new_cap == swarm_cap:
                    continue

                summary = f"route_swarm_cap {route_id}: {swarm_cap} -> {new_cap}"
                if summary in tried:
                    continue

                path = f"rules[{idx}].route_swarm_cap"
                mutations.append({
                    "phase": 3,
                    "family": "route_swarm_cap",
                    "summary": summary,
                    "target_file": "manifest",
                    "changes": [
                        {"path": path, "old_value": swarm_cap, "new_value": new_cap},
                    ],
                })

        # --- Max parallel packets ---
        max_parallel = rule.get("max_parallel_packets")
        if isinstance(max_parallel, int):
            for delta in (-1, 1):
                new_val = _clamp(max_parallel + delta, MAX_PARALLEL_RANGE)
                if new_val == max_parallel:
                    continue

                summary = f"max_parallel_packets {route_id}: {max_parallel} -> {new_val}"
                if summary in tried:
                    continue

                path = f"rules[{idx}].max_parallel_packets"
                mutations.append({
                    "phase": 3,
                    "family": "max_parallel_packets",
                    "summary": summary,
                    "target_file": "manifest",
                    "changes": [
                        {"path": path, "old_value": max_parallel, "new_value": new_val},
                    ],
                })

        # --- Frontier dispatch order ---
        current_order = rule.get("frontier_dispatch_order", [])
        if len(current_order) >= 2:
            # Generate a bounded set of meaningful permutations rather than
            # all 24.  We try: validator-first, explorer-first, worker-first,
            # and reviewer-first variants (the remaining slots sorted to give
            # deterministic candidates), plus the full reverse.
            candidates: list[list[str]] = []

            # "X first" variants: promote one agent to position 0, keep the
            # rest in their current relative order.
            for lead in _DISPATCH_AGENTS:
                if lead not in current_order:
                    continue
                rest = [a for a in current_order if a != lead]
                candidate = [lead] + rest
                if candidate != current_order and candidate not in candidates:
                    candidates.append(candidate)

            # Full reverse.
            rev = list(reversed(current_order))
            if rev != current_order and rev not in candidates:
                candidates.append(rev)

            # Bounded permutation sample: up to 8 additional distinct orders.
            perm_count = 0
            for perm in permutations(current_order):
                perm_list = list(perm)
                if perm_list == current_order:
                    continue
                if perm_list not in candidates:
                    candidates.append(perm_list)
                    perm_count += 1
                    if perm_count >= 8:
                        break

            for candidate in candidates:
                summary = f"dispatch_order {route_id}: {current_order} -> {candidate}"
                if summary in tried:
                    continue

                path = f"rules[{idx}].frontier_dispatch_order"
                mutations.append({
                    "phase": 3,
                    "family": "dispatch_order",
                    "summary": summary,
                    "target_file": "manifest",
                    "changes": [
                        {"path": path, "old_value": current_order, "new_value": candidate},
                    ],
                })

    if not allowed_families:
        return mutations
    return [
        mutation
        for mutation in mutations
        if mutation.get("family") in allowed_families
    ]


# ---------------------------------------------------------------------------
# Dispatch helpers
# ---------------------------------------------------------------------------

_PHASE_GENERATORS = {
    1: generate_phase1_mutations,
    2: generate_phase2_mutations,
    3: generate_phase3_mutations,
}


def get_next_mutation(
    phase: int,
    tried: set[str],
    phase3_allowed_families: Optional[set[str]] = None,
) -> Optional[dict]:
    """Get the next untried mutation for *phase*.

    Returns ``None`` if all mutations for the phase have been tried.
    """
    generator = _PHASE_GENERATORS.get(phase)
    if generator is None:
        return None

    if phase == 3:
        candidates = generator(tried, allowed_families=phase3_allowed_families)
    else:
        candidates = generator(tried)
    if not candidates:
        return None

    return candidates[0]


def get_mutation_count(
    phase: int,
    phase3_allowed_families: Optional[set[str]] = None,
) -> int:
    """Estimate total mutations possible for *phase* (with an empty tried set)."""
    generator = _PHASE_GENERATORS.get(phase)
    if generator is None:
        return 0

    if phase == 3:
        return len(generator(set(), allowed_families=phase3_allowed_families))
    return len(generator(set()))
