---
name: memory-adaptation
description: Use when a coding task should adapt to durable user/project preferences stored in omni-mem; skip for trivial factual/chat requests.
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names,destructive_rollback,branch_policy_live
---

# Memory Adaptation Skill

This skill owns omni-mem preference retrieval and adaptation workflow only. Global and workspace policy own routing, git safety, review requirements, and notification rules.

## Use When
- The user asks to adapt style, workflow, or implementation behavior to prior preferences.
- The task is non-trivial and preference-aware behavior can change planning or implementation decisions.
- Prompt contracts, planning gates, or review output require preference retrieval evidence.

## Do Not Use When
- The request is a one-off factual answer with no implementation.
- The task is trivial (single-line/simple edit) and preference retrieval adds no value.
- The user explicitly asks to ignore memory for this run.

## Workflow
1. Retrieve context with `mcp__omni-mem__build_context` scoped to `cwd`.
2. Run targeted `mcp__omni-mem__search` queries for `pref:` keys.
3. Resolve active preferences with deterministic order:
   1. explicit user > project > workspace > global
   2. higher confidence
   3. newest timestamp
4. Select and apply a prompt contract from `references/PROMPT_CONTRACTS.md`.
5. Record implementation evidence fields:
   - `memory_retrieval_evidence`
   - `preferences_applied`
   - `skill_trigger_eval_results`
   - `prompt_contract_used`
   - `frontend_roundtrip_evidence` (frontend scope only)
6. Save durable updates with `save_preference` (stable preference) or `save_memory` (episodic lesson).

## Degraded Memory Handling
- If omni-mem returns a structured runtime error (`MEMORY_DAEMON_UNAVAILABLE`, `MEMORY_DB_CORRUPT`, `MEMORY_RECOVERY_REQUIRED`), record that incident explicitly instead of acting like retrieval returned an empty result.
- Treat degraded memory as blocked retrieval evidence for non-trivial work.
- Escalate to the adaptive memory runbook at [ADAPTIVE_MEMORY_RUNBOOK.md](/Users/chadsimon/.claude/standards/ADAPTIVE_MEMORY_RUNBOOK.md) when the runtime is degraded.
- Do not save new memory entries until the daemon and DB health are restored.

## Trigger Reliability Harness
Use this skill-level eval corpus and keep thresholds fail-closed:
- `false_positive_rate <= 0.10`
- `false_negative_rate <= 0.10`

### Positive Trigger Examples (`should_trigger=true`)
- "Learn how I code and apply my preferred testing/style choices automatically."
- "Use my saved frontend iteration preferences and update the implementation plan."
- "Resolve project preferences before coding this multi-file refactor."

### Negative Trigger Examples (`should_trigger=false`)
- "What time is it?"
- "Summarize this paragraph."
- "Translate this sentence to Spanish."

If rates exceed thresholds, block approval and fix boundaries/corpus before rollout.
