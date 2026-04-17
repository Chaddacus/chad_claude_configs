#!/usr/bin/env bash
set -euo pipefail

APP_HOME="${CLAUDE_HOME:-${CODEX_HOME:-$HOME/.claude}}"
CASES_FILE="${APP_HOME}/state/route_test_cases.json"
AUDIT_FILE="${APP_HOME}/state/route_audit.jsonl"
MANIFEST_FILE="${APP_HOME}/state/route_manifest.json"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cases)
      CASES_FILE="$2"
      shift 2
      ;;
    --audit)
      AUDIT_FILE="$2"
      shift 2
      ;;
    --manifest)
      MANIFEST_FILE="$2"
      shift 2
      ;;
    --help|-h)
      cat <<'USAGE'
Usage: route_validate_audit.sh [--cases PATH] [--audit PATH] [--manifest PATH]

Validates routing outcomes against route test cases and manifest thresholds.
Exits 0 on pass; exits 1 on threshold failure.
USAGE
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$CASES_FILE" ]]; then
  echo "Missing cases file: $CASES_FILE" >&2
  exit 1
fi
if [[ ! -f "$AUDIT_FILE" ]]; then
  echo "Missing audit file: $AUDIT_FILE" >&2
  exit 1
fi
if [[ ! -f "$MANIFEST_FILE" ]]; then
  echo "Missing manifest file: $MANIFEST_FILE" >&2
  exit 1
fi

python3 - "$CASES_FILE" "$AUDIT_FILE" "$MANIFEST_FILE" <<'PY'
import json
import sys
from pathlib import Path

cases_path = Path(sys.argv[1])
audit_path = Path(sys.argv[2])
manifest_path = Path(sys.argv[3])

cases_doc = json.loads(cases_path.read_text(encoding="utf-8"))
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
thresholds = manifest.get("thresholds", {})


def detect_model_family(observed_limit_name: str) -> str:
    value = (observed_limit_name or "").strip().lower()
    if not value:
        return "unknown"
    if "spark" in value or "bengalfox" in value:
        return "spark"
    if "sonnet" in value or "haiku" in value:
        return "sonnet"
    if "opus" in value:
        return "opus"
    if "codex" in value:
        return "codex"
    return "unknown"


def family_matches(expected_family: str, observed_family: str) -> bool:
    expected = (expected_family or "").strip().lower()
    observed = (observed_family or "").strip().lower()
    if expected == observed:
        return True
    # Cross-runtime equivalence: Codex plan families vs Claude plan families.
    if expected == "codex" and observed in {"opus"}:
        return True
    if expected == "spark" and observed in {"sonnet", "haiku"}:
        return True
    return False

cases = cases_doc.get("cases", [])
if not isinstance(cases, list) or not cases:
    print(json.dumps({"status": "fail", "reason": "empty_cases"}, sort_keys=True))
    sys.exit(1)

records = []
for line in audit_path.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    try:
        records.append(json.loads(line))
    except json.JSONDecodeError:
        continue

# Use the most recent record for each route_task_id.
latest_by_task = {}
for rec in records:
    task = rec.get("route_task_id")
    if not isinstance(task, str) or not task:
        continue
    latest_by_task[task] = rec

total = len(cases)
matched = 0
rule_match = 0
fallback_count = 0
high_risk_false_negatives = 0
model_family_match = 0
model_family_total = 0
missing_ids = []
mismatch_ids = []
high_risk_fail_ids = []
model_family_mismatch_ids = []

for case in cases:
    case_id = case.get("id")
    expected = case.get("expected", {})
    expected_rule = expected.get("rule_id")
    rec = latest_by_task.get(case_id)
    if rec is None:
        missing_ids.append(case_id)
        if expected_rule == "R4":
            high_risk_false_negatives += 1
        continue

    matched += 1
    observed_rule = rec.get("rule_id")
    if observed_rule == expected_rule:
        rule_match += 1
    else:
        mismatch_ids.append(case_id)

    if bool(rec.get("fallback_used")):
        fallback_count += 1

    expected_family = str(expected.get("expected_model_family") or "").lower()
    if expected_family in {"codex", "spark"}:
        model_family_total += 1
        observed_family = detect_model_family(str(rec.get("observed_limit_name") or ""))
        if observed_family == "unknown":
            # Fallback to declared route metadata when provider telemetry omits rate-limit names.
            observed_family = detect_model_family(str(rec.get("declared_execution") or ""))
        if family_matches(expected_family, observed_family):
            model_family_match += 1
        else:
            model_family_mismatch_ids.append(case_id)

    if expected_rule == "R4":
        declared_execution = str(rec.get("declared_execution") or "").lower()
        low_tier_primary = (
            any(x in declared_execution for x in ("spark", "sonnet", "haiku"))
            and "reviewer" not in declared_execution
            and "coordinator" not in declared_execution
        )
        if observed_rule != "R4" or low_tier_primary:
            high_risk_false_negatives += 1
            high_risk_fail_ids.append(case_id)

coverage = matched / total if total else 0.0
accuracy = rule_match / total if total else 0.0
fallback_rate = fallback_count / matched if matched else 0.0
model_family_accuracy = model_family_match / model_family_total if model_family_total else 1.0

max_high_risk_fn = thresholds.get("high_risk_false_negatives", 0)
min_accuracy = thresholds.get("rule_match_accuracy_min", 0.95)
max_fallback_rate = thresholds.get("fallback_rate_max", 0.10)
min_coverage = thresholds.get("audit_coverage", 1.0)
min_model_family_accuracy = thresholds.get("observed_model_family_accuracy_min", 0.95)

checks = {
    "high_risk_false_negatives": high_risk_false_negatives <= max_high_risk_fn,
    "rule_match_accuracy": accuracy >= min_accuracy,
    "fallback_rate": fallback_rate <= max_fallback_rate,
    "audit_coverage": coverage >= min_coverage,
    "observed_model_family_accuracy": model_family_accuracy >= min_model_family_accuracy,
}

result = {
    "status": "pass" if all(checks.values()) else "fail",
    "summary": {
      "total_cases": total,
      "matched_cases": matched,
      "coverage": round(coverage, 6),
      "rule_match_accuracy": round(accuracy, 6),
      "fallback_rate": round(fallback_rate, 6),
      "high_risk_false_negatives": high_risk_false_negatives,
      "observed_model_family_accuracy": round(model_family_accuracy, 6)
    },
    "thresholds": {
      "max_high_risk_false_negatives": max_high_risk_fn,
      "min_rule_match_accuracy": min_accuracy,
      "max_fallback_rate": max_fallback_rate,
      "min_audit_coverage": min_coverage,
      "min_observed_model_family_accuracy": min_model_family_accuracy
    },
    "checks": checks,
    "diagnostics": {
      "missing_case_ids": missing_ids,
      "rule_mismatch_case_ids": mismatch_ids,
      "high_risk_fail_case_ids": high_risk_fail_ids,
      "model_family_mismatch_case_ids": model_family_mismatch_ids
    }
}

print(json.dumps(result, sort_keys=True))
sys.exit(0 if result["status"] == "pass" else 1)
PY
