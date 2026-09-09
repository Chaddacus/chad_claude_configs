# Configuration specification

## Purpose and installation

Maintain equivalent engineering guidance for Claude Code and Codex. This repository is installed at `~/.claude`; `codex/` contains portable files installed into `~/.codex`. Machine-specific configuration and credentials remain private. Preserve unrelated settings when installing. [README.md](README.md) documents installation, recovery, context measurements, and source references.

## Current behavior

`CLAUDE.md` and `codex/AGENTS.md` route tasks to eight matching guides. They cover maintained application specs, clean documented code, a small spine with cohesive modules and plugins, lifecycle ownership, API/MCP interfaces, development access through trusted Tailscale boundaries, workflow testing, independent review, and existing project-board/vault updates. Guides are read when relevant; links are explicit reading instructions, not automatic imports.

Bounded independent reviews use fresh context and self-contained, concern-specific handoffs, with necessary history retained when applicable. The identical `scripts/read-guidance.py` helpers emit exact sections with introductions, source paths, line ranges, and hashes. Known files use bounded direct reads; verbose logs remain complete on disk with faithful diagnostics. Bundles and parallel workers require a concrete benefit.

Claude's reviewer inherits the model, allows Read/Grep/Glob, and denies mutation, Bash, and delegation tools. Codex's reviewer retains its configured model and read-only sandbox setting; configuration alone does not prove runtime isolation.

Claude sets `SLASH_COMMAND_TOOL_CHAR_BUDGET=8000`. Codex's portable `codex/context-settings.toml` sets `skills.max_context_tokens=2000` and disables three redundant skill copies and one placeholder, preserving their files. These character/token budgets are not equivalent. Desktop runtime 0.153.4 accepts the Codex setting; standalone CLI 0.144.1 does not.

## Boundaries and verification

This is a configuration repository, with no application API, MCP service, or deployment target. Existing MCP assets remain; registration and authentication are machine-specific. Claude's user-scope omni-mem-manage endpoint was aligned with Codex. Authentication/tool execution in a fresh Claude session remains unverified. No existing Omni-mem board mapped to this repository; none was created.

Run `python3.11 scripts/validate-config.py`, `PYTHONDONTWRITEBYTECODE=1 python3.11 -m unittest discover -s tests -v`, and `git diff --check`; obtain independent review before promotion. Update the installed clients from the reviewed revision.

Legacy configuration is archived under `archive/2026-09-09/`; private backups live outside this public repository. Archive material is historical, not active guidance. Large evaluations were stopped; later small screens support narrow context preferences, not general quality, cost, or quota claims. Human calibration remains pending. Restart clients to rebuild loaded context; inspect a fresh Claude `/context` to measure runtime effects. Static checks do not establish live behavior or retroactively remove context.
