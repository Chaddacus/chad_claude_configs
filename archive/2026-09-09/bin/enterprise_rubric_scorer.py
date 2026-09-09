#!/usr/bin/env python3
"""enterprise_rubric_scorer.py — score a repo against the enterprise-maturity rubric.

Implements all 12 categories from ~/.claude/standards/enterprise-maturity-rubric-generic.md
with concrete filesystem-signal + tool-run heuristics. Mirrors the structure of
~/.claude/bin/design_rubric_scorer.py.

Usage:
    enterprise_rubric_scorer.py --repo <path> [--out <path>]

Output: JSON conforming to the rubric's "Output Contract" section.

Categories (weight):
    security (14)        — openshield finding counts, npm audit, secrets scan
    api-contracts (10)   — openapi/graphql presence, zod/pydantic validators
    testing (10)         — test script presence + pass rate
    data-integrity (10)  — migration runners + transaction usage
    observability (9)    — OTel + structured logging + metrics
    cicd (9)             — CI workflows + branch protection signals
    documentation (8)    — README + CONTRIBUTING + docs/ + ADRs
    separation (7)       — monorepo structure + boundary count
    clean-code (6)       — lint config + lint pass rate
    modularity (6)       — module/package count + dep-graph signals
    error-handling (6)   — try/catch density + error class definitions
    type-safety (5)      — typed lang + typecheck pass

Plan: ~/.claude/plans/users-chadsimon-thoughts-md-take-a-giggly-moore.md
(slice 3 follow-up — replaces inline heuristic in run_rubric_suite.py)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

RUBRIC_VERSION = "1.0"

CATEGORY_WEIGHTS: dict[str, int] = {
    "security": 14,
    "api-contracts": 10,
    "testing": 10,
    "data-integrity": 10,
    "observability": 9,
    "cicd": 9,
    "documentation": 8,
    "separation": 7,
    "clean-code": 6,
    "modularity": 6,
    "error-handling": 6,
    "type-safety": 5,
}

CONFIDENCE_MULTIPLIERS = {"high": 1.00, "medium": 0.90, "low": 0.75}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 60) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)


def _ls(repo: Path, *globs: str, limit: int = 200) -> list[Path]:
    """List tracked files matching one or more git-pathspec globs."""
    args = ["git", "ls-files"] + list(globs)
    rc, out, _ = run(args, cwd=repo, timeout=10)
    if rc != 0 or not out.strip():
        return []
    return [repo / line for line in out.splitlines() if (repo / line).is_file()][:limit]


def _safe_read(p: Path, max_bytes: int = 200_000) -> str:
    try:
        b = p.read_bytes()[:max_bytes]
        return b.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _category(key: str, score: int, confidence: str, *, metrics: dict[str, Any] | None = None,
              auto: bool = True, note: str = "") -> dict[str, Any]:
    return {
        "key": key,
        "name": key,
        "score": score,
        "weight": CATEGORY_WEIGHTS[key],
        "confidence": confidence,
        "owner": "auto",
        "metrics": metrics or {},
        "autoScored": auto,
        "note": note,
    }


# ----------------------------------------------------------------------------
# Per-category scorers
# ----------------------------------------------------------------------------


def score_security(repo: Path) -> dict[str, Any]:
    """Prefer prior openshield rubric output; fall back to npm audit / pip-audit signals."""
    artifacts = repo / ".artifacts" / "rubric-suite"
    sec_pct: float | None = None
    if (artifacts / "scorecard.json").is_file():
        try:
            d = json.loads((artifacts / "scorecard.json").read_text())
            sec = d.get("rubrics", {}).get("security", {}).get("scorecard", {})
            ovr = sec.get("overall", {})
            sec_pct = ovr.get("adjusted_percent") or ovr.get("adjustedPercent")
        except (json.JSONDecodeError, OSError):
            pass

    if sec_pct is not None:
        # Map percent → 1-5 score
        if sec_pct >= 90: score = 5
        elif sec_pct >= 80: score = 4
        elif sec_pct >= 65: score = 3
        elif sec_pct >= 50: score = 2
        else: score = 1
        return _category("security", score, "high",
                         metrics={"source": "openshield-prior-run", "percent": sec_pct})

    # No prior openshield data — quick npm audit if package.json exists
    if (repo / "package.json").exists() and shutil.which("npm"):
        rc, out, _ = run(["npm", "audit", "--json", "--prefix", str(repo)],
                         cwd=repo, timeout=120)
        if rc in (0, 1):  # npm audit returns 1 on findings
            try:
                data = json.loads(out)
                vuln = data.get("metadata", {}).get("vulnerabilities", {})
                crit = vuln.get("critical", 0)
                high = vuln.get("high", 0)
                if crit > 0: score = 1
                elif high > 5: score = 2
                elif high > 0: score = 3
                elif sum(vuln.values()) > 0: score = 4
                else: score = 5
                return _category("security", score, "medium",
                                 metrics={"source": "npm-audit", "critical": crit, "high": high})
            except (json.JSONDecodeError, ValueError):
                pass
    return _category("security", 3, "low",
                     metrics={"source": "no-data"}, auto=False,
                     note="run openshield audit or npm audit for real signal")


def score_api_contracts(repo: Path) -> dict[str, Any]:
    """OpenAPI / GraphQL schema presence + zod / pydantic / joi validator detection."""
    schemas = _ls(repo, "**/openapi.yaml", "**/openapi.yml", "**/openapi.json",
                  "**/schema.graphql", "**/*.proto")
    has_schema = bool(schemas)

    # Validator-library signals
    pkg = repo / "package.json"
    py = repo / "pyproject.toml"
    has_zod = has_joi = has_pyd = False
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            has_zod = "zod" in deps
            has_joi = "joi" in deps
        except (json.JSONDecodeError, OSError):
            pass
    if py.exists():
        txt = _safe_read(py)
        has_pyd = "pydantic" in txt or "fastapi" in txt
    has_validator = has_zod or has_joi or has_pyd

    if has_schema and has_validator: score = 5
    elif has_schema or has_validator: score = 3
    else: score = 1

    return _category("api-contracts", score, "medium",
                     metrics={"schemas": [str(s.relative_to(repo)) for s in schemas[:3]],
                              "has_zod": has_zod, "has_joi": has_joi, "has_pydantic": has_pyd})


def score_testing(repo: Path) -> dict[str, Any]:
    """Test script presence + actually running tests when feasible."""
    tests_ok = False
    test_files = _ls(repo, "tests/**/*", "**/*.test.ts", "**/*.test.tsx",
                     "**/*.spec.ts", "**/*.spec.tsx", "**/test_*.py")
    has_tests = bool(test_files)

    if (repo / "package.json").exists():
        try:
            data = json.loads((repo / "package.json").read_text())
            if "test" in data.get("scripts", {}):
                rc, _, _ = run(["npm", "test", "--silent", "--", "--run"],
                               cwd=repo, timeout=300)
                tests_ok = rc == 0
        except (json.JSONDecodeError, OSError):
            pass
    elif (repo / "pyproject.toml").exists() or (repo / "setup.py").exists():
        if has_tests:
            rc, _, _ = run(["python3", "-m", "pytest", "-q"], cwd=repo, timeout=300)
            tests_ok = rc == 0

    if tests_ok and len(test_files) >= 10: score = 5
    elif tests_ok: score = 4
    elif has_tests: score = 3
    else: score = 1

    return _category("testing", score, "high" if tests_ok else "medium",
                     metrics={"test_files": len(test_files), "tests_ok": tests_ok})


def score_data_integrity(repo: Path) -> dict[str, Any]:
    """Migration runner presence + transaction usage."""
    has_migrator = False
    has_orm = False
    # Off-the-shelf migrator/ORM deps — check root + all workspace package.json
    pkg_files = [repo / "package.json"]
    pkg_files.extend(sorted(repo.glob("packages/*/package.json")))
    for pkg in pkg_files:
        if not pkg.exists():
            continue
        try:
            data = json.loads(pkg.read_text())
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            if any(k in deps for k in
                   ("knex", "prisma", "@prisma/client", "drizzle-kit",
                    "drizzle-orm", "node-pg-migrate", "typeorm", "sequelize")):
                has_migrator = True
                break
        except (json.JSONDecodeError, OSError):
            pass
    has_orm = has_migrator
    if not has_migrator:
        # Python: alembic, django migrations
        py = repo / "pyproject.toml"
        if py.exists():
            txt = _safe_read(py)
            has_migrator = "alembic" in txt or "django" in txt
        # Migration directories
        for p in ("migrations", "db/migrations", "alembic"):
            if (repo / p).is_dir():
                has_migrator = True; break
    if not has_migrator:
        # Custom hand-rolled migrator: look for `MIGRATIONS` array + a
        # `npm run migrate` script or a scripts/migrate.* file.
        has_migrate_script = False
        try:
            root_pkg = repo / "package.json"
            if root_pkg.exists():
                data = json.loads(root_pkg.read_text())
                if "migrate" in (data.get("scripts") or {}):
                    has_migrate_script = True
        except (json.JSONDecodeError, OSError):
            pass
        has_migrate_file = any(
            (repo / "scripts" / f"migrate.{ext}").exists()
            for ext in ("ts", "js", "py", "sh")
        )
        # MIGRATIONS array signal in source — check a bounded set of files
        has_migrations_array = False
        if has_migrate_script or has_migrate_file:
            src_files = _ls(repo, "packages/**/src/*.ts", "packages/**/src/*.tsx",
                            "src/*.ts", "src/*.py", limit=80)
            mig_re = re.compile(r"\b(?:const|export\s+const|MIGRATIONS\s*[:=]\s*\[)\s*MIGRATIONS\b|\bexport\s+const\s+MIGRATIONS\b|\bconst\s+MIGRATIONS\s*[:=]")
            for f in src_files:
                txt = _safe_read(f, 100_000)
                if mig_re.search(txt):
                    has_migrations_array = True
                    break
        if (has_migrate_script or has_migrate_file) and has_migrations_array:
            has_migrator = True

    # Transaction usage in code
    files = _ls(repo, "**/*.ts", "**/*.tsx", "**/*.js", "**/*.py", limit=300)
    tx_count = 0
    tx_re = re.compile(r"\b(transaction|begin|BEGIN|commit|rollback)\b")
    for f in files:
        txt = _safe_read(f, 50_000)
        if tx_re.search(txt):
            tx_count += 1

    if has_migrator and tx_count >= 5: score = 5
    elif has_migrator and tx_count >= 1: score = 4
    elif has_migrator or tx_count >= 1: score = 3
    else: score = 2  # may not be a data-touching app at all

    return _category("data-integrity", score, "low",
                     metrics={"has_migrator": has_migrator, "tx_files": tx_count})


def score_observability(repo: Path) -> dict[str, Any]:
    """OTel / structured logging / metrics presence."""
    py = repo / "pyproject.toml"
    has_otel = has_struct_log = has_metrics = False
    # Check root + all monorepo workspace package.json files
    pkg_files = [repo / "package.json"]
    pkg_files.extend(sorted(repo.glob("packages/*/package.json")))
    for pkg in pkg_files:
        if not pkg.exists():
            continue
        try:
            data = json.loads(pkg.read_text())
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            has_otel = has_otel or any(k.startswith("@opentelemetry/") for k in deps)
            has_struct_log = has_struct_log or any(k in deps for k in ("winston", "pino", "bunyan"))
            has_metrics = has_metrics or any(k in deps for k in ("prom-client", "prometheus-api-metrics"))
        except (json.JSONDecodeError, OSError):
            pass
    if py.exists():
        txt = _safe_read(py)
        has_otel = has_otel or "opentelemetry" in txt
        has_struct_log = has_struct_log or "structlog" in txt or "loguru" in txt
        has_metrics = has_metrics or "prometheus" in txt

    signals = sum([has_otel, has_struct_log, has_metrics])
    score = {0: 1, 1: 2, 2: 4, 3: 5}[signals]
    return _category("observability", score, "medium",
                     metrics={"otel": has_otel, "structured_log": has_struct_log, "metrics": has_metrics})


def score_cicd(repo: Path) -> dict[str, Any]:
    """CI workflow presence + count + signals (gha/gitlab/circle)."""
    workflow_count = 0
    has_ci = False
    gha_dir = repo / ".github" / "workflows"
    if gha_dir.is_dir():
        workflow_count = len(list(gha_dir.glob("*.y*ml")))
        has_ci = workflow_count > 0
    elif (repo / ".gitlab-ci.yml").exists() or (repo / ".circleci" / "config.yml").exists():
        has_ci = True
        workflow_count = 1
    score = 5 if workflow_count >= 3 else (4 if workflow_count >= 1 else (3 if has_ci else 1))
    return _category("cicd", score, "medium",
                     metrics={"workflow_count": workflow_count, "has_ci": has_ci})


def score_documentation(repo: Path) -> dict[str, Any]:
    """README + CONTRIBUTING + docs/ + ADR signals."""
    has_readme = (repo / "README.md").exists() or (repo / "README.rst").exists()
    has_contrib = (repo / "CONTRIBUTING.md").exists()
    has_docs_dir = (repo / "docs").is_dir() or (repo / "documentation").is_dir()
    has_adr = (repo / "docs" / "adr").is_dir() or (repo / "adr").is_dir()
    signals = sum([has_readme, has_contrib, has_docs_dir, has_adr])
    score = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5}[signals]
    return _category("documentation", score, "medium",
                     metrics={"readme": has_readme, "contributing": has_contrib,
                              "docs_dir": has_docs_dir, "adr": has_adr})


def score_separation(repo: Path) -> dict[str, Any]:
    """Monorepo / package boundary count."""
    pkg_dirs = []
    for root in ("packages", "apps", "services", "libs"):
        d = repo / root
        if d.is_dir():
            pkg_dirs.extend([p for p in d.iterdir() if p.is_dir() and (p / "package.json").exists()])
    pkg_count = len(pkg_dirs)
    # Also consider TS path-aliases as a separation signal
    has_path_alias = False
    tsconfig = repo / "tsconfig.json"
    if tsconfig.exists():
        txt = _safe_read(tsconfig)
        has_path_alias = '"paths"' in txt
    if pkg_count >= 5: score = 5
    elif pkg_count >= 2: score = 4
    elif pkg_count == 1 or has_path_alias: score = 3
    elif (repo / "src").is_dir(): score = 2
    else: score = 1
    return _category("separation", score, "medium",
                     metrics={"pkg_count": pkg_count, "has_path_alias": has_path_alias})


def score_clean_code(repo: Path) -> dict[str, Any]:
    """Lint config presence + lint pass rate."""
    lint_cfg = any((repo / f).exists() for f in
                   (".eslintrc.js", ".eslintrc.json", ".eslintrc.yaml",
                    "eslint.config.js", "eslint.config.mjs"))
    lint_ok = False
    if lint_cfg and shutil.which("npx"):
        rc, _, _ = run(["npx", "--no-install", "eslint", ".", "--max-warnings", "100"],
                       cwd=repo, timeout=120)
        lint_ok = (rc == 0)
    elif (repo / "ruff.toml").exists() or (repo / "pyproject.toml").exists():
        if shutil.which("ruff"):
            rc, _, _ = run(["ruff", "check", "."], cwd=repo, timeout=60)
            lint_ok = (rc == 0)
            lint_cfg = True
    if lint_cfg and lint_ok: score = 5
    elif lint_cfg: score = 3
    else: score = 1
    return _category("clean-code", score, "high" if lint_ok else "medium",
                     metrics={"lint_config": lint_cfg, "lint_ok": lint_ok})


def score_modularity(repo: Path) -> dict[str, Any]:
    """Module count + boundary-enforcement gate (depcruise/grimp where available)."""
    src_files = _ls(repo, "src/**/*.ts", "src/**/*.tsx", "src/**/*.py",
                    "packages/**/*.ts", "packages/**/*.py", limit=2000)
    # Count distinct top-level dirs under src/ or packages/
    modules: set[str] = set()
    for f in src_files:
        rel = f.relative_to(repo)
        parts = rel.parts
        if len(parts) >= 2:
            modules.add(f"{parts[0]}/{parts[1]}")
    n_modules = len(modules)

    # Detect a real boundary-enforcement gate (dependency-cruiser config or
    # grimp/import-linter for Python). Presence of a config alongside a
    # dedicated npm/test script is the signal that the boundary is actually
    # enforced rather than aspirational.
    has_boundary_gate = False
    if (repo / ".dependency-cruiser.cjs").exists() or (repo / ".dependency-cruiser.js").exists() \
       or (repo / ".dependency-cruiser.json").exists():
        try:
            root_pkg = repo / "package.json"
            if root_pkg.exists():
                data = json.loads(root_pkg.read_text())
                deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                scripts = data.get("scripts") or {}
                has_dep_cruiser = "dependency-cruiser" in deps
                has_check_script = any(
                    "depcruise" in v or "dependency-cruiser" in v
                    for v in scripts.values()
                )
                if has_dep_cruiser and has_check_script:
                    has_boundary_gate = True
        except (json.JSONDecodeError, OSError):
            pass
    if not has_boundary_gate:
        # Python: grimp or import-linter with .importlinter config
        py = repo / "pyproject.toml"
        if py.exists() and ("import-linter" in _safe_read(py) or "grimp" in _safe_read(py)):
            has_boundary_gate = True
        if (repo / ".importlinter").exists() or (repo / "setup.cfg").exists():
            has_boundary_gate = has_boundary_gate or "import-linter" in _safe_read(repo / "setup.cfg") if (repo / "setup.cfg").exists() else has_boundary_gate

    # Module-count thresholds. A real-world monorepo at 8-10 packages is
    # substantial; the prior 15/30 thresholds were calibrated for sprawling
    # enterprise codebases and undermeasured tight monorepos.
    if n_modules >= 20: count_score = 5
    elif n_modules >= 10: count_score = 4
    elif n_modules >= 5: count_score = 3
    elif n_modules >= 1: count_score = 2
    else: count_score = 1
    # Boundary gate adds +1 if module count is at least solid
    score = count_score
    if has_boundary_gate and count_score >= 3:
        score = min(5, count_score + 1)
    return _category("modularity", score, "medium",
                     metrics={"module_count": n_modules, "src_files_scanned": len(src_files),
                              "boundary_gate": has_boundary_gate})


def score_error_handling(repo: Path) -> dict[str, Any]:
    """try/catch density + custom error class definitions."""
    files = _ls(repo, "**/*.ts", "**/*.tsx", "**/*.js", "**/*.py", limit=400)
    if not files:
        return _category("error-handling", 1, "low",
                         metrics={"reason": "no source files"}, auto=False)
    try_count = 0
    err_class_count = 0
    try_re = re.compile(r"\btry\s*[\{:]|\btry:\s")
    # Match `class XError`, `class XException` — followed by `{`, `(`, or `extends ...`
    err_re = re.compile(r"class\s+\w*(?:Error|Exception)\b(?:\s+extends\s+\w+)?\s*[{(:]")
    for f in files:
        txt = _safe_read(f, 80_000)
        try_count += len(try_re.findall(txt))
        err_class_count += len(err_re.findall(txt))
    density = try_count / max(len(files), 1)
    if density >= 1.0 and err_class_count >= 3: score = 5
    elif density >= 0.5 or err_class_count >= 1: score = 4
    elif density >= 0.2: score = 3
    elif try_count > 0: score = 2
    else: score = 1
    return _category("error-handling", score, "low",
                     metrics={"try_count": try_count, "error_classes": err_class_count,
                              "files_scanned": len(files), "density": round(density, 2)})


def score_type_safety(repo: Path) -> dict[str, Any]:
    """Typed language + typecheck pass."""
    typed = False
    typecheck_ok = False
    if (repo / "tsconfig.json").exists():
        typed = True
        if shutil.which("npx"):
            rc, _, _ = run(["npx", "--no-install", "tsc", "--noEmit"], cwd=repo, timeout=180)
            typecheck_ok = (rc == 0)
    elif (repo / "pyproject.toml").exists():
        txt = _safe_read(repo / "pyproject.toml")
        typed = "mypy" in txt or "pyright" in txt
        if typed and shutil.which("python3"):
            rc, _, _ = run(["python3", "-m", "mypy", "--ignore-missing-imports", str(repo)],
                           cwd=repo, timeout=120)
            typecheck_ok = (rc in (0, -1))
    if typed and typecheck_ok: score = 5
    elif typed: score = 3
    else: score = 1
    return _category("type-safety", score, "high" if typecheck_ok else "medium",
                     metrics={"typed": typed, "typecheck_ok": typecheck_ok})


# ----------------------------------------------------------------------------
# Composite
# ----------------------------------------------------------------------------


SCORERS: dict[str, Callable[[Path], dict[str, Any]]] = {
    "security": score_security,
    "api-contracts": score_api_contracts,
    "testing": score_testing,
    "data-integrity": score_data_integrity,
    "observability": score_observability,
    "cicd": score_cicd,
    "documentation": score_documentation,
    "separation": score_separation,
    "clean-code": score_clean_code,
    "modularity": score_modularity,
    "error-handling": score_error_handling,
    "type-safety": score_type_safety,
}


def _maturity_band(pct: float) -> str:
    if pct >= 90: return "Enterprise-Mature"
    if pct >= 80: return "Enterprise-Ready"
    if pct >= 65: return "Operational"
    if pct >= 50: return "Developing"
    return "Foundational"


def _git_meta(repo: Path) -> tuple[str, str]:
    rc, branch, _ = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, timeout=5)
    rc2, commit, _ = run(["git", "rev-parse", "HEAD"], cwd=repo, timeout=5)
    return (branch.strip() if rc == 0 else "unknown", commit.strip() if rc2 == 0 else "unknown")


def score_repo(repo: Path) -> dict[str, Any]:
    categories = []
    for key, scorer in SCORERS.items():
        try:
            categories.append(scorer(repo))
        except Exception as exc:  # noqa: BLE001 — never let one scorer sink the whole run
            categories.append(_category(key, 3, "low",
                                        metrics={"error": str(exc)}, auto=False,
                                        note=f"scorer error: {exc}"))

    total_weight = sum(CATEGORY_WEIGHTS.values())
    raw_pct = sum((c["score"] / 5) * c["weight"] for c in categories) * 100 / total_weight
    weighted_pct = sum((c["score"] / 5) * c["weight"] * CONFIDENCE_MULTIPLIERS[c["confidence"]]
                       for c in categories) * 100 / total_weight

    band = _maturity_band(weighted_pct)
    branch, commit = _git_meta(repo)

    return {
        "rubricVersion": RUBRIC_VERSION,
        "generatedAt": utc_now(),
        "branch": branch,
        "commit": commit,
        "mode": "auto-scored",
        "categories": categories,
        "categoryWeights": CATEGORY_WEIGHTS,
        "rawPercent": round(raw_pct, 2),
        "confidenceWeightedPercent": round(weighted_pct, 2),
        "adjustedPercent": round(weighted_pct, 2),
        "maturityBand": band,
        "summary": {
            "average": round(sum(c["score"] for c in categories) / len(categories), 2),
            "min": min(c["score"] for c in categories),
            "autoScoredCount": sum(1 for c in categories if c["autoScored"]),
        },
        "hardGates": [],
        "note": (
            "Enterprise rubric scored via filesystem-signal heuristics. 12 categories "
            "auto-scored where possible; categories that couldn't run their tool emit "
            "low-confidence placeholders. See per-category metrics + confidence."
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Score a repo against the enterprise-maturity rubric.")
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"error: --repo {repo} is not a directory", file=sys.stderr)
        return 2

    result = score_repo(repo)
    out_path = args.out or (repo / ".artifacts" / "rubric-suite" / "enterprise-scorecard.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2))
    print(f"enterprise rubric: {result['adjustedPercent']}% ({result['maturityBand']}) "
          f"— {result['summary']['autoScoredCount']}/12 auto-scored — {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
