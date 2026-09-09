#!/usr/bin/env python3
"""Analyze accumulated observations and propose fixes.

Reads ~/.claude/evolve/history.jsonl, detects recurring patterns over the last
N runs, and writes structured proposals to ~/.claude/evolve/proposals.jsonl.

A "proposal" is a machine-applicable change to a prompt/skill/preset file.
Each proposal has a type, target file, content, evidence, confidence, and
auto_apply flag.

Proposal schema:
{
    "id": str,                  # stable hash of evidence + type + target
    "created_at": iso-ts,
    "type": "append_rule" | "append_skill_line" | "add_preset" | "append_example",
    "target": "~/.goosehints" | path,
    "anchor": str | None,       # optional: where to append (e.g. a section header)
    "content": str,
    "evidence": [str, ...],     # the observation excerpts that motivated this
    "evidence_hash": str,       # dedupe key
    "confidence": "low" | "medium" | "high",
    "auto_apply": bool,         # true for additive, low-risk changes
    "applied": bool,            # set by evolve_apply.py
    "applied_at": iso-ts | null,
}

Usage:
    evolve_analyze.py [--window 5] [--print]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

HOME = Path(os.path.expanduser("~"))
HISTORY = HOME / ".claude" / "evolve" / "history.jsonl"
PROPOSALS = HOME / ".claude" / "evolve" / "proposals.jsonl"
GOOSEHINTS = HOME / ".goosehints"


def load_history() -> list[dict]:
    if not HISTORY.exists():
        return []
    return [json.loads(line) for line in HISTORY.read_text().splitlines() if line.strip()]


def load_proposals() -> list[dict]:
    if not PROPOSALS.exists():
        return []
    return [json.loads(line) for line in PROPOSALS.read_text().splitlines() if line.strip()]


def _save_proposal(prop: dict) -> bool:
    """Append a proposal. Returns True if new, False if duplicate by evidence_hash."""
    existing = load_proposals()
    if any(p["evidence_hash"] == prop["evidence_hash"] for p in existing):
        return False
    PROPOSALS.parent.mkdir(parents=True, exist_ok=True)
    with PROPOSALS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(prop) + "\n")
    return True


def _make_proposal(
    type_: str, target: str, content: str, evidence: list[str],
    confidence: str, auto_apply: bool, anchor: str | None = None,
    dedup_phrases: list[str] | None = None,
) -> dict:
    """Build a proposal dict.

    `dedup_phrases`: distinctive phrases that, if present in the target file,
    mean this proposal's intent is already covered. Used by evolve_apply.py
    to avoid re-introducing rules that were merged into other rules during
    consolidation. Match is case-insensitive substring.
    """
    evidence_key = "|".join(sorted(evidence)) + "::" + type_ + "::" + target
    h = hashlib.sha256(evidence_key.encode()).hexdigest()[:16]
    return {
        "id": h,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "type": type_,
        "target": target,
        "anchor": anchor,
        "content": content,
        "evidence": evidence,
        "evidence_hash": h,
        "confidence": confidence,
        "auto_apply": auto_apply,
        "applied": False,
        "applied_at": None,
        "dedup_phrases": dedup_phrases or [],
    }


_STRAY_DIR_PATTERNS = (
    "test_dir", "tmp_test", "tmp_dir", "scratch", "scratch_dir",
    "debug", "debug_dir", "temp", "tmpdir", "playground", "sandbox_dir",
    "experiment", "throwaway", "_tmp", "_test_tmp",
)
_EXPECTED_TOP_LEVEL = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", ".tox", ".eggs",
    "src", "lib", "app", "tests", "test", "docs", "doc", "scripts",
    "static", "templates", "public", "assets", "data", "migrations",
    "backups", ".github", ".vscode", ".idea", "config", "conf",
    "frontend", "backend", "client", "server", "api", "web",
}


def _scan_workspace_antipatterns(workspace: str) -> list[str]:
    """Static scan of a completed workspace for known anti-patterns.

    Returns a list of antipattern tags found, e.g. ['staticpool-no-thread',
    'fastapi-post-no-body-model', 'redirect-test-no-follow-redirects-false',
    'stray-fixture-dir']. Used by detect_patterns to surface code-shape
    failures the metric-based analyzer would otherwise miss.
    """
    from pathlib import Path
    tags: list[str] = []
    ws = Path(workspace)
    if not ws.exists():
        return tags

    # Antipattern 4: stray fixture / scratch directories at workspace root.
    # Goose occasionally creates throwaway dirs like test_dir/, tmp_test/,
    # scratch/ when it should be using pytest's tmp_path fixture or running
    # in-process. Flag any top-level dir whose name matches a known stray
    # pattern OR isn't in the expected-top-level allowlist and contains only
    # transient-looking content (a single .py file, a single .txt, etc.).
    try:
        for entry in ws.iterdir():
            if not entry.is_dir():
                continue
            name = entry.name.lower()
            if name in _EXPECTED_TOP_LEVEL:
                continue
            # Skip hidden/dotted dirs — they're config or tooling, not stray.
            if name.startswith("."):
                continue
            if any(pat in name for pat in _STRAY_DIR_PATTERNS):
                tags.append(f"stray-fixture-dir:{entry.name}")
                continue
            # Unknown dir: flag if it looks throwaway (<=2 files, all small)
            try:
                children = list(entry.iterdir())
            except OSError:
                continue
            if 0 < len(children) <= 2 and all(
                c.is_file() and c.stat().st_size < 4096 for c in children
            ):
                tags.append(f"stray-fixture-dir:{entry.name}")
    except OSError:
        pass

    # Scan Python files (capped to avoid runaway on huge workspaces)
    py_files = []
    for p in ws.rglob("*.py"):
        if any(x in p.parts for x in (".venv", "__pycache__", ".pytest_cache", "node_modules")):
            continue
        py_files.append(p)
        if len(py_files) > 50:
            break

    for p in py_files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Antipattern 1: StaticPool used without check_same_thread=False
        if "StaticPool" in text and "check_same_thread" not in text:
            tags.append(f"staticpool-no-thread:{p.name}")
        # Antipattern 2: FastAPI POST handler with bare-type non-Body param
        # heuristic: @app.post followed by `def fn(<name>: str)` — likely query, not body
        import re
        for m in re.finditer(r"@app\.post\([^)]+\)\s*\n\s*async\s+def\s+\w+\(([^)]+)\)", text):
            sig = m.group(1)
            # Look for primitive-typed param without `Body(`, `Depends(`, or pydantic model
            if re.search(r"\b\w+\s*:\s*(str|int|float|bool)\b", sig) and "Body" not in sig and "BaseModel" not in text:
                tags.append(f"fastapi-post-bare-type:{p.name}")
                break
        # Antipattern 3: redirect test asserts 3xx without follow_redirects=False
        if "test_" in p.name or "/tests/" in str(p):
            if "RedirectResponse" in text or "redirect" in text.lower():
                # Check redirect-related test bodies for follow_redirects setting
                if re.search(r"client\.get\([^)]*\)\s*\n\s*assert.*status_code\s*==\s*30[0-9]", text):
                    if "follow_redirects" not in text:
                        tags.append(f"redirect-test-no-follow:{p.name}")
    return tags


def detect_patterns(history: list[dict], window: int = 5) -> list[dict]:
    """Detect recurring failures in the last `window` runs. Return proposals."""
    recent = history[-window:] if len(history) >= window else history
    if not recent:
        return []

    proposals: list[dict] = []

    # ---- Code-shape antipattern aggregation across recent workspaces ----
    workspace_tags: list[str] = []
    workspace_evidence: dict[str, list[str]] = {}
    for run in recent:
        ws = run.get("workspace", "")
        if not ws:
            continue
        tags = _scan_workspace_antipatterns(ws)
        for tag in tags:
            kind = tag.split(":", 1)[0]
            workspace_tags.append(kind)
            workspace_evidence.setdefault(kind, []).append(f"{run['task_id']}: {tag}")

    from collections import Counter
    ws_counter = Counter(workspace_tags)

    if ws_counter.get("staticpool-no-thread", 0) >= 2:
        proposals.append(_make_proposal(
            type_="append_rule",
            target=str(GOOSEHINTS),
            content="- When using StaticPool with SQLite (in-memory or otherwise), you MUST also pass connect_args={'check_same_thread': False} to create_engine. Without it, FastAPI's threaded request handling raises 'SQLite objects created in a thread can only be used in that same thread'.",
            evidence=workspace_evidence.get("staticpool-no-thread", []),
            confidence="high",
            auto_apply=True,
        ))

    if ws_counter.get("fastapi-post-bare-type", 0) >= 2:
        proposals.append(_make_proposal(
            type_="append_rule",
            target=str(GOOSEHINTS),
            content="- For FastAPI POST endpoints accepting a JSON body, declare a Pydantic BaseModel for the request shape and type the parameter as that model. A bare-type annotation (e.g. `url: str`) is treated as a QUERY parameter, causing 422 errors when callers POST JSON.",
            evidence=workspace_evidence.get("fastapi-post-bare-type", []),
            confidence="high",
            auto_apply=True,
        ))

    if ws_counter.get("redirect-test-no-follow", 0) >= 2:
        proposals.append(_make_proposal(
            type_="append_rule",
            target=str(GOOSEHINTS),
            content="- Tests that assert a 3xx redirect status MUST pass `follow_redirects=False` to the TestClient/httpx call. Default behavior is to follow redirects, so you'd see 200 from the final URL instead of 307/302 from the first hop.",
            evidence=workspace_evidence.get("redirect-test-no-follow", []),
            confidence="high",
            auto_apply=True,
        ))

    if ws_counter.get("stray-fixture-dir", 0) >= 2:
        proposals.append(_make_proposal(
            type_="append_rule",
            target=str(GOOSEHINTS),
            content="- Do NOT create scratch/fixture directories at the workspace root (test_dir/, tmp_test/, scratch/, debug/, etc.). Tests that need a temp filesystem MUST use pytest's `tmp_path` fixture, which creates an isolated dir per test and cleans up automatically. Throwaway dirs left at the repo root pollute the workspace and trigger sandbox-violation escalations.",
            evidence=workspace_evidence.get("stray-fixture-dir", []),
            confidence="high",
            auto_apply=True,
        ))

    # Aggregate failure categories across recent window
    cat_counter: Counter[str] = Counter()
    for run in recent:
        for cat, n in run.get("failure_categories", {}).items():
            cat_counter[cat] += n

    # Pattern 1: repeated test-cheat flags
    if cat_counter.get("test-cheat", 0) >= 2:
        evidence = [
            f"{run['task_id']}: {run['metrics']['cheat_count']} cheat flags"
            for run in recent if run["metrics"].get("cheat_count", 0) > 0
        ]
        proposals.append(_make_proposal(
            type_="append_rule",
            target=str(GOOSEHINTS),
            content="- When a verify gate fails, NEVER alter test logic to make it pass; fix the implementation. Tests are the acceptance spec, not flexible paperwork.",
            evidence=evidence,
            confidence="high",
            auto_apply=True,
            dedup_phrases=["NEVER weaken tests", "NEVER alter test logic", "Acceptance fidelity"],
        ))

    # Pattern 2: repeated sandbox violations (goose creating unexpected files)
    if cat_counter.get("sandbox-violation", 0) >= 2:
        evidence = [
            f"{run['task_id']}: {run['metrics'].get('sandbox_violation_count', 0)} escalations"
            for run in recent if run["metrics"].get("sandbox_violation_count", 0) > 0
        ]
        proposals.append(_make_proposal(
            type_="append_rule",
            target=str(GOOSEHINTS),
            content="- Do NOT create auxiliary helper files (run_tests.py, setup_helper.py, debug.py) unless the slice brief explicitly requests them. Only modify files in the slice's listed `--allowed-paths`.",
            evidence=evidence,
            confidence="high",
            auto_apply=True,
            dedup_phrases=["auxiliary helper files", "Do NOT create auxiliary"],
        ))

    # Pattern 3: repeated infra_down
    if cat_counter.get("infra", 0) >= 2:
        evidence = [
            f"{run['task_id']}: infra_down"
            for run in recent if run["metrics"].get("infra_down_count", 0) > 0
        ]
        proposals.append(_make_proposal(
            type_="append_example",
            target=str(HOME / ".claude" / "bin" / "orchestrate_preflight.sh"),
            content="# Consider a retry-with-backoff here if infra is flaky.",
            evidence=evidence,
            confidence="medium",
            auto_apply=False,  # changes executable preflight — review required
        ))

    # Pattern 4: first-try pass rate trending low
    rates = [r["metrics"].get("first_try_pass_rate", 0) for r in recent]
    if rates and sum(rates) / len(rates) < 0.5 and len(rates) >= 3:
        evidence = [f"{r['task_id']}: first_try={r['metrics']['first_try_pass_rate']:.0%}" for r in recent]
        proposals.append(_make_proposal(
            type_="append_rule",
            target=str(GOOSEHINTS),
            content="- Before declaring a slice done, re-read the acceptance script requirements against your implementation. If any assertion is not directly satisfied, fix before declaring.",
            evidence=evidence,
            confidence="medium",
            auto_apply=True,
            dedup_phrases=[
                "re-read the acceptance script",
                "Acceptance fidelity",
                "re-read the acceptance gate",
            ],
        ))

    # Pattern 5: supervisor takeovers on similar task type → capability gap
    takeover_tasks = [r["task_id"] for r in recent if r["metrics"].get("supervisor_takeovers", 0) > 0]
    if len(takeover_tasks) >= 3:
        evidence = [f"supervisor takeover in {t}" for t in takeover_tasks]
        proposals.append(_make_proposal(
            type_="append_rule",
            target=str(GOOSEHINTS),
            content="- If a task involves non-trivial state-machine, parser, solver, or protocol logic, search pip/npm for a maintained library FIRST and wrap it. Hand-coded state machines are a frequent failure source.",
            evidence=evidence,
            confidence="high",
            auto_apply=True,
            dedup_phrases=["Library-first", "search pip/npm for a maintained library"],
        ))

    return proposals


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--window", type=int, default=5)
    p.add_argument("--print", action="store_true")
    args = p.parse_args()

    history = load_history()
    proposals = detect_patterns(history, window=args.window)

    new_count = 0
    dup_count = 0
    for prop in proposals:
        if _save_proposal(prop):
            new_count += 1
        else:
            dup_count += 1

    print(f"analyzed {len(history)} runs (window={args.window}): "
          f"{new_count} new proposals, {dup_count} duplicates")
    if args.print:
        for prop in proposals:
            print(json.dumps(prop, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
