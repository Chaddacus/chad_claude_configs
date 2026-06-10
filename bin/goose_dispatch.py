#!/usr/bin/env python3
"""Dispatch a bounded coding slice to a local goose worker and verify it.

Called by the Claude supervisor. Assembles a tiered prompt (non-negotiables +
matched skills + scoped brief + spec), invokes `goose run`, runs a verification
command, retries on failure, and returns a structured JSON result on stdout.

Return contract (stdout = single JSON object, stderr = logs):
{
  "slice_id": str,
  "outcome": "pass" | "fail" | "escalate" | "infra_down" | "gate_cheat_suspected",
  "attempts": int,
  "goose_sessions": [str, ...],
  "verify_exit_codes": [int, ...],
  "files_changed": [str, ...],
  "evidence_log": str,           # path to full log for audit
  "failure_tail": str | None,    # last 2KB of verify stderr when fail/escalate
  "gate_cheat_flags": [str, ...] # populated when outcome == gate_cheat_suspected
}

Exit codes:
  0  pass
  1  fail (retries exhausted, no escalation yet — caller decides)
  2  escalate (deterministic escalation signal, e.g. sandbox violation)
  3  invocation error (bad args, missing goose, etc.)
  4  infra_down (LM Studio unreachable or preflight failed; NOT goose's fault)
  5  gate_cheat_suspected (verify passed but test-cheat patterns found)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
GOOSEHINTS_PATH = HOME / ".goosehints"
SKILLS_DIR = HOME / ".config" / "goose" / "skills"
DEFAULT_LOG_DIR = HOME / ".claude" / "state" / "goose_dispatch"
GOOSE_BIN = "/opt/homebrew/bin/goose"
PREFLIGHT_SCRIPT = HOME / ".claude" / "bin" / "orchestrate_preflight.sh"
PRESETS_DIR = HOME / ".claude" / "bin" / "presets"
UPSTREAM_MODELS_URL = "http://localhost:1234/v1/models"

# Test-cheat patterns to scan for in newly-modified test_*.py / tests/** files.
# High-confidence only — keep narrow to avoid false positives.
CHEAT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "bare-except-swallow",
        re.compile(r"except\b[^:]*:\s*\n\s+(pass|continue|print\s*\()", re.MULTILINE),
    ),
    (
        "pytest-skip-added",
        re.compile(r"^\s*@pytest\.mark\.skip\b", re.MULTILINE),
    ),
    (
        "assert-true-only",
        re.compile(r"^\s*assert\s+(True|1)\s*(#.*)?$", re.MULTILINE),
    ),
]


def load_goosehints() -> str:
    if GOOSEHINTS_PATH.exists():
        return GOOSEHINTS_PATH.read_text(encoding="utf-8").strip()
    return ""


def load_skills(spec: str, file_hints: list[str]) -> list[tuple[str, str]]:
    """Return [(name, body), ...] for skills whose triggers match the slice."""
    if not SKILLS_DIR.exists():
        return []
    matched: list[tuple[str, str]] = []
    spec_low = spec.lower()
    for skill_path in sorted(SKILLS_DIR.glob("*.md")):
        text = skill_path.read_text(encoding="utf-8")
        fm, body = _split_frontmatter(text)
        triggers = (fm or {}).get("triggers", {}) or {}
        kw = [k.lower() for k in triggers.get("keywords", []) or []]
        fpats = triggers.get("files", []) or []
        keyword_hit = any(k in spec_low for k in kw)
        file_hit = any(_fnmatch_any(h, fpats) for h in file_hints)
        if keyword_hit or file_hit:
            name = (fm or {}).get("name", skill_path.stem)
            matched.append((name, body.strip()))
    return matched


def _split_frontmatter(text: str) -> tuple[dict | None, str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return None, text
    fm_raw = text[4:end]
    body = text[end + 5 :]
    fm = _parse_mini_yaml(fm_raw)
    return fm, body


def _parse_mini_yaml(raw: str) -> dict:
    """Tiny YAML subset: key: value, key:\n  subkey: [a, b], lists on one line."""
    out: dict = {}
    current_key: str | None = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  "):
            if current_key is None:
                continue
            sub = line[2:]
            if ":" in sub:
                k, _, v = sub.partition(":")
                v = v.strip()
                if v.startswith("["):
                    v = [s.strip().strip('"').strip("'") for s in v.strip("[]").split(",") if s.strip()]
                elif v == "":
                    v = []
                out.setdefault(current_key, {})[k.strip()] = v
        else:
            k, _, v = line.partition(":")
            v = v.strip()
            if v == "":
                out[k.strip()] = {}
                current_key = k.strip()
            else:
                out[k.strip()] = v
                current_key = None
    return out


def _fnmatch_any(path_hint: str, patterns: list[str]) -> bool:
    import fnmatch

    for p in patterns:
        if p.endswith("/"):
            if p.rstrip("/") in path_hint:
                return True
        if fnmatch.fnmatch(path_hint, p):
            return True
        if fnmatch.fnmatch(Path(path_hint).name, p):
            return True
    return False


WORKER_TOOL_GUIDANCE = """## File-writing protocol — follow EXACTLY

