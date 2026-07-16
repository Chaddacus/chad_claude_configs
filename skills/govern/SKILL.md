---
name: govern
description: Wire the governed runtime into Claude Code agent teams. Classifies work into R1–R5, spawns bounded swarm teams for R3/R4, manages packet execution with reviewer barriers, enforces postflight gates, and sends completion notifications.
effort: high
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names,destructive_rollback,branch_policy_live
---

# /govern - Governed Runtime Orchestration

This skill is the runtime interpreter for the route manifest (`~/.claude/state/route_manifest.json`).
It classifies work, enforces route-specific execution discipline, spawns agent teams for bounded swarms,
and gates closure on convergence and postflight evidence.

## Usage

```text
/govern Add a helper function to utils.ts
/govern Add JWT auth to the API with refresh tokens
/govern --route R2 Fix the typo in the README
/govern --dry-run Refactor the billing module
```

## Flags

| Flag | Effect |
| --- | --- |
| `(none)` | Classify and execute with full governance |
| `--route R{n}` | Override auto-classification (still enforces route constraints) |
| `--dry-run` | Classify and show plan, but do not execute |
| `--no-team` | Force single-lane execution even for R3/R4 |
| `--via-hermes` | Route the whole task through Hermes phase orchestrator (`~/code/hermes`, REST `:3345`) instead of in-session dispatch. R3/R4 only. Greenfield uses 8 phases (plan/design/backend/frontend/test/e2e/security/validate); refactor uses 5 (index/plan/refactor/test/validate). Falls back to standard dispatch if Hermes is unreachable and `--strict-hermes` is not set. |
| `--worker-runtime {claude\|goose\|opencode}` | Override the dispatch profile's `worker_runtime` for this run. `goose` routes through `~/.claude/bin/goose_dispatch.py` (ACP → Pro/Max subscription, no per-token cost). `opencode` routes through anthropic-concurrency-system. Default inherits from `route_manifest.json` profile. |

## Canonical Inputs

- Route manifest: `~/.claude/state/route_manifest.json`
- Agent definitions: `~/.claude/agents/*.md`
- Planning-gate contracts: `~/.claude/skills/planning-gate/references/contracts.md`
- Output contracts: `references/contracts.md`

## Workflow

### Phase 0: Memory Retrieval

Before any classification or execution:
1. Search codex-mem for workspace preferences, prior route decisions, known constraints
2. Search claude-mem for durable decisions affecting this workspace
3. Record `memory_retrieval_evidence` for the implementation contract

If memory is weak or low-confidence, surface that weakness — do not pretend the context is strong.

### Phase 1: Route Classification

Extract task characteristics from the user prompt:
- **file_count_estimate**: Count file/path mentions and infer scope
- **touches_auth/security/migrations**: Scan for risk keywords
- **touches_production_behavior**: Detect behavioral changes
- **estimated_complexity**: Assess based on scope, dependencies, ambiguity
- **has_ambiguity**: Flag when intent cannot be resolved from context

Run the classifier:
```bash
echo '<task_json>' | python3 ~/.claude/skills/govern/scripts/classify_route.py
```

Display the route decision:
```
Route: R3 (non_trivial_impl) | Shape: bounded_swarm | Risk: medium | Swarm cap: 4
```

If `--dry-run` was specified, display the full classification and stop.

### Phase 2: Execute by Route

#### R1 — Quick Factual

Answer directly. No gates, no team, no postflight, no planning-gate.
Skip to Phase 5 (notification).

#### R2 — Single-Lane Governed

1. Execute inline — no planning-gate overhead
2. Run validation commands (typecheck, tests, lint)
3. Skip to Phase 5

#### R3 — Bounded Swarm

1. Run full planning-gate frontend:
   - `compile_intent` → `initialize_session` → `compile_plan` → `verify_plan` → `validate_plan`
2. Generate team spec:
   ```bash
   echo '<route_json>' | python3 ~/.claude/skills/govern/scripts/build_team_spec.py
   ```
3. Create the team using `TeamCreate`:
   - Team name: `govern-<track-id>`
   - Members per the team spec (respecting lane_caps and profile_overrides)
4. Create worktree for isolation:
   - Branch: `govern-<track-id>`
   - Workers operate in the worktree
