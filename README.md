# Claude and Codex engineering guidance

This repository contains matching, focused engineering instructions for Claude Code and Codex. Start with [spec.md](spec.md) for the maintained configuration description.

The global instruction file is a short router. The agent reads applicable guidance when needed, grounds decisions in evidence, implements authorized recommendations, and uses independent review for meaningful changes. Parallel work remains available when it improves delivery.

## Active files

| Path | Purpose |
| --- | --- |
| `CLAUDE.md` | Claude global preferences and conditional guidance routing. |
| `guidance/` | Engineering, code construction, architecture, lifecycle, development, review, project tracking, and vault instructions. |
| `agents/reviewer.md` | Native Claude reviewer with read-only file tools and the parent model. |
| `skills/vault-knowledge/` | Shared vault retrieval and entry conventions. |
| `settings.json` | Portable settings; the retired foundation plugin is disabled. |
| `codex/` | Matching Codex router, guidance, and existing reviewer configuration. |
| `archive/2026-09-09/` | Previously committed legacy configuration, retained as an inert historical reference. |
| `mcp-servers/`, `sounds/`, `plugins/blocklist.json` | Existing support assets retained for compatibility. |

Claude automatically loads `~/.claude/CLAUDE.md`. Its `@path` imports load immediately, and its `rules/` directory is automatically discovered. Our separate `guidance/` directory uses explicit conditional reading instructions instead. It is not an automatic import or a guarantee of exact context savings. See [Claude memory documentation](https://code.claude.com/docs/en/memory) and [subagent documentation](https://code.claude.com/docs/en/sub-agents).

## Installation and migration

For a new Claude installation, clone this repository to `~/.claude`. Configure authentication and machine-specific integrations separately. Do not copy credentials between machines through this repository.

For an existing installation, first create a private backup outside `~/.claude` containing the current instructions, settings, agents, skills, commands, rules, hooks, contexts, and output styles, including uncommitted files. Preserve a Git patch and the current revision. Reconcile local edits before pulling main; never discard them merely to make a pull succeed.

Move remaining untracked legacy agents, skills, commands, rules, hooks, contexts, and output styles into that private archive so they cannot remain discoverable. The committed versions are already under `archive/2026-09-09/`. Archived instructions are historical material, not active policy; do not use archived scripts or restore hooks during ordinary work.

Keep machine-specific preferences, existing permission restrictions, MCP connections, and non-foundation plugins in the ignored `settings.local.json`. Remove legacy `hooks`, `statusLine`, `outputStyle`, and custom `worktree` callbacks from effective settings, along with `FOUNDATION_*` and `POLICY_EDIT_GATE_*` environment settings. Ensure the retired foundation plugin remains disabled in local overrides. Preserve authentication, secrets, sessions, project history, and unrelated applications. Review [Claude setting precedence](https://code.claude.com/docs/en/settings) when reconciling local overrides.

For Codex, back up `~/.codex/AGENTS.md`, `~/.codex/guidance`, and `~/.codex/agents/reviewer.toml`; copy the matching files from `codex/` into those locations. Expand `~/` paths to the destination user's home directory in copied files. Preserve `~/.codex/config.toml` and all MCP connections, credentials, and unrelated agent definitions. The portable reviewer specifies the existing model; check its availability on the destination host before use. Install `skills/vault-knowledge` under `~/.codex/skills/` if it is not already present.

Run `python3.11 scripts/validate-config.py` after changing the shared instructions. Start fresh Claude and Codex sessions after installation. In Claude, use `/memory` and `/context` to inspect the loaded instructions, and `/agents` to confirm the native reviewer. Project-level instructions, managed settings, plugins, and previously running sessions can add their own context; this change does not replace those scopes.

## Evaluation and adoption decision

The application evaluations supported retaining engineering principles and independent review. Restricting every task to one builder did not establish an efficiency advantage, so flexible parallel work remains. Failed account-switch workflows also showed why tests must inspect rendered private state and delayed responses, not only ordinary happy paths.

The adopted changes request focused evidence retrieval, reading applicable guidance once per working context, and concise reviewer handoffs with targeted follow-up after fixes. These are practical simplifications adopted at the user's direction. The user stopped further evaluation to conserve usage. The final comparison and holdout were not completed, so no general quality improvement, cost saving, or efficiency threshold is claimed. Raw local evaluation artifacts were retained and were not published into this public configuration repository.

## Recovery

Use the private pre-migration archive for exact local restoration, including uncommitted configuration. The Git archive contains only previously committed files and cannot restore local-only edits or credentials. Restore chosen files deliberately, review effective settings, and start a fresh session. Do not restore an entire archive over active authentication or session state. For a shared rollback, make a normal revert commit and merge it; do not rewrite shared history.

## Review context refinement

Two small task cases compared inherited review context with fresh, concern-scoped assignments. Both preserved the expected findings; scoped reviews used 24% and 39% less total input in those observations. The second case supplied only passing fast-test evidence. These are provisional local results, not a general savings guarantee; cache warmth and timing were uncontrolled. Fresh context alone increased total input in the first screen.

Bounded reviews now default to fresh context with complete handoffs and concern-based guidance selection. Essential history remains available when needed. Codex callers can use `fork_turns="none"`; other clients should use their supported fresh-session mechanism. This changes instructions, not automatic platform context injection, and does not guarantee section-only retrieval. Raw local evidence is retained under `evaluation/runs/context-review-20260909` and `evaluation/runs/context-review-holdout-20260909` in the local engineering evaluation workspace.

## Compact skill discovery and exact guidance excerpts

The desktop runtime (verified version 0.153.4) supports `skills.max_context_tokens`; use the 2,000-token catalog setting in `codex/context-settings.toml` by merging it into existing machine configuration. Expand home-relative paths on installation. Keep existing skill overrides, credentials, MCP registration, and unrelated settings. The four disable entries hide three older duplicate system skills and an unusable placeholder; their files remain installed, and the current system skills remain available.

The older standalone CLI 0.144.1 rejects `skills.max_context_tokens` under strict configuration, so it does not receive the catalog-budget benefit. Verify support on each host before claiming it works. Restart the desktop app to rebuild context; existing conversations retain previously loaded content. See the [official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

The catalog budget shortens metadata, while preserving functional skill names and paths. Read a plausible skill's full file when needed. It does not remove tool definitions or platform instructions, and prompt-preview character counts are not token usage or account-cost measurements.

Use `python3.11 scripts/read-guidance.py --list` to list guide headings. To prepare a review handoff, for example:

```sh
python3.11 scripts/read-guidance.py \
  'review:Scope and evidence' \
  'review:Findings and resolution' \
  'coding:Contracts and valid state' \
  'coding:Errors and changes' \
  'engineering:Verify completion'
```

Supply the output to the reviewer with the requirements, revision, paths, and verification evidence. The reader includes exact section text, general document introductions, source paths, line ranges, and source hashes. It fails on missing or ambiguous selectors and emits no partial packet. Hashes identify the snapshot rather than certify its authority. Retrieve additional sections when needed. Install the matching script from `codex/scripts/` into `~/.codex/scripts/`; the root script serves Claude in `~/.claude/scripts/`.

Run `PYTHONDONTWRITEBYTECODE=1 python3.11 -m unittest discover -s tests -v` and the existing configuration validator after edits. Tests cover boundaries, fenced headings, preserved introductions, duplicate requests, ambiguous/missing headings, and all current guide sections.

## Installed Claude and Codex parity

Both clients use the same engineering guides, fresh and self-contained review handoffs, and exact excerpt reader. Claude's portable settings set `SLASH_COMMAND_TOOL_CHAR_BUDGET=8000` through `env`; Codex uses `skills.max_context_tokens=2000`. These are native character and token limits, respectively, not mathematically equivalent budgets or evidence of equal usage. Claude documents that its skill listing preserves names while shortening descriptions when the budget is exceeded. See [Claude skill-list budgeting](https://code.claude.com/docs/en/skills#skill-descriptions-are-cut-short).

Restart both clients to rebuild already-loaded context. The settings and file parity are verified locally; the effective Claude skill-list size should be inspected in a fresh session with `/context`. A static settings check does not prove live model behavior. Preserve any unrelated machine-specific settings during installation.
