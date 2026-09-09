"""Benchmark evaluation harness for the autoconfig system.

Runs benchmarks defined as JSON files in the benchmarks/ directory against
``claude -p`` and evaluates acceptance checks on the output and workspace.

Each benchmark contains one or more variants.  The harness selects a random
variant per run, prepares an isolated workspace (if needed), executes Claude
in non-interactive mode, and scores the result against the variant's
acceptance checks.

Usage (programmatic)::

    from eval_harness import run_full_suite
    results = run_full_suite()                   # all benchmarks
    results = run_full_suite(["r1_factual"])      # specific benchmarks

The module is also runnable as a script::

    python eval_harness.py                       # all benchmarks
    python eval_harness.py r1_factual r2_small_impl
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent))

from config_mutator import (
    AGENT_NAMES,
    _get_current_effort,
    _get_current_model,
    _parse_yaml_frontmatter,
    load_agent,
    load_manifest,
    load_settings,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BENCHMARKS_DIR: Path = Path.home() / ".claude" / "skills" / "autoconfig" / "benchmarks"
TEMPLATES_DIR: Path = BENCHMARKS_DIR / "templates"
SOURCE_CLAUDE_HOME: Path = Path(
    os.environ.get("CLAUDE_HOME", str(Path.home() / ".claude"))
)
ROUTE_PRIMARY_PROFILE: dict[str, str] = {
    "R1": "coordinator",
    "R2": "worker",
    "R3": "worker",
    "R4": "reviewer",
}


def _same_home(path: Optional[Path], other: Path) -> bool:
    if path is None:
        return False
    try:
        return path.expanduser().resolve() == other.expanduser().resolve()
    except OSError:
        return path.expanduser() == other.expanduser()


def _load_settings_from_home(source_home: Optional[Path]) -> dict:
    if source_home is None or _same_home(source_home, SOURCE_CLAUDE_HOME):
        return load_settings()
    path = source_home / "settings.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_manifest_from_home(source_home: Optional[Path]) -> dict:
    if source_home is None or _same_home(source_home, SOURCE_CLAUDE_HOME):
        return load_manifest()
    path = source_home / "state" / "route_manifest.json"
    if not path.is_file():
        return {"rules": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_agent_from_home(source_home: Optional[Path], agent_name: str) -> tuple[dict, str]:
    if source_home is None or _same_home(source_home, SOURCE_CLAUDE_HOME):
        return load_agent(agent_name)
    path = source_home / "agents" / f"{agent_name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Agent file not found: {path}")
    text = path.read_text(encoding="utf-8")
    return _parse_yaml_frontmatter(text)


def _build_prompt(benchmark: dict, variant: dict) -> str:
    """Build the final prompt sent to Claude for a benchmark variant."""
    prompt = variant.get("prompt", "")
    if benchmark.get("route") != "R1":
        return prompt
    return (
        "Use the local runtime snapshot in the current working directory as "
        "the source of truth. Inspect `state/route_manifest.json` and "
        "`agents/*.md` before answering. Base the answer only on those local "
        "files, not on prior model knowledge. Return valid JSON only.\n\n"
        f"{prompt}"
    )


def _parse_cli_payload(raw_stdout: str) -> Optional[dict[str, Any]]:
    """Parse Claude's JSON envelope from stdout when present."""
    if not raw_stdout:
        return None

    candidates = [raw_stdout]
    candidates.extend(
        line for line in reversed(raw_stdout.splitlines()) if line.strip()
    )

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue

        if isinstance(parsed, dict):
            return parsed

    return None


def _extract_output_text(raw_stdout: str) -> str:
    """Extract assistant text from Claude JSON output."""
    payload = _parse_cli_payload(raw_stdout)
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, str):
            return result

        content = payload.get("content")
        if isinstance(content, str):
            return content

        message = payload.get("message")
        if isinstance(message, dict):
            nested_content = message.get("content")
            if isinstance(nested_content, str):
                return nested_content

    return raw_stdout


def _classify_terminal_state(
    payload: Optional[dict[str, Any]],
    *,
    timed_out: bool,
    error: Optional[str],
    exit_code: Optional[int],
    output_text: str,
) -> tuple[str, bool]:
    """Return a terminal-state label and whether the run completed cleanly."""
    if timed_out:
        return "timeout", False
    if error:
        return "process_error", False
    if exit_code not in (None, 0):
        return "nonzero_exit", False

    if isinstance(payload, dict):
        subtype = payload.get("subtype")
        if isinstance(subtype, str) and subtype:
            if subtype.startswith("error_"):
                return subtype, False
            if subtype in {"success", "completed"}:
                return "completed", True
            return subtype, False

        if payload.get("result") is not None or payload.get("content") is not None:
            return "completed", True

    if output_text.strip():
        return "completed", True
    return "missing_output", False


def _resolve_speed_baseline_seconds(
    benchmark: dict,
    variant_id: Optional[str],
) -> Optional[float]:
    """Resolve the configured speed baseline for a benchmark variant."""
    raw = benchmark.get("speed_baselines_seconds")
    if not isinstance(raw, dict):
        return None

    variant_value: Optional[object] = None
    variants = raw.get("variants")
    if isinstance(variants, dict) and variant_id is not None:
        variant_value = variants.get(variant_id)

    candidate = variant_value if variant_value is not None else raw.get("default")
    if candidate is None:
        return None

    try:
        return float(candidate)
    except (TypeError, ValueError):
        return None


def _parse_output_json(output: str) -> tuple[Optional[Any], Optional[str]]:
    """Parse candidate output as JSON, tolerating fenced code blocks."""
    if not output or not output.strip():
        return None, "output is empty"

    stripped = output.strip()
    candidates = [stripped]

    fence_match = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", stripped, re.DOTALL)
    if fence_match:
        candidates.insert(0, fence_match.group(1).strip())

    decoder = json.JSONDecoder()
    for idx, char in enumerate(stripped):
        if char not in "{[":
            continue
        try:
            parsed, end_idx = decoder.raw_decode(stripped[idx:])
        except json.JSONDecodeError:
            continue
        trailing = stripped[idx + end_idx :].strip()
        if trailing and not trailing.startswith("```"):
            candidates.append(stripped[idx : idx + end_idx].strip())
        else:
            candidates.insert(0, stripped[idx : idx + end_idx].strip())

    for candidate in candidates:
        try:
            return json.loads(candidate), None
        except json.JSONDecodeError:
            continue
    return None, "output is not valid JSON"


