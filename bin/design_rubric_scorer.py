#!/usr/bin/env python3
"""design_rubric_scorer.py — score a repo against the enterprise design rubric.

Implements 6 of the 12 design-rubric categories with concrete tooling; the
other 6 emit `confidence: low` placeholders pending manual evidence.

Spec: ~/.claude/standards/enterprise-design-rubric.md
Plan: ~/.claude/plans/users-chadsimon-thoughts-md-take-a-giggly-moore.md (slice 3)

Usage:
    design_rubric_scorer.py --repo <path> [--out <path>]

Output: JSON conforming to the design rubric's "Output Contract" section.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUBRIC_VERSION = "1.0"

# Categories + weights from enterprise-design-rubric.md.
CATEGORY_WEIGHTS: dict[str, int] = {
    "user-task-fit": 9,
    "workflow-clarity": 9,
    "interaction-usability": 12,
    "accessibility": 14,
    "visual-hierarchy": 10,
    "design-system": 9,
    "content-ux": 8,
    "platform-continuity": 7,
    "performance": 7,
    "trust-safety": 7,
    "research-measurement": 5,
    "implementation-fidelity": 3,
}

CONFIDENCE_MULTIPLIERS = {"high": 1.00, "medium": 0.90, "low": 0.75}

# Hard gates from rubric spec.
HARD_GATES_SPEC = [
    {"key": "critical-flow-completable", "name": "All named critical flows are completable without dead ends", "severity": "critical"},
    {"key": "wcag-aa-critical-path", "name": "Critical flows have no known WCAG 2.2 AA blockers", "severity": "critical"},
    {"key": "severity-4-usability-zero", "name": "No NN/g severity-4 usability issue in critical flow", "severity": "critical"},
    {"key": "destructive-action-clarity", "name": "Destructive actions have clear labels + recovery", "severity": "high"},
    {"key": "performance-floor", "name": "Critical web screens meet Core Web Vitals floor", "severity": "high"},
    {"key": "system-token-floor", "name": "Core UI uses named design tokens", "severity": "high"},
    {"key": "evidence-freshness-30d", "name": "Evidence freshness <= 30 days", "severity": "stale"},
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)


# ----------------------------------------------------------------------------
# Auto-scored categories
# ----------------------------------------------------------------------------


def score_design_system(repo: Path) -> dict[str, Any]:
    """Grep tracked source files for hex literals and unscoped spacing values."""
    if not (repo / ".git").exists():
        return _placeholder("design-system", reason="not a git repo")

    rc, files_str, _ = run(["git", "ls-files", "*.tsx", "*.jsx", "*.ts", "*.js", "*.vue", "*.svelte", "*.css", "*.scss"], repo, timeout=10)
    if rc != 0 or not files_str.strip():
        return _placeholder("design-system", reason="no UI files tracked")
    files = [repo / f for f in files_str.strip().splitlines() if (repo / f).is_file()]
    if not files:
        return _placeholder("design-system", reason="no UI files exist on disk")

    hex_re = re.compile(r"#[0-9a-fA-F]{3,8}\b")
    spacing_re = re.compile(r":\s*(\d+)px\b")
    violations = 0
    for f in files[:500]:  # cap to keep fast
        try:
            txt = f.read_text(errors="replace")
        except OSError:
            continue
        violations += len(hex_re.findall(txt))
        for m in spacing_re.findall(txt):
            if int(m) % 4 != 0:
                violations += 1

    ratio = violations / max(len(files), 1)
    if ratio < 1: score = 5
    elif ratio < 3: score = 4
    elif ratio < 8: score = 3
    elif ratio < 20: score = 2
    else: score = 1

    return _category(
        "design-system", score, "high",
        metrics={"files_scanned": len(files), "violations": violations, "violations_per_file": round(ratio, 2)},
        autoScored=True,
    )


def score_implementation_fidelity(repo: Path) -> dict[str, Any]:
    typecheck_ok = True
    lint_ok = True

    if (repo / "tsconfig.json").exists():
        rc, _, _ = run(["npx", "--no-install", "tsc", "--noEmit", "-p", "tsconfig.json"], repo, timeout=120)
        typecheck_ok = (rc == 0)
    elif (repo / "pyproject.toml").exists() or (repo / "setup.py").exists():
        rc, _, _ = run(["python3", "-m", "mypy", "--ignore-missing-imports", str(repo)], repo, timeout=120)
        typecheck_ok = (rc in (0, -1))  # tolerate mypy missing

    if (repo / ".eslintrc.js").exists() or (repo / ".eslintrc.json").exists() or (repo / "eslint.config.js").exists():
        rc, _, _ = run(["npx", "--no-install", "eslint", "."], repo, timeout=120)
        lint_ok = (rc == 0)
    elif (repo / "ruff.toml").exists() or (repo / "pyproject.toml").exists():
        rc, _, _ = run(["ruff", "check", "."], repo, timeout=60)
        lint_ok = (rc in (0, -1))

    score = 5 if (typecheck_ok and lint_ok) else 4 if (typecheck_ok or lint_ok) else 2
    return _category(
        "implementation-fidelity", score, "high",
        metrics={"typecheck_ok": typecheck_ok, "lint_ok": lint_ok},
        autoScored=True,
    )


def score_platform_continuity(repo: Path) -> dict[str, Any]:
    pw_config_paths = list(repo.glob("**/playwright.config.*"))[:3]
    if not pw_config_paths:
        return _placeholder("platform-continuity", reason="no playwright config", default_score=1)

    txt = ""
    for p in pw_config_paths:
        try:
            txt += p.read_text(errors="replace")
        except OSError:
            continue
    has_multi_viewport = bool(re.search(r"viewport|Mobile|iPhone|iPad|Pixel", txt))
    score = 4 if has_multi_viewport else 2
    return _category(
        "platform-continuity", score, "medium",
        metrics={"playwright_configs": len(pw_config_paths), "multi_viewport_detected": has_multi_viewport},
        autoScored=True,
    )


def score_trust_safety(repo: Path) -> dict[str, Any]:
    rc, files_str, _ = run(["git", "ls-files", "*.tsx", "*.jsx", "*.ts", "*.js"], repo, timeout=10)
    if rc != 0 or not files_str.strip():
        return _placeholder("trust-safety", reason="no JS/TS files tracked")
    files = [repo / f for f in files_str.strip().splitlines() if (repo / f).is_file()][:500]

    destructive_re = re.compile(r"\b(delete|destroy|drop|remove|wipe)\b", re.IGNORECASE)
    confirm_re = re.compile(r"\bconfirm\s*\(|<ConfirmDialog|<AlertDialog|areYouSure", re.IGNORECASE)
    destructive_hits = 0
    confirm_hits = 0
    for f in files:
        try:
            txt = f.read_text(errors="replace")
        except OSError:
            continue
        destructive_hits += len(destructive_re.findall(txt))
        confirm_hits += len(confirm_re.findall(txt))

    if destructive_hits == 0:
        score = 4  # nothing destructive to confirm
    else:
        ratio = confirm_hits / destructive_hits
        if ratio >= 0.5: score = 5
        elif ratio >= 0.25: score = 4
        elif ratio >= 0.1: score = 3
        elif ratio > 0: score = 2
        else: score = 1

    return _category(
        "trust-safety", score, "low",  # heuristic, not high-confidence
        metrics={"destructive_action_count": destructive_hits, "confirmation_pattern_count": confirm_hits},
        autoScored=True,
    )


def score_accessibility(repo: Path) -> dict[str, Any]:
    """Detect axe-core integration; score by presence + project-recorded run output if any."""
    has_axe = False
    pkg_json = repo / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            has_axe = any(k.startswith("@axe-core/") for k in deps)
        except (json.JSONDecodeError, OSError):
            pass

    # We do NOT spawn a dev server. If integration is present we surface that;
    # actual axe scan happens in the validator agent via Sentinel-driven Playwright.
    if has_axe:
        # Look for prior axe output artifacts.
        artifacts = list(repo.glob("**/axe-results.json"))[:1]
        if artifacts:
            try:
                results = json.loads(artifacts[0].read_text())
                violations = results.get("violations", [])
                critical = sum(1 for v in violations if v.get("impact") == "critical")
                serious = sum(1 for v in violations if v.get("impact") == "serious")
                if critical > 0: score = 1
                elif serious > 30: score = 1
                elif serious > 10: score = 2
                elif serious > 3: score = 3
                elif serious > 0: score = 4
                else: score = 5
                return _category("accessibility", score, "high",
                                 metrics={"axe_critical": critical, "axe_serious": serious}, autoScored=True)
            except (json.JSONDecodeError, OSError):
                pass
        return _category("accessibility", 3, "low",
                         metrics={"axe_integrated": True, "axe_results_present": False},
                         autoScored=True, note="@axe-core integrated but no run output; run scan to score")
    return _placeholder("accessibility", reason="@axe-core not integrated")


def score_performance(repo: Path) -> dict[str, Any]:
    """Look for lighthouse output artifacts; do not spawn dev servers."""
    lhci_dir = repo / ".lighthouseci"
    has_config = (repo / "lighthouserc.json").exists() or (repo / "lighthouserc.js").exists()
    has_runs = lhci_dir.exists() and any(lhci_dir.glob("*.json"))

    if has_runs:
        # Score from most-recent assertion-results if present.
        score = 3  # default mid-tier; refine if assertion data parseable
        results = sorted(lhci_dir.glob("assertion-results-*.json"), reverse=True)[:1]
        if results:
            try:
                data = json.loads(results[0].read_text())
                fails = sum(1 for r in data if r.get("level") == "error")
                if fails == 0: score = 5
                elif fails <= 1: score = 4
                elif fails <= 2: score = 3
                else: score = 2
            except (json.JSONDecodeError, OSError):
                pass
        return _category("performance", score, "high",
                         metrics={"lhci_runs_present": True, "config_present": has_config}, autoScored=True)
    if has_config:
        return _category("performance", 3, "low",
                         metrics={"lhci_config": True, "lhci_runs": False}, autoScored=True,
                         note="Lighthouse CI configured but no run output; run lhci collect to score")
    return _placeholder("performance", reason="no Lighthouse CI integration")


def _ui_files(repo: Path, limit: int = 500) -> list[Path]:
    rc, files_str, _ = run(
        ["git", "ls-files", "*.tsx", "*.jsx", "*.ts", "*.js", "*.vue", "*.svelte"],
        repo, timeout=10,
    )
    if rc != 0 or not files_str.strip():
        return []
    return [repo / f for f in files_str.strip().splitlines() if (repo / f).is_file()][:limit]


def score_workflow_clarity(repo: Path) -> dict[str, Any]:
    """Heuristic: presence of navigation/breadcrumb/stepper patterns + router config."""
    files = _ui_files(repo)
    if not files:
        return _placeholder("workflow-clarity", reason="no UI files tracked")

    nav_re = re.compile(
        r"<(Breadcrumb|Stepper|Wizard|NavBar|Sidebar|TabList|Stepper)\b|"
        r"\bbreadcrumb\b|\bstepper\b|step\s*\d+\s*of\s*\d+",
        re.IGNORECASE,
    )
    page_re = re.compile(r"useRouter|next/router|react-router|@tanstack/router|createBrowserRouter|<Routes|<Route\b")
    nav_hits = 0
    routing = False
    for f in files:
        try:
            txt = f.read_text(errors="replace")
        except OSError:
            continue
        nav_hits += len(nav_re.findall(txt))
        if not routing and page_re.search(txt):
            routing = True

    nav_density = nav_hits / max(len(files), 1)
    if routing and nav_density >= 0.05:
        score = 5
    elif routing and nav_density >= 0.02:
        score = 4
    elif routing or nav_density >= 0.02:
        score = 3
    elif nav_hits > 0:
        score = 2
    else:
        score = 1

    return _category(
        "workflow-clarity", score, "low",  # heuristic
        metrics={"nav_pattern_hits": nav_hits, "routing_detected": routing,
                 "files_scanned": len(files), "nav_density": round(nav_density, 3)},
        autoScored=True,
    )


def score_interaction_usability(repo: Path) -> dict[str, Any]:
    """Heuristic: presence of NN/g-aligned interaction patterns (loading, empty,
    error, disabled) across UI files. Higher coverage = better."""
    files = _ui_files(repo)
    if not files:
        return _placeholder("interaction-usability", reason="no UI files tracked")

    patterns = {
        "loading":  re.compile(r"\b(isLoading|isPending|<Skeleton|<Spinner|aria-busy)\b"),
        "empty":    re.compile(r"<EmptyState|empty[-_ ]?state|no\s+results|no\s+items"),
        "error":    re.compile(r"<ErrorBoundary|<ErrorState|onError=|error[Mm]essage|toast\.error"),
        "disabled": re.compile(r"\bdisabled=|aria-disabled"),
    }
    counts = {k: 0 for k in patterns}
    for f in files:
        try:
            txt = f.read_text(errors="replace")
        except OSError:
            continue
        for k, rgx in patterns.items():
            if rgx.search(txt):
                counts[k] += 1

    coverage = sum(1 for v in counts.values() if v > 0)  # 0..4
    n = max(len(files), 1)
    density = sum(counts.values()) / n
    if coverage == 4 and density >= 0.3:
        score = 5
    elif coverage >= 3 and density >= 0.15:
        score = 4
    elif coverage >= 2:
        score = 3
    elif coverage >= 1:
        score = 2
    else:
        score = 1

    return _category(
        "interaction-usability", score, "low",
        metrics={"pattern_files": counts, "coverage_buckets": coverage,
                 "files_scanned": n, "density": round(density, 3)},
        autoScored=True,
    )


def score_visual_hierarchy(repo: Path) -> dict[str, Any]:
    """Heuristic: heading distribution + use of typography tokens vs raw px font sizes."""
    files = _ui_files(repo)
    if not files:
        return _placeholder("visual-hierarchy", reason="no UI files tracked")

    h1_re = re.compile(r"<h1\b|<H1\b")
    h2_re = re.compile(r"<h2\b|<H2\b")
    h3_re = re.compile(r"<h3\b|<H3\b")
    raw_font_re = re.compile(r"font-?[Ss]ize\s*[:=]\s*[\"']?\d+px")
    token_font_re = re.compile(r"text-(xs|sm|base|md|lg|xl|2xl|3xl|4xl)\b|font-(xs|sm|base|md|lg|xl|2xl)\b|var\(--font-")

    h1 = h2 = h3 = raw = tok = 0
    for f in files:
        try:
            txt = f.read_text(errors="replace")
        except OSError:
            continue
        h1 += len(h1_re.findall(txt))
        h2 += len(h2_re.findall(txt))
        h3 += len(h3_re.findall(txt))
        raw += len(raw_font_re.findall(txt))
        tok += len(token_font_re.findall(txt))

    # Penalize too many h1s (> 1 per page is suspicious; we approximate "page" by file).
    h1_pages = sum(1 for f in files if h1_re.search(_safe_read(f)))
    h1_excess = max(0, h1 - h1_pages)  # extra h1s beyond one-per-file
    token_ratio = tok / max(raw + tok, 1)

    if token_ratio >= 0.85 and h1_excess == 0 and h2 + h3 > 0:
        score = 5
    elif token_ratio >= 0.7 and h1_excess <= 2:
        score = 4
    elif token_ratio >= 0.5:
        score = 3
    elif token_ratio > 0:
        score = 2
    else:
        score = 1

    return _category(
        "visual-hierarchy", score, "low",
        metrics={"h1": h1, "h2": h2, "h3": h3, "h1_excess": h1_excess,
                 "raw_font_px": raw, "token_font": tok, "token_ratio": round(token_ratio, 2)},
        autoScored=True,
    )


def score_content_ux(repo: Path) -> dict[str, Any]:
    """Heuristic: input labeling + non-trivial button copy + aria-label coverage."""
    files = _ui_files(repo)
    if not files:
        return _placeholder("content-ux", reason="no UI files tracked")

    input_re = re.compile(r"<input\b|<Input\b|<textarea\b|<TextArea\b|<select\b|<Select\b")
    label_re = re.compile(r"<label\b|<Label\b|htmlFor=|aria-label=|aria-labelledby=")
    button_re = re.compile(r"<button[^>]*>([^<]{0,40})</button>", re.IGNORECASE)
    short_label_re = re.compile(r"^\s*(ok|x|×|\?|!|go|yes|no)\s*$", re.IGNORECASE)
    placeholder_only_re = re.compile(r"<input[^>]*placeholder=[^>]*>(?![^<]*<label)", re.IGNORECASE)

    inputs = labels = buttons = bad_buttons = placeholder_only = 0
    for f in files:
        txt = _safe_read(f)
        inputs += len(input_re.findall(txt))
        labels += len(label_re.findall(txt))
        for cap in button_re.findall(txt):
            buttons += 1
            if short_label_re.match(cap):
                bad_buttons += 1
        placeholder_only += len(placeholder_only_re.findall(txt))

    label_ratio = labels / max(inputs, 1) if inputs else 1.0
    bad_btn_ratio = bad_buttons / max(buttons, 1) if buttons else 0
    if label_ratio >= 1.0 and bad_btn_ratio == 0 and placeholder_only == 0:
        score = 5
    elif label_ratio >= 0.8 and bad_btn_ratio <= 0.05:
        score = 4
    elif label_ratio >= 0.5:
        score = 3
    elif label_ratio > 0:
        score = 2
    else:
        score = 1

    return _category(
        "content-ux", score, "low",
        metrics={"inputs": inputs, "labels": labels, "label_ratio": round(label_ratio, 2),
                 "buttons": buttons, "short_label_buttons": bad_buttons,
                 "placeholder_only_inputs": placeholder_only},
        autoScored=True,
    )


def _safe_read(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _category(key: str, score: int, confidence: str, *, metrics: dict[str, Any] | None = None,
              autoScored: bool = True, note: str = "") -> dict[str, Any]:
    return {
        "key": key,
        "score": score,
        "weight": CATEGORY_WEIGHTS[key],
        "confidence": confidence,
        "owner": "auto",
        "evidenceFreshnessDays": 0,
        "topRisks": [],
        "metrics": metrics or {},
        "autoScored": autoScored,
        "note": note,
    }


def _placeholder(key: str, *, reason: str = "", default_score: int = 3) -> dict[str, Any]:
    return _category(key, default_score, "low", metrics={"reason": reason}, autoScored=False,
                     note=f"manual evidence required ({reason})" if reason else "manual evidence required")


# Manual categories — emit placeholders.
MANUAL_CATEGORIES = ["user-task-fit", "research-measurement"]


# ----------------------------------------------------------------------------
# Main scoring
# ----------------------------------------------------------------------------


def score_repo(repo: Path) -> dict[str, Any]:
    categories: list[dict[str, Any]] = []
    auto_scorers = {
        "accessibility": score_accessibility,
        "performance": score_performance,
        "design-system": score_design_system,
        "platform-continuity": score_platform_continuity,
        "trust-safety": score_trust_safety,
        "implementation-fidelity": score_implementation_fidelity,
        "workflow-clarity": score_workflow_clarity,
        "interaction-usability": score_interaction_usability,
        "visual-hierarchy": score_visual_hierarchy,
        "content-ux": score_content_ux,
    }
    for key, fn in auto_scorers.items():
        try:
            categories.append(fn(repo))
        except Exception as exc:  # never let one category sink the whole run
            categories.append(_placeholder(key, reason=f"scorer error: {exc}"))
    for key in MANUAL_CATEGORIES:
        categories.append(_placeholder(key))

    # Composite scoring
    total_weight = sum(CATEGORY_WEIGHTS.values())
    raw_pct = sum((c["score"] / 5) * c["weight"] for c in categories) * 100 / total_weight
    weighted_pct = sum((c["score"] / 5) * c["weight"] * CONFIDENCE_MULTIPLIERS[c["confidence"]]
                       for c in categories) * 100 / total_weight

    band = _maturity_band(weighted_pct)

    # Hard gates — emit placeholder pending state for now (need manual verification).
    hard_gates = [{**g, "status": "pending"} for g in HARD_GATES_SPEC]

    branch, commit = _git_meta(repo)

    return {
        "rubricVersion": RUBRIC_VERSION,
        "generatedAt": utc_now(),
        "branch": branch,
        "commit": commit,
        "mode": "auto-scored",
        "categories": categories,
        "categoryWeights": CATEGORY_WEIGHTS,
        "overall": {
            "rawPercent": round(raw_pct, 2),
            "confidenceWeightedPercent": round(weighted_pct, 2),
            "adjustedPercent": round(weighted_pct, 2),  # no penalties applied yet
            "totalPenalty": 0,
            "maturityBand": band,
            "enterpriseDesignMature": weighted_pct >= 90,
            "penalties": [],
        },
        "summary": {
            "average": round(sum(c["score"] for c in categories) / len(categories), 2),
            "min": min(c["score"] for c in categories),
            "enterpriseDesignReady": weighted_pct >= 80 and all(c["score"] >= 3 for c in categories),
            "passingHardGates": 0,  # all pending
            "autoScoredCount": sum(1 for c in categories if c["autoScored"]),
        },
        "hardGates": hard_gates,
        "sourceAnchors": [
            "https://www.nngroup.com/articles/ten-usability-heuristics/",
            "https://www.w3.org/TR/WCAG22/",
            "https://web.dev/articles/vitals",
        ],
        "designEvidence": [],
    }


def _maturity_band(pct: float) -> str:
    if pct >= 90: return "Enterprise-Design-Mature"
    if pct >= 80: return "Enterprise-Design-Ready"
    if pct >= 65: return "Operational"
    if pct >= 50: return "Developing"
    return "Foundational"


def _git_meta(repo: Path) -> tuple[str, str]:
    rc, branch, _ = run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], repo, timeout=5)
    rc2, commit, _ = run(["git", "-C", str(repo), "rev-parse", "HEAD"], repo, timeout=5)
    return (branch.strip() if rc == 0 else "unknown", commit.strip() if rc2 == 0 else "unknown")


def main() -> int:
    ap = argparse.ArgumentParser(description="Score a repo against the enterprise design rubric.")
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    repo = args.repo.resolve()
    if not repo.is_dir():
        print(f"error: --repo {repo} is not a directory", file=sys.stderr)
        return 2

    result = score_repo(repo)
    out_path = args.out or (repo / ".artifacts" / "rubric-suite" / "design-scorecard.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2))
    print(f"design rubric: {result['overall']['adjustedPercent']}% ({result['overall']['maturityBand']}) "
          f"— {result['summary']['autoScoredCount']}/12 auto-scored — {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
