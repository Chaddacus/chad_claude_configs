#!/usr/bin/env python3
"""
product_truth_check.py — Deterministic gate for ~/.claude/state/product_truth/<slug>.json.

Validates claim → evidence completeness, blocks vague-claim phrases, enforces
length budgets on human_wedge / agent_facing_summary, range-checks scorecard.

NO LLM IN THE LOOP. Karpathy Rule 5: model is for judgment; this is deterministic.

Exit codes:
  0 — ok=true (gate passed)
  2 — ok=false, blocked (structured missing/blocked/risks list)
  1 — script error (bad path, malformed JSON, etc.)

Optional flag --register-facts emits omni-mem fact_add calls (subject=slug,
predicate=claims/audience/evidence_ref/wedge, object=value).

Mirrors finalize_gate.py / postflight_acceptance_check.py shape:
  { "ok": bool, "missing": [...], "blocked": [...], "risks": [...] }
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from omni_mem_route import container_for_cwd

REPO_HOME = Path.home() / ".claude"
SCHEMA_PATH = REPO_HOME / "state" / "product_truth" / "_schema.json"

# Regex-blocked vague phrases. Word-boundary, case-insensitive.
# Source of truth — keep in sync with _template.md "Banned vague phrases".
VAGUE_PHRASES = [
    r"AI-powered",
    r"seamless(?:ly)?",
    r"intelligent",
    r"best-in-class",
    r"revolutionary",
    r"cutting-edge",
    r"next-generation",
    r"state-of-the-art",
    r"world-class",
    r"industry-leading",
    r"transformative",
    r"game-changing",
    r"disruptive",
    r"synergy",
    r"holistic",
    r"paradigm-shift(?:ing)?",
    r"unlock(?:s|ing)?",
    r"empower(?:s|ing)?",
    r"leverage(?:s|ing|d)?",
]
VAGUE_RE = re.compile(r"\b(" + "|".join(VAGUE_PHRASES) + r")\b", re.IGNORECASE)

# Length budgets (must match _schema.json maxLength constraints).
WEDGE_MAX = 140
SUMMARY_MAX = 400

# Scorecard ranges.
SCORE_MIN, SCORE_MAX = 1, 5
RISK_VALUES = {"low", "medium", "high"}


def _err(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    print(json.dumps({"ok": False, "error": msg}), file=sys.stderr)
    sys.exit(code)


def load_artifact(path: Path) -> dict[str, Any]:
    if not path.exists():
        _err(f"artifact not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _err(f"artifact is not valid JSON: {exc}")


def load_schema() -> dict[str, Any]:
    if not SCHEMA_PATH.exists():
        _err(f"schema not found at {SCHEMA_PATH}")
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _check_required_fields(data: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Shallow required-field check at the top level (good enough for v1)."""
    missing = []
    for field in schema.get("required", []):
        if field not in data:
            missing.append(f"required_field_missing: {field}")
    return missing


def _check_schema_version(data: dict[str, Any]) -> list[str]:
    sv = data.get("schema_version")
    if sv != "product_truth.v1":
        return [f"unknown_schema_version: {sv!r} (expected 'product_truth.v1')"]
    return []


def _check_slug(data: dict[str, Any]) -> list[str]:
    slug = data.get("product_slug", "")
    if not isinstance(slug, str) or not re.match(r"^[a-z0-9][a-z0-9-]*$", slug):
        return [f"invalid_product_slug: {slug!r} (must be lowercase-kebab)"]
    return []


def _check_lengths(data: dict[str, Any]) -> list[str]:
    issues = []
    wedge = data.get("human_wedge", "")
    if not isinstance(wedge, str) or len(wedge) < 1:
        issues.append("human_wedge_missing_or_empty")
    elif len(wedge) > WEDGE_MAX:
        issues.append(f"human_wedge_too_long: {len(wedge)} > {WEDGE_MAX}")

    summary = data.get("agent_facing_summary", "")
    if not isinstance(summary, str) or len(summary) < 1:
        issues.append("agent_facing_summary_missing_or_empty")
    elif len(summary) > SUMMARY_MAX:
        issues.append(f"agent_facing_summary_too_long: {len(summary)} > {SUMMARY_MAX}")

    what = data.get("what_it_does", "")
    if isinstance(what, str) and len(what) > 200:
        issues.append(f"what_it_does_too_long: {len(what)} > 200")

    return issues


