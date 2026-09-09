# Configuration specification

## Purpose and current behavior

Provide equivalent, straightforward engineering guidance for Claude Code and Codex. The root Claude files are installed through this repository at `~/.claude`; `codex/` is the versioned Codex guidance snapshot for installation into `~/.codex`.

The two routers provide matching working preferences and conditional access to eight guides. The principles cover maintained application specs, clean documented code, a small application spine with cohesive modules and plugins, lifecycle ownership, API and MCP interfaces, trusted development access, workflow testing, independent review, and existing project-board and knowledge-vault updates.

Focused context retrieval and concise review handoffs are applied. Bounded reviews default to fresh context with self-contained assignments, explicit review concerns, and exceptions for necessary history. Guide selection follows the operation assigned rather than every responsibility of the surrounding project. Parallel execution is conditional on useful independent work; no single-builder rule, mandatory swarm, or custom governance hook is installed. Claude's native reviewer has only Read, Grep, and Glob tools, inherits the parent model, and examines retained execution evidence. The Codex reviewer retains its existing model and read-only sandbox configuration.

## Configuration and boundaries

See README.md for file locations, official Claude loading documentation, migration, verification, and recovery. Machine-specific settings and credentials remain private. Previously committed legacy configuration lives under `archive/2026-09-09/`; exact live pre-migration configuration, including uncommitted edits, is separately archived locally. The archive is not an instruction source for ordinary work.

This repository is configuration, not an application service, and has no new API or MCP endpoint. Existing MCP support assets remain available; authentication and server registration stay in machine-specific configuration. During this migration, Claude user scope was given the same omni-mem-manage endpoint as Codex while preserving all existing server definitions. Claude authentication and tool execution require verification in a fresh session. Omni-mem project listing was reachable during migration, but no board mapped to this repository was returned; no board was created or changed.

## Verification and limits

Run `python3.11 scripts/validate-config.py` to check JSON, router targets, shared guidance parity, reviewer tool restrictions, and retired active surfaces. Git diff checks and independent static review apply before merge. This change does not require an application build or repeat the stopped evaluation.

The final evaluation comparison and holdout were interrupted or not run at the user's direction. Adoption reflects a practical configuration decision, not a demonstrated general efficiency or quality advantage. Static verification cannot prove actual model behavior or that a previously running client reloaded its instructions. Verify loading in a fresh client session.

Two small reviewer-context cases retained expected defect findings with lower total input for fresh, scoped handoffs. These provisional checks support the narrow delegation preference; they do not validate general efficiency, section-only retrieval, or changes to platform context injection. See README.md for scope and evidence locations.

The supported desktop skill catalog is capped at 2,000 tokens, with only three redundant local system-skill copies and one placeholder disabled. Portable merge settings are in `codex/context-settings.toml`. The standalone CLI 0.144.1 lacks this budget setting; desktop runtime 0.153.4 accepts it. `scripts/read-guidance.py` and its identical Codex copy deterministically prepare source-addressed excerpts so a handoff need not load complete guides. Reviewer instructions reuse current supplied excerpts and retrieve missing or stale material. Tests and migration instructions are in README.md.

Claude also has its native skill-list metadata limit configured as `SLASH_COMMAND_TOOL_CHAR_BUDGET=8000` in portable settings. Codex retains its 2,000-token catalog limit. Guidance and handoff behavior are aligned, while native budget units and reviewer tool mechanisms differ. Fresh-session context reporting is required to measure Claude runtime effects; no identical-token claim is made.

Known small sets of source files are read in a bounded initial batch; an additional all-source bundle is optional and must earn its preparation/duplication cost. Verbose tool output is retained in complete logs with concise, faithful diagnostics; short outputs stay direct, and exit/session status is preserved. A two-review screen did not show a context benefit from an all-source bundle, so no mandatory bundle mechanism was adopted.
