---
name: ecosystem-update
description: Daily self-improvement loop — scans GitHub, arxiv, and Claude community sources for new patterns, diffs against current setup, implements Quick Wins, writes report. Use --dry-run for report only.
context: fork
policy_doc_kind: skill
classification: canonical
canonical_owner: self
authority_level: procedural
in_verifier_scope: true
---

# Ecosystem Update

Scan for new Claude Code patterns from the community, diff against current setup, implement Quick Wins, and write a dated report.

**Default (loop-safe):** `/ecosystem-update` — backs up, implements Quick Wins, writes report
**Report only:** `/ecosystem-update --dry-run` — fetches and scores but makes no changes
**Professional / public:** `/ecosystem-update --professional` (aka "professional variant", "public variant", "chadacus variant") — generate the standard report, then render the scrubbed public version via the chadacus.dev pipeline. See "Professional Variant" section below.

**Output:** `~/.claude/reports/ecosystem/{YYYY-MM-DD}.md`

---

## Step 1 — Read Current State

Before fetching anything external, snapshot the current config so you can diff against it:

1. Read `~/.claude/CLAUDE.md` — note: hooks wired, agents defined, skills installed, memory workflow
2. Read `~/.claude/settings.json` — note: all hook events, permissions, MCPs, plugins
3. Glob `~/.claude/agents/*.md` — read frontmatter of each (name, tools, model, isolation flag)
4. Glob `~/.claude/skills/` — list all installed skill directories
5. Search omni-mem: `mcp__omni-mem__search "ecosystem-update seen"` — retrieve previously reported item hashes to skip

Also read `~/.claude/state/ecosystem-update-last-run.json` — the `seen_items` array contains identifiers of previously reported items. Skip any candidate whose identifier appears in that list.

Build an internal "already have" list dynamically from the above reads. Do not hardcode specific items — always derive from current file state.

---

## Step 2 — Fetch Sources

Fetch in parallel where possible. **Tool preference:** use wigolo (`mcp__wigolo__search` / `mcp__wigolo__fetch`) when available — its persistent cache means repeat fetches are instant and `diff`/`cache` can show what changed on a source since the last run. Fall back to WebSearch/WebFetch only if wigolo is not granted. (Some agents, e.g. chad-work, carry wigolo but not WebSearch/WebFetch — the skill must still run there.)

### Tier 1 — Always fetch (daily signal)

| Source | What to extract |
|--------|----------------|
| `https://github.com/hesreallyhim/awesome-claude-code` | New skills, hooks, agents, orchestrators added to the catalog |
| `https://howborisusesclaudecode.com/` | Boris's latest tips and workflow patterns |
| `https://github.com/shanraisshan/claude-code-best-practice` | New tips, CLAUDE.md patterns, hook techniques |
| Official settings JSON schema (`https://json.schemastore.org/claude-code-settings.json`) | Diff schema keys against current `settings.json` — new settings not yet adopted. Highest-yield source on the 2026-07-29 run when community catalogs were quiet |

Search supplement: `"claude code" new hooks agents skills site:github.com {current year}`

### Tier 2 — Daily (skip if state file shows `tier2_last_run` within 24 hours)

| Source | What to extract |
|--------|----------------|
| `https://arxiv.org/search/?searchtype=all&query=LLM+agent+coding&order=-announced_date_first` | Papers published since `tier2_last_run` (not a fixed 24h — runs slip) with applicable multi-agent or verification patterns |

Search supplement: `arxiv.org LLM agent coding autonomous {current year} site:arxiv.org`

### Tier 3 — Weekly (skip if state file shows `tier3_last_run` within 7 days)

| Source | What to extract |
|--------|----------------|
| `https://github.com/rohitg00/awesome-claude-code-toolkit` | New agents (135+), hooks (19+), skills from comprehensive catalog |
| `https://code.claude.com/docs/` | New official features, new hook events, new CLI flags |

