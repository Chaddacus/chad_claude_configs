---
name: runtime-procedures
description: Procedural commands for auto-runtime tracks, completion records, memory workflow, and route-specific work gates. Load when you need the specific commands — the behavioral rules live in CLAUDE.md.
---

# Runtime Procedures

Procedural detail for the autonomous execution surface. CLAUDE.md owns the behavioral rules; this skill owns the commands and checklists.

## Auto-Runtime Track Management

Initialize a track for non-trivial work:
```bash
python3 ~/.claude/bin/auto_runtime.py init --task "<objective>" --cwd "$PWD"
```

For autonomous task runs, use the invocation-scoped manager loop:
```bash
auto_runtime.py manager-run-task --cwd <repo> --task "<objective>"
```

Advance the track through dispatch -> verification -> acceptance:
```bash
auto_runtime.py cycle
```

Update slice state with evidence refs on completion:
```bash
auto_runtime.py update-node --state accepted --evidence "..."
```

Operational mechanics (hook wiring, team spawning via TeamCreate/TaskCreate, state directory layout, memory lifecycle gates, `readiness` checks): `~/.claude/standards/AUTO_RUNTIME.md`.

## Completion Records (Stop-Gate L2)

Before stopping on non-trivial work, file a structured completion record (`completion` | `blocked` | `fork`). The stop-gate L2 layer validates the record against recorded tool activity; file it before the final response, not after — the Stop hook reads `completion.json` from disk.

Full procedure (JSON shapes, required fields per kind, example invocation of `~/.claude/bin/claim_complete.py`): `~/.claude/standards/STOP_GATE_L2.md`.

Completion checklist:
- Don't ask "should I proceed?" unless there is genuine ambiguity about DIRECTION.
- "Default to action" means: execute the next governed step. It does NOT mean: bypass governance checkpoints, reviewer barriers, or verification gates.
- Close the auto-runtime track: mark slices accepted with evidence, then run `auto_runtime.py cycle` to reach `OBJECTIVE_COMPLETE`.
- Ask whether there is one more bounded, local, high-leverage step toward the user's real goal. If yes, take it.
- Stop only when the goal is actually satisfied, verification is complete, the track is closed, and further work would open a new track or cross a boundary.

## Support Confidence And Closure

- Accepted progress still needs evidence-backed support.
- Missing support triggers remediation-first behavior when safe momentum remains.
- Unsupported closure becomes blocked closure, not reported success.
- Trust output should distinguish strong closure, weak closure, and blocked closure.
- Review must challenge unsupported closure claims explicitly.

## Review Requirements

Before delivering non-trivial work, perform both checks:

### Self-Audit
- Re-read the request and verify every requirement was addressed.
- Name concrete gaps, assumptions, edge cases, or missing handling.
- Check whether the chosen solution layer matches the real recurrence and spread of the problem.
- Fix each issue you find before finalizing.

### Expert Review
- Review for correctness, regressions, failure modes, security, missing tests, and data-flow traceability.
- Was this solved at the highest useful layer, or only the nearest layer?
- Cite concrete file/line references or exact artifacts.
- Fix every real defect found before finalizing.

## Memory Workflow

Two-tier model (native markdown memory + omni-mem MCP; architecture detail in `~/.claude/standards/REFERENCE_INDEX.md`).

- Use omni-mem retrieval (`search`, `build_context`, `build_memory_pack`) before non-trivial implementation; prefer exact workspace scope via `workspaceId`.
- Save durable decisions via `save_memory`; stable preferences via `save_preference`; session handoffs via `journal_write`; factual relationships via `fact_add`.
- Stop hooks auto-trigger memory persistence every 15 exchanges and at compaction.
- Never store secrets in memory.

## Non-Trivial Work Gates

### R2 (fast worker lane)
- Slice the work into small batches. Implement, test the relevant slice, continue. No planning-gate, no solution ladder, no enterprise scorecard.
- Before any final delivery, run a silent gap review. Fix discoverable gaps internally; ask only when a real ambiguity remains.
- If the work adds persisted state, bootstrap/recovery, or public API growth, apply overengineering guardrails from control_plane.json.
- `omni-mem` retrieval is recommended, not required.

### R3/R4 (governed lanes)
- Use the governed path: `omni-mem` retrieval -> `planning-gate` skill -> validation -> `finalize_gate.py` must return `ok=true` before approval.
- R3 defaults to `single_lane`; `bounded_swarm` requires justification. R4 may use reviewer-centered `bounded_swarm` under the same rule.
- Full R3/R4 gate set (solution ladder, reuse-first decisions, simplicity budget, enterprise scorecard) is injected on R3/R4/R5 prompts by `classify_prompt.py`.

## Governance Activation

- Act on the `UserPromptSubmit` hook's `route_hint`/`governance_recommended` signals; when governance is recommended and the work is non-trivial, use `/govern` to orchestrate execution.
- Init tracks for non-trivial work, advance with `auto_runtime.py cycle`, close with evidence via `update-node`.
