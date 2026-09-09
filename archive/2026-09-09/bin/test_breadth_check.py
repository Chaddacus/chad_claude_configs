#!/usr/bin/env python3
"""test_breadth_check.py — classify a diff into required test breadths.

Postflight classifier for the test_breadth_check gate (route_manifest.json
postflight.gate_chain). Reads a git diff, applies the breadth-classification
rules from ~/.claude/standards/testing-standard.md + manifest globs, and emits
a JSON record listing the required breadths and rationale.

Does NOT run tests itself — that's the validator agent's job. This script
just produces the required-breadth set so validator can enforce it.

Usage:
    test_breadth_check.py --repo <path> [--base <ref>] [--head <ref>]
                          [--out <path>] [--format json|text]

Defaults:
    --base = $(git merge-base HEAD origin/main) || HEAD~1
    --head = HEAD (or working tree if both are unset and dirty)

Output schema (json):
    {
        "schema_version": "1.0",
        "repo": "...",
        "base": "...",
        "head": "...",
        "changed_files": [...],
        "required_breadths": ["smoke", "full", "browser-e2e", "data-combo"],
        "rationale": [
            {"breadth": "full",        "reason": "code edit not covered by smoke"},
            {"breadth": "browser-e2e", "reason": "ui_globs match: ['app/page.tsx']"}
        ],
        "adjacent_escalation": {
            "tool": "dependency-cruiser|grimp|skipped",
            "depth": 2,
            "additional_breadths": []
        }
    }

Exit codes:
    0 — classification produced (always, even if no changed files)
    1 — bad arguments / not a git repo
    2 — git plumbing failure
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME = Path(os.path.expanduser("~"))
MANIFEST = HOME / ".claude" / "state" / "route_manifest.json"
TESTING_STANDARD = HOME / ".claude" / "standards" / "testing-standard.md"

DEFAULT_UI_GLOBS = [
    "**/*.tsx", "**/*.jsx", "**/*.vue", "**/*.svelte",
    "**/app/**/page.*", "**/pages/**",
]
DEFAULT_DATA_COMBO_TRIGGERS = [
    "**/openapi.{yaml,yml,json}",
    "**/schema.graphql",
    "**/validators/*",
    "**/schemas/*",
    "**/*.proto",
]
DEFAULT_ADJACENT_DEPTH = 2


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 60) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)


def load_manifest_config() -> dict[str, Any]:
    """Pull the test_breadth_check sub-config from the route manifest, falling back to defaults."""
    if not MANIFEST.is_file():
        return {
            "ui_globs": DEFAULT_UI_GLOBS,
            "data_combo_triggers": DEFAULT_DATA_COMBO_TRIGGERS,
            "adjacent_depth": DEFAULT_ADJACENT_DEPTH,
        }
    try:
        m = json.loads(MANIFEST.read_text())
    except (json.JSONDecodeError, OSError):
        return {
            "ui_globs": DEFAULT_UI_GLOBS,
            "data_combo_triggers": DEFAULT_DATA_COMBO_TRIGGERS,
            "adjacent_depth": DEFAULT_ADJACENT_DEPTH,
        }
    cfg = (m.get("postflight") or {}).get("test_breadth_check") or {}
    return {
        "ui_globs": cfg.get("ui_globs") or DEFAULT_UI_GLOBS,
        "data_combo_triggers": cfg.get("data_combo_triggers") or DEFAULT_DATA_COMBO_TRIGGERS,
        "adjacent_depth": cfg.get("adjacent_depth") or DEFAULT_ADJACENT_DEPTH,
    }


def _expand_brace(pattern: str) -> list[str]:
    """Expand a single brace expansion in a glob (e.g. *.{yaml,yml,json}).
    fnmatch doesn't handle brace expansion, so we expand it ourselves."""
    if "{" not in pattern or "}" not in pattern:
        return [pattern]
    head = pattern[: pattern.index("{")]
    tail = pattern[pattern.index("}") + 1 :]
    options = pattern[pattern.index("{") + 1 : pattern.index("}")].split(",")
    out = []
    for opt in options:
        out.extend(_expand_brace(head + opt + tail))
    return out


