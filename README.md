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