5. Create tasks for each packet using `TaskCreate`:
   - Set `addBlockedBy` for DAG dependencies between packets
   - Include acceptance checks in task description
6. **Sprint-contract ack (ENFORCED gate):** send the planner's sprint contract
   to the reviewer; on explicit ack, record it — R3/R4 `dispatch_track` blocks
   with `missing_reviewer_ack` until this runs:
   ```bash
   python3 ~/.claude/bin/auto_runtime.py record-ack --track-id <track_id> \
       --by reviewer --ref '<criteria hash or ack message>'
   ```
   Reviewer rejects → planner revises criteria → re-ack. Never record an ack
   the reviewer didn't give.
7. Enter the orchestration loop (Phase 3)
8. At reviewer barrier points → spawn reviewer, wait for verdict
9. On completion → postflight gates (Phase 4)

#### R4 — Conservative Bounded Swarm

Same as R3 with tighter constraints:
- Reviewer is co-primary from the start (joins team at creation, not just at barriers)
- Swarm cap: 2 (from manifest)
- All lane caps: 1
- Extra barrier: `adaptation_generated_packets`
- Reviewer must approve the plan before any execution begins
- All worker packets require explicit reviewer sign-off

**Codex delegation:** For complex investigation or fix sub-tasks within R4, use `/codex:rescue` to hand off asynchronously while Claude coordinates:
```bash
/codex:rescue --background investigate <specific problem>
/codex:rescue --resume apply the top fix from the last run
/codex:status    # check progress
/codex:result    # retrieve findings
```
Use `codex resume <session-id>` (from `/codex:result` output) to hand off into Codex directly.

#### R5 — Ambiguity Resolution

1. Identify the specific ambiguities preventing classification
2. Generate targeted clarifying questions (batch-limited per manifest)
3. Present questions to the user
4. On resolution, re-classify the prompt and re-enter Phase 2

### Phase 3: Orchestration Loop (R3/R4)

The session running `/govern` acts as team lead (coordinator):

```
LOOP:
  1. TaskList → get current task states
  2. Compute runnable set:
     - Tasks whose blockedBy are all completed
     - Respect lane_caps (don't exceed per-lane concurrent limit)
     - Respect route_swarm_cap (total concurrent active tasks)
  3. Dispatch in frontier_dispatch_order:
     validator → explorer → worker → reviewer
     For each dispatchable task:
     - TaskUpdate: set owner = agent name, status = in_progress
     - SendMessage: packet instructions + acceptance checks + scope constraints
  4. Wait for agent completion messages
  5. On task completion:
     - Handoff-integrity check FIRST (see "Truncation tripwire" below);
       a result that fails it is a FAILED dispatch, not a completion
     - TaskUpdate: mark completed with evidence
     - Check reviewer_barrier_points:
       - "closure": all required packets must be reviewer-approved before closing
       - "high_risk_boundary_shrink": reviewer approves any scope reduction
       - "adaptation_generated_packets" (R4): reviewer approves generated work
     - If barrier hit → dispatch reviewer task, wait for verdict
     - If reviewer rejects → create rework tasks, continue loop
     - Recompute runnable set
  6. Check terminal conditions:
     - All required packets accepted + convergence → OBJECTIVE_COMPLETE
     - No runnable packets + all blocked → ESCALATION_REQUIRED
     - Retry budget exhausted (max 2 same-strategy) → boundary shrink
     - No frontier movement for 2 cycles → escalate
  7. If not terminal → GOTO 1
```

**Timeout safety:**
- If an agent hasn't reported in 5 minutes, send a status ping via SendMessage
- If no response after 2 pings (10 min total), mark task failed and attempt reassignment
- Track `noop_cycle_count` and `no_frontier_movement_cycle_count` per manifest thresholds

**Truncation tripwire (handoff integrity):**
maxTurns truncation cuts the END of an agent's output, so a turn-capped agent
looks like a finished one unless you check. Worker/planner/test-strategist
handoffs are contractually required to end with the literal final line
`HANDOFF-COMPLETE`. On any completion message:
- Missing `HANDOFF-COMPLETE` final line, OR missing mandatory handoff artifacts
  (worker: diff + test output + criterion mapping + `verify:<slice-id>:exit=<code>`
  token) → treat as a FAILED dispatch. Never grade prose; never accept partial
  artifacts as "close enough".