def matches_any(path: str, patterns: list[str]) -> bool:
    """Return True if path matches any of the (possibly brace-expanded) globs.

    fnmatch's `**` requires at least one directory segment, so a glob like
    `**/openapi.yaml` won't match a root-level `openapi.yaml`. We work around
    that by stripping the leading `**/` and retrying against both the full
    path and the basename.
    """
    basename = path.rsplit("/", 1)[-1]
    for pat in patterns:
        for expanded in _expand_brace(pat):
            if fnmatch.fnmatch(path, expanded):
                return True
            # Strip leading "**/" and retry — handles root-level files
            stripped = expanded[3:] if expanded.startswith("**/") else expanded
            if stripped != expanded and fnmatch.fnmatch(path, stripped):
                return True
            # And try basename match for trailing-segment globs
            if fnmatch.fnmatch(basename, stripped):
                return True
    return False


def changed_files(repo: Path, base: str | None, head: str | None) -> tuple[str, str, list[str]]:
    """Return (resolved_base, resolved_head, list_of_changed_files).

    Resolution rules:
      - If both base+head are given: use them as-is.
      - If neither: try working-tree changes first (git status --porcelain).
        Fall back to base=$(git merge-base HEAD origin/main), head=HEAD if working tree clean.
      - If only one given: error.
    """
    if (base is None) != (head is None):
        raise ValueError("--base and --head must both be provided or both omitted")

    if base is None and head is None:
        # Working tree first
        rc, out, _ = run(["git", "status", "--porcelain"], cwd=repo, timeout=10)
        if rc == 0 and out.strip():
            files = []
            for line in out.splitlines():
                # porcelain format: "XY path" with possible " -> path" for renames
                rest = line[3:].strip()
                if " -> " in rest:
                    rest = rest.split(" -> ", 1)[1]
                files.append(rest)
            return ("WORKING_TREE", "WORKING_TREE", files)
        # Clean tree → diff against merge-base
        rc, mb, _ = run(["git", "merge-base", "HEAD", "origin/main"], cwd=repo, timeout=10)
        if rc != 0 or not mb.strip():
            # Fall back to HEAD~1
            base, head = "HEAD~1", "HEAD"
        else:
            base, head = mb.strip(), "HEAD"

    rc, out, err = run(["git", "diff", "--name-only", f"{base}..{head}"], cwd=repo, timeout=30)
    if rc != 0:
        raise RuntimeError(f"git diff failed: {err.strip()}")
    files = [line for line in out.splitlines() if line.strip()]
    return (base, head, files)


def classify(files: list[str], cfg: dict[str, Any]) -> tuple[set[str], list[dict[str, str]]]:
    """Apply the testing-standard rules to classify required breadths.

    Returns (set_of_required_breadths, rationale_records).

    Rules (per ~/.claude/standards/testing-standard.md):
      - Any code edit not purely doc → full
      - UI surface change → +browser-e2e
      - Schema/contract/validator change → +data-combo
      - Pure doc change → smoke only
    """
    breadths: set[str] = set()
    rationale: list[dict[str, str]] = []

    if not files:
        breadths.add("smoke")
        rationale.append({"breadth": "smoke", "reason": "no changes detected"})
        return breadths, rationale

    code_extensions = {".py", ".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte",
                       ".go", ".rs", ".rb", ".java", ".kt", ".scala", ".cs", ".cpp", ".c",
                       ".sh", ".sql", ".yaml", ".yml", ".json", ".toml"}
    doc_only = True
    for f in files:
        ext = Path(f).suffix.lower()
        if ext in code_extensions:
            doc_only = False
            break

    if doc_only:
        breadths.add("smoke")
        rationale.append({"breadth": "smoke", "reason": "doc-only change (no code extensions)"})
        return breadths, rationale

    # Code edit → full at minimum
    breadths.add("full")
    rationale.append({"breadth": "full", "reason": "code edit not covered by smoke"})

    # UI surface
    ui_hits = [f for f in files if matches_any(f, cfg["ui_globs"])]
    if ui_hits:
        breadths.add("browser-e2e")
        rationale.append({
            "breadth": "browser-e2e",
            "reason": f"ui_globs match: {ui_hits[:3]}{'...' if len(ui_hits) > 3 else ''}",
        })

    # Data-combo triggers
    dc_hits = [f for f in files if matches_any(f, cfg["data_combo_triggers"])]
    if dc_hits:
        breadths.add("data-combo")
        rationale.append({
            "breadth": "data-combo",
            "reason": f"data_combo_triggers match: {dc_hits[:3]}{'...' if len(dc_hits) > 3 else ''}",
        })

    return breadths, rationale


_TS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

