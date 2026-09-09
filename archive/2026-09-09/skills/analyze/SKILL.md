---
name: analyze
description: Consolidate build observations across sessions, detect patterns, promote rules, and regenerate the chad-memory.md rules file.
context: fork
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names,destructive_rollback,branch_policy_live
---

# /analyze — Cross-Session Pattern Analysis & Rule Promotion

Aggregate observations across builds, detect recurring patterns, and promote proven observations to active rules.

This skill owns build-pattern analysis only. Global policy owns routing, git safety, planning-gate requirements, and final delivery rules.

## Usage

```
/analyze
/analyze --last 5
/analyze --project my-api
/analyze --since 2025-01-01
```

## Step 1: Query Database

```bash
chad-memory query --project <X> --since <Y> --last <N>
chad-memory stats
```

## Step 2: Aggregate Metrics

| Metric | Calculation |
|--------|-------------|
| Avg fix attempts | sum(fix_attempts) / count(builds) |
| Block rate | builds with blocks / total builds * 100 |
| Avg review findings | sum(findings) / count(builds) |
| Test pass rate | sum(passed) / sum(total) * 100 |

## Step 3: Pattern Detection

Group observations by similarity. Observations progress: raw → candidate (2+ sightings) → active (promoted) → archived (stale).

## Step 4: Promotion Gate

For candidates (frequency >= 2), check ALL:
- Actionable (contains a concrete verb)
- Measurable (can verify compliance)
- Not redundant (no existing active rule covers it)
- Specific (not vague)

Promote: `chad-memory promote <id>`

## Step 5: Archive Stale Rules

Rules not seen in 10+ builds: `chad-memory archive <id>`

## Step 6: Regenerate

```bash
chad-memory generate
```

## Output

```
════════════════════════════════════════
ANALYSIS REPORT
════════════════════════════════════════
Builds analyzed: <n>

Trends:
  Fix attempts:     <n>/task
  Block rate:       <n>%
  Review findings:  <n>/build

New Rules Promoted:
  [category] <rule> (seen Nx)

Rules Archived:
  [category] <rule> (not seen in N builds)

Active Rules: <count>/15
════════════════════════════════════════
```

If `chad-memory` not installed, fail with install instructions.
