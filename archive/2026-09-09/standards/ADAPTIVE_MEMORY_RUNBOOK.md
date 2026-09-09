---
policy_doc_kind: adaptive_memory_runbook
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names
---

# Adaptive Memory Runbook

> **Status (2026-07-16 audit M8, paths re-checked 2026-07-26): LEGACY.** The
> incident dir (`~/.claude-mem/incidents/`) does not exist on disk <!-- pointer-check:skip -->
> — it is created only by a `recover`/`repair-db` run.
> The codex-mem CLI this runbook operates still resolves, at
> `/Users/chadsimon/chad_work/codex-mem/dist/cli.js` (the tree moved out of
> `~/code/`; the M8 note predates the move and read as deleted).
> Live memory is the two-tier native-markdown + omni-mem stack (see
> REFERENCE_INDEX.md). Kept for historical procedure reference only.

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
- `P5` codex-mem runtime degradation, daemon outage, or DB corruption

### Response flow
1. Contain: disable affected preference application path.
2. Assess: identify impacted scopes and entries.
3. Remediate: purge or supersede affected notes; add validation rules.
4. Verify: rerun retrieval/resolution and trigger eval gates.
5. Report: write incident note with root cause and permanent fix.

## Runtime Health

Treat codex-mem as healthy only when both are true:
- `codex-mem status --json` reports healthy DB/service-path status
- `codex-mem daemon-status --json` reports `daemonState=running`

Degraded runtime signals must be surfaced explicitly in execution evidence. Do not silently skip retrieval.

## Daemon Model

- Normal Codex use goes through `mcp-server`, CLI retrieval/save commands, and the local daemon.
- The daemon is the only normal owner of the live SQLite store in `~/.claude-mem/`.
- `mcp-server` is a stdio proxy, not a DB owner.
- Maintenance commands (`repair-db`, `recover`, snapshot inspection) require daemon inactivity and a maintenance lock.
- Browser dashboard access to the shared live Codex store is blocked until it is migrated to a daemon-backed proxy path.

## Health Checks

1. `node /Users/chadsimon/chad_work/codex-mem/dist/cli.js status --json`
2. `node /Users/chadsimon/chad_work/codex-mem/dist/cli.js daemon-status --json`
3. If daemon metadata is missing, run `node /Users/chadsimon/chad_work/codex-mem/dist/cli.js ensure-daemon --json`
4. If daemon health is degraded, inspect the structured error code:
   - `MEMORY_DAEMON_UNAVAILABLE`
   - `MEMORY_DB_CORRUPT`
   - `MEMORY_RECOVERY_REQUIRED`

## Corruption Incident Flow

1. Stop normal runtime owners. Do not run more MCP/CLI retrieval traffic against the live store.
2. Preserve the current live state. `recover` and `repair-db` now create an incident bundle under `~/.claude-mem/incidents/` <!-- pointer-check:skip --> (created on first incident) containing:
   - live DB/WAL/SHM copies
   - status report
   - sqlite quick check output
   - runtime lock state
   - process list
3. Prefer snapshot restore first:
   - `node /Users/chadsimon/chad_work/codex-mem/dist/cli.js recover --mode db --json`
4. If no valid healthy snapshot exists, fall back to in-place salvage:
   - `node /Users/chadsimon/chad_work/codex-mem/dist/cli.js repair-db --mode db --json`
5. Rebuild the query layer if recovery succeeds:
   - `node /Users/chadsimon/chad_work/codex-mem/dist/cli.js rebuild-query-layer --json`
6. Re-run both health checks before resuming normal Codex sessions.

## Snapshot Rules

- Snapshot metadata is portable by `id` + `relative_path`; absolute `path` is legacy only.
- Snapshot creation belongs to the daemon. Transient CLI/MCP startup must not create prestart snapshots.
- Recovery prefers the latest validated healthy snapshot even if legacy manifest entries contain stale absolute paths such as `/root/.codex-mem/...`.

## Maintenance Lock

- Maintenance requires no active daemon lock or normal runtime owner.
- Daemon startup must not proceed while the maintenance lock is held.
- If maintenance is blocked, resolve live ownership first instead of forcing DB repair through active traffic.

## Rollback
- Keep previous resolver behavior behind a feature flag when possible.
- Maintain backup export of preference notes before schema changes.
- Validate one-step rollback script during release gates.
