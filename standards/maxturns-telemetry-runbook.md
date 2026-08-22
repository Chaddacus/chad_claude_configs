# maxTurns Telemetry Runbook

**Cert tie-in:** Architect Foundations Task 1.1. Anti-pattern is "setting arbitrary
iteration caps as the primary stopping mechanism." A healthy agentic loop
terminates on `stop_reason: "end_turn"`. If `max_turns` is firing, the loop
didn't recognize a natural stop — that's the bug, not the cap.

## What gets logged

`~/.claude/bin/stop_reason_telemetry.py` runs on `Stop` and `SubagentStop`.
For each terminal assistant message it records:

- **`~/.claude/state/stop_reason_telemetry.jsonl`** — one record per
  termination: `ts`, `event`, `agent_type`, `agent_id`, `session_id`,
  `stop_reason`, `model`.
- **`~/.claude/state/stop_reason_counters.json`** — rolling counters
  `{agent_type: {stop_reason: count}}` for quick review.

## How to read the signal

The cert says "if hit rate > a few %, the loop is broken." Healthy
`stop_reason` values: `end_turn`, `tool_use` (mid-loop), `stop_sequence`.
Unhealthy: `max_tokens`, `max_turns`, `pause_turn`.

Pull the bad rate for the past week:

```bash
# Total terminations per agent_type
jq -r '. | to_entries[] | "\(.key)\t\(.value | to_entries | map(.value) | add)"' \
  ~/.claude/state/stop_reason_counters.json

# Bad-rate per agent_type (max_tokens + max_turns)
jq -r '. | to_entries[] |
  .key as $a |
  .value as $v |
  ([($v.max_tokens // 0), ($v.max_turns // 0)] | add) as $bad |
  (.value | to_entries | map(.value) | add) as $total |
  "\($a)\t\($bad)/\($total)\t\(if $total>0 then ($bad*100/$total) else 0 end)%"' \
  ~/.claude/state/stop_reason_counters.json
```

If any agent shows `>3%` bad rate over a 7-day window, treat it as a real
incident — the agent's prompt/loop logic isn't recognizing its natural
stop, and the `maxTurns` cap is masking the bug. Fix the loop, do not raise
the cap.

## Where the cap is set

- Per-agent: `tools:` and `maxTurns:` frontmatter in `~/.claude/agents/*.md`
- chad-agent: 200
- Most specialized agents: 20-35

## Reset counters

```bash
echo '{}' > ~/.claude/state/stop_reason_counters.json
# Optional: archive the jsonl
mv ~/.claude/state/stop_reason_telemetry.jsonl \
   ~/.claude/state/stop_reason_telemetry.$(date +%Y%m%d).jsonl
```

## Implementation notes

- Hook payload does NOT include `stop_reason` directly (verified 2026-05-13);
  the hook reads the last 64KB of the transcript JSONL referenced by
  `agent_transcript_path` (for `SubagentStop`) or `transcript_path` (for `Stop`).
- Failures are silent — telemetry must not block the agent loop.
- 9 unit tests in `~/.claude/tests/test_stop_reason_telemetry.py` cover
  end_turn / max_tokens distinction, counter accumulation, malformed
  transcripts, and event-type filtering.
