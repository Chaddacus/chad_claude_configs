---
policy_doc_kind: adaptive_memory_runbook
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names
---

# Adaptive Memory Runbook

## Purpose
Operational guide for codex-mem preference adaptation, maintenance, and incident response.

## Scope
- Preference notes (`pref-note.v1`)
- Retrieval/resolution behavior
- Prompt contract enforcement
- Skill trigger reliability

## Weekly Operations
1. Export and review active preferences by scope (`user`, `project`, `workspace`, `global`).
2. Prune stale or conflicting entries using `supersedes` chains.
3. Lower confidence for stale preferences that no longer match recent outcomes.
4. Promote repeated high-signal patterns into durable preferences.
5. Regenerate summary metrics and store artifact output.

## Preference Lifecycle
1. Capture: save with full `pref-note.v1` fields.
2. Resolve: apply deterministic precedence and confidence/timestamp tie-breakers.
3. Apply: record `preferences_applied` evidence in implementation artifacts.
4. Supersede: mark replaced notes via `supersedes`.
5. Retire: archive obsolete or low-confidence notes.

## Quality Gates
- Retrieval evidence required for non-trivial tasks.
- Prompt contract evidence required for non-trivial tasks.
- Skill trigger thresholds:
  - false positive rate <= 10%
  - false negative rate <= 10%
- Frontend scope requires frontend roundtrip evidence.

## Metrics
Emit and review weekly:
- `prefs_loaded`
- `prefs_applied`
- `prefs_ignored`
- `trigger_eval_summary`
- preference conflict count
- stale preference count

## Security Controls
- Reject secrets/tokens/password-like content in preference saves.
- Enforce scope-bound retrieval (`cwd`) by default.
- Avoid cross-project preference leakage unless explicitly requested.

## Incident Response

### Incident classes
- `P1` secret leakage risk in stored notes
- `P2` cross-scope preference leakage
- `P3` trigger reliability drift
- `P4` stale preference quality degradation

### Response flow
1. Contain: disable affected preference application path.
2. Assess: identify impacted scopes and entries.
3. Remediate: purge or supersede affected notes; add validation rules.
4. Verify: rerun retrieval/resolution and trigger eval gates.
5. Report: write incident note with root cause and permanent fix.

## Rollback
- Keep previous resolver behavior behind a feature flag when possible.
- Maintain backup export of preference notes before schema changes.
- Validate one-step rollback script during release gates.
