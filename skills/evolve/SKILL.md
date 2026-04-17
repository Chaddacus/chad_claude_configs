---
name: evolve
description: Self-evolving autonomous development mode. Pops the next task from ~/.claude/evolve/task_queue.jsonl, runs it through /orchestrate-local, extracts structured observations, auto-analyzes failure patterns across run history, applies safe additive fixes to prompts/skills/presets, and prints fitness deltas. Use when the user wants the orchestrator system to measurably improve across successive builds without manual prompt-engineering between runs. Pairs with /loop — `/loop /evolve` runs one task per iteration.
---

# /evolve — Self-evolving autonomous development

The outer loop around `/orchestrate-local` that turns each build run into training signal for the orchestrator itself. Ships tasks, measures failures, auto-applies additive fixes, tracks fitness over time.

## When to use

- You want the orchestrator to get better at building things across successive runs without hand-editing prompts between builds.
- You have a task queue (or are fine using the curated kata list at `~/.claude/evolve/task_queue.jsonl`).
- You're OK with the system literally editing its own `.goosehints` and skill files within the auto-apply safety envelope.

## When NOT to use

- For a single one-off project — just use `/orchestrate-local` directly.
- If the LM Studio upstream is down — preflight will fail, /evolve will mark the task as infra_down and try the next one, but if the whole loop can't see the model, nothing progresses.
- If you want hand control over every rule that ends up in `.goosehints` — /evolve auto-appends to it.

## Workflow — one iteration

### Phase 0 — Pick

```bash
python3 ~/.claude/bin/evolve_run.py pick
```

Output is a JSON with the chosen task, its workspace (`~/code/evolve-{task_id}/`), and an instruction blurb. The queue entry is marked `in_progress` and `started_at` is set.

Abort if:
- Output has `"error": "empty queue"` — nothing to do.
- Output has `"error": "no pending tasks"` — queue fully consumed or stuck.

### Phase 1-3 — Run /orchestrate-local on the task

Follow the full `/orchestrate-local` workflow on the staged workspace and goal:
- Plan slices (≤3 files each, deterministic verify per slice).
- Write acceptance scripts in `<workspace>/.claude-gates/verify_slice_N.sh` BEFORE dispatching.
- For each slice: dispatch via `~/.claude/bin/goose_dispatch.py`, SAVE the stdout (JSON) to `<workspace>/.claude-gates/slice_N_result.json`, interpret outcome, escalate to supervisor-takeover on `fail`/`escalate`/`gate_cheat_suspected`.
- Phase 3.5 user-journey smoke — mandatory. Capture artifacts.

Track the list of slice IDs where YOU (Claude supervisor) had to take over — these become part of the observation.

### Phase 4 — Record

```bash
python3 ~/.claude/bin/evolve_run.py record \\
    --task-id <id> \\
    --workspace <abs-path> \\
    --dispatch-logs "<ws>/.claude-gates/slice_1_result.json,<ws>/.claude-gates/slice_2_result.json,..." \\
    --supervisor-takeovers "<slice-id-1>,<slice-id-2>" \\
    --started-at "<iso-ts from pick output>" \\
    --window 5
```

This one command does FOUR things in sequence:
1. `evolve_extract.py` — parses each dispatch JSON, classifies failures, appends a run record to `~/.claude/evolve/history.jsonl`.
2. `evolve_analyze.py` — reads recent history, detects recurring patterns, writes new proposals to `~/.claude/evolve/proposals.jsonl` (deduped by evidence hash).
3. `evolve_apply.py` — applies proposals with `auto_apply=true` that target files in the allowlist (`~/.goosehints`, `~/.config/goose/skills/`, `~/.claude/bin/presets/`). Never-modify list blocks changes to dispatcher, settings.json, auto_runtime.py, or the evolve scripts themselves. Backups are written to `~/.claude/evolve/backups/`.
4. `evolve_fitness.py` — prints a fitness report comparing the last `window` runs to the prior `window`.

Also marks the task `complete` in the queue.

### Phase 5 — Report

Summarize to the user in 3-5 lines:
- Task id and outcome (all pass / N supervisor takeovers / infra_down)
- Any proposals that auto-applied (show their target + one-line content)
- Fitness delta (first-try rate, takeovers/run, cheat count)

Do NOT summarize every slice — the history file captures that.

## Safety envelope (what /evolve CAN and CANNOT self-modify)

**Can auto-modify:**
- `~/.goosehints` (append rules)
- `~/.config/goose/skills/*.md` (append to existing skills, add new skills)
- `~/.claude/bin/presets/*.sh` (add new presets; existing are modifiable if proposal is additive)

**Cannot auto-modify (proposals get saved but require manual review):**
- `~/.claude/bin/goose_dispatch.py` (dispatcher logic)
- `~/.claude/bin/auto_runtime.py` (governance stack)
- `~/.claude/bin/evolve_*.py` (the evolve system itself)
- `~/.claude/settings.json` / `settings.local.json`
- Anything outside `~/.claude/` or `~/.goosehints`

**Diff size cap:** proposals adding > 500 characters in one block require manual review regardless of target.

## Running autonomously via /loop

```
/loop /evolve
```

This makes /evolve self-pace: run one task, record, analyze, apply, print fitness, then decide when to fire again (typically 1-2 minutes between tasks to let the user see the fitness report). The loop exits when the queue is drained.

## Common pitfalls

- **Weak acceptance gates**: /evolve inherits the calibration skill from /orchestrate-local — don't write substring greps as gates. Use presets + Phase 3.5 smoke.
- **Skipping Phase 3.5 smoke for speed**: the smoke is non-negotiable. A task that skips it doesn't count as a successful run and shouldn't be recorded.
- **Not saving dispatch JSONs**: without the dispatch result files, `evolve_extract.py` can't classify failures. Always capture stdout per dispatch to `.claude-gates/slice_N_result.json`.
- **Adding too many tasks at once**: start with 3-5 tasks, run them, verify proposals are sensible, THEN add more. Don't queue 50 kata tasks and run unsupervised overnight on v1.

## Files

- `~/.claude/evolve/task_queue.jsonl` — the task queue
- `~/.claude/evolve/history.jsonl` — one record per completed task run
- `~/.claude/evolve/proposals.jsonl` — proposed fixes (applied or pending)
- `~/.claude/evolve/applied.jsonl` — audit log of applied proposals
- `~/.claude/evolve/backups/` — before-images for every auto-applied edit
- `~/.claude/bin/evolve_*.py` — the scripts

## Rollback

If a run's fitness regresses after an auto-apply, restore the most recent backup:
```bash
ls -lt ~/.claude/evolve/backups/ | head -3
# find the relevant backup, then:
cp ~/.claude/evolve/backups/<ts>__Users_chadsimon_.goosehints ~/.goosehints
```

Or surgically reject a specific proposal by editing `~/.claude/evolve/proposals.jsonl` (set `applied: false` on its record) and undoing the content change manually.