_DEPCRUISE_MIN_CONFIG = """\
module.exports = {
  options: {
    tsPreCompilationDeps: true,
    doNotFollow: { path: "node_modules" },
    exclude: { path: ["node_modules", "dist", "build", ".next", "coverage"] },
    enhancedResolveOptions: { extensions: [".ts",".tsx",".js",".jsx",".mjs",".cjs"] }
  },
  forbidden: []
};
"""


def _depcruise_reverse_graph(repo: Path) -> dict[str, list[str]]:
    """Run dependency-cruiser to get the import graph; invert to {imported: [importers]}.

    depcruise resolves imports starting from the entry points it's given. To
    get a full repo graph we pass ALL tracked TS/JS source files as entry
    points and let depcruise dedupe + walk. Output is forward edges; we
    invert to reverse-import map.
    """
    if not shutil.which("depcruise"):
        return {}

    # Collect entry points — tracked source files only, exclude obvious noise
    rc, out, _ = run(
        ["git", "ls-files",
         "*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cjs",
         ":!:node_modules/**", ":!:dist/**", ":!:build/**", ":!:.next/**"],
        cwd=repo, timeout=10,
    )
    if rc != 0 or not out.strip():
        return {}
    entry_points = [line for line in out.splitlines() if line.strip()][:1000]
    if not entry_points:
        return {}

    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False) as cfg_f:
        cfg_f.write(_DEPCRUISE_MIN_CONFIG)
        cfg_path = cfg_f.name
    try:
        rc, out, err = run(
            ["depcruise", "--config", cfg_path, "--output-type", "json",
             "--no-progress", *entry_points],
            cwd=repo, timeout=300,
        )
        if rc != 0 or not out.strip():
            return {}
        try:
            graph = json.loads(out)
        except json.JSONDecodeError:
            return {}
        rev: dict[str, list[str]] = {}
        for mod in graph.get("modules", []):
            src = mod.get("source")
            if not src:
                continue
            for dep in mod.get("dependencies", []):
                resolved = dep.get("resolved")
                if resolved and not resolved.startswith("node_modules"):
                    rev.setdefault(resolved, []).append(src)
        return rev
    finally:
        try: os.unlink(cfg_path)
        except OSError: pass


def _grimp_reverse_set(repo: Path, py_files: list[str], depth: int) -> tuple[set[str], list[str]]:
    """Use grimp to compute the reverse-import set for changed Python files.

    grimp imports the package — requires the repo to be a proper Python package
    importable from sys.path. We auto-detect the top-level package by looking
    for src/<pkg>/__init__.py or <pkg>/__init__.py.
    """
    try:
        import grimp  # type: ignore
    except ImportError:
        return set(), ["grimp not importable"]

    # Find top-level package
    pkg_candidates = []
    for d in (repo / "src").iterdir() if (repo / "src").is_dir() else []:
        if (d / "__init__.py").is_file():
            pkg_candidates.append(d.name)
    for d in repo.iterdir():
        if d.is_dir() and (d / "__init__.py").is_file() and not d.name.startswith("."):
            pkg_candidates.append(d.name)
    if not pkg_candidates:
        return set(), ["no python package (no <pkg>/__init__.py)"]

    seen: set[str] = set()
    notes: list[str] = []
    sys.path.insert(0, str(repo / "src") if (repo / "src").is_dir() else str(repo))
    try:
        for pkg in pkg_candidates[:1]:  # one root pkg per call to keep fast
            try:
                graph = grimp.build_graph(pkg)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"grimp.build_graph({pkg}) failed: {exc}")
                continue
            for f in py_files:
                # Convert path → module name
                rel = f.replace("\\", "/")
                if rel.startswith("src/"):
                    rel = rel[4:]
                mod_name = rel[:-3].replace("/", ".") if rel.endswith(".py") else rel.replace("/", ".")
                if mod_name.endswith(".__init__"):
                    mod_name = mod_name[:-9]
                if not mod_name.startswith(pkg):
                    continue
                try:
                    # downstream = modules that depend ON this one (the importers)
                    descendants = graph.find_downstream_modules(mod_name, as_package=False)
                    for d in descendants:
                        seen.add(d.replace(".", "/") + ".py")
                    if depth and len(descendants) > 0:
                        notes.append(f"grimp: {len(descendants)} downstream of {mod_name}")
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"grimp.find_downstream_modules({mod_name}) failed: {exc}")
    finally:
        try: sys.path.remove(str(repo / "src") if (repo / "src").is_dir() else str(repo))
        except ValueError: pass
    return seen, notes


