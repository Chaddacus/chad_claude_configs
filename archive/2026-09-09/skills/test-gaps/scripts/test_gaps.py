#!/usr/bin/env python3
"""test-gaps — find files with low coverage that have changed recently.

Workflow:
  1. Detect runner via detect_runner.sh
  2. Run coverage (pytest --cov OR jest --coverage), tolerating test failures
  3. Parse per-file coverage % (pytest coverage.json OR jest coverage-summary.json)
  4. Read recently-modified source files via `git log --since`
  5. Cross-reference: bucket files by coverage and recency
  6. Emit Markdown report at ~/.claude/reports/test-gaps/{date}-{repo-slug}.md
     and a short summary on stdout

Two flavors of "uncovered":
  - tracked-no-coverage : file exists in git AND was modified recently AND
                          appears nowhere in the coverage report (no test
                          imports it). Highest priority.
  - below-threshold     : file appears in coverage report but coverage% is
                          under --threshold.

Usage:
  test_gaps.py [--repo PATH] [--threshold N] [--days N] [--no-run]
               [--out PATH] [--quiet]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_DIR / "scripts"
DEFAULT_REPORT_DIR = Path.home() / ".claude" / "reports" / "test-gaps"

SOURCE_EXTENSIONS = {
    "pytest": {".py"},
    "jest":   {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"},
}
TEST_PATH_HINTS = re.compile(r"(^|/)(tests?|__tests__|spec|fixtures?)/", re.IGNORECASE)
TEST_FILE_HINTS = re.compile(r"(^test_|_test\.|\.test\.|\.spec\.)", re.IGNORECASE)
EXCLUDE_PATH_HINTS = re.compile(
    r"(^|/)(node_modules|dist|build|\.venv|venv|__pycache__|\.next|coverage|target)/",
    re.IGNORECASE,
)


def run(cmd: list[str], cwd: Path, *, check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), check=check,
        capture_output=capture, text=True, timeout=600,
    )


def detect_runner(repo: Path) -> str:
    proc = run([str(SCRIPTS / "detect_runner.sh"), str(repo)], cwd=repo)
    return (proc.stdout or "").strip() or "unsupported"


def repo_slug(repo: Path) -> str:
    try:
        proc = run(["git", "remote", "get-url", "origin"], cwd=repo)
        url = (proc.stdout or "").strip()
        if url:
            slug = url.rstrip("/").split("/")[-1].removesuffix(".git")
            if slug:
                return slug
    except Exception:
        pass
    return repo.resolve().name or "repo"


def recent_files(repo: Path, days: int, runner: str) -> set[str]:
    """Return repo-relative source paths modified in the last N days."""
    try:
        proc = run(
            ["git", "log", f"--since={days} days ago", "--pretty=format:", "--name-only"],
            cwd=repo,
        )
    except Exception:
        return set()
    raw = {ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()}
    exts = SOURCE_EXTENSIONS.get(runner, set())

    def keep(path: str) -> bool:
        if not exts:
            return True
        if not any(path.endswith(e) for e in exts):
            return False
        if TEST_PATH_HINTS.search(path) or TEST_FILE_HINTS.search(path):
            return False
        if EXCLUDE_PATH_HINTS.search(path):
            return False
        return (repo / path).exists()

    return {p for p in raw if keep(p)}


# ---------------------------------------------------------------------------
# Coverage runners
# ---------------------------------------------------------------------------

def run_pytest(repo: Path) -> Path | None:
    """Run pytest --cov, tolerate test failures, return path to coverage.json."""
    out = repo / ".test-gaps-coverage.json"
    cmd = [
        "pytest",
        f"--cov={repo}",
        "--cov-report=json:" + str(out),
        "--cov-report=term-missing:skip-covered",
        "-q",
        "--no-header",
    ]
    try:
        run(cmd, cwd=repo, check=False)
    except FileNotFoundError:
        # Try uv run as fallback
        try:
            run(["uv", "run"] + cmd, cwd=repo, check=False)
        except Exception:
            return None
    except Exception:
        return None
    return out if out.exists() else None


def run_jest(repo: Path) -> Path | None:
    """Run jest/vitest with json-summary reporter."""
    out_dir = repo / "coverage"
    summary = out_dir / "coverage-summary.json"

    pkg = repo / "package.json"
    has_jest = pkg.exists() and '"jest"' in pkg.read_text(encoding="utf-8", errors="replace")
    has_vitest = pkg.exists() and '"vitest"' in pkg.read_text(encoding="utf-8", errors="replace")

    if has_vitest:
        cmd = ["npx", "vitest", "run", "--coverage",
               "--coverage.reporter=json-summary", "--coverage.reporter=text"]
    elif has_jest:
        cmd = ["npx", "jest", "--coverage",
               "--coverageReporters=json-summary", "--coverageReporters=text"]
    else:
        return None

    try:
        run(cmd, cwd=repo, check=False)
    except Exception:
        return None
    return summary if summary.exists() else None


def parse_pytest_coverage(path: Path, repo: Path) -> dict[str, float]:
    """Map repo-relative path -> covered %."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, float] = {}
    files = data.get("files", {}) or {}
    for fpath, info in files.items():
        try:
            rel = str(Path(fpath).resolve().relative_to(repo.resolve()))
        except Exception:
            rel = fpath
        pct = float((info.get("summary") or {}).get("percent_covered", 0.0))
        out[rel] = pct
    return out


