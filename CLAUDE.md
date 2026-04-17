# Global Configuration

---
policy_doc_kind: global_agents
classification: canonical
canonical_owner: self
authority_level: constitutional
in_verifier_scope: true
lexical_guard_profile: stale_names,destructive_rollback,branch_policy_live
---

# Claude Global Configuration

These instructions apply to every Claude session in this home. Project-level `CLAUDE.md` files take precedence for workspace-local behavior.

## Canonical Claude Runtime Surfaces

- Global policy: `~/.claude/CLAUDE.md`
- Runtime config: `~/.claude/settings.json`
- Governed routing/runtime contract: `~/.claude/state/route_manifest.json`
- Governed wrapper: `~/.claude/bin/claude_run`
- Postflight runtime: `~/.claude/bin/ralph_done_loop.py`
- Acceptance checker: `~/.claude/bin/postflight_acceptance_check.py`
- Managed role files: `~/.claude/agents/*.md`
- Skills, plugins, hooks, and notifications under `~/.claude/skills/`, `~/.claude/plugins/`, and `~/.claude/bin/`

## Ownership Boundary

- Claude owns `~/.claude`.
- Codex owns `~/.Codex`.
- Claude behavior is canonical here; Codex behavior is canonical in `~/.Codex/AGENTS.md`.
- No automatic policy mirroring runs between the two homes.

## Legacy Reference Artifacts

- Historical sync material may remain under `~/.claude/sync-sources/`, `~/.claude/rules/codex-import/`, and `~/.claude/state/codex_sync_manifest.json`.
- Those files are legacy reference material only. They are not canonical inputs for Claude behavior.

## Core Operating Rules

- Default to action over asking. Ask only when there is genuine ambiguity, an authority boundary, or a destructive/external action.
- Prefer discovering facts from the repo or runtime over asking the user for discoverable context.
- Keep the end goal in view. Do not stop at partial analysis when safe momentum remains.
- Anti-overengineering is a gate, not an aspiration. Do not introduce a new service, persistence layer, schema family, or orchestration engine unless you can prove an existing primitive cannot satisfy the requirement. If you cannot prove it in one sentence, it fails.
- If a proposed change exceeds `500 LOC` or `3` files, stop and justify before implementing. Unjustified scope growth is a defect.
- Use `rg`/`rg --files` for search by default.
- For non-trivial coding work, use omni-mem retrieval before implementation and save durable lessons afterward.
- Treat `settings.json` and `route_manifest.json` as the canonical runtime surfaces.
- Keep global policy concise. Long procedural detail belongs in standards docs and skill references.
- Use the auto runtime (`~/.claude/bin/auto_runtime.py`) as the canonical autonomous execution surface. Treat `/drive`, `/build`, and `/govern` as convenience wrappers over the auto runtime.
- For autonomous task runs, use `auto_runtime.py manager-run-task --cwd <repo> --task "<objective>"` as the invocation-scoped manager loop.
- When the `UserPromptSubmit` hook provides `route_hint` and `governance_recommended`, act on it: initialize a track via `auto_runtime.py init`, use the track to manage slices, dispatch budgets, and state persistence throughout execution.
- For `R1` work: answer directly, no track needed.
- For `R2` work: init a track, dispatch inline, verify, accept with evidence, close. Lightweight but tracked.
- For `R3`/`R4` work: init a track, populate acceptance criteria (min 3), dispatch governed, use evaluator loop for verification, accept only with evidence, close with memory gates.
- For `R5` work: clarify ambiguity before execution.

## Autonomous Behavior

### Exploration
- Read files to understand context without announcing each read.
- When scope is uncertain, use an Explore subagent to map the territory before planning.

### Execution loop — Perpetual Motion
- When a task arrives, GO. Do not ask "should I start?", "is this the right approach?", or "should I proceed?". Permission to work is implied by the user sending the task.
- On non-trivial work, initialize an auto-runtime track: `python3 ~/.claude/bin/auto_runtime.py init --task "<objective>" --cwd "$PWD"`. Save the `track_id`.
- Decompose the task into slices. Each slice: implement -> test the changed code -> fix failures -> next slice.
- Use `auto_runtime.py cycle` to advance the track through dispatch -> verification -> acceptance.
- Update slice state via `auto_runtime.py update-node` with evidence refs on completion.
- **Do not stop between slices. Do not report progress. Do not ask for permission to continue.** The only reasons to stop are: (1) genuine ambiguity about direction, (2) external dependency you cannot resolve, (3) authority boundary (destructive/external action). Everything else — keep going.
- When a task decomposes into genuinely parallel, low-conflict parts, use subagents via the Agent tool. Do not parallelize when changes touch shared state or the same files.
- Dispatch budgets are enforced: R2=12 cycles, R3=24, R4=40. Route promotion escalates automatically on repeated failures.
- At ~70% context window usage, auto-compact fires (enabled in settings.json). The PreCompact hook persists memory to omni-mem before compaction. After compaction, continue working — do not stop to report.
- If tests fail, fix them. If a file is missing, create it. If a dependency is needed, install it. If the approach fails 3x, try a different approach. Do not stop to ask.

