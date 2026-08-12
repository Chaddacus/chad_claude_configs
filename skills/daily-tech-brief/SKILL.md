---
name: daily-tech-brief
description: Generate a TLDR-style daily tech brief covering AI, developer tooling, GitHub repos, and research papers, then map the result back to the current Codex setup.
context: fork
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
lexical_guard_profile: stale_names,destructive_rollback,branch_policy_live
---

# Daily Tech Brief

Use this skill when you need a short newsletter-style issue that aggregates public tech signals across:
- AI
- Developer tooling
- Trending GitHub repos
- Research papers

The output is a dated Markdown issue at:
- `~/.claude/reports/daily-tech-brief/{YYYY-MM-DD}.md`

## Workflow

1. Generate the issue with:

```bash
python3.11 ~/code2/utils/daily_tech_brief.py \
  --output ~/.claude/reports/daily-tech-brief/$(date +%F).md
```

Create the output directory if it doesn’t exist: `mkdir -p ~/.claude/reports/daily-tech-brief`.

2. The issue must include:
- `TL;DR`
- `AI`
- `Developer Tooling`
- `Trending GitHub Repos`
- `Research Papers`
- `What Matters For My Setup`
- `Recommended Changes`
- `Watchlist`

3. Relevance must be grounded in:
- `~/.claude/CLAUDE.md`
- `~/.claude/settings.json`
- `~/.claude/state/route_manifest.json`
- installed skills under `~/.claude/skills/`
- the current workspace

## Constraints

- Use only public unauthenticated sources.
- Keep the issue concise and TLDR-style.
- Do not mutate Claude config or rules automatically based on the day’s findings.
- Same-day reruns should overwrite the existing dated file.