def parse_jest_coverage(path: Path, repo: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, float] = {}
    for fpath, info in data.items():
        if fpath == "total":
            continue
        try:
            rel = str(Path(fpath).resolve().relative_to(repo.resolve()))
        except Exception:
            rel = fpath
        pct = float((info.get("lines") or {}).get("pct", 0.0))
        out[rel] = pct
    return out


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def bucket(coverage: dict[str, float], recent: set[str], threshold: float) -> dict[str, list]:
    cov_files = set(coverage.keys())
    tracked_no_cov = sorted(recent - cov_files)
    below = sorted(
        ((p, coverage[p]) for p in (recent & cov_files) if coverage[p] < threshold),
        key=lambda x: x[1],
    )
    above = sorted(
        ((p, coverage[p]) for p in (recent & cov_files) if coverage[p] >= threshold),
        key=lambda x: -x[1],
    )
    return {
        "tracked_no_coverage": tracked_no_cov,
        "below_threshold": below,
        "above_threshold": above,
    }


def render_markdown(slug: str, runner: str, threshold: float, days: int,
                    buckets: dict, recent_count: int, total_cov_files: int) -> str:
    today = dt.date.today().isoformat()
    lines: list[str] = []
    lines.append(f"# Test Gaps — {slug}")
    lines.append("")
    lines.append(f"_Generated: {today}_")
    lines.append(f"_Runner: `{runner}` · Threshold: {threshold:.0f}% · Recency window: {days} days_")
    lines.append(f"_Modified files in window: {recent_count} · Files in coverage report: {total_cov_files}_")
    lines.append("")

    tnc = buckets["tracked_no_coverage"]
    btr = buckets["below_threshold"]
    atr = buckets["above_threshold"]

    lines.append("## Priority 1 — Recently modified, NO test touches the file")
    lines.append("")
    if tnc:
        for p in tnc:
            lines.append(f"- `{p}`")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append(f"## Priority 2 — Recently modified, coverage below {threshold:.0f}%")
    lines.append("")
    if btr:
        lines.append("| File | Coverage |")
        lines.append("|---|---:|")
        for p, pct in btr:
            lines.append(f"| `{p}` | {pct:.1f}% |")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append(f"## Reference — Recently modified, coverage at or above {threshold:.0f}%")
    lines.append("")
    if atr:
        lines.append("| File | Coverage |")
        lines.append("|---|---:|")
        for p, pct in atr:
            lines.append(f"| `{p}` | {pct:.1f}% |")
    else:
        lines.append("_None._")
    lines.append("")
    return "\n".join(lines) + "\n"


def short_summary(slug: str, buckets: dict, threshold: float) -> str:
    tnc = len(buckets["tracked_no_coverage"])
    btr = len(buckets["below_threshold"])
    atr = len(buckets["above_threshold"])
    return (
        f"test-gaps · {slug}: "
        f"{tnc} no-coverage · {btr} below {threshold:.0f}% · {atr} healthy"
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.getcwd(), help="Path to repo root (default: cwd)")
    ap.add_argument("--threshold", type=float, default=60.0, help="Coverage %% below which a file is flagged (default 60)")
    ap.add_argument("--days", type=int, default=7, help="Recency window in days (default 7)")
    ap.add_argument("--no-run", action="store_true", help="Skip running coverage; reuse latest report file")
    ap.add_argument("--out", help="Override report output path")
    ap.add_argument("--quiet", action="store_true", help="Suppress per-step stderr noise")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if not repo.exists():
        sys.stderr.write(f"test-gaps: repo not found: {repo}\n")
        return 2

    runner = detect_runner(repo)
    if runner == "unsupported":
        sys.stderr.write(
            "test-gaps: could not detect a supported runner (pytest, jest/vitest). "
            "Add a pyproject.toml/pytest.ini/conftest.py for Python repos, "
            "or jest/vitest config in package.json for JS/TS.\n"
        )
        return 3

    coverage_path: Path | None = None
    if not args.no_run:
        if not args.quiet:
            sys.stderr.write(f"test-gaps: running {runner} coverage in {repo}...\n")
        coverage_path = run_pytest(repo) if runner == "pytest" else run_jest(repo)
    else:
        # Look for an existing report
        candidate = (
            repo / ".test-gaps-coverage.json" if runner == "pytest"
            else repo / "coverage" / "coverage-summary.json"
        )
        coverage_path = candidate if candidate.exists() else None

    if not coverage_path:
        sys.stderr.write(
            "test-gaps: no coverage report produced. "
            "Check that the runner can execute (deps installed, tests discoverable).\n"
        )
        return 4

    coverage = (
        parse_pytest_coverage(coverage_path, repo)
        if runner == "pytest"
        else parse_jest_coverage(coverage_path, repo)
    )
    if not coverage:
        sys.stderr.write(f"test-gaps: empty coverage report at {coverage_path}\n")
        return 5

    recent = recent_files(repo, args.days, runner)
    buckets = bucket(coverage, recent, args.threshold)

    md = render_markdown(
        slug=repo_slug(repo), runner=runner, threshold=args.threshold,
        days=args.days, buckets=buckets, recent_count=len(recent),
        total_cov_files=len(coverage),
    )

    if args.out:
        out_path = Path(args.out)
    else:
        DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = DEFAULT_REPORT_DIR / f"{dt.date.today().isoformat()}-{repo_slug(repo)}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")

    print(short_summary(repo_slug(repo), buckets, args.threshold))
    print(f"report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
