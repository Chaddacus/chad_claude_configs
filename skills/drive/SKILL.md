---
name: drive
description: Autonomous iterative development harness. Executes a full goal without stopping to report — plan, implement, test, fix, and verify in a continuous loop. Adjusts task priorities as test results arrive. Use when you want Claude to drive implementation end-to-end with minimal interruption.
---

# /drive — Autonomous Iterative Development

Execute a development goal continuously. Plan it, implement it, test it, fix it, verify it — without stopping to report between steps. Stop only when the goal is complete or there is a genuine blocker requiring your input.

## Usage

```text
/drive Add a fibonacci function with tests
/drive Fix all failing tests in the auth module
/drive --heavy Refactor the payment service
```

## Flags

| Flag | Effect |
|------|--------|
| (none) | Lightweight plan + single-lane autonomous loop, Sonnet |
| `--heavy` | Full planning-gate + sprint contract + reviewer ack before execution |

---

## Workflow

### Phase 0 — Session Setup

1. Initialize an auto-runtime objective track for state persistence and dispatch budgeting:
   ```bash
   python3 ~/.claude/bin/auto_runtime.py init --task "<goal>" --cwd "$PWD" --route R2
   ```
   For `--heavy` mode, use `--route R3` instead. Save the returned `track_id` for later updates.
   If resuming an existing track, run wake ceremony for session orientation:
   ```bash
   python3 ~/.claude/bin/auto_runtime.py wake --track-id <track_id> --progress
   cat ~/.claude/state/autonomy/<track_id>/objective.progress.md
   ```

2. Write the autonomous mode flag so the anticipation engine and hooks engage:
   ```bash
   python3 -c "
   import json, os, time
   from pathlib import Path
   sid = os.environ.get('CLAUDE_SESSION_ID', 'default')
   Path(f'/tmp/claude-drive-{sid}.json').write_text(json.dumps({'autonomous': True, 'started_at': time.time()}))
   "
   ```
3. Commit to not stopping between steps: *"You have plenty of context remaining. Drive this task to completion without stopping to report progress."*
4. Determine route: no flag → lightweight R2-style inline; `--heavy` → full R3 planning-gate + reviewer.

### Phase 1 — Plan

**Default (no --heavy):**
- Produce: one-sentence objective, sprint contract (≤8 falsifiable acceptance criteria), task list
- Create tasks with `TaskCreate`, set `priority` metadata (0.9 = critical, 0.5 = normal, 0.3 = nice-to-have)
- Do not wait for reviewer ack — proceed directly to Phase 2

**`--heavy`:**
- Full planning-gate workflow: solution ladder, existing_primitives_considered, reuse_first_decision
- Sprint contract: reviewer must explicitly ack each criterion before execution begins
- Use Sonnet for planner/reviewer (R3); Opus only if the task escalates to R4 risk level

### Phase 2 — Autonomous Execution Loop

**Do not stop between steps. Do not report mid-loop. Surface only genuine blockers.**

```
LOOP:
  1. TaskList → pick highest-priority runnable task
  2. Execute inline:
       implement → run relevant tests → fix failures → repeat until tests pass
       Spawn a subagent ONLY if the task is output-heavy AND isolated:
         - Test suites with >50 lines of output
         - Log file analysis
         - Documentation fetching
       Subagent handoff: sprint criteria slice + owned files only (no ambient context)
       Subagent result: compact summary — pass/fail + key findings only
  3. The anticipation engine hook fires automatically after each step and injects
     the highest-weight next step into context. Trust it and dispatch immediately
     when weight ≥ 0.60. Continue with a brief note at 0.30–0.60. Pause with a
     specific question below 0.30.
  3b. If the dispatch result includes `verification_hints.playwright_recommended: true`:
       - Start the dev server if not already running
       - Use Playwright MCP tools (browser_navigate, browser_snapshot, browser_take_screenshot)
       - Check browser_console_messages for JS errors
       - This is advisory — skip if dev server unavailable or Playwright not responding
  3c. For R3/R4: after implementation, mark slice `awaiting_verification` instead of `accepted`.
       Run `auto_runtime.py cycle` — if it returns `evaluate` action with `evaluator_dispatch`:
       - Run all verification commands from the contract
       - If Playwright recommended, use browser tools for E2E verification
       - Produce a verdict: `{"pass": bool, "criteria_results": [...], "failure_details": [...]}`
       - Record: `auto_runtime.py evaluate-verdict --track-id <id> --slice-id <id> --verdict '<json>'`
       - If fail → slice transitions to rework, loop back to step 1
       - If pass → slice transitions to accepted, continue
  4. Update task queue from new findings:
       test_fail           → TaskCreate fix task (priority 0.9)
       new_dependency      → TaskCreate explore task (priority 0.7)
       sprint_criteria_gap → TaskCreate gap task (priority 0.85)
  5. At ~70% context window usage → run /compact, then continue
  6. Terminal conditions:
       SUCCESS  → all tasks done + sprint criteria met + tests pass + finalize_gate ok=true
                  → notify_done → clean up drive state → report what was built
       BLOCKED  → genuine ambiguity, external dependency, authority boundary
                  → stop with ONE specific question
       STUCK    → same approach fails 3× → stop with root cause analysis, not retry
```

### Phase 3 — Closure

On success:
1. Mark the auto-runtime slice as accepted and trigger closure:
   ```bash
   python3 ~/.claude/bin/auto_runtime.py update-node --track-id <track_id> --node-id slice-1 --state accepted --acceptance-source inline_verified --evidence "test_pass,typecheck_pass"
   python3 ~/.claude/bin/auto_runtime.py cycle --track-id <track_id>
   ```
2. Run `validate_impl.py` and `finalize_gate.py` (planning-gate scripts)
3. Send notification:
   ```bash
   bash $CLAUDE_HOME/bin/notify_done.sh --status success --task "<goal summary>" --channel desktop
   ```
4. Clean up drive state:
   ```bash
   rm -f /tmp/claude-drive-$CLAUDE_SESSION_ID.json
   ```
5. Report: what was built, evidence (test output + finalize_gate result), any deferred items

---

## Token Budget

| Default | Why |
|---------|-----|
| Single-lane inline, no TeamCreate | Avoids 15× parallel overhead |
| Sonnet for all execution | Opus reserved for R4 only |
| Zero-call anticipation engine | Pure Python, no token cost |
| Subagents: compact summaries only | Prevents output bloat in coordinator |
| /compact at 70% context | Extends session 30–50% |

---

## Self-Audit (before stopping)

- [ ] All sprint criteria have concrete evidence, not summary claims
- [ ] Tests pass and typecheck is clean
- [ ] No unverified code edits remaining in ledger
- [ ] finalize_gate returned `ok=true`
- [ ] Drive state file cleaned up
- [ ] Notification sent