The `write` tool in your runtime has a proven bug: it intermittently emits
empty-argument tool calls on content longer than ~40 lines, wastes 4+ minutes
per failure, and causes the slice to time out. You will obey the following
protocol without exception:

RULE 1. DO NOT CALL THE `write` TOOL. If you feel an urge to call it, stop and
use the `shell` tool instead.

RULE 2. Create or overwrite a file (any size) using a shell heredoc:

    cat > relative/path/to/file.py << 'GOOSE_EOF'
    <full file content here — real newlines, no \\n escape sequences>
    GOOSE_EOF

Use single quotes around the sentinel ('GOOSE_EOF') so shell does not expand
$variables or backticks inside your content.

RULE 3. After writing a file, verify it landed:
    cat relative/path/to/file.py | head -5
    wc -l relative/path/to/file.py

RULE 4. For modifying specific lines in existing files, use the `edit` tool.

RULE 5. For Python files, sanity-check syntax after writing:
    python3 -m py_compile relative/path/to/file.py

The `write` tool will appear to be available. Ignore it. It is broken.

## Library-first heuristic

Before hand-coding algorithms, state machines, parsers, solvers, or protocol
clients, check if a maintained pip/npm package already exists. If one does,
wrap it — do not reinvent. Roll your own only when (a) no suitable library
exists, or (b) the slice brief explicitly forbids adding a dependency.

Examples of where to reach for a library first:
- Cube math → pycuber
- Rubik solver → kociemba
- HTTP client → httpx / requests (check if already a project dep first)
- MCP protocol → the `mcp` SDK
- Date/time arithmetic → stdlib datetime, zoneinfo
- Parsing → stdlib ast / json / csv / email; Lark or parsimonious for grammars
- Validation / schema → pydantic (often already a project dep)
- Retry / backoff → tenacity
- Env config → pydantic-settings or stdlib os.environ

Hand-coded state machines with 10+ cases (e.g., cube moves, protocol state,
parser states) are a strong red flag — search for a library first.

## Test-writing discipline

When writing test files, the following patterns are banned because they
silently swallow real failures and will be flagged by the post-dispatch
cheat scanner:

- `except ... : pass` / `except ... : continue` / `except ... : print(e)`
  Instead: let exceptions propagate, or `except SpecificError: raise` with
  a clear message. Never absorb a failure in a test.
- `assert True` as the only assertion in a test function.
- `@pytest.mark.skip` added in the same slice as the code it's skipping.
- Assertions that are trivially satisfied (e.g. `assert x != 0 or True`).

If your implementation genuinely can't satisfy the acceptance criteria, say
so and stop — do NOT rewrite the test to pass. The dispatcher will detect
`except:pass`-style patterns in test files you modify and return outcome
`gate_cheat_suspected` even if the test "passes"."""


USER_PROMPT_PREFIX = """FILE WRITE PROTOCOL: Do NOT use the `write` tool. Use
`shell` with `cat > path << 'GOOSE_EOF' ... GOOSE_EOF` for all file creation.
See the system prompt for full protocol.

