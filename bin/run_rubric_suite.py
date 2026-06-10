#!/usr/bin/env python3
"""run_rubric_suite.py — unified rubric pipeline (enterprise + security + design).

Runs three rubric scorers in parallel and emits a merged scorecard JSON.

Spec: ~/.claude/plans/users-chadsimon-thoughts-md-take-a-giggly-moore.md (slice 3)

Usage:
    run_rubric_suite.py --repo <path> [--strict] [--out <dir>]
                        [--rubric-bypass <reason>]

Output: `<repo>/.artifacts/rubric-suite/scorecard.json` (or `--out`).

Exit codes:
    0   — all three rubrics ran (or were bypassed); scorecard emitted
    2   — at least one rubric runner failed and no `--rubric-bypass` was set
    3   — repo invalid / IO error
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SUITE_VERSION = "1.0"
HOME = Path(os.path.expanduser("~"))
OPENSHIELD_HOME = Path(os.environ.get("OPENSHIELD_HOME", HOME / "code" / "openshield"))
DESIGN_SCORER = HOME / ".claude" / "bin" / "design_rubric_scorer.py"
ENTERPRISE_RUBRIC_SPEC = HOME / ".claude" / "standards" / "enterprise-maturity-rubric-generic.md"

BAND_ORDER = ["Foundational", "Developing", "Operational", "Enterprise-Ready",
              "Enterprise-Mature", "Enterprise-Design-Ready", "Enterprise-Design-Mature"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)


# ----------------------------------------------------------------------------
# Profile auto-detection (for openshield)
# ----------------------------------------------------------------------------


def detect_profile(repo: Path) -> str:
    """Return one of: ai-app, api, web-core. Default to ai-app on uncertainty."""
    ai_keywords = {"anthropic", "openai", "@anthropic-ai", "langchain", "llamaindex",
                   "@modelcontextprotocol", "mcp-server"}
    pkg = repo / "package.json"
    pyproj = repo / "pyproject.toml"
    reqs = repo / "requirements.txt"

    has_ai = False
    has_backend = False
    has_frontend = False

    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            keys = " ".join(deps.keys()).lower()
            if any(k in keys for k in ai_keywords):
                has_ai = True
            if any(k in keys for k in ("express", "fastify", "koa", "hono", "@nestjs")):
                has_backend = True
            if any(k in keys for k in ("react", "vue", "svelte", "next", "nuxt")):
                has_frontend = True
        except (json.JSONDecodeError, OSError):
            pass

    if pyproj.exists() or reqs.exists():
        for f in (pyproj, reqs):
            try:
                if f.exists():
                    txt = f.read_text().lower()
                    if any(k in txt for k in ai_keywords):
                        has_ai = True
                    if any(k in txt for k in ("fastapi", "flask", "django", "starlette")):
                        has_backend = True
            except OSError:
                pass

    if has_ai: return "ai-app"
    if has_frontend: return "web-core"
    if has_backend: return "api"
    return "ai-app"  # default


# ----------------------------------------------------------------------------
# Rubric runners
# ----------------------------------------------------------------------------


def run_enterprise(repo: Path, tmp: Path) -> dict[str, Any]:
    """Run the enterprise-maturity rubric via enterprise_rubric_scorer.py.

    Twelve-category filesystem-signal scorer modeled on design_rubric_scorer.py.
    Replaces the original `claude -p '/audit ...'` path which was structurally
    infeasible (slash commands don't work in non-interactive claude mode).
    """
    scorer = HOME / ".claude" / "bin" / "enterprise_rubric_scorer.py"
    if not scorer.exists():
        return _enterprise_fallback(repo)  # legacy path, retained as safety net

    out_path = tmp / "enterprise-scorecard.json"
    rc, _, err = run([sys.executable, str(scorer), "--repo", str(repo), "--out", str(out_path)],
                     timeout=600)
    if rc != 0 or not out_path.exists():
        return {"rubric": "enterprise", "ok": False,
                "error": f"enterprise scorer exit {rc}", "stderr_tail": err[-500:] if err else ""}
    try:
        scorecard = json.loads(out_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return {"rubric": "enterprise", "ok": False, "error": f"unparseable scorecard: {exc}"}
    return {"rubric": "enterprise", "ok": True, "via": "enterprise_rubric_scorer.py",
            "scorecard": scorecard}


def _enterprise_fallback(repo: Path) -> dict[str, Any]:
    """Heuristic enterprise-rubric scorer covering 4 of 12 categories from
    filesystem signals: testing, type-safety, cicd, documentation. Other 8
    categories emit `low` confidence placeholders."""
    # ---- testing (weight 10) ----
    tests_ok = False
    test_present = False
    if (repo / "package.json").exists():
        try:
            data = json.loads((repo / "package.json").read_text())
            if "test" in data.get("scripts", {}):
                test_present = True
                rc, _, _ = run(["npm", "test", "--silent", "--", "--run"], cwd=repo, timeout=300)
                tests_ok = rc == 0
        except (json.JSONDecodeError, OSError):
            pass
    elif (repo / "pyproject.toml").exists() or (repo / "setup.py").exists():
        test_present = bool(list(repo.glob("tests/**/test_*.py"))[:1] or list(repo.glob("**/test_*.py"))[:1])
        if test_present:
            rc, _, _ = run(["python3", "-m", "pytest", "-q"], cwd=repo, timeout=300)
            tests_ok = rc == 0
    testing_score = 5 if tests_ok else (3 if test_present else 1)

    # ---- type-safety (weight 5) ----
    typecheck_ok = False
    typed = False
    if (repo / "tsconfig.json").exists():
        typed = True
        rc, _, _ = run(["npx", "--no-install", "tsc", "--noEmit"], cwd=repo, timeout=120)
        typecheck_ok = rc == 0
    elif (repo / "pyproject.toml").exists():
        try:
            txt = (repo / "pyproject.toml").read_text()
            typed = "mypy" in txt or "pyright" in txt
        except OSError:
            pass
    type_score = 5 if (typed and typecheck_ok) else (3 if typed else 1)

    # ---- cicd (weight 9) ----
    ci_dirs = [(repo / ".github" / "workflows"), (repo / ".gitlab-ci.yml"),
               (repo / "circle.yml"), (repo / ".circleci" / "config.yml")]
    ci_present = any(p.exists() for p in ci_dirs)
    workflow_count = 0
    if (repo / ".github" / "workflows").is_dir():
        workflow_count = len(list((repo / ".github" / "workflows").glob("*.y*ml")))
    cicd_score = 5 if workflow_count >= 3 else (4 if workflow_count >= 1 else (3 if ci_present else 1))

    # ---- documentation (weight 8) ----
    has_readme = (repo / "README.md").exists() or (repo / "README.rst").exists()
    has_contrib = (repo / "CONTRIBUTING.md").exists()
    has_docs_dir = (repo / "docs").is_dir() or (repo / "documentation").is_dir()
    doc_signals = sum([has_readme, has_contrib, has_docs_dir])
    doc_score = {0: 1, 1: 2, 2: 4, 3: 5}[doc_signals]

    # ---- assemble categories ----
    auto_cats = [
        {"key": "testing", "name": "Test Strategy and Reliability", "score": testing_score, "weight": 10,
         "confidence": "high" if tests_ok else "medium",
         "metrics": {"test_script_present": test_present, "tests_ok": tests_ok}},
        {"key": "type-safety", "name": "Type and Schema Safety", "score": type_score, "weight": 5,
         "confidence": "high" if typecheck_ok else "medium",
         "metrics": {"typed": typed, "typecheck_ok": typecheck_ok}},
        {"key": "cicd", "name": "CI/CD and Build Governance", "score": cicd_score, "weight": 9,
         "confidence": "medium",
         "metrics": {"workflow_count": workflow_count, "ci_present": ci_present}},
        {"key": "documentation", "name": "Operational Documentation and Runbooks", "score": doc_score, "weight": 8,
         "confidence": "medium",
         "metrics": {"readme": has_readme, "contributing": has_contrib, "docs_dir": has_docs_dir}},
    ]
    placeholder_cats = [
        ("security", "Security", 14),
        ("api-contracts", "API Contracts and Boundary Validation", 10),
        ("data-integrity", "Database and Data Integrity", 10),
        ("observability", "Observability and Traceability", 9),
        ("separation", "Separation of Concerns", 7),
        ("clean-code", "Clean Code and Maintainability", 6),
        ("modularity", "Modularity and Extensibility", 6),
        ("error-handling", "Error Handling and Recovery", 6),
    ]
    for key, name, weight in placeholder_cats:
        auto_cats.append({"key": key, "name": name, "score": 3, "weight": weight,
                          "confidence": "low", "metrics": {}, "note": "manual evidence required"})

    confidence_mult = {"high": 1.00, "medium": 0.90, "low": 0.75}
    total_weight = sum(c["weight"] for c in auto_cats)
    raw_pct = sum((c["score"] / 5) * c["weight"] for c in auto_cats) * 100 / total_weight
    weighted_pct = sum((c["score"] / 5) * c["weight"] * confidence_mult[c["confidence"]]
                       for c in auto_cats) * 100 / total_weight

    return {
        "rubric": "enterprise",
        "ok": True,
        "via": "heuristic-scorer",
        "scorecard": {
            "rubricVersion": "1.0-heuristic",
            "rawPercent": round(raw_pct, 2),
            "confidenceWeightedPercent": round(weighted_pct, 2),
            "adjustedPercent": round(weighted_pct, 2),
            "maturityBand": _band_for(weighted_pct),
            "categories": auto_cats,
            "hardGates": [],
            "summary": {
                "typecheck_ok": typecheck_ok, "tests_ok": tests_ok,
                "auto_scored": 4, "placeholders": len(placeholder_cats),
            },
            "note": (
                "Enterprise rubric ran via filesystem-signal heuristic — slash commands "
                "don't work in `claude -p` mode, so the /audit skill cannot be invoked "
                "non-interactively. 4/12 categories auto-scored (testing, type-safety, "
                "cicd, documentation); 8 categories emit `low` confidence placeholders."
            ),
        },
    }


def run_security(repo: Path, tmp: Path) -> dict[str, Any]:
    cli = OPENSHIELD_HOME / "packages" / "cli" / "src" / "index.ts"
    if not cli.exists():
        return {"rubric": "security", "ok": False, "error": f"openshield CLI source missing at {cli}"}

    profile = detect_profile(repo)
    out_dir = tmp / "security-audit"
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "npx", "tsx", str(cli), "audit", str(repo),
        "--policy-pack", "default",
        "--profile", profile,
        "--mode", "full",
        "--format", "json",
        "--out", str(out_dir),
        "--offline",
        "--deterministic",
    ]
    # Auto-detect repo-local suppressions file. Honors openshield's standard
    # location (repo root) plus our convention under .artifacts/.
    # Also walks `git worktree list` to find the source repo when `repo` is a
    # worktree — .artifacts/ is gitignored so worktrees don't see it.
    candidates = [repo / "openshield-suppressions.json",
                  repo / ".artifacts" / "openshield-suppressions.json"]
    try:
        wt_out = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        if wt_out.returncode == 0:
            for line in wt_out.stdout.splitlines():
                if line.startswith("worktree "):
                    main_repo = Path(line.split(maxsplit=1)[1])
                    if main_repo != repo:
                        candidates.extend([
                            main_repo / "openshield-suppressions.json",
                            main_repo / ".artifacts" / "openshield-suppressions.json",
                        ])
                    break
    except Exception:  # noqa: BLE001
        pass
    for cand in candidates:
        if cand.is_file():
            cmd += ["--suppressions", str(cand)]
            break
    rc, out, err = run(cmd, cwd=OPENSHIELD_HOME, timeout=600)

    report = out_dir / "audit-report.json"
    if not report.exists():
        return {"rubric": "security", "ok": False,
                "error": f"openshield audit produced no report (exit={rc})",
                "stderr_tail": err[-500:] if err else ""}

    try:
        scorecard = json.loads(report.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return {"rubric": "security", "ok": False, "error": f"unparseable audit report: {exc}"}

    return {"rubric": "security", "ok": True, "via": "openshield-tsx", "profile": profile,
            "scorecard": scorecard}


def run_design(repo: Path, tmp: Path) -> dict[str, Any]:
    if not DESIGN_SCORER.exists():
        return {"rubric": "design", "ok": False, "error": f"design scorer missing at {DESIGN_SCORER}"}

    out_path = tmp / "design-scorecard.json"
    rc, out, err = run(["python3", str(DESIGN_SCORER), "--repo", str(repo), "--out", str(out_path)],
                       timeout=300)
    if rc != 0 or not out_path.exists():
        return {"rubric": "design", "ok": False,
                "error": f"design scorer exit {rc}", "stderr_tail": err[-500:]}
    try:
        scorecard = json.loads(out_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return {"rubric": "design", "ok": False, "error": f"unparseable design scorecard: {exc}"}
    return {"rubric": "design", "ok": True, "via": "design_rubric_scorer.py", "scorecard": scorecard}


# ----------------------------------------------------------------------------
# Merge
# ----------------------------------------------------------------------------


def _band_for(pct: float) -> str:
    if pct >= 90: return "Enterprise-Mature"
    if pct >= 80: return "Enterprise-Ready"
    if pct >= 65: return "Operational"
    if pct >= 50: return "Developing"
    return "Foundational"


def _band_index(b: str) -> int:
    try:
        return BAND_ORDER.index(b)
    except ValueError:
        return -1


def _extract_pct(scorecard: dict[str, Any]) -> float:
    """Pull adjusted percent from a scorecard, normalizing across camelCase + snake_case shapes."""
    def _pick(d: dict[str, Any]) -> float | None:
        for key in ("adjustedPercent", "adjusted_percent",
                    "confidenceWeightedPercent", "confidence_weighted_percent",
                    "rawPercent", "raw_percent"):
            if key in d and d[key] is not None:
                return float(d[key])
        return None

    val = _pick(scorecard)
    if val is not None:
        return val
    if "overall" in scorecard and isinstance(scorecard["overall"], dict):
        val = _pick(scorecard["overall"])
        if val is not None:
            return val
    # openshield-multi-track shape
    if "tracks" in scorecard and scorecard["tracks"]:
        vals = []
        for t in scorecard["tracks"]:
            v = _pick(t)
            if v is not None:
                vals.append(v)
        if vals:
            return sum(vals) / len(vals)
    return 0.0


def _extract_band(scorecard: dict[str, Any]) -> str:
    """Extract maturity band; normalize across camelCase + snake_case + lowercase variants."""
    def _pick(d: dict[str, Any]) -> str | None:
        for key in ("maturityBand", "maturity_band"):
            if key in d and d[key]:
                return str(d[key])
        return None

    band = _pick(scorecard)
    if band is None and isinstance(scorecard.get("overall"), dict):
        band = _pick(scorecard["overall"])
    if band is None and scorecard.get("tracks"):
        bands = [b for b in (_pick(t) for t in scorecard["tracks"]) if b]
        bands.sort(key=lambda b: _band_index(_normalize_band(b)))
        band = bands[0] if bands else None
    return _normalize_band(band) if band else "Foundational"


def _normalize_band(b: str) -> str:
    """Normalize band names; openshield emits lowercase like 'enterprise-mature'."""
    if not b:
        return "Foundational"
    m = {
        "foundational": "Foundational",
        "developing": "Developing",
        "operational": "Operational",
        "enterprise-ready": "Enterprise-Ready",
        "enterprise-mature": "Enterprise-Mature",
        "enterprise-design-ready": "Enterprise-Design-Ready",
        "enterprise-design-mature": "Enterprise-Design-Mature",
    }
    return m.get(b.lower().strip(), b)


def _extract_hard_gates(scorecard: dict[str, Any], rubric: str) -> list[dict[str, Any]]:
    gates: list[dict[str, Any]] = []
    if "hardGates" in scorecard:
        for g in scorecard["hardGates"]:
            gates.append({**g, "rubric": rubric})
    if "hard_gates" in scorecard:  # snake_case from openshield
        for g in scorecard["hard_gates"]:
            gates.append({**g, "rubric": rubric})
    return gates


def merge_results(results: dict[str, dict[str, Any]], bypasses: list[str]) -> dict[str, Any]:
    rubrics: dict[str, Any] = {}
    pcts: list[float] = []
    bands: list[str] = []
    all_gates: list[dict[str, Any]] = []

    for name in ("enterprise", "security", "design"):
        r = results.get(name)
        if not r or not r.get("ok"):
            rubrics[name] = {"status": "missing", "error": (r or {}).get("error", "not run")}
            continue

        sc = r["scorecard"]
        rubrics[name] = {"status": "ok", "via": r.get("via"), "scorecard": sc}
        pcts.append(_extract_pct(sc))
        bands.append(_extract_band(sc))
        all_gates.extend(_extract_hard_gates(sc, name))

    weighted_avg = round(sum(pcts) / len(pcts), 2) if pcts else 0.0
    sorted_bands = sorted(bands, key=_band_index)
    min_band = sorted_bands[0] if sorted_bands else "Foundational"
    max_band = sorted_bands[-1] if sorted_bands else "Foundational"

    any_critical_failed = any(
        (g.get("severity") == "critical" and g.get("status") == "fail") for g in all_gates
    )

    return {
        "rubricSuiteVersion": SUITE_VERSION,
        "generatedAt": utc_now(),
        "bypasses": bypasses,
        "rubrics": rubrics,
        "merged": {
            "minBand": min_band,
            "maxBand": max_band,
            "weightedAverage": weighted_avg,
            "anyCriticalGateFailed": any_critical_failed,
            "allHardGates": all_gates,
            "rubricCount": len(pcts),
        },
    }


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Run enterprise + security + design rubrics in parallel.")
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--strict", action="store_true",
                    help="Exit non-zero if any critical hard gate fails.")
    ap.add_argument("--rubric-bypass", default=None,
                    help="If set, missing/failing rubrics are recorded as bypasses instead of fatal.")
    args = ap.parse_args()

    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"error: --repo {repo} is not a directory", file=sys.stderr)
        return 3

    git_branch, git_commit = "unknown", "unknown"
    rc, b, _ = run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], timeout=5)
    if rc == 0: git_branch = b.strip()
    rc, c, _ = run(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=5)
    if rc == 0: git_commit = c.strip()

    bypasses: list[str] = []
    if args.rubric_bypass:
        bypasses.append(args.rubric_bypass)

    with tempfile.TemporaryDirectory(prefix="rubric-suite-") as td:
        tmp = Path(td)
        results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {
                ex.submit(run_enterprise, repo, tmp): "enterprise",
                ex.submit(run_security, repo, tmp): "security",
                ex.submit(run_design, repo, tmp): "design",
            }
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    results[name] = fut.result()
                except Exception as exc:
                    results[name] = {"rubric": name, "ok": False, "error": f"runner crash: {exc}"}

        merged = merge_results(results, bypasses)
        merged["branch"] = git_branch
        merged["commit"] = git_commit

    failed = [n for n, r in results.items() if not r.get("ok")]
    if failed and not args.rubric_bypass:
        print(f"error: rubric(s) failed without --rubric-bypass: {failed}", file=sys.stderr)
        for n in failed:
            print(f"  {n}: {results[n].get('error', '?')}", file=sys.stderr)
        return 2

    out_path = args.out or (repo / ".artifacts" / "rubric-suite" / "scorecard.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(merged, indent=2))

    m = merged["merged"]
    print(f"rubric suite: weighted_avg={m['weightedAverage']}% min={m['minBand']} max={m['maxBand']} "
          f"critical_gate_failed={m['anyCriticalGateFailed']} rubrics={m['rubricCount']}/3 "
          f"→ {out_path}")
    if failed:
        print(f"  (bypassed failures: {failed} — bypass reason: {args.rubric_bypass})")

    if args.strict and m["anyCriticalGateFailed"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