---

## Step 3 — Extract Candidates

For each source, extract discrete items. Each candidate must have:
- **Title**: short name
- **Source**: URL or repo
- **Type**: `hook` | `agent-pattern` | `skill` | `claude-md` | `mcp` | `research`
- **Description**: what it does, one sentence

Types to look for:

**Hooks:** New hook events not in settings.json (e.g., `PostCompact`, `PermissionRequest`, `once: true` modifiers, `type: prompt` hooks, `statusMessage`, shell output injection via `!command`). Check against current `settings.json` hook events.

**Agent patterns:** `isolation: worktree`, `context: fork`, `allowed-tools` wildcards (`Bash(gh *)`), `argument-hint`, per-agent model overrides, tool restriction patterns. Check against current agents/*.md frontmatter.

**Skills:** Domain-specific skills worth adapting (security, DevOps, language-specific reviewers). Check against skills/ directory listing.

**CLAUDE.md patterns:** `@import` for modular rules, keyword-routing tables, instinct scoring, prune/dream cycles, agent shield patterns. Check against current CLAUDE.md.

**MCP servers:** New MCP integrations with clear utility. Check against mcpServers in settings.json.

**Research:** Papers with directly applicable patterns (role-based SOPs, topology-aware orchestration, verifier convergence). Only include if directly applicable to current multi-agent setup.

---

## Step 4 — Diff and Classify

For each candidate, assign a bucket:

- **HAVE** — already implemented (skip entirely, add to Already Have list)
- **PARTIAL** — partially implemented, gap exists (include with gap description)
- **MISSING** — not in current setup
- **CONFLICTS** — contradicts an existing rule (note conflict, do not recommend)

Compare dynamically against what you read in Step 1. Do not rely on hardcoded lists — the config changes over time and hardcoded entries go stale.

---

## Step 5 — Score Candidates

Score each MISSING or PARTIAL candidate:

```
Impact  (1–3): 1=minor convenience, 2=meaningful improvement, 3=significant autonomy/safety/quality gain
Effort  (1–3): 1=one-liner or frontmatter, 2=new skill <50 LOC, 3=new script or multi-file
Alignment (Y/N): Does it pass the anti-overengineering gate?
  - Fails if: requires new service, persistence layer, orchestration engine without proof
  - Fails if: contradicts "simple is better" principle
  - Fails if: adds complexity without a concrete recurring problem to solve

Priority = Impact / Effort  (higher = better)
```

**Quick Wins**: Alignment=Y AND Priority ≥ 2.0 (high impact, low effort)
**Build Queue**: Alignment=Y AND Priority 1.0–1.9
**Research**: Papers/concepts worth understanding before deciding

---

## Step 6 — Write Report

Output file: `~/.claude/reports/ecosystem/{YYYY-MM-DD}.md`

If the file already exists (same-day rerun), overwrite it.

```markdown
# Ecosystem Update — {YYYY-MM-DD}

## TL;DR
- {most important finding, one line}
- {second finding}
- {third finding}

## Quick Wins
| Item | Source | Type | Impact | Effort | Action |
|------|--------|------|--------|--------|--------|
| ... | ... | hook | 3 | 1 | Add PermissionRequest hook to settings.json |

## Build Queue
- **{Item name}** ({type}) — {source} — {what it does and why it's worth building}

## Research
- [{Paper title}]({URL}) — {one-line relevance to current multi-agent setup}

## Already Have
{Comma-separated list of items that are already implemented — no need to revisit}

## Auto-Implemented
{Only on non-dry-run: list of Quick Wins actually applied, with file touched. Omit section if none.}

## Blocked Quick Wins
{Quick Wins the permission layer denied — one ready-to-run command or exact edit per item so Chad can apply manually. Omit section if none.}

## Rejected
- {Item} — {reason: overengineered / already covered by X / alignment failure}

---
_Sources checked: {list URLs}_
_Tier 2 fetched: {yes/no}_
_Run at: {timestamp}_
```

---

## Step 7 — Save State

**State file** — write/update `~/.claude/state/ecosystem-update-last-run.json`:
```json
{
  "last_run": "{ISO timestamp}",
  "tier2_last_run": "{ISO timestamp, only update if tier2 was fetched}",
  "tier3_last_run": "{ISO timestamp, only update if tier3 was fetched}",
  "items_seen_count": {total count},
  "seen_items": ["{item-title-slug}", ...]
}
```

The `seen_items` array is the primary deduplication mechanism. Each entry is a short slug derived from the item title (lowercase, hyphens). On the next run, any candidate whose slug matches an entry in this list is immediately bucketed as HAVE and skipped.

**omni-mem** — if available, also save a `type: reference` observation via `mcp__omni-mem__save_memory` summarizing this run's Quick Wins. This is secondary — the state file is the source of truth. If omni-mem is unavailable, skip silently and note it in the report.

---

## Step 8 — Implement Quick Wins (default, skip if --dry-run)

Unless `--dry-run` was passed, implement all Quick Wins automatically.

**Before any changes — backup:**
```bash
mkdir -p ~/.claude/backups/{YYYY-MM-DD}
cp ~/.claude/settings.json ~/.claude/backups/{YYYY-MM-DD}/settings.json
cp ~/.claude/agents/*.md ~/.claude/backups/{YYYY-MM-DD}/
```

**Then for each Quick Win:**
- Read the target file first
- Apply the change using Edit tool
- Verify the file is syntactically valid after the edit
- Add to `## Auto-Implemented` section of the report

**Hard limits — do not cross these regardless of scoring:**
- Never touch `~/.claude/CLAUDE.md` (constitutional policy doc)
- Never add a new hook to `settings.json` that requires a new script — the script must exist first
- Never rewrite agent or skill bodies — frontmatter additions only
- Never create new files (those are Build Queue items, not Quick Wins)
- Never modify a file that wasn't explicitly identified as the target in the Quick Win Action column

**Permission-denial fallback:** if the permission classifier denies an edit (this happened on the 2026-06-07 run), do NOT retry or escalate. Move the item to the report's `## Blocked Quick Wins` section with the exact command/edit for Chad to apply manually, set `last_run_mode` in the state file to note the denial, and continue with the remaining Quick Wins.

---

## Step 9 — Notify

```bash
bash ~/.claude/bin/notify_done.sh --status success --task ecosystem-update --channel desktop
```

---

## Scheduling

To run daily at 8am:
```
/schedule daily 8am /ecosystem-update
```

To run as a local loop:
```
/loop 24h /ecosystem-update
```

The skill is headless-safe: no interactive prompts, deterministic exit, idempotent same-day reruns.

---

## Philosophy Gate (applied at Step 5)

Every recommendation must pass this test before being recommended:

> "Can I prove in one sentence that an existing primitive cannot satisfy this requirement?"

If the answer is "no" or requires more than one sentence → Rejected, reason: overengineering.

Examples:
- "Add `context: fork` to agents" → passes (one-line frontmatter, existing primitive)
- "Build a new orchestration layer for source fetching" → fails (WebFetch already handles this)
- "Add a new MCP server for GitHub" → needs proof that `gh` CLI + WebFetch can't cover the use case

---

## Professional Variant

The public-facing run: generate the standard report, then push it through the
chadacus.dev scrubber/renderer to strip Chad-internal language and publish a
neutral digest.

**When the user asks for the professional, public, chadacus, or resume variant —
or "run it for the site" — read `references/professional-variant.md`.** It
carries the render/deploy pipeline, the scrubber surface, and the daily-cron
contract. Skip it on a standard run.

> On professional runs, skip Step 8 (auto-implement) unless explicitly asked —
> public output is the goal, not local config changes.