"""


def build_system_prompt(
    goosehints: str, skills: list[tuple[str, str]], brief: str
) -> str:
    parts: list[str] = []
    if goosehints:
        parts.append("## Non-negotiables\n" + goosehints)
    for name, body in skills:
        parts.append(f"## Skill: {name}\n{body}")
    parts.append(WORKER_TOOL_GUIDANCE)
    if brief.strip():
        parts.append("## Slice brief\n" + brief.strip())
    return "\n\n".join(parts).strip()


def build_user_prompt(spec: str, verify_cmd: str, files: list[str]) -> str:
    files_str = "\n".join(f"- {f}" for f in files) if files else "(not specified)"
    return (
        f"{USER_PROMPT_PREFIX}"
        f"# Task\n{spec}\n\n"
        f"# Files in scope\n{files_str}\n\n"
        f"# Acceptance gate\n"
        f"When you believe the task is done, the supervisor will run:\n\n"
        f"    {verify_cmd}\n\n"
        f"You do NOT need to run this yourself — but the code you ship must make it exit 0. "
        f"If the task involves tests, run the narrowest relevant subset yourself before declaring done. "
        f"Do not declare completion with failing tests or typecheck errors."
    )


def workspace_snapshot(workspace: Path) -> dict[str, float]:
    """mtime snapshot of files in workspace (for detecting changes)."""
    out: dict[str, float] = {}
    if not workspace.exists():
        return out
    excluded = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", ".tox", ".eggs", "data"}
    excluded_suffixes = (".db", ".sqlite", ".sqlite3", ".db-journal", ".db-shm", ".db-wal", ".log")
    # Lockfiles auto-updated by package managers — benign side effects of `uv sync`,
    # `npm install`, etc. Treating them as goose-authored writes triggers spurious
    # sandbox violations on every Python/JS slice.
    excluded_names = {"uv.lock", "poetry.lock", "Pipfile.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
    for p in workspace.rglob("*"):
        if p.is_file() and not (excluded & set(p.parts)):
            # Exclude *.egg-info directories (Python packaging metadata)
            if any(part.endswith(".egg-info") for part in p.parts):
                continue
            # Exclude SQLite + log runtime artifacts
            if p.name.endswith(excluded_suffixes):
                continue
            # Exclude package-manager lockfiles
            if p.name in excluded_names:
                continue
            try:
                out[str(p.relative_to(workspace))] = p.stat().st_mtime
            except OSError:
                pass
    return out


def files_changed(before: dict[str, float], after: dict[str, float]) -> list[str]:
    changed = []
    for k, v in after.items():
        if before.get(k) != v:
            changed.append(k)
    for k in before:
        if k not in after:
            changed.append(k + " (deleted)")
    return sorted(changed)


def invoke_goose(
    *,
    session_id: str,
    system_prompt: str,
    user_prompt: str,
    workspace: Path,
    model: str | None,
    max_turns: int,
    log_file: Path,
) -> tuple[int, str]:
    env = os.environ.copy()
    env.setdefault("LM_STUDIO_API_KEY", "lm-studio")
    if model:
        env["GOOSE_MODEL"] = model
    cmd = [
        GOOSE_BIN,
        "run",
        "--no-session",
        "--max-turns",
        str(max_turns),
        "--max-tool-repetitions",
        "5",
        "--system",
        system_prompt,
        "--text",
        user_prompt,
    ]
    with log_file.open("a", encoding="utf-8") as lf:
        lf.write(f"\n\n===== GOOSE INVOCATION {session_id} @ {time.strftime('%H:%M:%S')} =====\n")
        lf.write("CMD: " + " ".join(shlex.quote(c) for c in cmd[:-2]) + " --system <...> --text <...>\n")
        lf.write(f"CWD: {workspace}\n")
        lf.write(f"SYSTEM PROMPT ({len(system_prompt)} chars):\n{system_prompt}\n")
        lf.write(f"USER PROMPT ({len(user_prompt)} chars):\n{user_prompt}\n")
        lf.write("----- goose stdout/stderr -----\n")
        lf.flush()
        proc = subprocess.run(
            cmd,
            cwd=workspace,
            env=env,
            stdout=lf,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=1200,
        )
    tail = _tail_bytes(log_file, 2048)
    return proc.returncode, tail


def run_verify(verify_cmd: str, workspace: Path, log_file: Path) -> tuple[int, str]:
    with log_file.open("a", encoding="utf-8") as lf:
        lf.write(f"\n===== VERIFY @ {time.strftime('%H:%M:%S')} =====\n")
        lf.write(f"CMD: {verify_cmd}\nCWD: {workspace}\n")
        lf.flush()
        proc = subprocess.run(
            verify_cmd,
            cwd=workspace,
            shell=True,
            stdout=lf,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=600,
        )
    tail = _tail_bytes(log_file, 2048)
    return proc.returncode, tail


def _tail_bytes(path: Path, n: int) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def sandbox_check(
    changed: list[str],
    allowed_paths: list[str] | None,
    workspace: Path,
    protected_paths: list[str] | None = None,
) -> str | None:
    """Return a violation message, or None if changes are within sandbox.

    `protected_paths` lists files that goose MUST NOT modify even if they live
    inside `allowed_paths`. Used to guard the supervisor's acceptance script.
    """
    protected = set()
    if protected_paths:
        for p in protected_paths:
            resolved = Path(p).resolve() if os.path.isabs(p) else (workspace / p).resolve()
            protected.add(resolved)
    for f in changed:
        fp_raw = f.split(" (deleted)")[0]
        fp = (workspace / fp_raw).resolve()
        if fp in protected:
            return f"sandbox violation: {fp_raw} is a protected (supervisor-owned) path"
    if not allowed_paths:
        return None
    allowed = [Path(p).resolve() if os.path.isabs(p) else (workspace / p).resolve() for p in allowed_paths]
    for f in changed:
        fp_raw = f.split(" (deleted)")[0]
        fp = (workspace / fp_raw).resolve()
        if not any(_is_within(fp, a) for a in allowed):
            return f"sandbox violation: {fp_raw} is outside allowed paths"
    return None


def ping_upstream(timeout: float = 5.0) -> bool:
    """Return True iff LM Studio's /v1/models responds within `timeout` seconds."""
    import urllib.request

    try:
        with urllib.request.urlopen(UPSTREAM_MODELS_URL, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def run_preflight(log_file: Path) -> tuple[bool, str]:
    """Invoke orchestrate_preflight.sh. Return (ok, output)."""
    if not PREFLIGHT_SCRIPT.exists():
        # Missing script: fall back to a minimal upstream ping.
        ok = ping_upstream()
        return ok, "preflight script missing; used upstream ping fallback"
    env = os.environ.copy()
    env.setdefault("LM_STUDIO_API_KEY", "lm-studio")
    try:
        proc = subprocess.run(
            [str(PREFLIGHT_SCRIPT)],
            capture_output=True, text=True, env=env, timeout=30,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        with log_file.open("a", encoding="utf-8") as lf:
            lf.write(f"\n===== PREFLIGHT @ {time.strftime('%H:%M:%S')} (exit {proc.returncode}) =====\n{out}\n")
        return proc.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "preflight script timed out after 30s"


def scan_for_cheats(changed: list[str], workspace: Path) -> list[str]:
    """Return a list of cheat-flag descriptions for newly-modified test files."""
    flags: list[str] = []
    for f in changed:
        rel = f.split(" (deleted)")[0]
        if rel.endswith(" (deleted)"):
            continue
        name = Path(rel).name
        parts = Path(rel).parts
        is_test_file = (
            name.startswith("test_") and name.endswith(".py")
            or name.endswith("_test.py")
            or any(p in ("tests", "test") for p in parts)
        )
        if not is_test_file:
            continue
        abs_path = (workspace / rel).resolve()
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for label, pattern in CHEAT_PATTERNS:
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                flags.append(f"{rel}:{line_no}: {label}: {match.group(0).strip()[:80]}")
    return flags


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dispatch a slice to goose worker")
    p.add_argument("--slice-id", default=f"slice-{uuid.uuid4().hex[:8]}")
    p.add_argument("--spec", required=True, help="Task description for the worker")
    p.add_argument(
        "--verify-cmd", default=None,
        help="Shell command; exit 0 = pass. Required unless --acceptance-script or --verify-preset is set."
    )
    p.add_argument(
        "--acceptance-script", default=None,
        help="Path to a supervisor-authored verification script. Used as verify_cmd; goose is forbidden from modifying it."
    )
    p.add_argument(
        "--verify-preset", default=None,
        help="Name of a preset in ~/.claude/bin/presets/ (without .sh). Composes preset args via --preset-args."
    )
    p.add_argument(
        "--preset-args", default="",
        help="Arguments to pass to the preset script (single string, shell-style)."
    )
    p.add_argument("--workspace", default=os.getcwd(), help="Working directory")
    p.add_argument("--brief", default="", help="Scoped brief from supervisor")
    p.add_argument("--files", default="", help="Comma-separated file path hints")
    p.add_argument("--allowed-paths", default="", help="Comma-separated write-allowed prefixes (rel to workspace or abs)")
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--max-turns", type=int, default=25)
    p.add_argument("--model", default=None)
    p.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-preflight", action="store_true",
                   help="Skip auto-preflight. Use only when caller has verified upstream.")
    return p.parse_args()


def resolve_verify_cmd(args: argparse.Namespace, workspace: Path) -> str:
    """Determine which verify command to run based on --verify-cmd, --acceptance-script, --verify-preset."""
    if args.acceptance_script:
        script = Path(args.acceptance_script)
        if not script.is_absolute():
            script = (workspace / script).resolve()
        if not script.exists():
            raise SystemExit(f"--acceptance-script not found: {script}")
        if not os.access(script, os.X_OK):
            raise SystemExit(f"--acceptance-script not executable: {script}")
        cmd = shlex.quote(str(script))
        if args.preset_args and args.preset_args.strip():
            cmd += " " + args.preset_args.strip()
        return cmd
    if args.verify_preset:
        preset = PRESETS_DIR / f"{args.verify_preset}.sh"
        if not preset.exists():
            raise SystemExit(f"--verify-preset not found: {preset}")
        if not os.access(preset, os.X_OK):
            raise SystemExit(f"--verify-preset not executable: {preset}")
        cmd = shlex.quote(str(preset))
        if args.preset_args.strip():
            cmd += " " + args.preset_args.strip()
        return cmd
    if args.verify_cmd:
        return args.verify_cmd
    raise SystemExit("must provide one of --verify-cmd, --acceptance-script, or --verify-preset")


def _emit(result: dict) -> None:
    print(json.dumps(result, indent=2), flush=True)


def main() -> int:
    args = parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.exists():
        _emit({"error": f"workspace does not exist: {workspace}"})
        return 3

    log_dir = Path(args.log_dir).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{args.slice_id}.log"
    log_file.write_text(f"slice_id: {args.slice_id}\nstarted: {time.strftime('%Y-%m-%d %H:%M:%S')}\nworkspace: {workspace}\n")

    file_hints = [f.strip() for f in args.files.split(",") if f.strip()]
    allowed_paths = [p.strip() for p in args.allowed_paths.split(",") if p.strip()] or None

    # Resolve verify command and protected paths (acceptance script is supervisor-owned, never writable by goose).
    try:
        verify_cmd = resolve_verify_cmd(args, workspace)
    except SystemExit as e:
        _emit({"error": str(e)})
        return 3

    protected_paths: list[str] = []
    if args.acceptance_script:
        script = Path(args.acceptance_script)
        if not script.is_absolute():
            script = (workspace / script).resolve()
        protected_paths.append(str(script))
        # Reject configs where goose is allowed to write to the acceptance script.
        if allowed_paths:
            for p in allowed_paths:
                resolved = Path(p).resolve() if os.path.isabs(p) else (workspace / p).resolve()
                if _is_within(script, resolved):
                    _emit({"error": f"acceptance-script {script} is within allowed-paths — goose could overwrite it. Narrow --allowed-paths."})
                    return 3

    goosehints = load_goosehints()
    skills = load_skills(args.spec, file_hints)
    system_prompt = build_system_prompt(goosehints, skills, args.brief)
    user_prompt = build_user_prompt(args.spec, verify_cmd, file_hints)

    if args.dry_run:
        _emit({
            "slice_id": args.slice_id,
            "dry_run": True,
            "verify_cmd": verify_cmd,
            "protected_paths": protected_paths,
            "system_prompt_chars": len(system_prompt),
            "user_prompt_chars": len(user_prompt),
            "skills_matched": [name for name, _ in skills],
            "system_prompt_preview": system_prompt[:800],
        })
        return 0

    if not Path(GOOSE_BIN).exists():
        _emit({"error": f"goose binary not found at {GOOSE_BIN}"})
        return 3

    # ---- Auto-preflight (P3/P6) ---------------------------------------
    # Skip preflight automatically when goose is configured for an
    # account-backed ACP provider (codex-acp, claude-code, etc.) — the
    # default preflight checks LM Studio or env API keys, neither of which
    # apply to ACP bridges. The caller can still force preflight by
    # explicitly leaving --no-preflight off after setting CW_GOOSE_MODE=local.
    auto_skip_preflight = False
    try:
        cfg_path = Path.home() / ".config" / "goose" / "config.yaml"
        if cfg_path.is_file():
            for line in cfg_path.read_text().splitlines():
                if line.startswith("GOOSE_PROVIDER:"):
                    provider = line.split(":", 1)[1].strip().strip('"').strip("'")
                    if provider.endswith("-acp") or provider in {"codex-acp", "claude-code"}:
                        auto_skip_preflight = True
                    break
    except Exception:  # noqa: BLE001
        pass

    if not args.no_preflight and not auto_skip_preflight:
        ok, out = run_preflight(log_file)
        if not ok:
            _emit({
                "slice_id": args.slice_id,
                "outcome": "infra_down",
                "attempts": 0,
                "goose_sessions": [],
                "verify_exit_codes": [],
                "files_changed": [],
                "evidence_log": str(log_file),
                "failure_tail": f"preflight failed:\n{out[-1800:]}",
                "gate_cheat_flags": [],
            })
            return 4

    sessions: list[str] = []
    exits: list[int] = []
    last_tail = ""
    changed: list[str] = []

    for attempt in range(1, args.max_retries + 1):
        session_id = f"{args.slice_id}-try{attempt}"
        sessions.append(session_id)
        before = workspace_snapshot(workspace)

        attempt_user = user_prompt
        if attempt > 1 and last_tail:
            attempt_user = (
                user_prompt
                + f"\n\n# Previous attempt failed verification. Last 2KB of verify output:\n\n{last_tail}\n\n"
                f"Fix the cause of that failure, then declare done."
            )

        try:
            _, goose_tail = invoke_goose(
                session_id=session_id,
                system_prompt=system_prompt,
                user_prompt=attempt_user,
                workspace=workspace,
                model=args.model,
                max_turns=args.max_turns,
                log_file=log_file,
            )
        except subprocess.TimeoutExpired:
            exits.append(-1)
            last_tail = "goose invocation timed out"
            # Upstream may be down — check before counting this against goose.
            if not args.no_preflight and not ping_upstream(timeout=5):
                _emit({
                    "slice_id": args.slice_id,
                    "outcome": "infra_down",
                    "attempts": attempt,
                    "goose_sessions": sessions,
                    "verify_exit_codes": exits,
                    "files_changed": [],
                    "evidence_log": str(log_file),
                    "failure_tail": "goose timed out AND upstream LM Studio is unreachable — likely infra failure, not worker fault",
                    "gate_cheat_flags": [],
                })
                return 4
            continue

        # Detect "Server error: 500" pattern in goose tail → re-ping upstream.
        if "Server error: Server error (500" in goose_tail or "500 Internal Server Error" in goose_tail:
            if not args.no_preflight and not ping_upstream(timeout=5):
                _emit({
                    "slice_id": args.slice_id,
                    "outcome": "infra_down",
                    "attempts": attempt,
                    "goose_sessions": sessions,
                    "verify_exit_codes": exits,
                    "files_changed": [],
                    "evidence_log": str(log_file),
                    "failure_tail": "goose got HTTP 500 from LM Studio AND upstream is unreachable",
                    "gate_cheat_flags": [],
                })
                return 4

        after = workspace_snapshot(workspace)
        changed = files_changed(before, after)

        violation = sandbox_check(changed, allowed_paths, workspace, protected_paths)
        if violation:
            _emit({
                "slice_id": args.slice_id,
                "outcome": "escalate",
                "attempts": attempt,
                "goose_sessions": sessions,
                "verify_exit_codes": exits,
                "files_changed": changed,
                "evidence_log": str(log_file),
                "failure_tail": violation,
                "gate_cheat_flags": [],
            })
            return 2

        verify_exit, tail = run_verify(verify_cmd, workspace, log_file)
        exits.append(verify_exit)
        last_tail = tail

        if verify_exit == 0:
            # Verify passed — but scan for cheat patterns before declaring pass.
            cheat_flags = scan_for_cheats(changed, workspace)
            if cheat_flags:
                _emit({
                    "slice_id": args.slice_id,
                    "outcome": "gate_cheat_suspected",
                    "attempts": attempt,
                    "goose_sessions": sessions,
                    "verify_exit_codes": exits,
                    "files_changed": changed,
                    "evidence_log": str(log_file),
                    "failure_tail": "verify passed but test files contain cheat patterns — review before accepting",
                    "gate_cheat_flags": cheat_flags,
                })
                return 5
            _emit({
                "slice_id": args.slice_id,
                "outcome": "pass",
                "attempts": attempt,
                "goose_sessions": sessions,
                "verify_exit_codes": exits,
                "files_changed": changed,
                "evidence_log": str(log_file),
                "failure_tail": None,
                "gate_cheat_flags": [],
            })
            return 0

    _emit({
        "slice_id": args.slice_id,
        "outcome": "fail",
        "attempts": args.max_retries,
        "goose_sessions": sessions,
        "verify_exit_codes": exits,
        "files_changed": changed,
        "evidence_log": str(log_file),
        "failure_tail": last_tail,
        "gate_cheat_flags": [],
    })
    return 1


if __name__ == "__main__":
    sys.exit(main())