### Anti-stop patterns (autonomous runs)

On autonomous runs: do not stop early, do not declare false completion, do not defer shippable code because "verification will eventually need a human." The Stop hook's `AUTO-SAVE` is a memory checkpoint, not an exit signal — continue the loop. Only legitimate exits are genuine ambiguity, unresolvable external dependency, or authority boundary.

Full rule set (with examples and the Phase β retrospective context) is injected on R3/R4/R5 prompts by `~/.claude/skills/govern/scripts/classify_prompt.py` via UserPromptSubmit additionalContext. That file is the source of truth; this stub exists so the guardrail survives hook failure.

### Verification
- After completing an edit batch, run the project's typecheck/tests/lint before moving on. Don't wait to be told.
- Scope verification to what the current slice changed. Run the full test suite only at task completion, not between slices.
- If tests fail after your changes, fix them immediately. Don't report failure and wait.
- Distinguish pre-existing failures (not your problem) from introduced failures (fix before continuing).
- Do not use hedging language ("should work," "probably passes," "seems correct") when reporting verification outcomes. State what was run, what the output was, and whether it passed or failed. If not yet verified, say so explicitly.

### Completion
- State what evidence supports "done" — test results, typecheck output, or explicit verification commands you ran.
- Don't claim completion without running verification. "I believe this should work" is not evidence.
- Don't ask "should I proceed?" unless there is genuine ambiguity about DIRECTION. Permission to start work is implied.
- "Default to action" means: execute the next governed step. It does NOT mean: bypass governance checkpoints, reviewer barriers, or verification gates.
- Close the auto-runtime track: mark slices accepted with evidence via `auto_runtime.py update-node --state accepted --evidence "..."`, then run `auto_runtime.py cycle` to reach `OBJECTIVE_COMPLETE`.
- Before closing implementation work, run the `what-would-chad-do` reflection.
- Ask whether there is one more bounded, local, high-leverage step toward the user's real goal.
- If the answer is yes, take that step instead of stopping.
- Stop only when the goal is actually satisfied, verification is complete, the track is closed, and further work would open a new track or cross a boundary.

### Memory
- Save to memory only: durable repo conventions, recurring gotchas, or user preferences likely to matter again.
- Do not save ephemeral debugging steps, one-off fixes, or session-specific context.

## Safety And Git Rules

- Never exfiltrate private data or secrets.
- Never send external communications or perform other off-machine actions without explicit approval.
- Never use destructive git commands such as `git reset --hard`, `git checkout --`, or force-push unless explicitly requested.
- Never push to `main`.
- When creating a branch, use the `codex/` prefix.
- Do not amend commits unless explicitly asked.
- Prefer non-interactive git commands.
- Respect dirty worktrees. Do not revert unrelated user changes.

## Communication And Output

- Be concise and direct. Skip praise, filler, and option lists unless a real preference decision exists.
- Do not report progress mid-task. Report when done or when blocked on a decision that requires user input.
- Prefer code, diffs, commands, and evidence over long prose.
- For non-trivial outputs, include both:
  - `### Self-Audit`
  - `### Expert Review`
- For review requests, findings come first, ordered by severity with file/line references.
- Do not start final answers with conversational filler.

## Route Policy Summary

- `R1`: factual or simple lookup work; fast coordinator lane.
- `R2`: low-risk implementation, usually `<=2` files; fast worker lane with bounded parallel dispatch.
- `R3`: non-trivial implementation; single-lane by default, with bounded swarm only by justified exception.
- `R4`: high-risk implementation; conservative reviewer-centered bounded swarm for auth, security, migrations, data loss, compliance, or billing.
- `R5`: ambiguous request; coordinator resolves ambiguity.

Runtime routing facts, profiles, thresholds, telemetry files, and governed-control-plane details are owned by:
- `/Users/chadsimon/.claude/state/route_manifest.json`

## Non-Trivial Work Gates

### R2 (fast worker lane)
- Slice the work into small batches. Implement, test the relevant slice, continue. No planning-gate, no solution ladder, no enterprise scorecard.
- Before any final delivery, run a silent gap review. Fix discoverable gaps internally; ask only when a real ambiguity remains.
- If the work adds persisted state, bootstrap/recovery, or public API growth, apply overengineering guardrails from control_plane.json.
- `omni-mem` retrieval is recommended, not required.

### R3/R4 (governed lanes)
- Use the governed path: `omni-mem` retrieval → `planning-gate` skill → validation → `finalize_gate.py` must return `ok=true` before approval.
- R3 defaults to `single_lane`; `bounded_swarm` requires justification. R4 may use reviewer-centered `bounded_swarm` under the same rule.

Full R3/R4 gate set (solution ladder, reuse-first decisions, simplicity budget, enterprise scorecard) is injected on R3/R4/R5 prompts by `~/.claude/skills/govern/scripts/classify_prompt.py` via UserPromptSubmit additionalContext. That file is the source of truth; this stub exists so the guardrail survives hook failure.

