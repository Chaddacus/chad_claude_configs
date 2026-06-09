---
policy_doc_kind: auto_runtime_runbook
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names
---

# Auto Runtime & Governance Activation — Operational Reference

Canonical owner of auto-runtime and governance-activation operational detail, extracted verbatim from
`~/.claude/CLAUDE.md` on 2026-06-06; not a duplicate — CLAUDE.md retains the obligation stubs only
(init tracks for non-trivial work, act on route hints, close tracks with evidence). This document owns
the HOW. Dispatch budget values are owned by `~/.claude/bin/auto_runtime_common.py`
(`DISPATCH_CYCLE_MAX_BY_ROUTE`), not by this document.

## Governance Activation mechanics

- The `UserPromptSubmit` hook runs `classify_prompt.py` on every prompt, producing a `route_hint` and
  `governance_recommended` signal.
- When `governance_recommended` is true and the work is non-trivial, use `/govern` to orchestrate
  execution.
- `R3`/`R4`: `/govern` spawns agent teams via `TeamCreate`, manages packet DAGs via `TaskCreate`, and
  enforces reviewer barriers and postflight gates.
- `R1`/`R2`: `/govern` executes inline with lightweight or no governance overhead.
- The `Stop` hook persists high-signal memory via omni-mem every 15 exchanges and at session end.
- The `PreCompact` hook forces a full omni-mem memory dump before context compaction.

## Auto Runtime mechanics

- The auto runtime (`~/.claude/bin/auto_runtime.py`) provides event-sourced objective tracking with
  behavioral parity to Codex.
- State directory: `~/.claude/state/autonomy/{track_id}/` with replayable JSONL event log and
  materialized views.
- `/drive` initializes a track at Phase 0 (`auto_runtime.py init`) and marks slices accepted at
  closure (`auto_runtime.py update-node`).
- Route promotion escalates on repeated failures.
- Memory lifecycle gates fire to omni-mem at: objective init, slice acceptance/block, objective closure.
- `auto_runtime.py readiness` verifies infrastructure health (manifest, control plane, omni-mem,
  planning-gate scripts).
