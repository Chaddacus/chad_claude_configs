---
name: autoconfig
description: "24/7 self-improving Claude configuration loop — continuously optimizes runtime config via automated benchmarking"
user_invocable: true
commands:
  - status
  - run
  - report
  - rollback
  - best
  - pause
  - resume
---

# AutoConfig — Self-Improving Configuration Loop

Inspired by Karpathy's autoresearch: autonomously mutate config knobs, benchmark via `claude -p`, keep improvements, discard regressions. Improvements compound — each kept change becomes the new baseline.

## Commands

### `/autoconfig status`
Show current state: phase, experiments run, improvements found, rate limit status, daemon state.

**Steps:**
1. Read `~/.claude/state/autoconfig/program_state.json`
2. Query experiment DB for totals: `get_experiment_count()`, `get_total_kept()`, `get_cumulative_improvement()`
3. Check daemon process: `pgrep -f experiment_daemon.py`
4. Check rate limit state
5. Display summary table

### `/autoconfig run`
Run one experiment cycle manually (foreground).

**Steps:**
1. Import and call `run_one_experiment()` from `experiment_daemon.py`
2. Display the mutation tried, benchmark results, and keep/discard decision
3. If kept, show the improvement percentage

### `/autoconfig report`
Generate a comprehensive report of experiment history.

**Steps:**
1. Query experiment DB: top improvements, trend analysis, per-phase stats
2. Compute Pareto frontier (best composite at each phase)
3. Show per-knob attribution (which changes contributed most improvement)
4. Show total drift from original config (knobs changed)
5. Show cumulative improvement curve
6. Show convergence status per phase

### `/autoconfig rollback`
Restore the original baseline config.

**Steps:**
1. Confirm with user: "This will restore the config to the initial baseline. Continue?"
2. Call `restore_snapshot("baseline")`
3. Verify config is valid (JSON parse, required fields present)
4. Display what changed

### `/autoconfig best`
Apply the best-ever config.

**Steps:**
1. Check if best snapshot exists
2. Show comparison: current score vs best score
3. Call `restore_snapshot("best")`
4. Verify config validity

### `/autoconfig pause`
Stop the daemon temporarily.

**Steps:**
1. Run `launchctl unload ~/Library/LaunchAgents/com.chadsimon.autoconfig.plist`
2. Verify daemon stopped
3. If mid-experiment, restore checkpoint

### `/autoconfig resume`
Restart the daemon.

**Steps:**
1. Run `launchctl load ~/Library/LaunchAgents/com.chadsimon.autoconfig.plist`
2. Verify daemon started
3. Show current phase and experiment count

## Config Surface

| Target | File | Knobs |
|---|---|---|
| Route manifest | `~/.claude/state/route_manifest.json` | ~200 (models, effort, lanes, swarm, dispatch) |
| Settings | `~/.claude/settings.json` | ~5 (effort level, model defaults) |
| Agent defs | `~/.claude/agents/*.md` | ~15 (model, effort, behavioral rules per agent) |

## Safety

- Immutable fields are NEVER touched (permissions, hooks, MCP servers, risk classes)
- All mutations validated against bounds before application
- Checkpoint/rollback on every experiment
- Crash recovery on daemon restart
- See `references/safety_spec.md` for full guardrail documentation

## References

- `references/safety_spec.md` — Immutable fields, mutation bounds, isolation rules
- `references/metric_spec.md` — Composite metric formula, noise band, confirmation trials
- `program.md` — Current research phases and exploration strategy
- `scripts/run_benchmark_matrix.py` — Files-first repeated benchmark comparison across `current`, `baseline`, and `best` presets