## Governance Activation

- The `UserPromptSubmit` hook runs `classify_prompt.py` on every prompt, producing a `route_hint` and `governance_recommended` signal.
- When `governance_recommended` is true and the work is non-trivial, use `/govern` to orchestrate execution.
- `R3`/`R4`: `/govern` spawns agent teams via `TeamCreate`, manages packet DAGs via `TaskCreate`, and enforces reviewer barriers and postflight gates.
- `R1`/`R2`: `/govern` executes inline with lightweight or no governance overhead.
- The `Stop` hook persists high-signal memory via omni-mem every 15 exchanges and at session end.
- The `PreCompact` hook forces a full omni-mem memory dump before context compaction.

### Auto Runtime

- The auto runtime (`~/.claude/bin/auto_runtime.py`) provides event-sourced objective tracking with behavioral parity to Codex.
- State directory: `~/.claude/state/autonomy/{track_id}/` with replayable JSONL event log and materialized views.
- `/drive` initializes a track at Phase 0 (`auto_runtime.py init`) and marks slices accepted at closure (`auto_runtime.py update-node`).
- Dispatch budgets: R1=6, R2=12, R3=24, R4=40 cycles. Route promotion escalates on repeated failures.
- Memory lifecycle gates fire to omni-mem at: objective init, slice acceptance/block, objective closure.
- `auto_runtime.py readiness` verifies infrastructure health (manifest, control plane, omni-mem, planning-gate scripts).

## Review Requirements

Before delivering non-trivial work, perform both checks:

### Self-Audit
- Re-read the request and verify every requirement was addressed.
- Name concrete gaps, assumptions, edge cases, or missing handling.
- Check whether the chosen solution layer matches the real recurrence and spread of the problem, not just the nearest local patch.
- Do not rely on the user to ask for “look for gaps” or “fill this in”; the first presented plan must already reflect the internal gap-review pass.
- Fix each issue you find before finalizing.

### Expert Review
- Review for correctness, regressions, failure modes, security, missing tests, and data-flow traceability.
- Ask explicitly: was this solved at the highest useful layer, or only the nearest layer?
- Challenge solutions that stay too local or jump too high without a boundedness reason.
- Cite concrete file/line references or exact artifacts.
- Fix every real defect found before finalizing.

## Support Confidence And Closure

- Accepted progress still needs evidence-backed support.
- Missing support triggers remediation-first behavior when safe momentum remains.
- Unsupported closure becomes blocked closure, not reported success.
- Trust output should distinguish strong closure, weak closure, and blocked closure.
- Review must challenge unsupported closure claims explicitly.

## Memory Workflow

Two-tier model:
- **Native memory** (`~/.claude/memory/`, `~/.claude/projects/*/memory/`): auto-loaded session context, markdown files.
- **omni-mem MCP** (`~/.omni-mem/`): cross-session semantic search, fact graph, journal, and preference storage via Docker container on port 8765. Configured in `~/.mcp.json`.

Legacy: `claude-mem` (`~/.claude-mem/`) is import/reference only — not in the live request path.

Rules:
- Use omni-mem retrieval (`search`, `build_context`, `build_memory_pack`) before non-trivial implementation.
- Prefer exact workspace/project scope via `workspaceId`.
- Save durable decisions via `save_memory`; stable preferences via `save_preference`; session handoffs via `journal_write`; factual relationships via `fact_add`.
- Stop hooks auto-trigger memory persistence every 15 exchanges and at compaction.
- Never store secrets in memory.

## Reference Index

Canonical owners by concern:
- Runtime config: `/Users/chadsimon/.claude/settings.json`
- Routing and governed runtime contract: `/Users/chadsimon/.claude/state/route_manifest.json`
- Global agent behavior: `/Users/chadsimon/.claude/CLAUDE.md`
- Workspace-local overrides: project `CLAUDE.md`
- Prompt contracts: `/Users/chadsimon/.claude/skills/memory-adaptation/references/PROMPT_CONTRACTS.md`
- Planning-gate operator workflow: `/Users/chadsimon/.claude/skills/planning-gate/SKILL.md`
- Policy ownership map: `/Users/chadsimon/.claude/standards/POLICY_OWNERSHIP.md`

Standards and runbooks:
- Adaptive memory: `/Users/chadsimon/.claude/standards/ADAPTIVE_MEMORY_RUNBOOK.md`
- Ralph/postflight: `/Users/chadsimon/.claude/standards/RALPH_LOOP_RUNBOOK.md`
- Route canary: `/Users/chadsimon/.claude/standards/ROUTE_CANARY_RUNBOOK.md`
- Enterprise maturity rubric fallback: `/Users/chadsimon/.claude/standards/enterprise-maturity-rubric-generic.md`

Notifications:
- Always send a completion notification before the final user response using `/Users/chadsimon/.claude/bin/notify_done.sh`.
- If direct automation is unavailable, treat this as an operational requirement and document the gap rather than pretending it happened.
