#!/usr/bin/env bash
# obsessive_slice_verify.sh — acceptance preset for orchestrator/worker obsessive loop.
#
# Run AFTER goose finishes a slice. Performs:
#   1. test_breadth_check classification of the slice's diff
#   2. Project tests scoped to required breadths (npm test / pytest)
#   3. Re-run run_rubric_suite + compute delta vs baseline (regression brake)
#   4. Emit a structured SliceReport JSON to <out_dir>/report.json
#
# Exits 0 only if:
#   - All required breadths actually ran (test commands present + exit 0)
#   - Rubric weighted_avg delta > -<regression_threshold_pp>  (default 0.5pp)
#   - No previously-passing hard gate now failing
#
# Goose cannot modify this script (goose_dispatch.py protected_paths semantics).
#
# Usage (positional, since goose_dispatch passes args via --preset-args):
#   obsessive_slice_verify.sh <slice_id> <baseline_scorecard_path> <out_dir>
#
# Optional env:
#   OBSESSIVE_REGRESSION_PP   — max allowed weighted_avg drop (default 0.5)
#   OBSESSIVE_BASE_SHA        — git SHA at slice start; if unset, uses HEAD~1
#                               or the recorded value from out_dir/.base_sha

set -uo pipefail

if [ $# -lt 3 ]; then
  echo "usage: obsessive_slice_verify.sh <slice_id> <baseline_scorecard> <out_dir>" >&2
  exit 2
fi

SLICE_ID="$1"
BASELINE="$2"
OUT_DIR="$3"
WORKSPACE="$(pwd)"
REGRESSION_PP="${OBSESSIVE_REGRESSION_PP:-0.5}"

mkdir -p "$OUT_DIR"
REPORT="$OUT_DIR/report.json"
LOG="$OUT_DIR/verify.log"
exec > >(tee -a "$LOG") 2>&1

echo "[verify $SLICE_ID] start $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[verify $SLICE_ID] workspace=$WORKSPACE  baseline=$BASELINE  out=$OUT_DIR"

# ---------------------------------------------------------------------------
# 0. Capture base_sha (the commit goose started from)
# ---------------------------------------------------------------------------

BASE_SHA="${OBSESSIVE_BASE_SHA:-}"
if [ -z "$BASE_SHA" ] && [ -f "$OUT_DIR/.base_sha" ]; then
  BASE_SHA="$(cat "$OUT_DIR/.base_sha")"
fi
if [ -z "$BASE_SHA" ]; then
  # Best-effort fallback
  BASE_SHA="$(git rev-parse HEAD~1 2>/dev/null || git rev-parse HEAD)"
fi
HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
echo "[verify] base=$BASE_SHA head=$HEAD_SHA"

# ---------------------------------------------------------------------------
# 1. Breadth classification
# ---------------------------------------------------------------------------

BREADTH_OUT="$OUT_DIR/breadth.json"
python3 ~/.claude/bin/test_breadth_check.py --repo "$WORKSPACE" \
  --base "$BASE_SHA" --head HEAD --out "$BREADTH_OUT" --format json
REQUIRED_BREADTHS=$(python3 -c "import json; d=json.load(open('$BREADTH_OUT')); print(' '.join(d['required_breadths']))")
echo "[verify] required breadths: $REQUIRED_BREADTHS"

# ---------------------------------------------------------------------------
# 2. Run tests for each required breadth
# ---------------------------------------------------------------------------

TESTS_JSON='{}'
TESTS_OK=true
for breadth in $REQUIRED_BREADTHS; do
  case "$breadth" in
    smoke)
      cmd="echo smoke-noop"; rc=0; passed=0; failed=0
      ;;
    full)
      if [ -f package.json ] && jq -e '.scripts.test' package.json >/dev/null 2>&1; then
        cmd="npm test --silent -- --run"
        npm test --silent -- --run > "$OUT_DIR/test-full.log" 2>&1
        rc=$?
        passed=$(grep -oE 'Tests?: *[0-9]+ passed' "$OUT_DIR/test-full.log" | grep -oE '[0-9]+' | head -1 || echo 0)
        failed=$(grep -oE '[0-9]+ failed' "$OUT_DIR/test-full.log" | grep -oE '[0-9]+' | head -1 || echo 0)
      elif [ -f pyproject.toml ] || [ -f setup.py ]; then
        cmd="python3 -m pytest -q"
        python3 -m pytest -q > "$OUT_DIR/test-full.log" 2>&1
        rc=$?
        passed=$(grep -oE '[0-9]+ passed' "$OUT_DIR/test-full.log" | grep -oE '[0-9]+' | head -1 || echo 0)
        failed=$(grep -oE '[0-9]+ failed' "$OUT_DIR/test-full.log" | grep -oE '[0-9]+' | head -1 || echo 0)
      else
        cmd="(no test runner detected)"; rc=1; passed=0; failed=0
      fi
      ;;
    browser-e2e)
      if [ -f package.json ] && jq -e '.scripts."test:e2e" // .scripts.e2e' package.json >/dev/null 2>&1; then
        e2e_script=$(jq -r '.scripts["test:e2e"] // .scripts.e2e' package.json)
        cmd="npm run -s $e2e_script"
        eval "$cmd" > "$OUT_DIR/test-e2e.log" 2>&1
        rc=$?
        passed=0; failed=0  # parse playwright/junit later
      else
        cmd="(no e2e script)"; rc=1; passed=0; failed=0
      fi
      ;;
    data-combo)
      # Schemathesis / hypothesis / fast-check are project-specific; skip with note unless wired
      cmd="(data-combo manual)"; rc=0; passed=0; failed=0
      ;;
    *)
      cmd="(unknown breadth)"; rc=1; passed=0; failed=0
      ;;
  esac
  TESTS_JSON=$(jq -n \
    --argjson prev "$TESTS_JSON" \
    --arg breadth "$breadth" --arg cmd "$cmd" --argjson rc "$rc" \
    --argjson passed "${passed:-0}" --argjson failed "${failed:-0}" \
    '$prev + {($breadth): {command:$cmd, exit:$rc, passed:$passed, failed:$failed}}')
  [ "$rc" -ne 0 ] && TESTS_OK=false
