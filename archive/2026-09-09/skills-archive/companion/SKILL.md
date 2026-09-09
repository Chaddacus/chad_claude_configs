---
name: companion
description: Your Codex companion pet — show stats, level progress, pet it, hatch it
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
---

# Companion

Your persistent Codex companion. Earns XP from your work, levels up over time, evolves cosmetically.

## Subcommands

### `/companion` or `/companion show`
Run `python3 ~/.claude/skills/companion/scripts/show_card.py` and display the output.

### `/companion stats`
Same as show, but also display session XP summary.

### `/companion pet`
Run `python3 ~/.claude/skills/companion/scripts/pet.py`.

### `/companion mute`
Run `python3 ~/.claude/skills/companion/scripts/mute_toggle.py mute`.

### `/companion unmute`
Run `python3 ~/.claude/skills/companion/scripts/mute_toggle.py unmute`.

### `/companion hatch`
Generate a name and one-sentence personality for the companion's species (read from state or roll from USER env var). Then call:
`python3 ~/.claude/skills/companion/scripts/hatch_write.py "<name>" "<personality>"`

Guidelines for name/personality generation:
- Name: 1-2 words, max 20 chars, fits the species vibe
- Personality: one short sentence, max 100 chars, describes how they approach the world
- Examples: "Crumbs" / "chaotic-optimist who treats every error as a gift"

### `/companion rename <name>`
Run `python3 ~/.claude/skills/companion/scripts/hatch_write.py --rename-only "<name>"`.

## Current Companion Info
To read current state: `python3 -c "import json,pathlib; print(json.dumps(json.loads((pathlib.Path.home()/'.claude/state/companion-state.json').read_text()), indent=2))"`
