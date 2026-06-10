---
policy_doc_kind: protocol
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
---

# Replan Decision Protocol

Closes `CR-INV-009-REPLAN-CITES-EVIDENCE` from `~/.claude/standards/CHAD_RUNTIME_INVARIANTS.md`.

Modeled on AgentOps's `INV-REPLAN-CITES-EVIDENCE` (`~/automation_architecture/docs/ARCHITECTURE_INVARIANTS.md`): "Replans are decisions, not narrative."

## When this fires

A replan event is any of:
- chad-twin's 2-attempt rule fires (worker failed twice on the same slice, supervisor pivots approach)
- A blocker forces re-decomposition (slice can't proceed without changing the plan above it)
- A scope change forces a different solution shape (constraint discovered mid-run)
- An approach is abandoned mid-implementation (the elegant solution failed; we're switching strategies)

Cosmetic refactors and small tactical adjustments are NOT replan events. The bar is: did the *approach* change, not just the next line of code?

## The contract

On any replan event, the agent making the pivot writes ONE structured `journal_write` to omni-mem before continuing.

Topic: `replan-<short-slug>` (e.g. `replan-mailbox-dispatch-retry`)

Content (free-form prose, but covering these fields explicitly):

```
trigger_evidence:
  <one paragraph naming the concrete failure or constraint. file paths,
   test names, error strings, commit shas, observation ids. NOT "it didn't
   feel right" — actual evidence.>

candidates_scored:
  - <candidate 1 short name>: <one-line rationale>, <one-line risk>
  - <candidate 2 short name>: <one-line rationale>, <one-line risk>
  - <candidate 3 short name>: <one-line rationale>, <one-line risk>
  (at least 2 candidates; 3+ is better)

threshold:
  <what would make a candidate acceptable — e.g. "must run under 5s",
   "must not introduce a new dependency", "must work for both
   stdio and HTTP transports">

selected: <name of chosen candidate>

rejected_reasons:
  - <candidate name>: <one-line reason it failed the threshold>
  - <candidate name>: <one-line reason>

rationale:
  <one paragraph: why selected meets threshold, what evidence makes you
   believe it will work, what would falsify the choice. Calibrated, not
   confident.>
```

## Why this matters

Three reasons, in order:

1. **Anti-amnesia.** Without this, the next chad-twin run hits the same failed approach and tries it again because the prior failure is buried in conversation context that has been compacted away. With this, an omni-mem search on the task topic surfaces the prior pivot.

2. **Calibration over time.** Longitudinal signal: which candidate types keep getting selected and then failing, and which keep getting rejected and then succeeding? Six months of these records lets the planner agent learn which decompositions actually work for which task shapes.

3. **Reviewer evidence.** When the reviewer agent asks "why did you take this approach?", the answer is a single journal record, not a re-synthesis of conversation.

## What this is NOT

- It is not a planning document. Plans use TodoWrite or the `planning-gate` skill.
- It is not a postmortem. Postmortems happen after a run closes; replan records happen mid-run, before the new approach is implemented.
- It is not narrative. Skip flowery prose. Be the agent equivalent of a flight-recorder.

## Example

```
Topic: replan-omnimem-mailbox-cloud-mirror

trigger_evidence:
  Worker dispatched against feat/mailbox-cloud-endpoints failed verification
  twice — `pnpm test packages/cli` errored with "Missing field moduleType"
  on packages/dashboard/vite.config.ts at lines 14-22. Root cause: Vite 7
  workspace tsc collision documented in
  feedback_vite_workspace_tsc memory. Two prior worker attempts (commits
  9ab3f12, a4e8d09) tried inline fixes that touched tsconfig but didn't
  exclude the workspace from the root tsc surface.

candidates_scored:
  - exclude-in-root-tsconfig: surgical, one-file change in root tsconfig.json,
    matches the memorized fix. Risk: forgets to chain into the same PR if the
    worker doesn't see the memory.
  - extract-mailbox-package: split mailbox into its own workspace package with
    isolated tsconfig. Risk: scope explosion, drags in package.json renames.
  - downgrade-vite: pin packages/dashboard to vite@6. Risk: regresses other
    Vite 7-dependent features; defers the underlying problem.

threshold:
  Must close in <=2 files, must reuse the memorized fix shape, must not
  defer the root problem.

selected: exclude-in-root-tsconfig

rejected_reasons:
  - extract-mailbox-package: violates 2-file threshold; also out of scope
    for this slice.
  - downgrade-vite: defers the root problem, contradicts the persistent
    memory recommendation.

rationale:
  exclude-in-root-tsconfig is the documented fix in feedback_vite_workspace_tsc.
  Two prior worker attempts missed it because they tried tactical workarounds
  inside the workspace tsconfig instead of the root one. Falsifier: if root
  tsc still errors after the exclude, the memory is stale and we need to
  revisit the root cause.
```

## Wiring

- chad-twin's supervisor loop SHOULD invoke this protocol whenever the 2-attempt rule fires before re-dispatching a worker.
- The planner agent SHOULD invoke this protocol whenever a mid-plan replan happens (constraint discovered, scope changed).
- The worker agent SHOULD invoke this protocol whenever it abandons an in-progress implementation strategy. (Workers usually escalate to the supervisor instead, but if a worker self-pivots within its slice, the same record applies.)

## Enforcement (sentinel-file pattern)

Enforced via Stop-hook `~/.claude/bin/replan_evidence_check.py --strict`:

1. **Sentinel write.** When the chad-twin 2-attempt rule fires (or any other replan trigger), the supervisor writes:
   ```bash
   : > "/tmp/claude-replan-pending-${CLAUDE_SESSION_ID:-default}.json"
   ```
   BEFORE re-dispatching under the new approach.
2. **Journal write.** The supervisor records the pivot per the schema above using `journal_write --topic replan-<slug>`.
3. **Stop-hook check.** On session Stop, the hook:
   - If sentinel exists AND a matching `replan-*` entry was journaled in this session → removes sentinel, exits 0.
   - If sentinel exists AND no matching entry → exits 2 with a blocking `stopReason` message demanding the journal entry.
   - If no sentinel → falls back to the transcript-heuristic advisory (Task tool_use error count threshold).

Bypass: only by deleting the sentinel manually, which defeats the gate. Don't.

Promotion path: the heuristic-only (non-sentinel) fallback remains advisory because transcript errors are noisy; sentinel-file is the structural signal.

## Search recipe

```bash
docker exec omni-mem omni-mem search --workspaceId "<repo>" --query "replan <task topic>"
docker exec omni-mem omni-mem journal_read --workspaceId "<repo>" --agentName "chad-twin" --limit 50 | grep -A 20 '"topic":"replan-'
```
