---
name: build-backlog
description: Aggregates Build Queue items across all ecosystem-update reports, deduplicates, ranks by persistence, and filters out already-implemented items. Use --done <item-slug> to mark something built.
disable-model-invocation: true
---

# Build Backlog

Aggregate and rank unbuilt items from ecosystem-update reports.

**View backlog:** `/build-backlog`
**Mark done:** `/build-backlog --done <item-slug>`

---

## Mode: `--done <item-slug>`

If `$ARGUMENTS` starts with `--done`:

1. Extract the slug from the argument (everything after `--done `)
2. Read `~/.claude/state/build-backlog-implemented.json` (create if missing: `{"implemented": []}`)
3. Add the slug to the `implemented` array if not already present
4. Write the file back
5. Confirm: "Marked `<slug>` as implemented. It will no longer appear in the backlog."

Done. Exit.

---

## Mode: View Backlog (default)

### Step 1 — Load implemented list

Read `~/.claude/state/build-backlog-implemented.json`.
If missing, treat as `{"implemented": []}`.

### Step 2 — Read all ecosystem reports

Glob `~/.claude/reports/ecosystem/*.md` — read every file.

For each report, extract the `## Build Queue` section. Parse each bullet:
```
- **{Item name}** ({type}) — {source} — {description}
```

Build a candidate map keyed by normalized slug (item name → lowercase, spaces to hyphens):
```
slug → { name, type, source, description, appearances: [YYYY-MM-DD, ...] }
```

If the same slug appears in multiple reports, merge — increment appearances list, keep the most recent description.

### Step 3 — Filter

Remove any candidate whose slug is in the `implemented` list.

### Step 4 — Rank

Sort remaining candidates by:
1. **Persistence** (descending) — how many reports it appeared in. Appeared in 5 reports = high signal.
2. **Recency** — most recent appearance date as tiebreaker.

### Step 5 — Output

```markdown
# Build Backlog — {today}

{N} items pending | {M} implemented (filtered)

| # | Item | Type | Seen | Last Seen | Source | Description |
|---|------|------|------|-----------|--------|-------------|
| 1 | Parry | hook | 3x | 2026-04-05 | vaporif/parry | Prompt injection scanner |
| 2 | Trail of Bits Security Skills | skill | 3x | 2026-04-05 | trailofbits/skills | 12+ security audit skills |
...

## To mark an item as done:
/build-backlog --done parry
/build-backlog --done trailofbits-security-skills
```

Slugs are shown so you know exactly what to pass to `--done`.

---

## State File Format

`~/.claude/state/build-backlog-implemented.json`:
```json
{
  "implemented": [
    "parry",
    "trailofbits-security-skills"
  ]
}
```
