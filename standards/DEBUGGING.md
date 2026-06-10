# Debugging the Claude runtime

Fault-isolation procedure for hook/CLAUDE.md/skill misbehavior. Created
2026-06-09 (P3); verified against CLI 2.1.170.

## Step 0 — isolate with --safe-mode

`claude --safe-mode` starts a session with customizations disabled. If the
fault disappears, it lives in this config (hooks, CLAUDE.md, skills,
plugins); if it persists, it's the CLI or the model — stop debugging the
config.

## Hook faults

- Single hook, controlled stdin: `echo '<payload json>' | python3 ~/.claude/bin/<hook>.py`
  Always include `"session_id"` in the payload — hooks fail open without it
  (see `case_file.resolve_session_id`).
- Chains: `python3 ~/.claude/bin/hook_chain.py --chain {stop|post-edit|post-bash|post-failure}`
  with the same stdin. Members run in CHAINS order; first block wins.
- A hook that runs alone but not live: check `hook_profile.PROFILES` —
  hooks calling `should_run()` are silently disabled on R1/R2 routes unless
  their id is in the minimal/standard allowlists.
- Crashed Stop hooks: `~/.claude/state/stop-failures.jsonl` (StopFailure hook).
- Profiling: `~/.claude/bin/hook_profile.py`, `~/.claude/bin/hookeval`.

## Guard false positives

`pre_tool_guard.py` strips quoted spans before destructive-pattern matching
(inline-exec `-c` commands exempt, tested on stripped text). If a legitimate
command is blocked, check whether the trigger text is inside quotes — if it
is and still blocks, the stripper has a gap; extend the probe set in the
guard's history (omni-mem: "pre_tool_guard matching contract").

## Where evidence lives

- Per-session tool activity: `~/.claude/state/cases/<session_id>/`
- Verification ledgers: `~/.claude/state/verify-ledgers/<session_id>.json`
- Stop-gate audit: `~/.claude/state/stop_gate_audit-<session_id>.jsonl`
- Secret tripwire: `~/.claude/state/secret-leaks.jsonl`
- Config drift: `~/.claude/state/config-drift.jsonl`
