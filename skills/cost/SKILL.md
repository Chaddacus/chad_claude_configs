---
name: cost
description: "Run cc-cost on session transcripts: per-session cost, cache hit rate, tool-call distribution, top-expensive turns, optimization advice. Use for /cost or questions about token usage."
disallowed-tools: Write, Edit, NotebookEdit
---

# /cost — Local Claude Code Cost Diagnostics

Wraps `~/.claude/bin/cc-cost.py` (from `lob-labs/cc-cost`, single-file Python, ~330 lines, no external deps beyond stdlib).

## What it does

Parses session transcripts under `~/.claude/projects/<dir>/<id>.jsonl` and reports:
- Per-session cost in USD + token counts
- Prompt-cache hit rate (load-bearing for cost optimization)
- Tool call distribution + per-tool token cost
- Top expensive turns
- Actionable cost-optimization recommendations (`--diagnose`)

## How to invoke

Default scan across all projects:

```bash
python3 ~/.claude/bin/cc-cost.py
```

Top N expensive sessions:

```bash
python3 ~/.claude/bin/cc-cost.py --top 10
```

Drill into one transcript:

```bash
python3 ~/.claude/bin/cc-cost.py /Users/chadsimon/.claude/projects/-Users-chadsimon/<session-id>.jsonl
```

Diagnose a specific session with optimization advice:

```bash
python3 ~/.claude/bin/cc-cost.py --diagnose <transcript.jsonl>
```

Machine-readable output for piping:

```bash
python3 ~/.claude/bin/cc-cost.py --json
```

## When to suggest this

- User asks "how much did that cost" or "what's our token usage"
- A run blew through a dispatch budget — surface which turn was expensive
- Investigating low cache hit rate — `--diagnose` highlights cache-busting patterns
- Planning a new agent — check whether the current similar agent type is cost-efficient before adding load

## Out of scope

This skill reports on **local** transcript data only. Anthropic billing dashboard is the authoritative cost source; `cc-cost` is a fast local lens. If numbers diverge, the dashboard wins.
