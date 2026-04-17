#!/usr/bin/env python3
"""
RLM (Recursive Language Model) codebase scanner.

Usage:
  python3 rlm_scan.py <path> [--type general|security|architecture] [--force]

Recursively decomposes a codebase using rg + ast-grep + semgrep, calls an LLM
at each level for bounded analysis, then caches results to project memory.
"""
import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ── Constants ──────────────────────────────────────────────────────────────────
CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")))
CLAUDE_CMD  = os.environ.get("CLAUDE_CMD", os.path.expanduser("~/.local/bin/claude"))

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_KEY  = os.environ.get("OPENROUTER_API_KEY", "")
MODELS = {
    "leaf":      "anthropic/claude-haiku-4-5",
    "synthesis": "anthropic/claude-sonnet-4-6",
}

LEAF_THRESHOLD    = 20
MAX_DEPTH         = 4
MAX_FILES_PER_NODE = 500
MAX_FILE_CONTENT_BYTES = 3000
MAX_SYMBOLS_PER_LEAF   = 50
MAX_WORKERS = 4

# Thread safety for shared new_hashes dict
_hash_lock = threading.Lock()

# Language → list of validated ast-grep patterns
AST_PATTERNS: dict[str, list[str]] = {
    "ts": [
        "export function $NAME($$$) { $$$ }",
        "export async function $NAME($$$) { $$$ }",
        "export const $NAME = $$$",
        "export class $NAME { $$$ }",
    ],
    "tsx": [
        "export function $NAME($$$) { $$$ }",
        "export const $NAME = $$$",
        "export class $NAME { $$$ }",
    ],
    "js": [
        "export function $NAME($$$) { $$$ }",
        "export const $NAME = $$$",
        "export class $NAME { $$$ }",
        "module.exports.$NAME = $$$",
    ],
    "jsx": [
        "export function $NAME($$$) { $$$ }",
        "export const $NAME = $$$",
    ],
    "python": [
        "def $NAME($$$): $$$",
        "class $NAME($$$): $$$",
    ],
    "go":   ["func $NAME($$$) $$$"],
    "rust": ["fn $NAME($$$) $$$", "pub fn $NAME($$$) $$$"],
}

EXT_TO_LANG = {
    ".ts": "ts", ".tsx": "tsx", ".js": "js", ".jsx": "jsx",
    ".mjs": "js", ".cjs": "js",
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
}

LEAF_PROMPT_TMPL = """\
You are analyzing a code module. Given the file list, symbols, and file excerpts below,
produce a concise JSON response with exactly these keys:
  "summary": one paragraph describing what this module does
  "findings": list of notable observations (patterns, risks, dependencies, issues)

Scan type: {scan_type}
Module path: {module_path}

Files ({file_count}):
{file_list}

Symbols:
{symbols}

File excerpts:
{excerpts}

Respond with ONLY valid JSON. No markdown fences."""

SYNTH_PROMPT_TMPL = """\
You are synthesizing analysis results from multiple sub-modules of a codebase.
Given the sub-module summaries below, produce a JSON response with exactly these keys:
  "summary": one paragraph describing what this part of the codebase does
  "findings": list of cross-cutting observations (architecture, risks, patterns)

Scan type: {scan_type}
Parent path: {module_path}

Sub-modules:
{sub_summaries}

Respond with ONLY valid JSON. No markdown fences."""

PROJECT_PROMPT_TMPL = """\
You are summarizing a complete codebase scan. Given all module analyses below,
produce a JSON response with exactly these keys:
  "project_summary": 2-3 sentences describing the overall project
  "key_findings": list of the most important findings (max 10, prioritize by severity/interest)
  "architecture": one paragraph on the high-level architecture

Scan type: {scan_type}
Root: {root}

Modules:
{modules}

Respond with ONLY valid JSON. No markdown fences."""


# ── LLM client ────────────────────────────────────────────────────────────────
def call_llm(prompt: str, role: str = "leaf") -> dict:
    """Call OpenRouter API directly; fall back to claude subprocess if key missing."""
    t0 = time.time()

    if OPENROUTER_KEY:
        text = _call_openrouter(prompt, MODELS[role])
    else:
        text = _call_claude_subprocess(prompt, "haiku" if role == "leaf" else "sonnet")

    elapsed = time.time() - t0
    print(f"  [llm:{role}] {elapsed:.1f}s", file=sys.stderr)

    # Strip markdown fences
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"summary": text[:500], "findings": []}


