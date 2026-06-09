# Reference Index — canonical owners and runtime surfaces

Moved out of `~/.claude/CLAUDE.md` 2026-06-09 (P1.2: keep the constitution
behavioral; lookup material lives here). This file is pointer-only — no rules.

## Canonical Claude runtime surfaces

- Global policy: `~/.claude/CLAUDE.md`
- Runtime config: `~/.claude/settings.json`
- Governed routing/runtime contract: `~/.claude/state/route_manifest.json`
- Governed wrapper: `~/.claude/bin/claude_run`
- Postflight runtime: `~/.claude/bin/ralph_done_loop.py`
- Acceptance checker: `~/.claude/bin/postflight_acceptance_check.py`
- Hook chain runner: `~/.claude/bin/hook_chain.py` (Stop / post-edit / post-bash / post-failure)
- Managed role files: `~/.claude/agents/*.md`
- Skills, plugins, hooks, notifications: `~/.claude/skills/`, `~/.claude/plugins/`, `~/.claude/bin/`

## Canonical owners by concern

- Runtime config: `/Users/chadsimon/.claude/settings.json`
- Routing and governed runtime contract: `/Users/chadsimon/.claude/state/route_manifest.json`
- Global agent behavior: `/Users/chadsimon/.claude/CLAUDE.md`
- Workspace-local overrides: project `CLAUDE.md`
- Prompt contracts: `/Users/chadsimon/.claude/skills/memory-adaptation/references/PROMPT_CONTRACTS.md`
- Planning-gate operator workflow: `/Users/chadsimon/.claude/skills/planning-gate/SKILL.md`
- Policy ownership map: `/Users/chadsimon/.claude/standards/POLICY_OWNERSHIP.md`
- Session identity resolution: `case_file.resolve_session_id` (`~/.claude/bin/case_file.py`)

## Standards and runbooks

- Adaptive memory: `/Users/chadsimon/.claude/standards/ADAPTIVE_MEMORY_RUNBOOK.md`
- Ralph/postflight: `/Users/chadsimon/.claude/standards/RALPH_LOOP_RUNBOOK.md`
- Route canary: `/Users/chadsimon/.claude/standards/ROUTE_CANARY_RUNBOOK.md`
- Stop-gate L2 completion records: `/Users/chadsimon/.claude/standards/STOP_GATE_L2.md`
- Auto runtime & governance mechanics: `/Users/chadsimon/.claude/standards/AUTO_RUNTIME.md`
- Enterprise maturity rubric fallback: `/Users/chadsimon/.claude/standards/enterprise-maturity-rubric-generic.md`

## Memory architecture (two-tier)

- **Native memory** (`~/.claude/memory/`, `~/.claude/projects/*/memory/`):
  auto-loaded session context, markdown files.
- **omni-mem MCP** (`~/.omni-mem/`): cross-session semantic search, fact
  graph, journal, preference storage via Docker container on port 8765.
  Configured in `~/.mcp.json`.

## Legacy reference (not canonical inputs)

`~/.claude/sync-sources/`, `~/.claude/rules/codex-import/`,
`~/.claude/state/codex_sync_manifest.json`, `claude-mem` (`~/.claude-mem/`,
import/reference only — not in the live request path).
