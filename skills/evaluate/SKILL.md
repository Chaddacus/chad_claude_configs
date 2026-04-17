---
name: evaluate
description: Score a completed build against measurable criteria, extract observations, and write results to the chad-memory database for persistent learning.
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names,destructive_rollback,branch_policy_live
---

# /evaluate — Build Evaluation & Observation Extraction

Score a completed build, extract lessons learned, and persist results to the chad-memory database.

This skill owns build evaluation and observation extraction only. Global policy owns routing, git safety, planning-gate requirements, and final delivery rules.

## Usage

```
/evaluate
/evaluate /path/to/project
/evaluate "Notes about the build"
```

## Step 1: Gather Build Data

### Git History
```bash
git log --oneline -20
MERGE_BASE=$(git merge-base HEAD main 2>/dev/null || echo "")
git diff --stat "HEAD~${BUILD_COMMITS}" 2>/dev/null || git diff --stat HEAD~1
```

### Test Results
Run the project's test command and parse output for tests_total and tests_passed.

## Step 2: Score Metrics

| Metric | Source |
|--------|--------|
| `files_changed` | git diff --stat |
| `tests_total` | Test runner output |
| `tests_passed` | Test runner output |
| `fix_attempts` | Session history — retries |
| `review_findings` | Issues found during validation |
| `status` | completed / blocked / abandoned |

## Step 3: Extract Observations

For each observation:
- **category**: `pattern` (good approach), `mistake` (bad approach), `tool` (library learning), `process` (workflow improvement)
- **summary**: actionable "do X instead of Y" format
- **detail**: full context
- **applies_to**: typescript / python / go / api / testing / all

Look in: blocked tasks, failed gates, clean successes, review findings, user feedback.

## Step 4: Write to Database

```bash
chad-memory log-build '{"project":"<name>","task":"<description>","preset":"<preset>","files_changed":<n>,"tests_total":<n>,"tests_passed":<n>,"fix_attempts":<n>,"blocked_tasks":<n>,"review_findings":<n>,"status":"<status>"}'

chad-memory log-observation '{"build_id":<id>,"category":"<cat>","summary":"<summary>","detail":"<detail>","applies_to":"<scope>"}'
```

## Step 5: Output Scorecard

```
════════════════════════════════════════
BUILD EVALUATION
════════════════════════════════════════
Project: <name>
Task: <description>

Metrics:
  Files changed:    <n>
  Tests:            <passed>/<total>
  Fix attempts:     <n>
  Review findings:  <n>

Observations:
  [pattern]  <summary>
  [mistake]  <summary>

Score: Clean / Smooth / Rough / Struggled
════════════════════════════════════════
```

### Score Guide
- **Clean** — 0 fix attempts, 0 blocked, 0 review findings, all tests pass
- **Smooth** — <=1 fix attempt, 0 blocked, <=1 review finding
- **Rough** — 2+ fix attempts OR blocked tasks OR 2+ findings
- **Struggled** — 3+ fix attempts AND blocked AND findings

If `chad-memory` not installed, output scorecard but warn results not persisted.