- Respawn with the failure folded into the retry prompt ("your previous attempt
  was cut off after X; resume from the last complete artifact"), under the same
  retry policy caps below. Repeated truncation of the same packet → split the
  packet, don't raise maxTurns first.

**Retry policy:**
- Same method: up to 2 attempts
- Alternate strategy: up to 2 attempts
- Classify failures: incidental → retry, structural → repacketize, authority → escalate

**Effort escalation on rework dispatch:**
When re-dispatching a slice that entered `rework` (via `update-node --state rework`), the auto-runtime records a suggested bumped effort in `governance.slice_escalations[slice_id]`. Resolve the effective effort for the dispatch via:
```bash
EFFORT=$(python3 ~/.claude/bin/auto_runtime.py effort-for-slice \
    --track-id <track_id> --slice-id <slice_id> --base-effort <route_manifest_effort>)
```
Use `$EFFORT` when invoking the agent for the retry. The helper returns `base-effort` unchanged if no escalation is recorded, so it's safe to always call. Ladder: `low → medium → high → xhigh`; ceiling is `xhigh`.

### Phase 4: Postflight (R3/R4)

1. Compile artifacts in standard planning-gate schema:
   - `plan.json` from planning phase
   - `impl.json` from execution evidence
   - `review.json` from reviewer verdicts
2. Write artifacts to `planning_artifacts/<track-id>/`
3. Invoke postflight:
   ```bash
   ~/.claude/bin/claude_run --finalize-attempt \
     --route-class <route_id> --track-id <track_id> \
     --plan-json <path> --impl-json <path> --review-json <path> \
     --workspace-root $PWD -- exec "<prompt>"
   ```
4. `claude_run` handles auto-continue loop (up to 3 iterations per manifest)
5. Interpret result:
   - **approve** → proceed to notification
   - **revise** → auto-retry with revision instructions
   - **blocked** → report to user with blocking details

If `claude_run` is not available, fall back to manual postflight:
- Run `validate_impl.py` and `finalize_gate.py` from planning-gate
- Treat `finalize_gate.py ok=true` as approval

### Phase 5: Notification & Closure

1. Send completion notification:
   ```bash
   ~/.claude/bin/notify_done.sh --status <success|blocked> \
     --task "<objective>" --details "<route_id> | <N> packets | postflight: <result>"
   ```
   If `notify_done.sh` is not available, document the notification gap.

2. Persist to memory:
   - Route used and why
   - Outcome (success/blocked/boundary-shrunk)
   - Lessons learned, constraints discovered
   - Any workspace preferences that should apply to future work

3. Report final status using the governed closure vocabulary:
   - **OBJECTIVE_COMPLETE** → strong closure
   - **OBJECTIVE_COMPLETE_BOUNDARY_SHRUNK** → weak closure (document what was excluded)
   - **OBJECTIVE_BLOCKED_ESCALATION_REQUIRED** → blocked closure (document blockers)
   - **OBJECTIVE_REJECTED_FALSE_COMPLETION** → re-enter loop

### Self-Audit

Before delivering, verify:
- [ ] Route classification matches the actual work performed
- [ ] All packets have evidence-backed acceptance (not summary claims)
- [ ] Reviewer barriers were enforced at the correct points
- [ ] No scope expansion without supervisor rewrite
- [ ] Postflight ran to completion (or gap documented)
- [ ] Notification sent (or gap documented)
- [ ] Memory updated with durable decisions

### Expert Review

Challenge:
- Was the route classification correct? Should it have been higher/lower?
- Did the chosen solution layer (L1/L2/L3) represent the highest useful layer?
- Are there failure modes, security gaps, or missing tests?
- Was convergence genuine or were closure claims unsupported?
- Did retry/rework cycles produce new evidence or just repeat?

## Output

Report:
- Route used and classification rationale
- Execution shape and team composition (if swarm)
- Packet summary: total, accepted, rejected, reworked
- Postflight result
- Closure type: strong, weak, or blocked
- Evidence trail: plan → impl → review → postflight artifacts