def _validate_json_schema(value: Any, schema: dict, path: str = "$") -> list[str]:
    """Validate a small JSON-schema subset and return error strings."""
    errors: list[str] = []
    expected_type = schema.get("type")

    if expected_type == "object":
        if not isinstance(value, dict):
            return [f"{path} must be an object"]
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key} is required")
        properties = schema.get("properties", {})
        for key, prop_schema in properties.items():
            if key in value:
                errors.extend(
                    _validate_json_schema(value[key], prop_schema, f"{path}.{key}")
                )
        if schema.get("additionalProperties") is False:
            allowed = set(properties.keys())
            for key in value:
                if key not in allowed:
                    errors.append(f"{path}.{key} is not allowed")
        return errors

    if expected_type == "array":
        if not isinstance(value, list):
            return [f"{path} must be an array"]
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"{path} must contain at least {min_items} items")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            errors.append(f"{path} must contain at most {max_items} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(value):
                errors.extend(
                    _validate_json_schema(item, item_schema, f"{path}[{idx}]")
                )
        return errors

    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
    }
    if expected_type in type_map:
        if not isinstance(value, type_map[expected_type]) or (
            expected_type == "integer" and isinstance(value, bool)
        ):
            return [f"{path} must be of type {expected_type}"]
        if expected_type == "string":
            min_length = schema.get("minLength")
            if isinstance(min_length, int) and len(value) < min_length:
                errors.append(f"{path} must be at least {min_length} chars")
            max_length = schema.get("maxLength")
            if isinstance(max_length, int) and len(value) > max_length:
                errors.append(f"{path} must be at most {max_length} chars")
        if expected_type in {"integer", "number"}:
            minimum = schema.get("minimum")
            if minimum is not None and value < minimum:
                errors.append(f"{path} must be >= {minimum}")
            maximum = schema.get("maximum")
            if maximum is not None and value > maximum:
                errors.append(f"{path} must be <= {maximum}")

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"{path} must be one of {enum!r}")
    return errors


def _canonicalize_truth(value: Any) -> Any:
    """Normalize nested JSON for stable equality comparisons."""
    if isinstance(value, dict):
        return {
            key: _canonicalize_truth(value[key])
            for key in sorted(value.keys())
        }
    if isinstance(value, list):
        normalized = [_canonicalize_truth(item) for item in value]
        if all(isinstance(item, str) for item in normalized):
            return sorted(normalized)
        if all(isinstance(item, dict) and "name" in item for item in normalized):
            return sorted(normalized, key=lambda item: str(item["name"]))
        return normalized
    return value