def adjacent_escalation(repo: Path, files: list[str], depth: int,
                        cfg: dict[str, Any]) -> dict[str, Any]:
    """Compute the reverse-import set of `files` at depth <= depth via real
    tools (dependency-cruiser for JS/TS, grimp for Python), then re-classify
    those adjacent files for breadth. Any breadth not already required is
    surfaced as `additional_breadths`."""
    has_js = any(f.endswith(_TS_EXTS) for f in files)
    has_py = any(f.endswith(".py") for f in files)
    if not (has_js or has_py):
        return {"tool": "skipped", "depth": depth, "additional_breadths": [],
                "note": "no JS/TS/Python files in changeset"}

    tool = "skipped"
    seen: set[str] = set()
    extra_notes: list[str] = []

    if has_js:
        rev = _depcruise_reverse_graph(repo)
        if rev:
            tool = "dependency-cruiser"
            frontier = {f for f in files if f.endswith(_TS_EXTS)}
            for _ in range(depth):
                next_frontier: set[str] = set()
                for f in frontier:
                    for imp in rev.get(f, []):
                        if imp not in seen and imp not in files:
                            next_frontier.add(imp)
                            seen.add(imp)
                frontier = next_frontier
                if not frontier:
                    break
        else:
            extra_notes.append("depcruise unavailable or returned empty graph")

    if has_py:
        py_files = [f for f in files if f.endswith(".py")]
        py_seen, py_notes = _grimp_reverse_set(repo, py_files, depth)
        if py_seen:
            tool = "grimp" if tool == "skipped" else f"{tool}+grimp"
            seen.update(py_seen)
        extra_notes.extend(py_notes)

    if not seen:
        return {"tool": tool, "depth": depth, "additional_breadths": [],
                "reverse_set_size": 0,
                "notes": extra_notes or ["no adjacent files at the configured depth"]}

    # Re-classify the adjacent set
    adj_breadths, adj_rationale = classify(sorted(seen), cfg)
    return {
        "tool": tool,
        "depth": depth,
        "reverse_set_size": len(seen),
        "reverse_set_sample": sorted(seen)[:5],
        "additional_breadths": sorted(adj_breadths),
        "additional_rationale": adj_rationale,
        "notes": extra_notes,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--base", default=None, help="Base ref for diff (default: working tree, falls back to merge-base)")
    ap.add_argument("--head", default=None, help="Head ref for diff (default: HEAD)")
    ap.add_argument("--out", default=None, type=Path)
    ap.add_argument("--format", choices=["json", "text"], default="json")
    args = ap.parse_args()

    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"error: --repo {repo} is not a directory", file=sys.stderr)
        return 1
    rc, _, _ = run(["git", "rev-parse", "--git-dir"], cwd=repo, timeout=5)
    if rc != 0:
        print(f"error: --repo {repo} is not a git repo", file=sys.stderr)
        return 1

    cfg = load_manifest_config()
    try:
        base, head, files = changed_files(repo, args.base, args.head)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    breadths, rationale = classify(files, cfg)
    adj = adjacent_escalation(repo, files, cfg["adjacent_depth"], cfg)
    # Merge adjacency-derived breadths into the required set
    for b in adj.get("additional_breadths", []):
        if b not in breadths:
            breadths.add(b)
            rationale.append({"breadth": b, "reason": f"adjacent escalation at depth <= {cfg['adjacent_depth']}"})

    record = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(repo),
        "base": base,
        "head": head,
        "changed_files": files,
        "required_breadths": sorted(breadths),
        "rationale": rationale,
        "adjacent_escalation": adj,
        "config_source": str(MANIFEST) if MANIFEST.is_file() else "defaults",
        "testing_standard": str(TESTING_STANDARD),
    }

    if args.format == "json":
        out = json.dumps(record, indent=2)
    else:
        lines = [
            f"# test_breadth_check  ({base}..{head})",
            f"changed files: {len(files)}",
            f"required breadths: {', '.join(sorted(breadths))}",
            "rationale:",
        ]
        for r in rationale:
            lines.append(f"  - {r['breadth']}: {r['reason']}")
        lines.append(f"adjacent escalation: tool={adj['tool']}, additional={adj['additional_breadths']}")
        out = "\n".join(lines)

    if args.out:
        args.out.write_text(out)
        print(f"wrote {args.out}")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