def _check_scorecard(data: dict[str, Any]) -> list[str]:
    issues = []
    sc = data.get("scorecard", {})
    if not isinstance(sc, dict):
        return ["scorecard_not_object"]
    for field in (
        "truth_score",
        "differentiation_score",
        "agent_legibility_score",
        "human_memory_score",
    ):
        val = sc.get(field)
        if not isinstance(val, int) or not SCORE_MIN <= val <= SCORE_MAX:
            issues.append(
                f"scorecard.{field}_invalid: {val!r} (must be int {SCORE_MIN}-{SCORE_MAX})"
            )
    risk = sc.get("proof_gap_risk")
    if risk not in RISK_VALUES:
        issues.append(f"scorecard.proof_gap_risk_invalid: {risk!r} (must be {sorted(RISK_VALUES)})")
    return issues


def _check_claims(data: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """Returns (blocked, risks, vague_violations_found)."""
    blocked: list[str] = []
    risks: list[str] = []
    vague_found: list[str] = []

    claims = data.get("claims")
    if not isinstance(claims, list):
        return (["claims_not_array"], [], [])

    seen_ids: set[str] = set()

    # Pre-resolved vague_claim_violations: phrases the operator has already
    # acknowledged and either removed, weakened, or backed.
    resolved_pairs: set[tuple[str, str]] = set()
    for v in data.get("vague_claim_violations", []) or []:
        if isinstance(v, dict) and v.get("resolution") in {"removed", "backed_by_proof", "weakened"}:
            phrase = (v.get("phrase") or "").lower()
            location = v.get("location") or ""
            if phrase and location:
                resolved_pairs.add((phrase, location))

    for idx, claim in enumerate(claims):
        if not isinstance(claim, dict):
            blocked.append(f"claims[{idx}]_not_object")
            continue

        cid = claim.get("claim_id", "")
        if not isinstance(cid, str) or not re.match(r"^c[0-9]+$", cid):
            blocked.append(f"claims[{idx}]_invalid_claim_id: {cid!r}")
            continue
        if cid in seen_ids:
            blocked.append(f"claims[{idx}]_duplicate_claim_id: {cid}")
            continue
        seen_ids.add(cid)

        text = claim.get("claim", "")
        if not isinstance(text, str) or not text.strip():
            blocked.append(f"{cid}_claim_text_empty")
            continue

        # Vague-phrase regex match
        for m in VAGUE_RE.finditer(text):
            phrase = m.group(1).lower()
            if (phrase, cid) in resolved_pairs:
                continue  # operator acknowledged + resolved
            blocked.append(
                f"vague_claim_violation: {phrase!r} in {cid} (add to "
                f"vague_claim_violations with resolution removed/weakened/backed_by_proof)"
            )
            vague_found.append(f"{cid}:{phrase}")

        # Evidence / missing_evidence completeness
        evidence = claim.get("evidence", [])
        missing_ev = claim.get("missing_evidence", [])
        if not isinstance(evidence, list):
            blocked.append(f"{cid}_evidence_not_array")
            continue
        if not isinstance(missing_ev, list):
            blocked.append(f"{cid}_missing_evidence_not_array")
            continue

        if len(evidence) == 0 and len(missing_ev) == 0:
            blocked.append(
                f"{cid}_no_evidence_and_no_missing_evidence: every claim must have "
                f"at least one evidence entry OR be marked under missing_evidence"
            )

        # Evidence ref must exist on disk OR be a known proof_pending marker OR a URL
        for eidx, ev in enumerate(evidence):
            if not isinstance(ev, dict):
                blocked.append(f"{cid}.evidence[{eidx}]_not_object")
                continue
            ref = ev.get("ref", "")
            if not isinstance(ref, str) or not ref.strip():
                blocked.append(f"{cid}.evidence[{eidx}]_ref_missing")
                continue
            if ref == "proof_pending":
                risks.append(f"{cid}.evidence[{eidx}]_proof_pending")
                continue
            if ref.startswith(("http://", "https://")):
                # URL — can't verify without network; treat as risk, not block.
                continue
            # Relative path — resolve against cwd. Missing file = risk, not block.
            p = Path(ref).expanduser()
            if not p.is_absolute():
                p = Path.cwd() / p
            if not p.exists():
                risks.append(f"{cid}.evidence[{eidx}]_ref_not_found_on_disk: {ref}")

        # Acknowledged missing_evidence — risk, not block.
        for me in missing_ev:
            if isinstance(me, str) and me.strip():
                risks.append(f"{cid}_missing_evidence: {me}")

    return (blocked, risks, vague_found)


def _emit_facts(data: dict[str, Any], workspace_id: str = "chadsimon") -> int:
    """Emit omni-mem fact_add calls for each claim → evidence triple.

    Returns count of facts emitted.
    """
    slug = data.get("product_slug", "")
    count = 0

    def _fact(predicate: str, obj: str) -> None:
        nonlocal count
        try:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    # Vault routed by cwd: ~/chad_personal -> personal, else work.
                    container_for_cwd(),
                    "omni-mem",
                    "fact_add",
                    "--workspaceId",
                    workspace_id,
                    "--subject",
                    slug,
                    "--predicate",
                    predicate,
                    "--object",
                    obj,
                ],
                check=False,
                capture_output=True,
                timeout=10,
            )
            count += 1
        except Exception:
            pass  # best-effort

    # Wedge + summary
    if data.get("human_wedge"):
        _fact("wedge", data["human_wedge"])
    if data.get("agent_facing_summary"):
        _fact("agent_facing_summary", data["agent_facing_summary"])

    # Audience
    for a in data.get("audience", []) or []:
        if isinstance(a, str):
            _fact("audience", a)

    # Claims + evidence
    for claim in data.get("claims", []) or []:
        if not isinstance(claim, dict):
            continue
        cid = claim.get("claim_id", "?")
        if claim.get("claim"):
            _fact(f"claim:{cid}", claim["claim"])
        for ev in claim.get("evidence", []) or []:
            if isinstance(ev, dict) and ev.get("ref"):
                _fact(f"evidence:{cid}", f"{ev.get('type', 'unknown')}:{ev['ref']}")

    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Product truth-layer gate.")
    parser.add_argument("artifact", help="Path to product_truth/<slug>.json")
    parser.add_argument(
        "--register-facts",
        action="store_true",
        help="Emit omni-mem fact_add for each claim → evidence triple (best-effort)",
    )
    parser.add_argument(
        "--workspace-id",
        default=os.environ.get("OMNI_MEM_WORKSPACE_ID", "chadsimon"),
        help="omni-mem workspaceId (default: $OMNI_MEM_WORKSPACE_ID or 'chadsimon')",
    )
    args = parser.parse_args()

    path = Path(args.artifact).expanduser()
    data = load_artifact(path)
    schema = load_schema()

    missing: list[str] = []
    blocked: list[str] = []
    risks: list[str] = []

    missing.extend(_check_required_fields(data, schema))
    blocked.extend(_check_schema_version(data))
    blocked.extend(_check_slug(data))
    blocked.extend(_check_lengths(data))
    blocked.extend(_check_scorecard(data))

    claim_blocked, claim_risks, vague_found = _check_claims(data)
    blocked.extend(claim_blocked)
    risks.extend(claim_risks)

    ok = not missing and not blocked

    facts_emitted = 0
    if ok and args.register_facts:
        facts_emitted = _emit_facts(data, workspace_id=args.workspace_id)

    result = {
        "ok": ok,
        "artifact": str(path),
        "schema_version": data.get("schema_version"),
        "product_slug": data.get("product_slug"),
        "missing": missing,
        "blocked": blocked,
        "risks": risks,
        "vague_violations_found": vague_found,
    }
    if args.register_facts:
        result["facts_emitted"] = facts_emitted

    print(json.dumps(result, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