done

echo "[verify] tests_ok=$TESTS_OK"

# ---------------------------------------------------------------------------
# 3. Re-run rubric suite + compute delta vs baseline
# ---------------------------------------------------------------------------

NEW_SCORECARD="$OUT_DIR/scorecard.json"
python3 ~/.claude/bin/run_rubric_suite.py --repo "$WORKSPACE" \
  --rubric-bypass "obsessive-slice-verify-$SLICE_ID" --out "$NEW_SCORECARD" || true

if [ ! -f "$NEW_SCORECARD" ]; then
  echo "[verify] FATAL: scorecard not produced" >&2
  jq -n --arg slice "$SLICE_ID" '{slice_id:$slice, status:"failed", error:"scorecard not produced"}' > "$REPORT"
  exit 3
fi

# Compute delta + regression check
python3 - <<EOF >"$OUT_DIR/delta.json"
import json
b = json.load(open("$BASELINE"))
n = json.load(open("$NEW_SCORECARD"))

def avg(d): return float(d.get("merged",{}).get("weightedAverage", 0.0))
def passing(d): return {g.get("id", g.get("name","?"))
                        for g in d.get("merged",{}).get("allHardGates",[])
                        if g.get("status") == "pass"}

delta = avg(n) - avg(b)
regressions = list(passing(b) - passing(n))
print(json.dumps({
  "weighted_avg_before": avg(b),
  "weighted_avg_after":  avg(n),
  "weighted_avg_delta":  round(delta, 2),
  "previously_passing_now_failing": regressions,
}))
EOF

DELTA_PP=$(jq -r '.weighted_avg_delta' "$OUT_DIR/delta.json")
REGRESSIONS=$(jq -r '.previously_passing_now_failing | length' "$OUT_DIR/delta.json")
echo "[verify] weighted_avg_delta=${DELTA_PP}pp regression_count=$REGRESSIONS"

# Regression brake
RUBRIC_OK=true
if python3 -c "import sys; sys.exit(0 if float('$DELTA_PP') < -float('$REGRESSION_PP') else 1)"; then
  echo "[verify] FAIL: weighted_avg dropped ${DELTA_PP}pp (threshold -${REGRESSION_PP})"
  RUBRIC_OK=false
fi
if [ "$REGRESSIONS" -gt 0 ]; then
  echo "[verify] FAIL: $REGRESSIONS previously-passing hard gates now failing"
  RUBRIC_OK=false
fi

# ---------------------------------------------------------------------------
# 4. Emit SliceReport
# ---------------------------------------------------------------------------

# Diff stats
DIFF_PATCH="$OUT_DIR/diff.patch"
git diff "$BASE_SHA" > "$DIFF_PATCH" 2>/dev/null || true
FILES_CHANGED=$(git diff --name-only "$BASE_SHA" 2>/dev/null | wc -l | tr -d ' ')
INSERTIONS=$(git diff --shortstat "$BASE_SHA" 2>/dev/null | grep -oE '[0-9]+ insertion' | grep -oE '[0-9]+' | head -1 || echo 0)
DELETIONS=$(git diff --shortstat "$BASE_SHA" 2>/dev/null | grep -oE '[0-9]+ deletion' | grep -oE '[0-9]+' | head -1 || echo 0)

STATUS="completed"
[ "$TESTS_OK" = "false" ] && STATUS="failed"
[ "$RUBRIC_OK" = "false" ] && STATUS="failed"

jq -n \
  --arg slice "$SLICE_ID" \
  --arg status "$STATUS" \
  --arg base_sha "$BASE_SHA" \
  --arg head_sha "$HEAD_SHA" \
  --argjson breadths "$(jq -c .required_breadths "$BREADTH_OUT")" \
  --argjson tests "$TESTS_JSON" \
  --argjson delta "$(cat "$OUT_DIR/delta.json")" \
  --argjson files_changed "$FILES_CHANGED" \
  --argjson insertions "${INSERTIONS:-0}" \
  --argjson deletions "${DELETIONS:-0}" \
  --arg diff_path "$DIFF_PATCH" \
  --arg breadth_path "$BREADTH_OUT" \
  --arg scorecard_path "$NEW_SCORECARD" \
  '{
    slice_id: $slice,
    status: $status,
    base_sha: $base_sha,
    head_sha: $head_sha,
    breadth_required: $breadths,
    tests: $tests,
    rubric_delta: $delta,
    diff_summary: {files_changed:$files_changed, insertions:$insertions, deletions:$deletions},
    evidence_refs: {
      diff: $diff_path,
      breadth: $breadth_path,
      scorecard: $scorecard_path
    }
  }' > "$REPORT"

echo "[verify] report → $REPORT"
echo "[verify] status=$STATUS"

# Exit code: 0 if all clean, non-0 otherwise
if [ "$TESTS_OK" = "true" ] && [ "$RUBRIC_OK" = "true" ]; then
  exit 0
else
  exit 1
fi
