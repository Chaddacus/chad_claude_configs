#!/usr/bin/env python3
"""
product_truth_auto_dispatch.py — Stop hook that auto-dispatches the product-orchestrator
agent when product-shaped sessions try to close without a passing truth-layer gate.

Detection signals (any one fires "product-shaped session"):
  - Recent edits in the session touched product-surface paths
    (landing*, marketing*, pitch*, README.md, docs/, positioning*, pricing*,
     state/product_truth/*).
  - The session transcript contains ≥2 hits of product-keyword words
    (product, launch, release, landing page, positioning, pitch, marketing,
     claim, differentiation, wedge, truth layer, prove-it).
  - A ~/.claude/state/product_truth/<slug>.json was created/modified this session.

Decision logic:
  - No signals       → emit {} (non-blocking)
  - Signals + no JSON → BLOCK with "scaffold via product-orchestrator" reason
  - Signals + JSON + gate failing → BLOCK with "gate failing" reason
  - Signals + JSON + gate passing → emit {} (non-blocking)

Self-cap: counter file mirrors omni_mem_save_hook.sh. Caps at OMNI_MEM_BLOCK_HARD_CAP
(default 6) so a misfiring detection cannot lock a session — emits {} on overflow
with SELF-CAP log line.

NO LLM IN THE LOOP. Karpathy Rule 5: deterministic detection + script-based gate.

Hook input contract (read from stdin as JSON, same shape as other Stop hooks):
  {
    "session_id": "...",
    "stop_hook_active": false,
    "transcript_path": "/.../session-jsonl"
  }
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# ---- config -----------------------------------------------------------------

STATE_DIR = Path(
    os.environ.get("OMNI_MEM_HOOK_STATE_DIR", str(Path.home() / ".omni-mem" / "hook_state"))
)
BLOCK_HARD_CAP = int(os.environ.get("OMNI_MEM_BLOCK_HARD_CAP", "6"))
PRODUCT_TRUTH_DIR = Path.home() / ".claude" / "state" / "product_truth"
CHECK_SCRIPT = Path.home() / ".claude" / "bin" / "product_truth_check.py"

# Bypass switch — if set, hook never blocks.
BYPASS = os.environ.get("OMNI_MEM_PRODUCT_TRUTH_BYPASS", "") == "1"

# Path-based signals: file edits matching ANY pattern count.
PATH_SIGNAL_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"(^|/)landing[^/]*",
        r"(^|/)marketing[^/]*",
        r"(^|/)pitch[^/]*",
        r"(^|/)positioning[^/]*",
        r"(^|/)pricing[^/]*",
        r"(^|/)README\.md$",
        r"(^|/)docs?/",
        r"\.claude/state/product_truth/[^/]+\.json$",
    ]
]

# Keyword-based signals (case-insensitive, word-boundary; require ≥2 hits).
KEYWORD_PATTERN = re.compile(
    r"\b("
    r"product"
    r"|launch(?:ed|ing)?"
    r"|release"
    r"|landing\s+page"
    r"|positioning"
    r"|pitch"
    r"|marketing"
    r"|claim"
    r"|differentiation"
    r"|wedge"
    r"|truth\s+layer"
    r"|prove[\s-]?it"
    r")\b",
    re.IGNORECASE,
)
KEYWORD_HITS_REQUIRED = 2

# ---- helpers ----------------------------------------------------------------


def _log(session_id: str, message: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] session={session_id} product_truth: {message}\n"
    (STATE_DIR / "hook.log").open("a", encoding="utf-8").write(line)


def _emit_nonblock() -> int:
    print("{}")
    return 0


def _emit_block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


def _read_input() -> dict:
    raw = sys.stdin.read()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _sanitize_session(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", s) or "unknown"


def _path_signals(touched_paths: list[str]) -> list[str]:
    signals = []
    for p in touched_paths:
        for pat in PATH_SIGNAL_PATTERNS:
            if pat.search(p):
                signals.append(f"path:{p}")
                break
    return signals


def _transcript_keyword_signals(transcript_path: str) -> tuple[list[str], int]:
    """Returns (signal_messages, hit_count)."""
    if not transcript_path:
        return ([], 0)
    p = Path(transcript_path).expanduser()
    if not p.exists() or not p.is_file():
        return ([], 0)

    hits = 0
    matched_words: list[str] = []
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                role = entry.get("role")
                content = entry.get("content")
                if isinstance(entry.get("message"), dict):
                    role = entry["message"].get("role", role)
                    content = entry["message"].get("content", content)
                if role != "user":
                    continue
                # content can be str or list-of-blocks
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            t = block.get("text") or ""
                            if isinstance(t, str):
                                text += " " + t
                if not text:
                    continue
                if "<command-message>" in text:
                    continue
                for m in KEYWORD_PATTERN.finditer(text):
                    hits += 1
                    matched_words.append(m.group(1).lower())
                    if hits >= KEYWORD_HITS_REQUIRED * 3:
                        # cap inner counting; we already have plenty
                        break
                if hits >= KEYWORD_HITS_REQUIRED * 3:
                    break
    except Exception:
        return ([], 0)

    if hits < KEYWORD_HITS_REQUIRED:
        return ([], hits)

    uniq = sorted(set(matched_words[:8]))
    return ([f"keyword:{w}" for w in uniq], hits)


def _touched_paths_from_transcript(transcript_path: str) -> list[str]:
    """Extract paths touched by Write/Edit tool calls in this session."""
    if not transcript_path:
        return []
    p = Path(transcript_path).expanduser()
    if not p.exists() or not p.is_file():
        return []
    paths: list[str] = []
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                # tool_use entries: look for Write/Edit/MultiEdit with file_path
                msg = entry.get("message") if isinstance(entry.get("message"), dict) else entry
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_use":
                            continue
                        name = block.get("name", "")
                        if name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                            continue
                        inp = block.get("input", {}) or {}
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if isinstance(fp, str):
                            paths.append(fp)
    except Exception:
        return []
    return paths


def _infer_slug(touched_paths: list[str]) -> str | None:
    """Find the most recently-modified product_truth/<slug>.json this session."""
    candidates = [
        Path(p)
        for p in touched_paths
        if p.endswith(".json") and "/state/product_truth/" in p and "/_" not in p
    ]
    if candidates:
        return candidates[-1].stem  # most recent in order

    # Fallback: any existing artifact (not _-prefixed)
    if PRODUCT_TRUTH_DIR.exists():
        artifacts = [
            f
            for f in PRODUCT_TRUTH_DIR.glob("*.json")
            if not f.name.startswith("_")
        ]
        if artifacts:
            # Most recently modified
            artifacts.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            return artifacts[0].stem

    return None


def _run_check(artifact_path: Path) -> tuple[int, dict]:
    """Run product_truth_check.py; return (exit_code, parsed_json)."""
    try:
        proc = subprocess.run(
            ["python3", str(CHECK_SCRIPT), str(artifact_path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        try:
            return (proc.returncode, json.loads(proc.stdout or "{}"))
        except Exception:
            return (proc.returncode, {"raw_stdout": proc.stdout, "raw_stderr": proc.stderr})
    except Exception as exc:
        return (1, {"error": str(exc)})


def _block_count_path(session_id: str) -> Path:
    return STATE_DIR / f"{session_id}_product_truth_block_count"


def _increment_block_count(session_id: str) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _block_count_path(session_id)
    n = 0
    if path.exists():
        try:
            n = int(path.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            n = 0
    n += 1
    path.write_text(str(n), encoding="utf-8")
    return n


# ---- main -------------------------------------------------------------------


def main() -> int:
    if BYPASS:
        return _emit_nonblock()

    raw = _read_input()
    session_id = _sanitize_session(str(raw.get("session_id", "unknown")))
    stop_hook_active = bool(raw.get("stop_hook_active", False))
    transcript_path = str(raw.get("transcript_path", "") or "").replace("~", str(Path.home()))

    # Per-Claude-Code convention: if stop_hook_active is true, we're being re-invoked
    # by a prior blocking decision — do not block again, let the turn complete.
    if stop_hook_active:
        return _emit_nonblock()

    touched = _touched_paths_from_transcript(transcript_path)
    path_sigs = _path_signals(touched)
    kw_sigs, kw_count = _transcript_keyword_signals(transcript_path)

    all_signals = path_sigs + kw_sigs
    if not all_signals:
        return _emit_nonblock()

    slug = _infer_slug(touched)

    def _block_with_selfcap(reason: str, log_msg: str) -> int:
        """Emit block, but first check self-cap. If exceeded, emit non-block + log."""
        count = _increment_block_count(session_id)
        if count > BLOCK_HARD_CAP:
            _log(
                session_id,
                f"SELF-CAP block_count={count} cap={BLOCK_HARD_CAP} — emitting non-block",
            )
            return _emit_nonblock()
        if count >= 3:
            _log(
                session_id,
                f"WARN block_count={count} (runtime caps at 8 consecutive; self-cap at {BLOCK_HARD_CAP})",
            )
        _log(session_id, log_msg)
        return _emit_block(reason)

    if slug is None:
        reason = (
            "Product-shaped session detected (signals: "
            + ", ".join(all_signals[:6])
            + ("..." if len(all_signals) > 6 else "")
            + ") but no ~/.claude/state/product_truth/<slug>.json exists. "
            "Dispatch the product-orchestrator agent via Task tool to scaffold "
            "the truth layer before close. Bypass for this session: "
            "export OMNI_MEM_PRODUCT_TRUTH_BYPASS=1."
        )
        return _block_with_selfcap(
            reason,
            f"BLOCK no_slug signals={len(all_signals)} kw_hits={kw_count}",
        )

    artifact = PRODUCT_TRUTH_DIR / f"{slug}.json"
    if not artifact.exists():
        reason = (
            f"Product signals detected and inferred slug '{slug}' but "
            f"{artifact} does not exist. Scaffold via product-orchestrator agent "
            "or set OMNI_MEM_PRODUCT_TRUTH_BYPASS=1 to bypass."
        )
        return _block_with_selfcap(reason, f"BLOCK missing_artifact slug={slug}")

    exit_code, result = _run_check(artifact)
    if exit_code == 0:
        _log(session_id, f"PASS slug={slug} signals={len(all_signals)}")
        return _emit_nonblock()

    blocked = result.get("blocked", [])
    missing = result.get("missing", [])
    summary_items = (blocked[:3] + missing[:3]) or ["unknown failure"]
    reason = (
        f"Product truth gate failing on '{slug}' "
        f"({len(blocked)} blocked, {len(missing)} missing). "
        f"Top items: {summary_items}. "
        f"Run: python3 ~/.claude/bin/product_truth_check.py {artifact} "
        "to see the full structured output. Fix or dispatch product-orchestrator. "
        "Bypass: export OMNI_MEM_PRODUCT_TRUTH_BYPASS=1."
    )
    return _block_with_selfcap(
        reason,
        f"BLOCK gate_failing slug={slug} blocked={len(blocked)} missing={len(missing)}",
    )


if __name__ == "__main__":
    sys.exit(main())
