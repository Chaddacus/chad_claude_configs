# chad_claude_configs

Chad Simon's `~/.claude` — Claude Code runtime, agent definitions, skills, hooks, and governance configuration.

## Layout

| Path | Purpose |
|------|---------|
| `CLAUDE.md` | Constitutional policy — global operating rules, autonomy model, memory workflow |
| `POLICY_OWNERSHIP.md` | Policy ownership map across Claude/Codex homes |
| `settings.json` | Claude Code runtime: hooks, env, permissions, statusLine, plugins |
| `agents/` | Subagent definitions (planner, reviewer, worker, validator, chad-agent, chad-work, etc.) |
| `commands/` | Custom slash commands (`/commit-push-pr`, `/techdebt`) |
| `skills/` | Installed skills — each is a markdown + optional scripts bundle |
| `bin/` | Custom scripts: hook handlers, governance runtime, auto-runtime, notification pipeline |
| `standards/` | Runbooks for adaptive memory, Ralph postflight, route canary, enterprise maturity |
| `state/route_manifest.json` | Governed routing contract (R1–R5 profiles, dispatch budgets, reviewer barriers) |
| `state/control_plane.json` | Anti-overengineering guardrails |
| `sounds/` | Custom notification sounds |
| `mcp-servers/` | Local MCP server scripts |
| `tests/` | Harness tests for bin/ scripts |

## Hook events wired (`settings.json`)

`Stop`, `PreCompact`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SubagentStop`, `SessionEnd`, `SessionStart`, `TaskCompleted`.

## Not tracked (intentional)

- `settings.local.json` — machine-specific permission allowlist
- `auth.json`, `secrets/`, `.env*` — credentials
- `sessions/`, `projects/`, `todos/`, `telemetry/`, `history.jsonl`, `file-history/` — runtime state / session logs
- `plugins/marketplaces/` — bulk plugin content, re-fetchable
- `cache/`, `models_cache.json`, `.pytest_cache/`, `*.sqlite` — caches and transient state
- `backups/` — local snapshots

See `.gitignore` for the full allowlist.

## Restoring to a new machine

```bash
git clone git@github.com:Chaddacus/chad_claude_configs.git ~/.claude
# Fill in machine-specific state: settings.local.json, auth, secrets
```
