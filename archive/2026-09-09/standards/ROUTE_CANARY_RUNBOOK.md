---
policy_doc_kind: route_canary_runbook
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names
---

# Route Canary Runbook (v1)

> **Canonical.** `skills/govern/references/ROUTE_CANARY_RUNBOOK.md` is a pointer
> to this file (collapsed 2026-07-27). Do not re-fork it.

## Scope
Execute and validate the 48-hour Codex app router canary for Routing Contract v1.

## Preconditions
- Router config and agent profile changes are already applied.
- `route_debug.json` is `false` for normal UX unless running a focused debug session.
- `route_test_cases.json` contains the active corpus.

## Phase 1: Controlled Corpus Session
1. Set debug on for one dedicated validation session:
```bash
cat > /Users/chadsimon/.claude/state/route_debug.json <<'JSON'
{ "enabled": true }
JSON
```
2. In the Codex app, run each corpus prompt once and set route metadata task IDs to match case IDs (`R1-01` ... `R4-10`).
3. Extract audit records:
```bash
/Users/chadsimon/.claude/bin/route_audit_extract.sh --mode append
```
4. Validate thresholds:
```bash
/Users/chadsimon/.claude/bin/route_validate_audit.sh \
  --cases /Users/chadsimon/.claude/state/route_test_cases.json \
  --audit /Users/chadsimon/.claude/state/route_audit.jsonl \  <!-- pointer-check:skip -->
  --manifest /Users/chadsimon/.claude/state/route_manifest.json
```

## Phase 2: Canary (48 hours)
1. Set debug back off:
```bash
cat > /Users/chadsimon/.claude/state/route_debug.json <<'JSON'
{ "enabled": false }
JSON
```
2. Keep audit extraction on a regular interval (manual or automation):
```bash
/Users/chadsimon/.claude/bin/route_audit_extract.sh --mode append
```
3. Re-run threshold validation at least every 12 hours:
```bash
/Users/chadsimon/.claude/bin/route_validate_audit.sh \
  --cases /Users/chadsimon/.claude/state/route_test_cases.json \
  --audit /Users/chadsimon/.claude/state/route_audit.jsonl \  <!-- pointer-check:skip -->
  --manifest /Users/chadsimon/.claude/state/route_manifest.json
```

## Pass/Fail Criteria
- `high_risk_false_negatives = 0`
- `rule_match_accuracy >= 0.95`
- `fallback_rate <= 0.10`
- `audit_coverage = 1.00`
- `observed_model_family_accuracy >= 0.95`

## Rollback Trigger
Rollback immediately if any hard threshold fails in controlled corpus run or during canary.

## Rollback Command
```bash
/Users/chadsimon/.claude/bin/route_rollback.sh
```
