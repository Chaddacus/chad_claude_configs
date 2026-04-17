---
name: skills-janitor
description: Scan local session transcripts to find installed skills that haven't been invoked recently. Produces a report of active vs stale skills and flags stale ones as removal candidates. Use when skill count is growing and you want to reclaim context window.
---

# Skills Janitor

Every installed skill's description loads into the Skill tool schema at session start, costing tokens on every conversation. Stale skills that are never invoked are pure tax. This skill scans your transcripts, counts invocations per skill over a configurable window, and reports which skills are dead weight.

## How it works

Runs `~/.claude/bin/skills_janitor_scan.py`, which:

1. Enumerates installed skills in `~/.claude/skills/` and plugin skills under `~/.claude/plugins/`.
2. Walks every `.jsonl` transcript under `~/.claude/projects/*/`.
3. Counts `Skill` tool invocations by skill name within the lookback window (default: 60 days).
4. Classifies each installed skill as **active** (>=1 invocation) or **stale** (zero invocations).
5. Also reports **ghost invocations** — skills invoked in transcripts but no longer installed (e.g., renamed or removed).

## Usage

Text report:
```bash
python3 ~/.claude/bin/skills_janitor_scan.py
python3 ~/.claude/bin/skills_janitor_scan.py --days 30
```

JSON for scripting:
```bash
python3 ~/.claude/bin/skills_janitor_scan.py --json
```

## Interpretation

- **Stale** skills are removal candidates, not automatic deletes. Before removing a skill, check:
  - Is it a seasonal skill (used once a quarter)? Widen the window before judging.
  - Is it a one-off or scheduled skill (`/loop`, `/schedule`-adjacent)? May not appear in interactive transcripts.
  - Did you rename it recently? Check `ghost_invocations` for the old name.
- **Active** count with a very low invocation count (1-2 over 60 days) is a weaker version of stale — consider consolidation.
- **Ghost invocations** suggest old skills whose transcripts survive; no action unless renaming.

## Safe removal workflow

1. Run the scan.
2. For each stale skill, check the README/SKILL.md to confirm it's not infrastructure (e.g., invoked indirectly by another skill or a hook).
3. If safe, `rm -rf ~/.claude/skills/<name>/` (after a backup to `~/.claude/backups/<date>/`).
4. Re-run the scan to confirm the ghost now shows in `ghost_invocations`.

Do NOT delete plugin-provided skills — remove the plugin instead via `/plugin uninstall`.

## Output schema

```json
{
  "window_days": 60,
  "since": "2026-02-16T...",
  "installed_count": 37,
  "active_count": 12,
  "stale_count": 25,
  "active": [["ecosystem-update", 4, "2026-04-17"], ...],
  "stale": ["book-factory", "codebase-to-course", ...],
  "ghost_invocations": ["old-renamed-skill"]
}
```