def _load_runtime_manifest(root: Path) -> dict:
    manifest_path = root / "state" / "route_manifest.json"
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _load_agent_sandbox_map(root: Path) -> dict[str, str]:
    sandboxes: dict[str, str] = {}
    agents_dir = root / "agents"
    for path in sorted(agents_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^sandbox:\s*(.+)$", text, re.MULTILINE)
        if match:
            sandboxes[path.stem] = match.group(1).strip()
    return sandboxes


def _extract_truth(truth_extractor: dict, context_root: Path) -> Any:
    """Extract canonical benchmark truth from the isolated runtime snapshot."""
    extractor_type = truth_extractor.get("type")
    manifest = _load_runtime_manifest(context_root)
    rules = manifest.get("rules", [])

    if extractor_type == "agent_roles":
        sandboxes = _load_agent_sandbox_map(context_root)
        roles = [
            {"name": role, "sandbox": sandboxes.get(role, "unknown")}
            for role in sorted(sandboxes.keys())
        ]
        return {"roles": roles}

    if extractor_type == "execution_shapes":
        mapping: dict[str, list[str]] = {}
        for rule in rules:
            shape = rule.get("execution_shape")
            route_id = rule.get("id")
            if not shape or not route_id:
                continue
            mapping.setdefault(shape, []).append(route_id)
        return {
            "execution_shapes": {
                shape: sorted(route_ids)
                for shape, route_ids in sorted(mapping.items())
            }
        }

    if extractor_type == "route_pair_summary":
        route_ids = truth_extractor.get("route_ids", [])
        fields = truth_extractor.get("fields", [])
        result: dict[str, Any] = {}
        by_id = {
            rule.get("id"): rule for rule in rules if isinstance(rule, dict)
        }
        for route_id in route_ids:
            rule = by_id.get(route_id, {})
            result[route_id] = {field: rule.get(field) for field in fields}
        return result

    raise ValueError(f"Unknown truth extractor type: {extractor_type!r}")


def _build_semantic_judge_prompt(
    benchmark: dict,
    variant: dict,
    candidate_output: str,
) -> str:
    """Build the rubric-judge prompt for semantic evaluation."""
    rubric = benchmark.get("judge_rubric", {})
    route = benchmark.get("route", "unknown")
    profile = benchmark.get("judge_profile", "semantic_review")
    prompt = variant.get("prompt", "")
    candidate_output = candidate_output[:12000]
    rubric_json = json.dumps(rubric, indent=2, sort_keys=True)

    return (
        "You are grading an autoconfig benchmark result.\n"
        "Inspect the local workspace files in the current working directory as needed.\n"
        "Use the rubric below and return JSON only with this schema:\n"
        "{\"score\": <0-100>, \"summary\": \"...\", \"strengths\": [\"...\"], "
        "\"issues\": [{\"severity\": \"high|medium|low\", \"message\": \"...\"}], "
        "\"missed_expectations\": [\"...\"], \"verdict\": \"pass|warning|fail\"}\n\n"
        f"Route: {route}\n"
        f"Judge profile: {profile}\n"
        f"Original benchmark prompt:\n{prompt}\n\n"
        f"Rubric:\n{rubric_json}\n\n"
        "Candidate response/output:\n"
        f"{candidate_output}\n"
    )


def _run_semantic_judge(
    benchmark: dict,
    variant: dict,
    candidate_output: str,
    workspace: Optional[Path],
    benchmark_home: Path,
) -> dict:
    """Run a rubric-based semantic judge for benchmarks that request it."""
    prompt = _build_semantic_judge_prompt(benchmark, variant, candidate_output)
    judge_settings = benchmark_home / "benchmark-settings.json"
    judge_cmd = [
        _CLAUDE_CMD,
        "-p",
        prompt,
        "--output-format", "json",
        "--max-turns", "4",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        "--setting-sources", "local",
        "--settings", str(judge_settings),
        "--mcp-config", json.dumps({"mcpServers": {}}),
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--agent", "reviewer",
        "--model", "claude-sonnet-4-6",
        "--effort", "high",
    ]

    env = {
        **os.environ,
        "AUTOCONFIG_BENCHMARK": "1",
        "CLAUDE_HOME": str(benchmark_home),
    }
    cwd = workspace if workspace is not None else benchmark_home
    try:
        proc = subprocess.run(
            judge_cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=90,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return {
            "score": 0.0,
            "summary": "semantic judge timed out",
            "issues": [{"severity": "high", "message": "semantic judge timed out"}],
            "missed_expectations": ["semantic judge timed out"],
            "verdict": "fail",
            "error": "judge timeout",
        }
    except Exception as exc:
        return {
            "score": 0.0,
            "summary": f"semantic judge failed: {exc}",
            "issues": [{"severity": "high", "message": str(exc)}],
            "missed_expectations": ["semantic judge failed"],
            "verdict": "fail",
            "error": str(exc),
        }

    output_text = _extract_output_text(proc.stdout or "")
    parsed_json, parse_error = _parse_output_json(output_text)
    if proc.returncode != 0 or parse_error or not isinstance(parsed_json, dict):
        stderr = (proc.stderr or "").strip()
        return {
            "score": 0.0,
            "summary": "semantic judge returned invalid output",
            "issues": [{"severity": "high", "message": stderr or parse_error or "invalid judge output"}],
            "missed_expectations": ["semantic judge output was invalid"],
            "verdict": "fail",
            "error": stderr or parse_error or "invalid judge output",
        }

    score = parsed_json.get("score", 0.0)
    try:
        parsed_json["score"] = max(0.0, min(100.0, float(score)))
    except (TypeError, ValueError):
        parsed_json["score"] = 0.0
    return parsed_json

def _resolve_claude_cmd() -> str:
    """Resolve the Claude CLI path for daemon-launched benchmark runs."""
    candidates = [
        os.environ.get("CLAUDE_BIN"),
        shutil.which("claude"),
        str(Path.home() / ".local" / "bin" / "claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return "claude"


_CLAUDE_CMD: str = _resolve_claude_cmd()

log = logging.getLogger("autoconfig.eval_harness")


def _json_copy(value):
    """Deep-copy JSON-compatible data without importing copy."""
    return json.loads(json.dumps(value))


def _build_benchmark_settings(
    benchmark_home: Path,
    *,
    source_home: Optional[Path] = None,
) -> dict:
    """Construct isolated settings for benchmark subprocesses.

    Keep the fast local governance hooks that classify prompts and validate
    the runtime, but strip plugins, MCP servers, and Stop hooks that spawn
    extra model calls and hang print-mode benchmarks.
    """
    source = _load_settings_from_home(source_home)
    settings: dict = {}

    env = dict(source.get("env", {}))
    env["CLAUDE_HOME"] = str(benchmark_home)
    settings["env"] = env

    if "permissions" in source:
        settings["permissions"] = _json_copy(source["permissions"])
    if "effortLevel" in source:
        settings["effortLevel"] = source["effortLevel"]
    if "skipDangerousModePermissionPrompt" in source:
        settings["skipDangerousModePermissionPrompt"] = source[
            "skipDangerousModePermissionPrompt"
        ]

    hooks = source.get("hooks", {})
    benchmark_hooks: dict = {}
    for event_name in ("SessionStart", "UserPromptSubmit"):
        if event_name in hooks:
            benchmark_hooks[event_name] = _json_copy(hooks[event_name])
    if benchmark_hooks:
        settings["hooks"] = benchmark_hooks

    settings["enabledPlugins"] = {}
    settings["mcpServers"] = {}
    return settings


def _materialize_benchmark_home(
    *,
    source_home: Optional[Path] = None,
) -> tuple[Path, Path]:
    """Copy the mutable config surface into an isolated Claude home."""
    source_root = source_home or SOURCE_CLAUDE_HOME
    benchmark_home = Path(tempfile.mkdtemp(prefix="autoconfig_claude_home_"))
    (benchmark_home / "state").mkdir(parents=True, exist_ok=True)
    (benchmark_home / "state" / "locks").mkdir(parents=True, exist_ok=True)
    (benchmark_home / "agents").mkdir(parents=True, exist_ok=True)

    route_manifest = source_root / "state" / "route_manifest.json"
    if route_manifest.is_file():
        shutil.copy2(route_manifest, benchmark_home / "state" / "route_manifest.json")

    for agent_name in AGENT_NAMES:
        src = source_root / "agents" / f"{agent_name}.md"
        if src.is_file():
            shutil.copy2(src, benchmark_home / "agents" / f"{agent_name}.md")

    settings_path = benchmark_home / "benchmark-settings.json"
    settings_path.write_text(
        json.dumps(
            _build_benchmark_settings(benchmark_home, source_home=source_root),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return benchmark_home, settings_path


def _resolve_profile_runtime(
    route_id: str,
    *,
    source_home: Optional[Path] = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve the benchmark's primary profile, model, and effort.

    The benchmark harness runs one Claude CLI subprocess per route benchmark.
    To make route and agent mutations observable under isolated settings, pin
    the effective model/effort explicitly for the route's primary profile.
    """
    profile = ROUTE_PRIMARY_PROFILE.get(route_id, "coordinator")
    manifest = _load_manifest_from_home(source_home)
    model = _get_current_model(manifest, route_id, profile)
    effort = _get_current_effort(manifest, route_id, profile)
    agent_name: Optional[str] = profile if profile in AGENT_NAMES else None

    if agent_name is not None:
        try:
            frontmatter, _body = _load_agent_from_home(source_home, agent_name)
        except FileNotFoundError:
            frontmatter = {}

        model_overrides = frontmatter.get("model_overrides")
        if isinstance(model_overrides, dict) and model_overrides.get(route_id):
            model = model_overrides[route_id]
        elif frontmatter.get("model"):
            model = frontmatter["model"]

        effort_overrides = frontmatter.get("effort_overrides")
        if isinstance(effort_overrides, dict) and effort_overrides.get(route_id):
            effort = effort_overrides[route_id]
        elif frontmatter.get("effort"):
            effort = frontmatter["effort"]

    return agent_name, model, effort


# ---------------------------------------------------------------------------
# Benchmark loading
# ---------------------------------------------------------------------------


def load_benchmark(benchmark_id: str) -> dict:
    """Load a benchmark JSON file by its ID.

    Parameters
    ----------
    benchmark_id : str
        The benchmark identifier, e.g. ``"r1_factual"``.  Must correspond to
        a ``<benchmark_id>.json`` file inside :data:`BENCHMARKS_DIR`.

    Returns
    -------
    dict
        The parsed benchmark definition.

    Raises
    ------
    FileNotFoundError
        If no matching JSON file exists.
    json.JSONDecodeError
        If the file contains invalid JSON.
    """
    path = BENCHMARKS_DIR / f"{benchmark_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"Benchmark file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_all_benchmarks() -> list[dict]:
    """Load every benchmark JSON from :data:`BENCHMARKS_DIR`.

    Returns
    -------
    list[dict]
        Benchmark definitions sorted by ``id``.
    """
    benchmarks: list[dict] = []
    if not BENCHMARKS_DIR.is_dir():
        return benchmarks
    for path in sorted(BENCHMARKS_DIR.glob("*.json")):
        try:
            benchmarks.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Skipping malformed benchmark %s: %s", path.name, exc)
    return benchmarks


# ---------------------------------------------------------------------------
# Variant selection
# ---------------------------------------------------------------------------


def select_variant(benchmark: dict, variant_id: Optional[str] = None) -> dict:
    """Randomly select one variant from the benchmark.

    Parameters
    ----------
    benchmark : dict
        A benchmark definition with a ``"variants"`` list.

    Returns
    -------
    dict
        The selected variant.

    Raises
    ------
    ValueError
        If the benchmark has no variants.
    """
    variants = benchmark.get("variants")
    if not variants:
        raise ValueError(
            f"Benchmark {benchmark.get('id', '<unknown>')} has no variants"
        )
    if variant_id is not None:
        for variant in variants:
            if variant.get("id") == variant_id:
                return variant
        raise ValueError(
            f"Benchmark {benchmark.get('id', '<unknown>')} has no variant {variant_id!r}"
        )
    return random.choice(variants)


# ---------------------------------------------------------------------------
# Workspace management
# ---------------------------------------------------------------------------


def prepare_workspace(benchmark: dict) -> Optional[Path]:
    """Create an isolated temporary workspace for the benchmark.

    If *workspace_template* is ``None`` (R1 benchmarks), returns ``None``.

    For R3 workspaces, ``npm install`` is executed after copying the template
    so that dependencies are available during the benchmark run.

    Parameters
    ----------
    benchmark : dict
        The benchmark definition.

    Returns
    -------
    Path | None
        Path to the temporary workspace, or ``None``.
    """
    template_name = benchmark.get("workspace_template")
    if template_name is None:
        return None

    template_dir = TEMPLATES_DIR / template_name
    if not template_dir.is_dir():
        raise FileNotFoundError(
            f"Workspace template not found: {template_dir}"
        )

    workspace = Path(tempfile.mkdtemp(prefix=f"autoconfig_{benchmark['id']}_"))
    shutil.copytree(template_dir, workspace, dirs_exist_ok=True)

    # R3 workspaces ship a package.json — run npm install so that
    # TypeScript compilation and vitest are available.
    route = benchmark.get("route", "")
    if route == "R3" and (workspace / "package.json").is_file():
        try:
            subprocess.run(
                ["npm", "install"],
                cwd=workspace,
                capture_output=True,
                timeout=120,
                check=False,
            )
        except Exception as exc:
            log.warning(
                "npm install failed for %s: %s", benchmark["id"], exc
            )

    return workspace


def cleanup_workspace(workspace: Path) -> None:
    """Remove a temporary workspace directory.

    Parameters
    ----------
    workspace : Path
        The workspace directory to remove.  No-op if the path does not exist
        or is not inside the system temp directory.
    """
    if workspace is None:
        return
    try:
        # Safety: only remove directories that live under the system tmpdir.
        tmp_root = Path(tempfile.gettempdir()).resolve()
        resolved = workspace.resolve()
        if resolved == tmp_root or not str(resolved).startswith(str(tmp_root)):
            log.warning(
                "Refusing to remove workspace outside tmpdir: %s", workspace
            )
            return
        shutil.rmtree(workspace, ignore_errors=True)
    except Exception as exc:
        log.warning("Failed to clean up workspace %s: %s", workspace, exc)


def cleanup_temp_dir(path: Optional[Path]) -> None:
    """Remove a temporary directory created for benchmark isolation."""
    if path is None:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception as exc:
        log.warning("Failed to clean up temp dir %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Benchmark execution
# ---------------------------------------------------------------------------


def run_benchmark(
    benchmark: dict,
    variant: dict,
    workspace: Optional[Path] = None,
    *,
    benchmark_source_home: Optional[Path] = None,
) -> dict:
    """Execute ``claude -p`` for a single benchmark variant.

    Parameters
    ----------
    benchmark : dict
        The parent benchmark definition.
    variant : dict
        The selected variant containing the prompt.
    workspace : Path | None
        Working directory for the subprocess, or ``None`` for R1 runs.

    Returns
    -------
    dict
        Result dict with keys: ``benchmark_id``, ``variant_id``, ``output``,
        ``wall_time_seconds``, ``exit_code``, ``timed_out``, ``error``.
    """
    benchmark_id = benchmark.get("id", "unknown")
    variant_id = variant.get("id", "unknown")
    timeout_seconds = variant.get(
        "timeout_seconds",
        benchmark.get("timeout_seconds", 120),
    )
    max_turns = variant.get("max_turns", benchmark.get("max_turns", 10))
    prompt = _build_prompt(benchmark, variant)

    # Add startup buffer to timeout — MCP-free startup still takes ~10-15s
    effective_timeout = timeout_seconds + 30

    benchmark_home, benchmark_settings = _materialize_benchmark_home(
        source_home=benchmark_source_home
    )
    agent_name, model_name, effort_level = _resolve_profile_runtime(
        benchmark.get("route", ""),
        source_home=benchmark_source_home,
    )

    cmd = [
        _CLAUDE_CMD,
        "-p",
        prompt,
        "--output-format", "json",
        "--max-turns", str(max_turns),
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        "--setting-sources", "local",
        "--settings", str(benchmark_settings),
        # Skip MCP servers for faster startup and isolation
        "--mcp-config", json.dumps({"mcpServers": {}}),
        "--strict-mcp-config",
        "--disable-slash-commands",
    ]

    if agent_name:
        cmd.extend(["--agent", agent_name])
    if model_name:
        cmd.extend(["--model", str(model_name)])
    if effort_level:
        cmd.extend(["--effort", str(effort_level)])

    env = {
        **os.environ,
        "AUTOCONFIG_BENCHMARK": "1",
        "CLAUDE_HOME": str(benchmark_home),
    }
    path_entries = [str(Path.home() / ".local" / "bin")]
    current_path = env.get("PATH", "")
    if current_path:
        path_entries.append(current_path)
    env["PATH"] = os.pathsep.join(path_entries)
    run_cwd = workspace if workspace is not None else benchmark_home

    result: dict = {
        "benchmark_id": benchmark_id,
        "variant_id": variant_id,
        "output": "",
        "wall_time_seconds": 0.0,
        "exit_code": -1,
        "timed_out": False,
        "error": None,
        "agent": agent_name,
        "model": model_name,
        "effort": effort_level,
        "speed_baseline_seconds": _resolve_speed_baseline_seconds(
            benchmark, variant_id
        ),
        "retryable_benchmark_failure": False,
        "benchmark_retry_count": 0,
        "retry_count": 0,
        "benchmark_home": benchmark_home,
        "benchmark_source_home": str((benchmark_source_home or SOURCE_CLAUDE_HOME).expanduser()),
    }

    start = time.monotonic()
    raw_stdout = ""
    try:
        proc = subprocess.run(
            cmd,
            cwd=run_cwd,
            env=env,
            capture_output=True,
            timeout=effective_timeout,
            text=True,
        )
        result["wall_time_seconds"] = round(time.monotonic() - start, 2)
        result["exit_code"] = proc.returncode

        raw_stdout = proc.stdout or ""
        result["output"] = _extract_output_text(raw_stdout)

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            snippet = stderr or stdout
            if snippet:
                result["error"] = snippet[:2000]

    except subprocess.TimeoutExpired:
        result["wall_time_seconds"] = round(time.monotonic() - start, 2)
        result["timed_out"] = True
        result["error"] = f"Timed out after {timeout_seconds}s"
    except FileNotFoundError:
        result["wall_time_seconds"] = round(time.monotonic() - start, 2)
        result["error"] = (
            f"'{_CLAUDE_CMD}' not found — is Claude CLI installed and executable?"
        )
    except Exception as exc:
        result["wall_time_seconds"] = round(time.monotonic() - start, 2)
        result["error"] = f"Unexpected error: {exc}"

    payload = _parse_cli_payload(raw_stdout)
    terminal_state, completed_cleanly = _classify_terminal_state(
        payload,
        timed_out=bool(result["timed_out"]),
        error=result.get("error"),
        exit_code=result.get("exit_code"),
        output_text=result.get("output", ""),
    )
    if not benchmark.get("allow_incomplete_terminal_state", False) and not completed_cleanly:
        result["completed_cleanly"] = False
    else:
        result["completed_cleanly"] = True
    result["terminal_state"] = terminal_state

    return result


# ---------------------------------------------------------------------------
# Acceptance checking
# ---------------------------------------------------------------------------


def check_acceptance(
    benchmark: dict,
    variant: dict,
    checks: list[dict],
    output: str,
    workspace: Optional[Path] = None,
    context_root: Optional[Path] = None,
) -> dict:
    """Evaluate acceptance checks against the benchmark output and workspace.

    Parameters
    ----------
    checks : list[dict]
        Acceptance check definitions from the variant.
    output : str
        The text output from the benchmark run.
    workspace : Path | None
        The workspace directory (for file-based checks).

    Returns
    -------
    dict
        With keys ``passed``, ``failed``, ``total``, ``pass_rate``,
        ``details`` (list of per-check results).
    """
    details: list[dict] = []
    parsed_output_json, parsed_output_error = _parse_output_json(output)

    for check in checks:
        try:
            passed, message = _evaluate_check(
                benchmark=benchmark,
                variant=variant,
                check=check,
                output=output,
                workspace=workspace,
                context_root=context_root,
                parsed_output_json=parsed_output_json,
                parsed_output_error=parsed_output_error,
            )
        except Exception as exc:
            passed = False
            message = f"Check raised exception: {exc}"
        details.append({
            "check": check,
            "passed": passed,
            "message": message,
        })

    passed_count = sum(1 for d in details if d["passed"])
    total = len(details)
    return {
        "passed": passed_count,
        "failed": total - passed_count,
        "total": total,
        "pass_rate": round(passed_count / total, 4) if total > 0 else 0.0,
        "details": details,
    }


def _evaluate_check(
    benchmark: dict,
    variant: dict,
    check: dict,
    output: str,
    workspace: Optional[Path],
    context_root: Optional[Path],
    parsed_output_json: Optional[Any],
    parsed_output_error: Optional[str],
) -> tuple[bool, str]:
    """Dispatch a single acceptance check.  Returns ``(passed, message)``."""
    check_type = check.get("type", "")

    if check_type == "file_exists":
        return _check_file_exists(check, workspace)

    if check_type == "file_contains":
        return _check_file_contains(check, workspace)

    if check_type == "function_exists":
        return _check_function_exists(check, workspace)

    if check_type == "command_passes":
        return _check_command_passes(check, workspace)

    if check_type in ("contains", "output_contains"):
        return _check_contains(check, output)

    if check_type in ("contains_any", "output_contains_any"):
        return _check_contains_any(check, output)

    if check_type == "word_count_range":
        return _check_word_count_range(check, output)

    if check_type == "output_json_schema":
        return _check_output_json_schema(
            benchmark,
            variant,
            parsed_output_json,
            parsed_output_error,
        )

    if check_type == "truth_match":
        return _check_truth_match(
            variant,
            context_root,
            parsed_output_json,
            parsed_output_error,
        )

    return False, f"Unknown check type: {check_type!r}"


# -- File-based checks -----------------------------------------------------


def _check_file_exists(
    check: dict, workspace: Optional[Path]
) -> tuple[bool, str]:
    rel = check.get("path", "")
    if workspace is None:
        return False, "file_exists requires a workspace but none was provided"
    target = workspace / rel
    if target.is_file():
        return True, f"File exists: {rel}"
    return False, f"File not found: {rel}"


def _check_file_contains(
    check: dict, workspace: Optional[Path]
) -> tuple[bool, str]:
    rel = check.get("path", "")
    value = check.get("value", "")
    if workspace is None:
        return False, "file_contains requires a workspace but none was provided"
    target = workspace / rel
    if not target.is_file():
        return False, f"File not found: {rel}"
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"Cannot read {rel}: {exc}"
    if value in content:
        return True, f"File {rel} contains {value!r}"
    return False, f"File {rel} does not contain {value!r}"


def _check_function_exists(
    check: dict, workspace: Optional[Path]
) -> tuple[bool, str]:
    rel = check.get("file", "")
    func_name = check.get("function", "")
    if workspace is None:
        return False, "function_exists requires a workspace but none was provided"
    target = workspace / rel
    if not target.is_file():
        return False, f"File not found: {rel}"
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"Cannot read {rel}: {exc}"

    # Match Python: def func_name(
    # Match TypeScript: function func_name(, const func_name =, export function func_name(
    patterns = [
        rf"\bdef\s+{re.escape(func_name)}\s*\(",           # Python
        rf"\bfunction\s+{re.escape(func_name)}\s*[\(<]",   # TS/JS function decl
        rf"\bconst\s+{re.escape(func_name)}\s*=",          # TS/JS const arrow
        rf"\blet\s+{re.escape(func_name)}\s*=",            # TS/JS let arrow
        rf"\b{re.escape(func_name)}\s*\([^)]*\)\s*[{{:]",  # TS/JS method shorthand
        rf"\bexport\s+function\s+{re.escape(func_name)}\b",
        rf"\bexport\s+const\s+{re.escape(func_name)}\b",
        rf"\basync\s+function\s+{re.escape(func_name)}\b",
    ]

    for pattern in patterns:
        if re.search(pattern, content):
            return True, f"Function {func_name!r} found in {rel}"

    return False, f"Function {func_name!r} not found in {rel}"


def _check_command_passes(
    check: dict, workspace: Optional[Path]
) -> tuple[bool, str]:
    command = check.get("command", "")
    timeout = check.get("timeout", 30)

    # Replace {workspace} placeholder with the actual path.
    if workspace is not None:
        command = command.replace("{workspace}", str(workspace))

    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=timeout,
            text=True,
        )
        if proc.returncode == 0:
            return True, f"Command passed (exit 0): {command}"
        stderr_snippet = (proc.stderr or "").strip()[:500]
        return False, (
            f"Command failed (exit {proc.returncode}): {command}"
            + (f"\nstderr: {stderr_snippet}" if stderr_snippet else "")
        )
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout}s: {command}"
    except Exception as exc:
        return False, f"Command error: {exc}"


# -- Output-based checks ---------------------------------------------------


def _check_contains(check: dict, output: str) -> tuple[bool, str]:
    value = check.get("value", "")
    case_insensitive = check.get("case_insensitive", False)

    haystack = output.lower() if case_insensitive else output
    needle = value.lower() if case_insensitive else value

    if needle in haystack:
        return True, f"Output contains {value!r}"
    return False, f"Output does not contain {value!r}"


def _check_contains_any(check: dict, output: str) -> tuple[bool, str]:
    values = check.get("values", [])
    case_insensitive = check.get("case_insensitive", False)

    haystack = output.lower() if case_insensitive else output

    for value in values:
        needle = value.lower() if case_insensitive else value
        if needle in haystack:
            return True, f"Output contains {value!r} (from {values!r})"

    return False, f"Output contains none of {values!r}"


def _check_word_count_range(check: dict, output: str) -> tuple[bool, str]:
    min_words = check.get("min", 0)
    max_words = check.get("max", float("inf"))
    word_count = len(output.split())

    if min_words <= word_count <= max_words:
        return True, f"Word count {word_count} is within [{min_words}, {max_words}]"
    return False, (
        f"Word count {word_count} is outside [{min_words}, {max_words}]"
    )


def _check_output_json_schema(
    benchmark: dict,
    variant: dict,
    parsed_output_json: Optional[Any],
    parsed_output_error: Optional[str],
) -> tuple[bool, str]:
    schema = variant.get("output_schema") or benchmark.get("output_schema")
    if not isinstance(schema, dict):
        return False, "No output schema defined"
    if parsed_output_error:
        return False, parsed_output_error
    errors = _validate_json_schema(parsed_output_json, schema)
    if errors:
        return False, "; ".join(errors[:5])
    return True, "Output matches JSON schema"


def _check_truth_match(
    variant: dict,
    context_root: Optional[Path],
    parsed_output_json: Optional[Any],
    parsed_output_error: Optional[str],
) -> tuple[bool, str]:
    truth_extractor = variant.get("truth_extractor")
    if not isinstance(truth_extractor, dict):
        return False, "No truth extractor defined"
    if context_root is None:
        return False, "truth_match requires benchmark context root"
    if parsed_output_error:
        return False, parsed_output_error
    expected = _extract_truth(truth_extractor, context_root)
    if _canonicalize_truth(parsed_output_json) == _canonicalize_truth(expected):
        return True, "Output matches extracted runtime truth"
    return False, (
        "Output did not match extracted runtime truth: "
        f"expected={json.dumps(expected, sort_keys=True)}"
    )


def _is_retryable_incomplete_terminal_failure(
    benchmark: dict,
    run_result: dict,
) -> bool:
    """Return True when a benchmark can be retried inside the same trial."""
    if not benchmark.get("retry_on_incomplete_terminal_state", False):
        return False
    if run_result.get("completed_cleanly"):
        return False
    terminal_state = str(run_result.get("terminal_state", ""))
    if not terminal_state:
        return False
    if terminal_state in {"load_error", "selection_error", "harness_error"}:
        return False
    if terminal_state == "missing_output":
        return True
    return terminal_state in {"timeout", "process_error", "nonzero_exit"} or terminal_state.startswith(
        "error_"
    )


def _execute_benchmark_attempt(
    benchmark: dict,
    variant: dict,
    *,
    benchmark_source_home: Optional[Path] = None,
) -> dict:
    """Run one benchmark attempt end-to-end, including acceptance and judging."""
    benchmark_id = benchmark.get("id", "unknown")
    route = benchmark.get("route", "unknown")
    variant_id = variant.get("id", "unknown")
    checks = variant.get("acceptance_checks", [])
    workspace: Optional[Path] = None
    run_result: Optional[dict] = None

    try:
        workspace_benchmark = dict(benchmark)
        variant_template = variant.get("workspace_template")
        if variant_template:
            workspace_benchmark["workspace_template"] = variant_template
        workspace = prepare_workspace(workspace_benchmark)
        if benchmark_source_home is None:
            run_result = run_benchmark(benchmark, variant, workspace)
        else:
            run_result = run_benchmark(
                benchmark,
                variant,
                workspace,
                benchmark_source_home=benchmark_source_home,
            )
        benchmark_home = run_result.get("benchmark_home")
        context_root = workspace or benchmark_home
        acceptance = check_acceptance(
            benchmark,
            variant,
            checks,
            run_result["output"],
            workspace,
            context_root=context_root,
        )
        deterministic_quality_score = (
            float(acceptance.get("pass_rate", 0.0)) * 100.0
            if run_result.get("completed_cleanly")
            else 0.0
        )
        deterministic_gate_passed = (
            bool(run_result.get("completed_cleanly"))
            and acceptance.get("pass_rate", 0.0) == 1.0
        )
        semantic_judge = None
        semantic_quality_score = None
        if benchmark.get("judge_profile") and deterministic_gate_passed:
            semantic_judge = _run_semantic_judge(
                benchmark,
                variant,
                run_result["output"],
                workspace,
                benchmark_home,
            )
            semantic_quality_score = semantic_judge.get("score", 0.0)

        return {
            "benchmark_id": benchmark_id,
            "variant_id": variant_id,
            "route": route,
            "output": run_result["output"],
            "wall_time_seconds": run_result["wall_time_seconds"],
            "acceptance": acceptance,
            "exit_code": run_result["exit_code"],
            "speed_baseline_seconds": run_result.get("speed_baseline_seconds"),
            "terminal_state": run_result.get("terminal_state"),
            "completed_cleanly": bool(run_result.get("completed_cleanly")),
            "deterministic_gate_passed": deterministic_gate_passed,
            "deterministic_quality_score": deterministic_quality_score,
            "semantic_quality_score": semantic_quality_score,
            "judge_summary": (
                semantic_judge.get("summary")
                if isinstance(semantic_judge, dict)
                else None
            ),
            "judge_flags": (
                semantic_judge.get("missed_expectations")
                if isinstance(semantic_judge, dict)
                else None
            ),
            "judge_failures": (
                semantic_judge.get("issues")
                if isinstance(semantic_judge, dict)
                else None
            ),
            "timed_out": run_result["timed_out"],
            "error": run_result["error"],
            "agent": run_result.get("agent"),
            "model": run_result.get("model"),
            "effort": run_result.get("effort"),
            "retryable_benchmark_failure": _is_retryable_incomplete_terminal_failure(
                benchmark, run_result
            ),
            "benchmark_retry_count": 0,
            "retry_count": 0,
        }
    finally:
        benchmark_home = None
        if run_result is not None:
            benchmark_home = run_result.get("benchmark_home")
        if workspace is not None:
            cleanup_workspace(workspace)
        cleanup_temp_dir(benchmark_home)


def _execute_benchmark_with_retry(
    benchmark: dict,
    variant: dict,
    *,
    benchmark_source_home: Optional[Path] = None,
) -> dict:
    """Run a benchmark, allowing one retry for incomplete terminal failures."""
    if benchmark_source_home is None:
        first_result = _execute_benchmark_attempt(benchmark, variant)
    else:
        first_result = _execute_benchmark_attempt(
            benchmark,
            variant,
            benchmark_source_home=benchmark_source_home,
        )
    if not first_result.get("retryable_benchmark_failure"):
        return first_result

    if benchmark_source_home is None:
        retried = _execute_benchmark_attempt(benchmark, variant)
    else:
        retried = _execute_benchmark_attempt(
            benchmark,
            variant,
            benchmark_source_home=benchmark_source_home,
        )
    retried["benchmark_retry_count"] = 1
    retried["retry_count"] = 1
    retried["retryable_benchmark_failure"] = bool(
        retried.get("retryable_benchmark_failure")
    )
    retried["retry_history"] = [
        {
            "attempt": 1,
            "terminal_state": first_result.get("terminal_state"),
            "completed_cleanly": bool(first_result.get("completed_cleanly")),
            "error": first_result.get("error"),
        }
    ]
    return retried


# ---------------------------------------------------------------------------
# Full suite runner
# ---------------------------------------------------------------------------


def run_full_suite(
    benchmark_ids: Optional[list[str]] = None,
    variant_overrides: Optional[dict[str, str]] = None,
) -> list[dict]:
    """Run selected benchmarks (or all) and return scored results.

    Parameters
    ----------
    benchmark_ids : list[str] | None
        Specific benchmark IDs to run, or ``None`` for all benchmarks.

    Returns
    -------
    list[dict]
        One result dict per benchmark with keys: ``benchmark_id``,
        ``variant_id``, ``route``, ``output``, ``wall_time_seconds``,
        ``acceptance``, ``timed_out``, ``error``.
    """
    if benchmark_ids is not None:
        benchmarks = []
        for bid in benchmark_ids:
            try:
                benchmarks.append(load_benchmark(bid))
            except Exception as exc:
                log.error("Failed to load benchmark %s: %s", bid, exc)
                benchmarks.append({
                    "id": bid,
                    "route": "unknown",
                    "variants": [],
                    "_load_error": str(exc),
                })
    else:
        benchmarks = load_all_benchmarks()

    results: list[dict] = []

    for benchmark in benchmarks:
        benchmark_id = benchmark.get("id", "unknown")
        route = benchmark.get("route", "unknown")
        workspace: Optional[Path] = None

        # Handle benchmarks that failed to load.
        if "_load_error" in benchmark:
            results.append({
                "benchmark_id": benchmark_id,
                "variant_id": None,
                "route": route,
                "output": "",
                "wall_time_seconds": 0.0,
                "acceptance": {
                    "passed": 0,
                    "failed": 0,
                    "total": 0,
                    "pass_rate": 0.0,
                    "details": [],
                },
                "speed_baseline_seconds": None,
                "terminal_state": "load_error",
                "completed_cleanly": False,
                "deterministic_gate_passed": False,
                "deterministic_quality_score": 0.0,
                "semantic_quality_score": None,
                "judge_summary": None,
                "judge_flags": ["load_error"],
                "judge_failures": [benchmark["_load_error"]],
                "retryable_benchmark_failure": False,
                "benchmark_retry_count": 0,
                "retry_count": 0,
                "timed_out": False,
                "error": benchmark["_load_error"],
            })
            continue

        try:
            override_variant_id = (
                variant_overrides.get(benchmark_id) if variant_overrides else None
            )
            if override_variant_id is None:
                variant = select_variant(benchmark)
            else:
                variant = select_variant(benchmark, override_variant_id)
        except ValueError as exc:
            results.append({
                "benchmark_id": benchmark_id,
                "variant_id": None,
                "route": route,
                "output": "",
                "wall_time_seconds": 0.0,
                "acceptance": {
                    "passed": 0,
                    "failed": 0,
                    "total": 0,
                    "pass_rate": 0.0,
                    "details": [],
                },
                "speed_baseline_seconds": None,
                "terminal_state": "selection_error",
                "completed_cleanly": False,
                "deterministic_gate_passed": False,
                "deterministic_quality_score": 0.0,
                "semantic_quality_score": None,
                "judge_summary": None,
                "judge_flags": ["selection_error"],
                "judge_failures": [str(exc)],
                "retryable_benchmark_failure": False,
                "benchmark_retry_count": 0,
                "retry_count": 0,
                "timed_out": False,
                "error": str(exc),
            })
            continue

        variant_id = variant.get("id", "unknown")

        try:
            results.append(_execute_benchmark_with_retry(benchmark, variant))
        except Exception as exc:
            log.error(
                "Benchmark %s variant %s crashed: %s",
                benchmark_id,
                variant_id,
                exc,
            )
            results.append({
                "benchmark_id": benchmark_id,
                "variant_id": variant_id,
                "route": route,
                "output": "",
                "wall_time_seconds": 0.0,
                "acceptance": {
                    "passed": 0,
                    "failed": 0,
                    "total": 0,
                    "pass_rate": 0.0,
                    "details": [],
                },
                "speed_baseline_seconds": _resolve_speed_baseline_seconds(
                    benchmark, variant_id
                ),
                "terminal_state": "harness_error",
                "completed_cleanly": False,
                "deterministic_gate_passed": False,
                "deterministic_quality_score": 0.0,
                "semantic_quality_score": None,
                "judge_summary": None,
                "judge_flags": ["harness_error"],
                "judge_failures": [f"Harness error: {exc}"],
                "retryable_benchmark_failure": False,
                "benchmark_retry_count": 0,
                "retry_count": 0,
                "timed_out": False,
                "error": f"Harness error: {exc}",
            })

    trial_clean = all(bool(result.get("completed_cleanly")) for result in results)
    for result in results:
        result["trial_clean"] = trial_clean

    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _print_summary(results: list[dict]) -> None:
    """Print a human-readable summary of benchmark results."""
    total_pass = 0
    total_checks = 0

    for r in results:
        acc = r.get("acceptance", {})
        passed = acc.get("passed", 0)
        total = acc.get("total", 0)
        total_pass += passed
        total_checks += total

        status = "PASS" if acc.get("pass_rate", 0) == 1.0 else "FAIL"
        if r.get("timed_out"):
            status = "TIMEOUT"
        if r.get("error") and not r.get("output"):
            status = "ERROR"

        print(
            f"  [{status:>7}] {r['benchmark_id']}/{r.get('variant_id', '?')}"
            f"  checks={passed}/{total}"
            f"  time={r['wall_time_seconds']:.1f}s"
        )
        if r.get("error"):
            print(f"           error: {r['error'][:120]}")

    overall_rate = (total_pass / total_checks) if total_checks > 0 else 0.0
    print(
        f"\n  Overall: {total_pass}/{total_checks} checks passed"
        f" ({overall_rate:.0%})"
    )


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    ids = sys.argv[1:] if len(sys.argv) > 1 else None
    print("Running autoconfig eval harness...\n")
    suite_results = run_full_suite(ids)
    _print_summary(suite_results)