def _call_openrouter(prompt: str, model: str) -> str:
    resp = httpx.post(
        f"{OPENROUTER_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "rlm-scan",
            "X-Title": "rlm-scan",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1024,
            "temperature": 0.1,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _call_claude_subprocess(prompt: str, model: str) -> str:
    result = subprocess.run(
        [CLAUDE_CMD, "-p", prompt,
         "--output-format", "json",
         "--dangerously-skip-permissions",
         "--model", model],
        capture_output=True, text=True, timeout=300,
        env={**os.environ},
    )
    if result.returncode != 0:
        return '{"summary": "(claude call failed)", "findings": []}'
    try:
        outer = json.loads(result.stdout.strip())
        return outer.get("result", result.stdout.strip())
    except json.JSONDecodeError:
        return result.stdout.strip()


# ── Utilities ──────────────────────────────────────────────────────────────────
def encode_path(p: Path) -> str:
    return str(p).lstrip("/").replace("/", "-")


def md5_file(p: Path) -> str:
    try:
        return hashlib.md5(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def list_files_rg(path: Path) -> list[Path]:
    result = subprocess.run(
        ["rg", "--files", "--hidden",
         "--glob", "!.git", "--glob", "!node_modules",
         "--glob", "!.venv", "--glob", "!__pycache__",
         "--glob", "!dist", "--glob", "!build", "--glob", "!*.lock",
         str(path)],
        capture_output=True, text=True, timeout=30,
    )
    files = [Path(p) for p in result.stdout.splitlines() if p.strip()]
    return files[:MAX_FILES_PER_NODE]


def extract_symbols(path: Path) -> str:
    """Extract top-level symbols via multi-pattern ast-grep sweep."""
    lang_files: dict[str, list[Path]] = defaultdict(list)
    for f in list_files_rg(path):
        lang = EXT_TO_LANG.get(f.suffix)
        if lang:
            lang_files[lang].append(f)

    seen: set[tuple[str, int]] = set()  # (file, line) dedup key
    symbols: list[str] = []

    for lang, _files in lang_files.items():
        patterns = AST_PATTERNS.get(lang, [])
        for pattern in patterns:
            if len(symbols) >= MAX_SYMBOLS_PER_LEAF:
                break
            result = subprocess.run(
                ["ast-grep", "run", "--pattern", pattern,
                 "--lang", lang, "--json=stream", str(path)],
                capture_output=True, text=True, timeout=20,
            )
            if result.returncode not in (0, 1):
                continue
            for line in result.stdout.splitlines():
                if len(symbols) >= MAX_SYMBOLS_PER_LEAF:
                    break
                try:
                    obj = json.loads(line)
                    file_rel = obj.get("file", "")
                    lineno   = obj.get("range", {}).get("start", {}).get("line", 0)
                    key = (file_rel, lineno)
                    if key in seen:
                        continue
                    seen.add(key)
                    text = obj.get("text", "").split("\n")[0][:120]
                    if text:
                        symbols.append(f"  {file_rel}:{lineno}: {text}")
                except (json.JSONDecodeError, KeyError):
                    continue

    return "\n".join(symbols) if symbols else "(no symbols extracted)"


def run_semgrep(path: Path) -> list[str]:
    result = subprocess.run(
        ["semgrep", "scan", "--config", "p/default", "--json", "--quiet",
         "--max-target-bytes", "1000000", str(path)],
        capture_output=True, text=True, timeout=300,
    )
    findings = []
    try:
        data = json.loads(result.stdout)
        for r in data.get("results", [])[:20]:
            sev   = r.get("extra", {}).get("severity", "INFO")
            msg   = r.get("extra", {}).get("message", "")[:120]
            file_ = r.get("path", "")
            line  = r.get("start", {}).get("line", "?")
            findings.append(f"[{sev}] {file_}:{line} — {msg}")
    except (json.JSONDecodeError, KeyError):
        pass
    return findings


def read_file_excerpt(f: Path) -> str:
    try:
        return f.read_bytes()[:MAX_FILE_CONTENT_BYTES].decode("utf-8", errors="replace")
    except OSError:
        return ""


# ── Core RLM scanner ───────────────────────────────────────────────────────────
def analyze_leaf(path: Path, files: list[Path], scan_type: str,
                 cached_hashes: dict, new_hashes: dict) -> dict:
    rel_path = str(path)

    excerpts_parts = []
    for f in sorted(files)[:5]:
        excerpt = read_file_excerpt(f)
        if excerpt:
            excerpts_parts.append(f"--- {f.name} ---\n{excerpt}")
    excerpts = "\n\n".join(excerpts_parts) or "(no readable files)"

    symbols = extract_symbols(path)

    semgrep_findings = []
    if scan_type == "security":
        semgrep_findings = run_semgrep(path)

    file_list = "\n".join(f"  {f.name}" for f in sorted(files)[:30])
    prompt = LEAF_PROMPT_TMPL.format(
        scan_type=scan_type,
        module_path=rel_path,
        file_count=len(files),
        file_list=file_list,
        symbols=symbols,
        excerpts=excerpts,
    )

    result = call_llm(prompt, role="leaf")
    result.setdefault("findings", [])
    if semgrep_findings:
        result["findings"] = semgrep_findings + result["findings"]
    result["path"]       = rel_path
    result["file_count"] = len(files)
    result["depth"]      = 0
    return result


def synthesize(path: Path, sub_results: list[dict], scan_type: str) -> dict:
    sub_summaries = "\n\n".join(
        f"[{r.get('path','?')}]\n{r.get('summary','')}\nFindings: {r.get('findings',[])}"
        for r in sub_results
    )
    prompt = SYNTH_PROMPT_TMPL.format(
        scan_type=scan_type,
        module_path=str(path),
        sub_summaries=sub_summaries,
    )
    result = call_llm(prompt, role="synthesis")
    result["path"]       = str(path)
    result["file_count"] = sum(r.get("file_count", 0) for r in sub_results)
    result["sub_results"] = sub_results
    return result


def files_changed(files: list[Path], root: Path, cached_hashes: dict) -> bool:
    for f in files:
        rel = str(f.relative_to(root))
        if md5_file(f) != cached_hashes.get(rel):
            return True
    return False


def scan_dir(path: Path, root: Path, scan_type: str,
             cached_hashes: dict, new_hashes: dict,
             cached_modules: dict, depth: int = 0) -> dict:
    files = list_files_rg(path)

    # Update hashes (thread-safe)
    with _hash_lock:
        for f in files:
            rel = str(f.relative_to(root))
            new_hashes[rel] = md5_file(f)

    cache_key = str(path.relative_to(root)) if path != root else "."
    if cache_key in cached_modules and not files_changed(files, root, cached_hashes):
        print(f"  [cache] {cache_key}", file=sys.stderr)
        return cached_modules[cache_key]

    print(f"  [scan]  {cache_key} ({len(files)} files)", file=sys.stderr)

    if depth >= MAX_DEPTH or len(files) <= LEAF_THRESHOLD:
        return analyze_leaf(path, files, scan_type, cached_hashes, new_hashes)

    groups: dict[Path, list[Path]] = defaultdict(list)
    ungrouped: list[Path] = []
    for f in files:
        rel = f.relative_to(path)
        if len(rel.parts) > 1:
            groups[path / rel.parts[0]].append(f)
        else:
            ungrouped.append(f)

    sub_results = []

    # Root-level ungrouped files analyzed first (sequential — usually small)
    if ungrouped:
        sub_results.append(
            analyze_leaf(path, ungrouped, scan_type, cached_hashes, new_hashes)
        )

    # Subdirectories analyzed in parallel
    if groups:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(
                    scan_dir, subdir, root, scan_type,
                    cached_hashes, new_hashes, cached_modules, depth + 1
                ): subdir
                for subdir in sorted(groups)
            }
            for future in as_completed(futures):
                try:
                    sub_results.append(future.result())
                except Exception as exc:
                    subdir = futures[future]
                    print(f"  [error] {subdir}: {exc}", file=sys.stderr)

    if len(sub_results) == 1:
        return sub_results[0]

    return synthesize(path, sub_results, scan_type)


# ── Cache I/O ──────────────────────────────────────────────────────────────────
def get_cache_path(root: Path, scan_type: str) -> Path:
    mem_dir = CLAUDE_HOME / "projects" / encode_path(root) / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    return mem_dir / f"rlm_scan_{scan_type}.json"


def load_cache(root: Path, scan_type: str) -> dict:
    path = get_cache_path(root, scan_type)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_cache(root: Path, scan_type: str, data: dict) -> Path:
    path = get_cache_path(root, scan_type)
    path.write_text(json.dumps(data, indent=2))
    return path


def update_memory_md(root: Path, scan_type: str, project_summary: str) -> None:
    mem_dir = CLAUDE_HOME / "projects" / encode_path(root) / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    memory_md = mem_dir / "MEMORY.md"
    entry = f"- [RLM Scan ({scan_type})](rlm_scan_{scan_type}.json) — {project_summary[:100]}"
    with open(memory_md, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        content = f.read()
        lines = [l for l in content.splitlines() if f"rlm_scan_{scan_type}" not in l]
        lines.append(entry)
        f.seek(0)
        f.truncate()
        f.write("\n".join(lines) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)


def save_summary_md(root: Path, scan_type: str, data: dict) -> None:
    mem_dir = CLAUDE_HOME / "projects" / encode_path(root) / "memory"
    summary_path = mem_dir / f"rlm_scan_{scan_type}_summary.md"
    lines = [
        f"# RLM Scan — {scan_type}",
        f"**Root:** {data['root']}",
        f"**Scanned:** {data['scanned_at']}",
        f"**Files:** {len(data['file_hashes'])}",
        "",
        "## Project Summary",
        data.get("project_summary", ""),
        "",
        "## Key Findings",
    ]
    for finding in data.get("key_findings", []):
        lines.append(f"- {finding}")
    lines += ["", "## Architecture", data.get("architecture", "")]
    summary_path.write_text("\n".join(lines))


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="RLM codebase scanner")
    parser.add_argument("path", help="Root path to scan")
    parser.add_argument("--type", choices=["general", "security", "architecture"],
                        default="general", dest="scan_type")
    parser.add_argument("--force", action="store_true",
                        help="Re-scan all modules, ignoring cache")
    args = parser.parse_args()

    root = Path(args.path).resolve()
    if not root.exists():
        print(f"Error: path does not exist: {root}", file=sys.stderr)
        sys.exit(1)

    api_mode = "openrouter" if OPENROUTER_KEY else "claude-subprocess"
    print(f"[rlm-scan] root={root} type={args.scan_type} force={args.force} api={api_mode}",
          file=sys.stderr)

    wall_start = time.time()

    cache = {} if args.force else load_cache(root, args.scan_type)
    cached_hashes = cache.get("file_hashes", {})
    cached_modules: dict[str, dict] = {
        m.get("path", ""): m for m in cache.get("modules", [])
    }
    new_hashes: dict[str, str] = {}

    top_result = scan_dir(root, root, args.scan_type,
                          cached_hashes, new_hashes, cached_modules)

    # Flatten module tree
    def flatten(result: dict, acc: list) -> None:
        sub = result.pop("sub_results", None)
        acc.append(result)
        if sub:
            for s in sub:
                flatten(s, acc)

    modules: list[dict] = []
    flatten(dict(top_result), modules)

    # Project-level synthesis
    modules_text = "\n\n".join(
        f"[{m.get('path','?')}]\n{m.get('summary','')}"
        for m in modules[:30]
    )
    proj_prompt = PROJECT_PROMPT_TMPL.format(
        scan_type=args.scan_type,
        root=str(root),
        modules=modules_text,
    )
    print("[rlm-scan] synthesizing project summary...", file=sys.stderr)
    proj_result = call_llm(proj_prompt, role="synthesis")

    data = {
        "version": "1.0",
        "scanned_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "root": str(root),
        "scan_type": args.scan_type,
        "file_hashes": {**cached_hashes, **new_hashes},
        "modules": modules,
        "project_summary": proj_result.get("project_summary", top_result.get("summary", "")),
        "key_findings":    proj_result.get("key_findings",    top_result.get("findings", [])),
        "architecture":    proj_result.get("architecture", ""),
    }

    cache_path = save_cache(root, args.scan_type, data)
    save_summary_md(root, args.scan_type, data)
    update_memory_md(root, args.scan_type, data["project_summary"])

    total = time.time() - wall_start
    print(f"[rlm-scan] done — {len(new_hashes)} files, {total:.1f}s total", file=sys.stderr)
    print(f"[rlm-scan] cache → {cache_path}", file=sys.stderr)
    print(f"\n{data['project_summary']}")
    if data["key_findings"]:
        print("\nKey findings:")
        for finding in data["key_findings"][:10]:
            print(f"  • {finding}")


if __name__ == "__main__":
    main()
